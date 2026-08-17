"""
Cuts individual clips from the downloaded source video using FFmpeg.

Strategy: attempt a fast stream-copy (-c copy) first. After every cut,
validate the result with ffprobe (exists, size > 0, duration > 1s). If
validation fails, automatically retry with re-encoding, which is slower
but far more reliable across arbitrary keyframe positions.

If a watermark/text overlay is requested (see WatermarkSettings), the
fast stream-copy path is skipped entirely and every clip goes straight
through FFmpeg's `drawtext` video filter instead — burning text (e.g. a
handle like "@toniboiboi") into a corner of the video requires
re-encoding every frame; there's no way to "stream-copy" a filter.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.models import ClipRequest, ClipResult, ClipStatus
from app.utils.exceptions import CutError
from app.utils.time_utils import format_timestamp_for_ffmpeg

logger = logging.getLogger("clip_cutter")

_MIN_VALID_DURATION_SECONDS = 1.0
_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# (x-expression, y-expression) for FFmpeg's drawtext filter, in terms of
# the frame size (w, h) and the rendered text's size (text_w, text_h).
# 24px margin from whichever edges are relevant to that corner.
_WATERMARK_POSITIONS: dict[str, tuple[str, str]] = {
    "top-left": ("24", "24"),
    "top-right": ("w-text_w-24", "24"),
    "bottom-left": ("24", "h-text_h-24"),
    "bottom-right": ("w-text_w-24", "h-text_h-24"),
}

# Same four corners, but expressed in terms FFmpeg's `overlay` filter
# understands (main_w/main_h = frame size, overlay_w/overlay_h = the
# composed icon+text badge's own size) instead of drawtext's text_w/
# text_h — used only for the platform-icon badge path below.
_OVERLAY_POSITIONS: dict[str, tuple[str, str]] = {
    "top-left": ("24", "24"),
    "top-right": ("main_w-overlay_w-24", "24"),
    "bottom-left": ("24", "main_h-overlay_h-24"),
    "bottom-right": ("main_w-overlay_w-24", "main_h-overlay_h-24"),
}

# Common fonts that support Vietnamese diacritics well, checked in order.
# FFmpeg's Windows builds are typically compiled WITHOUT fontconfig, so
# drawtext needs an explicit fontfile — a bare font name isn't enough.
_FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\tahoma.ttf",
    r"C:\Windows\Fonts\seguisb.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


@dataclass
class WatermarkSettings:
    """Text to burn into a corner of every cut clip. `text` empty/blank
    means "no watermark" — callers should treat that as disabled rather
    than constructing this at all, but cut_clip() checks defensively
    too."""
    text: str
    position: str = "bottom-right"  # one of _WATERMARK_POSITIONS' keys
    text_color: str = "0xFFFFFF"    # FFmpeg color spec, e.g. "0xRRGGBB"
    box_enabled: bool = True
    box_color: str = "0x000000"
    font_size: int = 28
    platform: str | None = None
    """Optional platform key ('x', 'facebook', 'tiktok', 'youtube') —
    when set, a real logo glyph for that platform is drawn right before
    `text` instead of `text` being plain drawtext-only. None/empty means
    no icon, i.e. the original plain-text-only watermark."""
    use_brand_color: bool = False
    """Only relevant when `platform` is set and `fixed_color_logo` is
    False. If True, the icon glyph is tinted with that platform's own
    official brand color(s) instead of `text_color` (TikTok gets a
    real 3-layer brand mark; the others get a flat brand-color tint).
    If False (default), the icon keeps matching `text_color` exactly
    as before this option existed."""
    fixed_color_logo: bool = False
    """Only relevant when `platform` is set. If True, the platform's
    real fixed full-color logo artwork is burned in as-is (e.g.
    Facebook's actual blue circle + white "f"), ignoring both
    `text_color` and `use_brand_color` for the icon glyph. Takes
    priority over `use_brand_color`."""


_AUDIO_REMOVAL_FILTERS: dict[str, str] = {
    # Best-effort, classic signal-processing tricks — NOT true AI voice
    # separation (see AudioSettings.remove_mode's docstring for the
    # full caveat). "both" isn't here since it's just a full -an mute,
    # handled separately with no filter needed at all.
    "voice": "pan=stereo|c0=c0-c1|c1=c1-c0",
    "background": "highpass=f=200,lowpass=f=3000,afftdn=nf=-25",
}


@dataclass
class AudioSettings:
    """How to handle a clip's audio track. All-default (remove_mode
    "none", music_path None) means "leave audio exactly as in the
    source" — the original behaviour from before this feature existed,
    and the only combination that can still use the fast stream-copy
    path (see ClipCutter.cut_clip)."""

    remove_mode: str = "none"
    """One of:
      - "none": leave the original audio untouched (default).
      - "voice": best-effort removal of centered vocals/narration,
        keeping background music/ambience. Uses FFmpeg's classic
        stereo phase-cancellation trick (subtract each channel from
        the other) — this is a decades-old, purely signal-processing
        technique, NOT true AI source separation. It works reasonably
        on stereo music where the vocal is mixed dead-center, but does
        little to nothing on mono audio or on typical YouTube
        talking-head/podcast audio where the voice isn't isolated to
        the center channel in a cancellable way. There is no reliable
        way to do real vocal isolation without a heavy ML model
        (e.g. Demucs/UVR), which is far outside what a lightweight
        FFmpeg-based desktop tool like this can bundle.
      - "background": best-effort removal of background sound, keeping
        voice. Narrows the audio to the typical speech frequency band
        and applies light denoising — an approximation (voice will
        sound thinner/more "phone-call"-like), NOT true source
        separation either; background sound sharing the voice's
        frequency range will still bleed through.
      - "both": full mute (`-an`) — the one 100% reliable option, since
        it doesn't need to distinguish voice from background at all.
    """
    music_path: str | None = None
    """Optional path to a single local audio file to use as background
    music (already pre-concatenated by the caller if the user picked
    several — see AudioPicker.get_music_paths() / MainWindow — so this
    layer only ever has to deal with one file). Looped as needed to
    cover the whole clip. When remove_mode == "both", this REPLACES the
    (fully removed) original audio; otherwise it's mixed together with
    whatever remains of the original after remove_mode's filter (if
    any) is applied. None means no custom music."""
    music_volume: float = 1.0
    """Gain multiplier applied to `music_path` only (never the original
    audio) — 1.0 = unchanged, 0.5 = half volume, 2.0 = doubled. Ignored
    when `music_path` is None."""
    dub_path: str | None = None
    """Optional path to an AI-dubbed Vietnamese voice track for this
    SPECIFIC clip (see app.core.dubber.build_dub_track) — unlike
    `music_path`, this is never shared across clips (each clip's dub
    track has different words/timing) and is ALREADY exactly the
    clip's own duration, so it's added to FFmpeg as a plain input with
    no `-stream_loop` (looping would be nonsensical here — the whole
    point is that it's already time-aligned 1:1 with the clip, not a
    short track meant to repeat).

    Mixing priority when combined with `remove_mode`/`music_path` (see
    _run_ffmpeg_cut): the dub track always takes the "spoken voice"
    role — analogous to how remove_mode="voice" + music_path already
    mixes surviving background audio with music, this instead mixes
    whatever remove_mode leaves of the ORIGINAL audio (background
    ambience if "voice", nothing if "both", the untouched original if
    "none"/"background") together with the dub track, and then
    optionally music on top of that. Using remove_mode="voice" or
    "both" alongside dubbing is recommended for a clean result — see
    the docstring note on _run_ffmpeg_cut's dub branch for why "none"/
    "background" leave the ORIGINAL spoken voice audible underneath
    the Vietnamese dub, which most users won't want."""

    @property
    def is_active(self) -> bool:
        """False only for the original untouched-audio behaviour — used
        to decide whether the fast stream-copy path is still eligible."""
        return self.remove_mode != "none" or bool(self.music_path) or bool(self.dub_path)


