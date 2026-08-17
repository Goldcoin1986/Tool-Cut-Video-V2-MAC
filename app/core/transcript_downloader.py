"""
Extracts YouTube captions (manual, preferred, or auto-generated as a
fallback) and parses them into timestamped text segments so each cut
clip's corresponding transcript excerpt can be attached to its
ClipResult and shown in the Summary table.

IMPORTANT: this module deliberately makes at most ONE network request
(the actual subtitle file download) per video. It never calls
yt_dlp.extract_info() itself — subtitle metadata (available languages
and URLs) is read from the info dict that YouTubeDownloader already
obtained while downloading the video. Making additional metadata
requests just for captions is what was triggering YouTube's HTTP 429
("Too Many Requests") rate limiting. When the source video is reused
from cache (no fresh download happened), the transcript is loaded from
its own on-disk cache instead — zero network requests.

Best-effort only: if no captions exist in any form, this returns None
rather than failing the pipeline — transcript display is a convenience
feature, not a requirement for cutting clips.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

logger = logging.getLogger("clip_cutter")

_PREFERRED_LANGS = ["vi", "en"]
"""Fallback order used ONLY when the video's own original/spoken
language (see _pick_language) can't be determined or has no caption
track — NOT a blanket preference for Vietnamese over the video's real
language."""
_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


class _SilentLogger:
    """Passed as yt-dlp's `logger` option so it never writes raw
    (sometimes ANSI color-coded) text straight to stdout/stderr — see
    the matching `_SilentYtDlpLogger` in downloader.py for the full
    explanation. Routed to this app's own logger at DEBUG level, which
    stays out of the GUI's Activity Log but is still on disk for
    troubleshooting.
    """

    @staticmethod
    def debug(msg: str) -> None:
        logger.debug("[yt-dlp] %s", _ANSI_ESCAPE_RE.sub("", msg))

    info = debug
    warning = debug
    error = debug


_VTT_TIME_RE = re.compile(
    r"(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")
_SOURCE_FILENAME_RE = re.compile(r"^source_(?P<id>.+)_(?:best|\d+)$")

# Module-level (process-lifetime) cooldown: once YouTube rate-limits the
# captions endpoint, skip further attempts for a while instead of hitting
# it again on every subsequent Cut Clips run and waiting on a failure
# that's very likely to repeat.
_rate_limited_until: float = 0.0


@dataclass
class TranscriptCue:
    """One timed caption line."""
    start_seconds: float
    end_seconds: float
    text: str


@dataclass
class Transcript:
    """A full parsed caption track for one video."""
    cues: list[TranscriptCue]
    is_auto_generated: bool
    language: str

    def excerpt(self, start_seconds: float, end_seconds: float) -> str:
        """Concatenate cue text overlapping [start_seconds, end_seconds]
        into one readable transcript excerpt.

        YouTube's auto-generated (ASR) captions are "rolling": each cue
        is not an independent line but repeats a growing/shifting
        window of the same words as the previous cue plus a few new
        ones (a scrolling/karaoke display effect), e.g.
            cue 1: "luôn vui vẻ, luôn tràn đầy"
            cue 2: "luôn vui vẻ, luôn tràn đầy năng lượng tích cực và"
        Naively concatenating cues (or only dropping cues that are
        EXACT duplicates of the previous one) leaves every repeated
        word-run in the output, producing garbled, heavily-duplicated
        text. This merges cues by finding the longest word-level
        overlap between what's been accumulated so far and the start
        of the next cue, appending only the genuinely new words —
        the standard fix for YouTube's rolling-caption format.
        """
        parts = [
            cue.text
            for cue in self.cues
            if cue.end_seconds > start_seconds and cue.start_seconds < end_seconds
        ]
        return self._merge_rolling_cues(parts)

    @staticmethod
    def _merge_rolling_cues(parts: list[str]) -> str:
        words: list[str] = []
        for part in parts:
            part_words = part.split()
            if not part_words:
                continue
            if not words:
                words.extend(part_words)
                continue
            max_overlap = min(len(words), len(part_words))
            overlap = 0
            for k in range(max_overlap, 0, -1):
                if [w.casefold() for w in words[-k:]] == [
                    w.casefold() for w in part_words[:k]
                ]:
                    overlap = k
                    break
            words.extend(part_words[overlap:])
        return " ".join(words)

    _SUBTITLE_MIN_DURATION_SECONDS = 1.2
    _SUBTITLE_MAX_WORDS = 12

    def clip_cues(self, start_seconds: float, end_seconds: float) -> list[TranscriptCue]:
        """Like excerpt(), but returns a *sequence* of cleaned-up,
        non-overlapping-text cues covering [start_seconds, end_seconds]
        instead of one merged string — for burning as timed subtitles
        (see app.core.subtitles.build_translated_srt).

        Two things happen here that excerpt() doesn't need to do:
          1. Same rolling-caption dedup as excerpt() (see its
             docstring), but keeping each cue's own timing instead of
             collapsing everything into one string.
          2. Adjacent deduped cues get merged until each one is at
             least ~1.2s long and up to ~12 words — YouTube's raw ASR
             cues are often just 1-3 words each, which would flicker
             unreadably fast as individual subtitle lines.

        Returned cues' start/end are clamped to
        [start_seconds, end_seconds] and still in the SOURCE video's
        absolute timeline (not yet offset to the clip's own 0:00) —
        the caller subtracts start_seconds when writing the .srt.
        """
        raw = [
            cue for cue in self.cues
            if cue.end_seconds > start_seconds and cue.start_seconds < end_seconds
        ]
        if not raw:
            return []

        deduped: list[TranscriptCue] = []
        prev_words: list[str] = []
        for cue in raw:
            words = cue.text.split()
            if not words:
                continue
            if not prev_words:
                new_words = words
            else:
                max_overlap = min(len(prev_words), len(words))
                overlap = 0
                for k in range(max_overlap, 0, -1):
                    if [w.casefold() for w in prev_words[-k:]] == [
                        w.casefold() for w in words[:k]
                    ]:
                        overlap = k
                        break
                new_words = words[overlap:]
            prev_words = words
            if new_words:
                deduped.append(TranscriptCue(cue.start_seconds, cue.end_seconds, " ".join(new_words)))

        if not deduped:
            return []

        merged: list[TranscriptCue] = []
        cur_start = deduped[0].start_seconds
        cur_end = deduped[0].end_seconds
        cur_words = deduped[0].text.split()
        for cue in deduped[1:]:
            candidate_words = cur_words + cue.text.split()
            too_short = (cur_end - cur_start) < self._SUBTITLE_MIN_DURATION_SECONDS
            too_few_words = len(candidate_words) <= self._SUBTITLE_MAX_WORDS
            if too_short and too_few_words:
                cur_end = cue.end_seconds
                cur_words = candidate_words
            else:
                merged.append(TranscriptCue(cur_start, cur_end, " ".join(cur_words)))
                cur_start, cur_end, cur_words = cue.start_seconds, cue.end_seconds, cue.text.split()
        merged.append(TranscriptCue(cur_start, cur_end, " ".join(cur_words)))

        clamped: list[TranscriptCue] = []
        for cue in merged:
            s = max(cue.start_seconds, start_seconds)
            e = min(cue.end_seconds, end_seconds)
            if e > s and cue.text.strip():
                clamped.append(TranscriptCue(s, e, cue.text))
        return clamped


def video_id_from_source_path(source_path: Path) -> str | None:
    """Extract the YouTube video ID from a cached source_<id>.<ext> filename."""
    match = _SOURCE_FILENAME_RE.match(source_path.stem)
    return match.group("id") if match else None


class TranscriptDownloader:
    """Fetches (or loads from cache) and parses one video's caption track."""

    def fetch_from_info(self, info: dict, output_dir: Path) -> Transcript | None:
        """Get the transcript using an info dict already fetched by
        YouTubeDownloader — makes at most one network request (the
        subtitle file itself), never a fresh metadata lookup.
        """
        video_id = info.get("id")
        if not video_id:
            return None

        cached = self.load_cached(video_id, output_dir)
        if cached is not None:
            return cached

        global _rate_limited_until
        if time.time() < _rate_limited_until:
            remaining = int(_rate_limited_until - time.time())
            logger.info(
                "Skipping transcript fetch: YouTube rate-limited captions "
                "%ds ago, cooling down for %ds more.",
                _RATE_LIMIT_COOLDOWN_SECONDS - remaining, remaining,
            )
            return None

        manual_subs: dict = info.get("subtitles") or {}
        auto_subs: dict = info.get("automatic_captions") or {}
        original_lang = info.get("language")

        lang, sub_list, is_auto = self._pick_language(manual_subs, auto_subs, original_lang)
        if lang is None:
            logger.info("No captions available for this video.")
            return None

        vtt_url = self._pick_vtt_url(sub_list)
        if vtt_url is None:
            logger.info("Captions found but no VTT format available.")
            return None

        kind = "auto" if is_auto else "manual"
        dest = output_dir / f"transcript_{video_id}.{kind}.{lang}.vtt"

        if not self._download_file(vtt_url, dest):
            return None

        cues = self._parse_vtt(dest)
        if not cues:
            return None

        return Transcript(cues=cues, is_auto_generated=is_auto, language=lang)

    def load_cached(self, video_id: str, output_dir: Path) -> Transcript | None:
        """Load a previously-fetched transcript from disk. Zero network calls."""
        for kind, is_auto in (("manual", False), ("auto", True)):
            matches = sorted(output_dir.glob(f"transcript_{video_id}.{kind}.*.vtt"))
            if matches:
                cues = self._parse_vtt(matches[0])
                if cues:
                    lang = matches[0].suffixes[-2].lstrip(".")
                    return Transcript(cues=cues, is_auto_generated=is_auto, language=lang)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _download_file(url: str, dest: Path) -> bool:
        """Download one subtitle file. Returns False (never raises) on
        any failure, including HTTP 429 — captions are best-effort."""
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": _SilentLogger()}) as ydl:
                data = ydl.urlopen(url).read()
            dest.write_bytes(data)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                global _rate_limited_until
                _rate_limited_until = time.time() + _RATE_LIMIT_COOLDOWN_SECONDS
                logger.info(
                    "YouTube rate-limited the caption request (HTTP 429). "
                    "Pausing transcript fetching for %ds.",
                    _RATE_LIMIT_COOLDOWN_SECONDS,
                )
            else:
                logger.info("Caption download failed (HTTP %s).", exc.code)
            return False
        except Exception as exc:  # noqa: BLE001 - transcript is best-effort
            logger.info("Caption download failed: %s", exc)
            return False

    @staticmethod
    def _pick_language(
        manual: dict, auto: dict, original_lang: str | None = None
    ) -> tuple[str | None, list | None, bool]:
        """Pick which caption track to use.

        Priority:
        1. The video's own detected spoken/original language
           (`info['language']` from yt-dlp), manual track then auto
           track — this is what actually matches the words spoken in
           the clip.
        2. _PREFERRED_LANGS (vi, en) as a fallback ONLY when the
           original language can't be determined or has no caption
           track at all.
        3. Whatever's available.

        Why this order matters: YouTube auto-translates its
        auto-generated (ASR) captions into ~100+ languages and lists
        every one of them in `automatic_captions`, alongside the
        original. Always preferring "vi" first (the old behavior)
        meant grabbing a machine-translated Vietnamese track instead
        of the real transcript whenever the video's actual spoken
        language wasn't Vietnamese — wrong words, because translation
        introduces its own phrasing rather than transcribing what was
        actually said.
        """
        if original_lang:
            if original_lang in manual:
                return original_lang, manual[original_lang], False
            if original_lang in auto:
                return original_lang, auto[original_lang], True

        for lang in _PREFERRED_LANGS:
            if lang in manual:
                return lang, manual[lang], False
        for lang in _PREFERRED_LANGS:
            if lang in auto:
                return lang, auto[lang], True
        if manual:
            lang = next(iter(manual))
            return lang, manual[lang], False
        if auto:
            lang = next(iter(auto))
            return lang, auto[lang], True
        return None, None, False

    @staticmethod
    def _pick_vtt_url(sub_list: list) -> str | None:
        for entry in sub_list:
            if entry.get("ext") == "vtt":
                return entry.get("url")
        return sub_list[0].get("url") if sub_list else None

    def _parse_vtt(self, path: Path) -> list[TranscriptCue]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            logger.warning("Could not read subtitle file: %s", exc)
            return []

        cues: list[TranscriptCue] = []
        for block in text.split("\n\n"):
            match = _VTT_TIME_RE.search(block)
            if not match:
                continue
            g = match.groups()
            start = self._seconds_from_parts(g[0], g[1], g[2], g[3])
            end = self._seconds_from_parts(g[4], g[5], g[6], g[7])

            content_lines = [
                line for line in block.splitlines()
                if "-->" not in line and not line.strip().isdigit()
            ]
            content = " ".join(content_lines).strip()
            content = _TAG_RE.sub("", content)
            content = re.sub(r"\s+", " ", content).strip()
            if content:
                cues.append(TranscriptCue(start, end, content))
        return cues

    @staticmethod
    def _seconds_from_parts(hours, minutes, seconds, millis) -> float:
        h = int(hours) if hours else 0
        return h * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000