def _locate_watermark_font() -> str | None:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _escape_ffmpeg_filter_path(path: str) -> str:
    """Escape a filesystem path for safe use inside an FFmpeg filtergraph
    option value (e.g. fontfile=..., textfile=...). Windows drive-letter
    colons and backslashes both need escaping there."""
    return path.replace("\\", "/").replace(":", "\\:")


class ClipCutter:
    """Cuts and validates clips using FFmpeg.

    Safe to use from multiple threads concurrently (see
    warm_watermark_cache()) — cut_clip() itself only reads instance
    state after warm_watermark_cache() has run once, and each call
    shells out to its own independent FFmpeg/ffprobe process, so
    concurrent calls don't share any mutable state with each other.
    """

    def __init__(self, ffmpeg_path: str, ffprobe_path: str) -> None:
        self._ffmpeg = ffmpeg_path
        self._ffprobe = ffprobe_path
        self._watermark_textfile: Path | None = None
        self._watermark_textfile_text: str | None = None
        self._watermark_font: str | None = None
        self._watermark_font_checked = False

    @property
    def ffmpeg_path(self) -> str:
        return self._ffmpeg

    def warm_watermark_cache(self, watermark: "WatermarkSettings | None") -> None:
        """Pre-compute and cache everything cut_clip()'s watermark
        rendering depends on for `watermark` — the located system font,
        the shared drawtext textfile, and (for a platform-icon
        watermark) the composed [icon+text] badge PNG — exactly once,
        synchronously, before any concurrent cut_clip() calls begin.

        Every clip in one run shares the exact same watermark settings
        (only the in/out timestamps differ), so without this, multiple
        threads calling cut_clip() at once would all race to compute
        and write that *same* badge PNG file simultaneously the first
        time — harmless for the plain-text path (idempotent, same
        bytes each time), but for the composed badge PNG a genuine risk
        of one thread reading a half-written file another thread is
        still saving. Calling this once, single-threaded, up front
        avoids that race entirely instead of relying on a lock to
        paper over it during the actual concurrent cutting.
        """
        if watermark is None or not watermark.text.strip():
            return
        self._build_watermark_filter(watermark)

    def cut_clip(
        self,
        source_path: Path,
        request: ClipRequest,
        output_dir: Path,
        watermark: "WatermarkSettings | None" = None,
        audio: "AudioSettings | None" = None,
        subtitle_path: Path | None = None,
    ) -> ClipResult:
        """Cut one clip, retrying with re-encode if stream-copy fails validation.

        Args:
            watermark: If given (and its text is non-blank), burns that
                text into the requested corner of the output video.
                Forces re-encoding for this clip — drawtext can't be
                applied via stream-copy — so the fast path below is
                skipped entirely when this is set.
            audio: If given and `.is_active` (mute and/or custom music),
                forces re-encoding for this clip too — muting or mixing
                in a music track both require FFmpeg to actually touch
                the audio stream, which the fast stream-copy path can't
                do. Video itself is still re-encoded along with it in
                that case (rather than trying to keep video stream-
                copied while only re-touching audio) — simpler and more
                robust than juggling a third partial-copy code path, at
                the cost of a short re-encode even for audio-only
                changes.
            subtitle_path: If given, an already-built .srt file (see
                app.core.subtitles.build_translated_srt) with timestamps
                relative to THIS clip's own start (0 = clip's first
                frame) burned into the video. Forces re-encoding, same
                reasoning as watermark above.

        Returns:
            A ClipResult describing the outcome (DONE or FAILED).
        """
        output_path = output_dir / request.output_filename
        result = ClipResult(request=request, status=ClipStatus.CUTTING)
        watermark_filter = self._build_watermark_filter(watermark) if watermark else None
        audio = audio or AudioSettings()
        needs_reencode = watermark_filter is not None or audio.is_active or subtitle_path is not None

        try:
            if not needs_reencode:
                self._run_ffmpeg_cut(source_path, request, output_path, reencode=False)
                if self._validate_clip(output_path, request.duration_seconds):
                    return self._finalize_success(result, output_path, used_reencode=False)

                logger.warning(
                    "Stream-copy validation failed for %s; retrying with re-encode.",
                    request.output_filename,
                )

            self._run_ffmpeg_cut(
                source_path, request, output_path, reencode=True,
                watermark_filter=watermark_filter, audio=audio, subtitle_path=subtitle_path,
            )
            if self._validate_clip(output_path, request.duration_seconds):
                return self._finalize_success(result, output_path, used_reencode=True)

            result.status = ClipStatus.FAILED
            result.error_message = (
                "Clip failed validation even after re-encoding."
            )
            return result

        except CutError as exc:
            result.status = ClipStatus.FAILED
            result.error_message = exc.message
            return result

    def _build_watermark_filter(self, watermark: "WatermarkSettings") -> str | None:
        """Build the FFmpeg filter string for this watermark, or None if
        there's nothing to draw or no usable font was found (logged once,
        watermark silently skipped rather than failing the whole cut over
        a decorative feature).

        Dispatches to one of two renderers:
          - a platform icon is selected: a real logo glyph + the handle
            text, composed into one PNG and burned in via `overlay`.
          - otherwise (the original behaviour): plain `drawtext`.
        If composing the icon badge fails for any reason, this falls
        back to the plain-text renderer rather than dropping the
        watermark entirely.
        """
        text = watermark.text.strip()
        if not text:
            return None

        if not self._watermark_font_checked:
            self._watermark_font = _locate_watermark_font()
            self._watermark_font_checked = True
            if self._watermark_font is None:
                logger.warning(
                    "No usable font found for the video watermark/logo text "
                    "— skipping it for this run. (Looked for Tahoma/Segoe/"
                    "Arial/DejaVu Sans.)"
                )
        if self._watermark_font is None:
            return None

        if watermark.platform:
            overlay_filter = self._build_platform_overlay_filter(watermark, text)
            if overlay_filter is not None:
                return overlay_filter
            logger.warning(
                "Falling back to a plain-text watermark (no platform icon) "
                "for this run."
            )

        return self._build_text_only_filter(watermark, text)

    def _build_platform_overlay_filter(
        self, watermark: "WatermarkSettings", text: str
    ) -> str | None:
        """Build an `overlay=...` filter that burns in the composed
        [platform icon][handle text] badge PNG. Returns None (never
        raises) if the badge can't be composed, so the caller can fall
        back to the plain-text renderer instead."""
        from app.core.watermark_composer import (
            WatermarkComposeError,
            compose_platform_badge,
        )

        assert watermark.platform is not None
        try:
            png_path, _w, _h = compose_platform_badge(
                text=text,
                platform=watermark.platform,
                font_path=self._watermark_font,  # type: ignore[arg-type]
                text_color=watermark.text_color,
                box_enabled=watermark.box_enabled,
                box_color=watermark.box_color,
                font_size=watermark.font_size,
                cache_dir=Path(tempfile.gettempdir()) / "clipcutter_watermark_badges",
                use_brand_color=watermark.use_brand_color,
                fixed_color_logo=watermark.fixed_color_logo,
            )
        except WatermarkComposeError as exc:
            logger.warning("Could not compose platform-icon watermark: %s", exc)
            return None

        x_expr, y_expr = _OVERLAY_POSITIONS.get(
            watermark.position, _OVERLAY_POSITIONS["bottom-right"]
        )
        escaped_path = _escape_ffmpeg_filter_path(str(png_path))
        return f"movie='{escaped_path}'[wm];[in][wm]overlay={x_expr}:{y_expr}[out]"

    def _build_text_only_filter(self, watermark: "WatermarkSettings", text: str) -> str | None:
        """Build the original plain `drawtext=...` filter string (no
        platform icon) — unchanged from before this feature was added."""
        textfile_path = self._get_or_write_watermark_textfile(text)
        x_expr, y_expr = _WATERMARK_POSITIONS.get(
            watermark.position, _WATERMARK_POSITIONS["bottom-right"]
        )
        style = (
            f"box=1:boxcolor={watermark.box_color}@0.45:boxborderw=10"
            if watermark.box_enabled
            # No background box: fall back to a bordered/outlined
            # stroke around each glyph instead of plain flat text, or
            # the watermark can become unreadable depending on what's
            # behind it in the video.
            else "borderw=3:bordercolor=0x000000"
        )
        return (
            "drawtext="
            f"fontfile='{_escape_ffmpeg_filter_path(self._watermark_font)}':"
            f"textfile='{_escape_ffmpeg_filter_path(str(textfile_path))}':"
            f"fontsize={watermark.font_size}:fontcolor={watermark.text_color}:"
            f"{style}:"
            f"x={x_expr}:y={y_expr}"
        )

    def _get_or_write_watermark_textfile(self, text: str) -> Path:
        """Write `text` to a temp file FFmpeg's drawtext can read via
        `textfile=`, reusing the same file across every clip in this
        session (the watermark text doesn't change clip-to-clip) rather
        than writing it fresh each time. Using a textfile instead of
        drawtext's `text=` option sidesteps that option's much stricter
        escaping rules — arbitrary Vietnamese text, quotes, colons, etc.
        just work as plain UTF-8 file content.
        """
        if self._watermark_textfile is not None and self._watermark_textfile_text == text:
            return self._watermark_textfile

        fd, raw_path = tempfile.mkstemp(suffix=".txt", prefix="clipcutter_watermark_")
        path = Path(raw_path)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(text)

        self._watermark_textfile = path
        self._watermark_textfile_text = text
        return path

    def _run_ffmpeg_cut(
        self,
        source_path: Path,
        request: ClipRequest,
        output_path: Path,
        reencode: bool,
        watermark_filter: str | None = None,
        audio: "AudioSettings | None" = None,
        subtitle_path: Path | None = None,
    ) -> None:
        audio = audio or AudioSettings()
        start = format_timestamp_for_ffmpeg(request.start_seconds)
        duration = format_timestamp_for_ffmpeg(request.duration_seconds)

        cmd = [
            self._ffmpeg,
            "-y",
            # -ss/-t both placed BEFORE this -i so they unambiguously
            # apply to the source input specifically (limit reading to
            # `duration` seconds starting at `start`) regardless of
            # whether a second -i (music, below) follows afterward.
            # FFmpeg's CLI parser attaches "pending" input options to
            # whichever -i comes *next* — so if -t were placed AFTER
            # this -i instead, it would silently attach to the music
            # input instead of this one the moment a second -i exists,
            # leaving the source completely untrimmed. (This only
            # matters once a music input is in the picture; with a
            # single input, -t after -i is harmlessly reinterpreted as
            # an output-duration limit instead — which is how this
            # looked correct before AudioSettings existed.)
            "-ss", start,
            "-t", duration,
            "-i", str(source_path),
        ]

        # The dub track (if any) becomes the next input after the
        # source — added BEFORE music (matching the order the docstring
        # on AudioSettings.dub_path describes: dub always takes the
        # "spoken voice" role, music layers on top of/around it) — and,
        # unlike music, is NEVER looped: app.core.dubber.build_dub_track
        # already builds it to exactly this clip's own duration, so
        # looping would just be wrong (it's not a short track meant to
        # repeat, it's already 1:1 time-aligned with the clip).
        dub_input_index: int | None = None
        if reencode and audio.dub_path:
            dub_input_index = 1
            cmd += ["-i", audio.dub_path]

        # The music file becomes the NEXT input after source (+dub, if
        # present). Looped indefinitely (-stream_loop -1) so a music
        # file shorter than the clip never runs out and leaves silence
        # at the end — -shortest below then trims everything back down
        # to the video's own length (already capped to `duration`
        # above), exactly as if the music were the precise length
        # needed.
        music_input_index: int | None = None
        if reencode and audio.music_path:
            music_input_index = 2 if dub_input_index is not None else 1
            cmd += ["-stream_loop", "-1", "-i", audio.music_path]

        if reencode:
            # 'superfast' trades a bit more compression efficiency
            # (somewhat larger output files at the same quality/CRF —
            # picture quality itself is unchanged since CRF is fixed)
            # for a meaningfully bigger speedup than 'veryfast' — worth
            # it here since re-encoding is already the slow-path
            # fallback (stream-copy is used whenever possible; this
            # only runs when that fails validation, or a watermark/
            # audio change is requested).
            cmd += ["-c:v", "libx264", "-preset", "superfast", "-crf", "20"]
            combined_vf = self._combine_video_filters(
                watermark_filter, self._subtitle_filter(subtitle_path),
            )
            if combined_vf:
                cmd += ["-vf", combined_vf]

            if dub_input_index is not None:
                # 3-tier mix: [dub voice] + [whatever remove_mode leaves
                # of the original audio, if remove_mode != "both"] +
                # [music, if any]. This mirrors the existing
                # remove_mode="voice"+music_path mixing logic below,
                # just with the dub track standing in for "the voice"
                # instead of assuming the original centered-vocal-
                # cancellation trick already removed it.
                #
                # NOTE: remove_mode="none" or "background" leave the
                # ORIGINAL spoken voice in this mix too, playing
                # underneath the Vietnamese dub — usually not what a
                # user dubbing a clip wants. This is deliberate (matches
                # how remove_mode already governs "what survives of the
                # original" everywhere else in this file) rather than
                # silently overriding the user's remove_mode choice, but
                # AudioPicker's UI hints the user toward "voice" or
                # "both" once dubbing is enabled — see AudioPicker.
                filter_parts = [f"[{dub_input_index}:a]volume=1.0[dub]"]
                mix_labels = ["[dub]"]

                if audio.remove_mode != "both":
                    original_chain = _AUDIO_REMOVAL_FILTERS.get(audio.remove_mode)
                    orig_expr = f"[0:a]{original_chain}[orig]" if original_chain else "[0:a]anull[orig]"
                    filter_parts.append(orig_expr)
                    mix_labels.append("[orig]")

                if music_input_index is not None:
                    filter_parts.append(f"[{music_input_index}:a]volume={audio.music_volume}[music]")
                    mix_labels.append("[music]")

                filter_complex = ";".join(filter_parts)
                if len(mix_labels) == 1:
                    # Only the dub track itself (remove_mode == "both"
                    # and no music) — still route it through
                    # -filter_complex (rather than a plain -map
                    # 1:a) so the [dub]/volume=1.0 labeling stays
                    # consistent regardless of which branch was taken.
                    cmd += ["-filter_complex", filter_complex, "-map", "0:v", "-map", mix_labels[0]]
                else:
                    mix_inputs = "".join(mix_labels)
                    filter_complex += (
                        f";{mix_inputs}amix=inputs={len(mix_labels)}:duration=first:"
                        "dropout_transition=0[aout]"
                    )
                    cmd += ["-filter_complex", filter_complex, "-map", "0:v", "-map", "[aout]"]
                cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
            elif music_input_index is not None:
                if audio.remove_mode == "both":
                    # Replace: original audio fully dropped, only the
                    # music track is used.
                    cmd += ["-map", "0:v", "-map", f"{music_input_index}:a"]
                    cmd += ["-c:a", "aac", "-b:a", "192k"]
                    if abs(audio.music_volume - 1.0) > 1e-6:
                        cmd += ["-filter:a", f"volume={audio.music_volume}"]
                    cmd += ["-shortest"]
                else:
                    # Mix: whatever remains of the original audio after
                    # remove_mode's filter (if any — "none" applies no
                    # filter at all, just the original track as-is) is
                    # mixed together with the music. Assumes the source
                    # clip actually has an audio stream (true for
                    # virtually every real YouTube video) — if it
                    # somehow doesn't, this fails and the existing
                    # failed-cut handling in cut_clip() reports it
                    # rather than silently falling back.
                    original_chain = _AUDIO_REMOVAL_FILTERS.get(audio.remove_mode)
                    a0 = f"[0:a]{original_chain}," if original_chain else "[0:a]"
                    filter_complex = (
                        f"{a0}volume=1.0[a0];"
                        f"[{music_input_index}:a]volume={audio.music_volume}[a1];"
                        "[a0][a1]amix=inputs=2:duration=first:dropout_transition=0[aout]"
                    )
                    cmd += ["-filter_complex", filter_complex, "-map", "0:v", "-map", "[aout]"]
                    cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
            elif audio.remove_mode == "both":
                cmd += ["-an"]
            elif audio.remove_mode in _AUDIO_REMOVAL_FILTERS:
                cmd += ["-af", _AUDIO_REMOVAL_FILTERS[audio.remove_mode]]
                cmd += ["-c:a", "aac", "-b:a", "192k"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        cmd += [str(output_path)]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                creationflags=_CREATION_FLAGS,
            )
        except subprocess.SubprocessError as exc:
            raise CutError(
                f"Failed to run FFmpeg for {request.output_filename}",
                details=str(exc),
            ) from exc

        if proc.returncode != 0 and not reencode:
            # Stream-copy sometimes returns non-zero on partial success;
            # let validation below decide. Only raise on the re-encode pass,
            # where a non-zero exit is a genuine, unrecoverable failure.
            logger.debug("Stream-copy ffmpeg stderr: %s", proc.stderr[-1000:])
        elif proc.returncode != 0 and reencode:
            raise CutError(
                f"FFmpeg re-encode failed for {request.output_filename}",
                details=proc.stderr[-1000:],
            )

    def cleanup(self) -> None:
        """Delete the temp watermark textfile, if one was created for
        this session. Best-effort — a leftover few-byte temp file is
        harmless, so failures here are swallowed."""
        if self._watermark_textfile is not None:
            try:
                self._watermark_textfile.unlink(missing_ok=True)
            except OSError:
                pass

    def _validate_clip(self, output_path: Path, expected_duration: float) -> bool:
        """Check the produced file exists, has content, and runs long enough."""
        if not output_path.exists() or output_path.stat().st_size == 0:
            return False

        actual_duration = self._probe_duration(output_path)
        if actual_duration is None or actual_duration < _MIN_VALID_DURATION_SECONDS:
            return False

        return True

    def _probe_duration(self, path: Path) -> float | None:
        cmd = [
            self._ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=_CREATION_FLAGS,
            )
            if proc.returncode != 0:
                return None
            data = json.loads(proc.stdout)
            return float(data["format"]["duration"])
        except (subprocess.SubprocessError, KeyError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _combine_video_filters(watermark_filter: str | None, subtitle_filter: str | None) -> str | None:
        """Chains the watermark filter and the subtitles filter into
        one valid -vf string. Not simply comma-joinable in general:
        `_build_text_only_filter` returns a plain unlabeled filter (safe
        to comma-chain), but `_build_platform_overlay_filter` returns a
        multi-node graph using explicit `[in]`/`[out]` pad labels
        (`movie='...'[wm];[in][wm]overlay=...[out]`) — appending
        `,subtitles=...` directly after a `[out]` label is invalid
        FFmpeg filtergraph syntax, not just untidy. When that shape is
        detected, the trailing `[out]` is renamed to a private
        intermediate label and the subtitles filter is chained after it
        via `;`, ending at a fresh `[out]` instead — the convention
        this file already uses to mark the -vf graph's implicit output.
        """
        if watermark_filter is None:
            return subtitle_filter
        if subtitle_filter is None:
            return watermark_filter
        if "[out]" in watermark_filter:
            prefix = watermark_filter.replace("[out]", "[premux]")
            return f"{prefix};[premux]{subtitle_filter}[out]"
        return f"{watermark_filter},{subtitle_filter}"

    @staticmethod
    def _subtitle_filter(subtitle_path: Path | None) -> str | None:
        """Builds the `subtitles=...` filtergraph fragment for a
        pre-built .srt file — deliberately left un-styled (no
        force_style override) so libass just uses its own sane default
        rendering (white text, black outline, bottom-centered), which
        keeps this filter string simple/robust rather than fighting
        FFmpeg's filtergraph escaping rules for a nested, comma- and
        colon-heavy style string on top of the path escaping already
        needed below."""
        if subtitle_path is None:
            return None
        escaped = _escape_ffmpeg_filter_path(str(subtitle_path))
        return f"subtitles=filename='{escaped}'"

    def _finalize_success(
        self, result: ClipResult, output_path: Path, used_reencode: bool
    ) -> ClipResult:
        result.status = ClipStatus.DONE
        result.output_path = str(output_path)
        result.file_size_bytes = output_path.stat().st_size
        result.actual_duration_seconds = self._probe_duration(output_path)
        result.used_reencode = used_reencode
        return result

    def probe_duration(self, path: Path) -> float | None:
        """Public wrapper around _probe_duration — used by
        ClipPipeline.run() to fill in the merged output's duration
        after merge_clips() below produces it."""
        return self._probe_duration(path)


def concat_audio_files(ffmpeg_path: str, audio_paths: list[str], output_path: Path) -> None:
    """Concatenate multiple user-picked music files (in the given
    order) into one combined audio file at `output_path` — used when
    AudioPicker.get_music_paths() returns more than one file, so
    everything downstream (AudioSettings.music_path) only ever has to
    deal with a single, already-combined track.

    Same fast-path-then-fallback strategy as merge_clips() below:
    stream-copy concat first (lossless, instant when every file
    shares the same codec — e.g. all mp3), re-encoding to a single AAC
    track only if that fails (mixed formats, e.g. one mp3 + one wav).
    """
    if not audio_paths:
        raise CutError("Không có file nhạc nào để gộp.")
    if len(audio_paths) == 1:
        shutil.copyfile(audio_paths[0], output_path)
        return

    fd, list_path_raw = tempfile.mkstemp(suffix=".txt", prefix="clipcutter_audio_concat_")
    list_path = Path(list_path_raw)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            for audio_path in audio_paths:
                escaped = str(Path(audio_path).resolve()).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        copy_cmd = [
            ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path), "-c", "copy", str(output_path),
        ]
        proc = subprocess.run(
            copy_cmd, capture_output=True, text=True, timeout=300,
            creationflags=_CREATION_FLAGS,
        )
        if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return

        logger.warning(
            "Gộp nhạc bằng stream-copy thất bại (các file khác định dạng?) "
            "— thử lại bằng re-encode: %s", proc.stderr[-500:],
        )

        reencode_cmd = [
            ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path), "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ]
        proc = subprocess.run(
            reencode_cmd, capture_output=True, text=True, timeout=600,
            creationflags=_CREATION_FLAGS,
        )
        if proc.returncode != 0:
            raise CutError("Gộp nhạc thất bại (cả stream-copy lẫn re-encode).", details=proc.stderr[-1000:])
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise CutError("Gộp nhạc thất bại: file kết quả trống hoặc không tồn tại.")
    except subprocess.SubprocessError as exc:
        raise CutError("Không chạy được FFmpeg để gộp nhạc.", details=str(exc)) from exc
    finally:
        list_path.unlink(missing_ok=True)


def merge_clips(ffmpeg_path: str, clip_paths: list[Path], output_path: Path) -> None:
    """Concatenate `clip_paths` (in the given order) into one file at
    `output_path`.

    Tries FFmpeg's concat *demuxer* with a plain stream-copy first —
    lossless and fast — which works whenever every clip shares the same
    codec/profile/resolution. That's true for the overwhelming majority
    of real runs here, since every clip is cut from the very same
    source video with the very same settings. Falls back to a full
    re-encode of the concatenated result only if that fails (e.g. one
    clip took the stream-copy cutting path and another took the
    re-encode fallback path, and they ended up just different enough
    for the muxer to refuse joining them directly) — slower, but works
    regardless of what mix of codecs the individual clips ended up
    with.
    """
    if not clip_paths:
        raise CutError("Không có clip nào để gộp.")

    fd, list_path_raw = tempfile.mkstemp(suffix=".txt", prefix="clipcutter_concat_")
    list_path = Path(list_path_raw)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            for clip_path in clip_paths:
                # FFmpeg's concat-demuxer list format: single-quoted
                # path per line, with any literal single-quote in the
                # path itself escaped as '\''  (close quote, escaped
                # quote, reopen quote) — standard shell-style escaping,
                # not FFmpeg-filtergraph escaping like elsewhere in this
                # file, since this file is parsed by the demuxer, not a
                # filter option string.
                escaped = str(clip_path.resolve()).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        copy_cmd = [
            ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path), "-c", "copy", str(output_path),
        ]
        proc = subprocess.run(
            copy_cmd, capture_output=True, text=True, timeout=600,
            creationflags=_CREATION_FLAGS,
        )
        if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return

        logger.warning(
            "Gộp clip bằng stream-copy thất bại (các clip có codec khác "
            "nhau?) — thử lại bằng re-encode: %s", proc.stderr[-500:],
        )

        reencode_cmd = [
            ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c:v", "libx264", "-preset", "superfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ]
        proc = subprocess.run(
            reencode_cmd, capture_output=True, text=True, timeout=1800,
            creationflags=_CREATION_FLAGS,
        )
        if proc.returncode != 0:
            raise CutError(
                "Gộp clip thất bại (cả stream-copy lẫn re-encode).",
                details=proc.stderr[-1000:],
            )
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise CutError("Gộp clip thất bại: file kết quả trống hoặc không tồn tại.")
    except subprocess.SubprocessError as exc:
        raise CutError("Không chạy được FFmpeg để gộp clip.", details=str(exc)) from exc
    finally:
        list_path.unlink(missing_ok=True)
