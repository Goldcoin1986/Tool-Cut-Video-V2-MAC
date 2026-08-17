"""
Orchestrates the full clip-cutting workflow: validate FFmpeg, download the
source video once, cut every requested clip, and delete the temporary
downloaded file. This is the single place that knows the *order* of
operations — worker threads just call into it and relay its callbacks as
Qt signals.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Callable

from app.core.diarization import Diarizer, extract_clip_audio
from app.core.downloader import YouTubeDownloader
from app.core.dubber import Dubber, build_dub_track, pick_speaker_for_cue
from app.core.ffmpeg_cutter import (
    AudioSettings,
    ClipCutter,
    WatermarkSettings,
    concat_audio_files,
    merge_clips,
)
from app.core.ffmpeg_locator import locate_ffmpeg
from app.core.models import ClipRequest, ClipResult, ClipStatus
from app.core.subtitles import translate_clip_cues, write_srt
from app.core.transcript_downloader import (
    Transcript,
    TranscriptDownloader,
    video_id_from_source_path,
)
from app.core.translator import Translator
from app.utils.exceptions import AppError, CutError
from app.utils.file_utils import ensure_dir

logger = logging.getLogger("clip_cutter")

ProgressCallback = Callable[[int, str], None]
"""Signature: callback(percent_0_to_100, status_message)"""
LogCallback = Callable[[str], None]
ClipUpdateCallback = Callable[[ClipResult], None]

# How many clips to cut with FFmpeg at once. Each FFmpeg process already
# uses several threads internally for its own encoding (libx264 with
# 'superfast' parallelizes across rows of each frame), so running an
# unbounded number of them at once would badly oversubscribe the CPU
# and likely end up *slower* overall from context-switching and disk
# contention — not faster. Capping at 4 concurrent processes is the
# same conservative, widely-used default video tools like HandBrake's
# batch/queue mode reach for: real overlap between clips (I/O-wait on
# one clip's read/write while another is mid-encode) without saturating
# every core on typical 4-8 core consumer machines. Never more than the
# number of clips actually requested (no point spinning up idle workers
# for a 2-clip run), and always at least 1.
_MAX_CONCURRENT_CUTS = 4


def _default_worker_count(clip_count: int) -> int:
    cpu_count = os.cpu_count() or 2
    return max(1, min(_MAX_CONCURRENT_CUTS, cpu_count, clip_count))


class ClipPipeline:
    """Runs the full download -> cut -> cleanup workflow for a batch of clips."""

    def __init__(
        self,
        ffmpeg_override: str = "",
        ffprobe_override: str = "",
    ) -> None:
        self._ffmpeg_override = ffmpeg_override
        self._ffprobe_override = ffprobe_override

    @staticmethod
    def _resolve_music_path(
        music_paths: list[str] | None,
        temp_dir: Path,
        ffmpeg_path: str,
        log: Callable[[str], None],
    ) -> str | None:
        """Reduce however many music files the user picked down to the
        single path AudioSettings.music_path expects. 0 files -> None,
        1 file -> that file's path unchanged (no FFmpeg call needed),
        2+ files -> concatenated in pick order into one combined track
        under temp_dir first."""
        paths = [p for p in (music_paths or []) if p]
        if not paths:
            return None
        if len(paths) == 1:
            return paths[0]

        ensure_dir(temp_dir)
        combined_path = temp_dir / "clipcutter_combined_music.m4a"
        log(f"Đang nối {len(paths)} file nhạc thành 1 danh sách phát…")
        concat_audio_files(ffmpeg_path, paths, combined_path)
        return str(combined_path)

    def run(
        self,
        url: str,
        clip_requests: list[ClipRequest],
        output_dir: Path,
        temp_dir: Path,
        on_progress: ProgressCallback | None = None,
        on_log: LogCallback | None = None,
        on_clip_updated: ClipUpdateCallback | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        preferred_height: int | None = None,
        cookies_from_browser: str = "",
        cookies_file: str = "",
        watermark_text: str = "",
        watermark_position: str = "bottom-right",
        watermark_text_color: str = "0xFFFFFF",
        watermark_box_enabled: bool = True,
        watermark_box_color: str = "0x000000",
        watermark_font_size: int = 28,
        watermark_platform: str = "",
        watermark_use_brand_color: bool = False,
        watermark_fixed_color_logo: bool = False,
        audio_remove_mode: str = "none",
        audio_music_paths: list[str] | None = None,
        audio_music_volume: float = 1.0,
        merge_clips_enabled: bool = False,
        merge_output_filename: str = "video_gop.mp4",
        merge_only: bool = False,
        subtitles_enabled: bool = False,
        dubbing_enabled: bool = False,
    ) -> list[ClipResult]:
        """Execute the pipeline end to end.

        Args:
            preferred_height: Max desired vertical resolution in pixels
                (e.g. 1080, 720). None means "best available".
            cookies_from_browser: Optional browser keyword to pull
                YouTube login cookies from, to unlock higher-resolution
                downloads that YouTube otherwise restricts anonymously.
            cookies_file: Optional path to a cookies.txt file, preferred
                over cookies_from_browser when both are given (see
                YouTubeDownloader.download for why).
            watermark_text: Optional text (e.g. a handle) burned into a
                corner of every cut clip. Blank/empty means no watermark.
            watermark_position: Which corner — 'top-left', 'top-right',
                'bottom-left', or 'bottom-right'. Ignored if
                watermark_text is blank.
            watermark_platform: Optional platform key ('x', 'facebook',
                'tiktok', 'youtube') — when set, that platform's real
                logo glyph is drawn before watermark_text instead of
                plain text-only. Empty string = no icon.
            watermark_use_brand_color: Only relevant when
                watermark_platform is set. If True, the icon is tinted
                with that platform's own official brand color(s)
                instead of watermark_text_color. Default False keeps
                the original behaviour (icon matches the text color).
                Ignored when watermark_fixed_color_logo is True.
            watermark_fixed_color_logo: Only relevant when
                watermark_platform is set. If True, that platform's
                real fixed full-color logo artwork is burned in as-is,
                ignoring both watermark_text_color and
                watermark_use_brand_color for the icon glyph.
            audio_remove_mode: One of "none" (default), "voice",
                "background", or "both" — see
                app.core.ffmpeg_cutter.AudioSettings.remove_mode for
                exactly what each does and its accuracy caveats.
            audio_music_paths: Local audio file(s) the user picked to
                use as background music for every clip. More than one
                file is concatenated (in list order) into a single
                combined track first. Looped to cover the whole clip if
                shorter than it. None/empty = no custom music (default).
            audio_music_volume: Gain multiplier for audio_music_paths
                only (1.0 = unchanged). Ignored when audio_music_paths
                is empty.
            merge_clips_enabled: If True and at least 2 clips finish
                successfully, an extra merge step runs after cutting:
                every successful clip (in request order) is
                concatenated into one combined file, added to the
                returned list as one more ClipResult so it shows up in
                the summary/table exactly like a normal clip.
            merge_output_filename: Filename (inside output_dir) for the
                combined video from merge_clips_enabled.
            merge_only: Only relevant when merge_clips_enabled is True.
                If True, the individual per-clip files are deleted from
                output_dir once the merge succeeds and dropped from the
                returned list entirely — the caller only ever sees (and
                the user's output folder only ever keeps) the one
                combined video, not the intermediate clips it was built
                from.
            subtitles_enabled: If True, burns Vietnamese subtitles
                (machine-translated from the video's own original-
                language captions — English, Chinese, Japanese, ...,
                see app.core.translator) into every clip. Requires the
                source video to actually have captions available
                (manual or auto-generated) — if none are found, this is
                logged and skipped for the whole run rather than
                failing it, same "best-effort" philosophy as the
                transcript feature this is built on. See
                app.core.translator's module docstring for the free-
                endpoint / machine-translation caveats.
            dubbing_enabled: If True, builds an AI-dubbed Vietnamese
                voice track (see app.core.dubber) for every clip and
                mixes it into the cut via AudioSettings.dub_path — same
                captions requirement (and the same "skip the whole run,
                don't fail it" fallback) as subtitles_enabled, plus its
                own per-clip speaker diarization step
                (app.core.diarization) and multiple edge-tts calls, all
                individually best-effort (see both modules'
                docstrings). Independent of subtitles_enabled — either,
                both, or neither can be on; when both are on, each
                clip's transcript lines are translated only ONCE and
                reused for both (see ClipPipeline.run()'s combined
                subtitle+dub preparation loop), not translated twice.

        Raises:
            AppError: Any subclass, propagated from the download or
                FFmpeg-location stages (per-clip cut failures are instead
                captured in each ClipResult so one bad clip doesn't abort
                the batch).
        """

        def log(msg: str) -> None:
            logger.info(msg)
            if on_log:
                on_log(msg)

        def progress(pct: int, msg: str) -> None:
            if on_progress:
                on_progress(pct, msg)

        log("Checking for FFmpeg…")
        ffmpeg_path, ffprobe_path = locate_ffmpeg(
            self._ffmpeg_override, self._ffprobe_override
        )
        log("FFmpeg found. Preparing output folder…")
        output_dir = ensure_dir(output_dir)
        temp_dir = ensure_dir(temp_dir)

        log(f"Downloading video ({len(clip_requests)} clip(s) requested)…")

        def download_hook(fraction: float, message: str) -> None:
            # Downloading the source video is normally the slowest part
            # of the whole run by far (often minutes, vs. seconds to cut
            # a short clip out of it afterwards) — especially for long
            # source videos. Giving it 90% of the bar instead of a flat
            # 50/50 split means the bar's progress actually tracks how
            # much of the real wall-clock time has elapsed, rather than
            # sitting at 50% for most of the run and then jumping straight
            # to 100% right at the very end.
            progress(int(fraction * 90), message)

        downloader = YouTubeDownloader()
        source_path = downloader.download(
            url, temp_dir, progress_callback=download_hook,
            preferred_height=preferred_height,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
        )
        log(f"Download complete: {source_path.name}")

        if downloader.last_info is not None:
            actual_height = downloader.last_info.get("height")
            if actual_height:
                requested_label = f"{preferred_height}p" if preferred_height else "Tốt nhất"
                log(f"Độ phân giải thực tế: {actual_height}p (yêu cầu: {requested_label})")
                if downloader.last_drm_detected:
                    log(
                        f"⚠ Bị giới hạn {actual_height}p — nhưng lần này KHÔNG "
                        "phải do thiếu PO Token. Video này bị YouTube khóa "
                        "DRM (mã hóa bản quyền), các luồng độ phân giải cao "
                        "đều bị mã hóa và yt-dlp cố tình không bẻ khóa để "
                        "tải chúng. Cài PO Token Provider sẽ không giúp "
                        f"được gì cho video này — {actual_height}p là giới "
                        "hạn thực tế cho video cụ thể này."
                    )
                elif downloader.last_tv_drm_false_positive:
                    log(
                        f"⚠ Bị giới hạn {actual_height}p — client 'tv' báo "
                        "video này bị DRM, nhưng không client nào khác xác "
                        "nhận điều đó. Nhiều khả năng đây là báo lỗi SAI do "
                        "một thử nghiệm đã biết của YouTube trên client "
                        "'tv' (yt-dlp issue #12563), không phải video thật "
                        "sự bị khóa DRM. Vấn đề thật sự là vì sao client "
                        "'web' thất bại trước đó (xem dòng log phía trên) "
                        "— nếu do PO Token chưa hoạt động đúng, sửa được "
                        "chỗ đó thì 'web' sẽ chạy thẳng, không cần rơi "
                        "xuống 'tv' nữa."
                    )
                elif actual_height <= 360 and (preferred_height is None or preferred_height > 360):
                    log(
                        "⚠ Bị giới hạn 360p — đây KHÔNG phải do cài đặt độ "
                        "phân giải trong app, mà do YouTube yêu cầu 'PO "
                        "Token' cho client 'web' từ đầu 2026, khiến app "
                        "phải dùng tạm client dự phòng chỉ có 360p. Xem "
                        "README.md, mục 'Fixing the 360p cap', để cài "
                        "plugin PO Token Provider và mở lại 720p/1080p."
                    )

        log("Fetching transcript (captions) for this video…")
        transcript = self._fetch_transcript_safe(downloader, source_path, temp_dir, log)
        if transcript is not None:
            source_note = "auto-generated" if transcript.is_auto_generated else "manual"
            log(f"Transcript found ({source_note}, language: {transcript.language}).")
        else:
            log("No transcript/captions available for this video.")

        cutter = ClipCutter(ffmpeg_path, ffprobe_path)
        watermark = (
            WatermarkSettings(
                text=watermark_text,
                position=watermark_position,
                text_color=watermark_text_color,
                box_enabled=watermark_box_enabled,
                box_color=watermark_box_color,
                font_size=watermark_font_size,
                platform=watermark_platform or None,
                use_brand_color=watermark_use_brand_color,
                fixed_color_logo=watermark_fixed_color_logo,
            )
            if watermark_text.strip()
            else None
        )
        resolved_music_path = self._resolve_music_path(
            audio_music_paths, temp_dir, ffmpeg_path, log,
        )
        audio = AudioSettings(
            remove_mode=audio_remove_mode,
            music_path=resolved_music_path,
            music_volume=audio_music_volume,
        )
        results: list[ClipResult] = []
        total = len(clip_requests)

        subtitle_paths: dict[int, Path] = {}
        dub_paths: dict[int, str] = {}

        if subtitles_enabled or dubbing_enabled:
            if transcript is None:
                what = []
                if subtitles_enabled:
                    what.append("phụ đề tiếng Việt")
                if dubbing_enabled:
                    what.append("lồng tiếng AI")
                log(
                    f"Đã bật {' và '.join(what)} nhưng video này không có "
                    "phụ đề gốc (thủ công hoặc tự động) để dịch — bỏ qua "
                    "(các) bước này cho lần cắt này."
                )
            else:
                # Use the caption track's OWN detected language (could be
                # en, zh-Hans, ja, ...) rather than hardcoding "en" — a
                # video whose real captions are e.g. Chinese/Japanese
                # still gets the CORRECT source language declared to
                # Google Translate instead of silently mistranslating
                # non-English original-language text as if it were
                # English. See Translator's own module docstring / the
                # _normalize_source_lang() it now applies for the small
                # YouTube->Google-Translate language-code differences
                # this can still hit (e.g. "zh-Hans" -> "zh-CN").
                translator = Translator(source_lang=transcript.language)

                # Diarizer/Dubber are only constructed (and their heavy
                # ML deps only imported) when dubbing is actually on —
                # see Diarizer._ensure_loaded()'s docstring for why that
                # laziness matters even here, one level up from the
                # class itself.
                diarizer = Diarizer() if dubbing_enabled else None
                dubber = Dubber() if dubbing_enabled else None

                if dubbing_enabled:
                    log(
                        f"Đang chuẩn bị lồng tiếng AI cho {total} clip "
                        "(tách giọng theo người nói + dịch + tạo giọng đọc) "
                        "— bước này chậm hơn nhiều so với cắt clip thông "
                        "thường, đặc biệt ở lần chạy đầu tiên khi cần tải "
                        "mô hình tách giọng…"
                    )
                elif subtitles_enabled:
                    log(f"Đang dịch phụ đề sang tiếng Việt cho {total} clip…")

                # NOTE on why THIS loop is still sequential (one clip at
                # a time), not parallelized with a ThreadPoolExecutor the
                # way the cutting stage below is: `translator` and
                # `dubber` are both single shared, stateful instances
                # (translation/TTS caches are dicts mutated on every
                # call) so concurrent calls from multiple threads would
                # need locking around every cache read/write to be safe.
                # Diarizer's PyTorch inference is also not verified
                # thread-safe here. This is about CLIPS running one at a
                # time, though — within a single clip's dubbing,
                # Dubber.synthesize_many() (see build_dub_track()) already
                # runs several of that clip's lines concurrently, since
                # edge-tts's bottleneck (network round-trip per line) has
                # nothing to do with this per-clip cache-safety concern.
                # If per-CLIP parallelism becomes worth it too, the
                # cleanest next step is a per-clip
                # ThreadPoolExecutor + a Lock around Translator/Dubber's
                # cache dicts (both already expose simple dict caches,
                # easy to guard).
                try:
                    for request in clip_requests:
                        try:
                            cues = translate_clip_cues(
                                transcript, request.start_seconds, request.end_seconds,
                                translator, log=log,
                            )
                        except Exception as exc:  # noqa: BLE001 - one bad clip shouldn't abort the whole run
                            log(f"Không dịch được phụ đề cho {request.output_filename}: {exc}")
                            cues = []

                        if not cues:
                            continue

                        if subtitles_enabled:
                            srt_path = temp_dir / f"clipcutter_subtitle_{request.index}.srt"
                            try:
                                if write_srt(cues, srt_path):
                                    subtitle_paths[request.index] = srt_path
                            except OSError as exc:
                                log(f"Không tạo được phụ đề cho {request.output_filename}: {exc}")

                        if dubbing_enabled and diarizer is not None and dubber is not None:
                            try:
                                clip_audio_path = temp_dir / f"clipcutter_dub_src_{request.index}.wav"
                                extract_clip_audio(
                                    ffmpeg_path, source_path,
                                    request.start_seconds, request.end_seconds,
                                    clip_audio_path,
                                )
                                segments = diarizer.diarize(clip_audio_path)
                                speaker_count = len({seg.speaker_label for seg in segments}) or 1
                                # Attach each cue's speaker identity now
                                # (see TranslatedCue.speaker_label's
                                # docstring) so it travels with the cue
                                # itself rather than only existing
                                # inside build_dub_track()'s local
                                # computation — reuses the exact same
                                # overlap-matching dubber.py already
                                # relies on, no second implementation.
                                if segments:
                                    for cue in cues:
                                        cue.speaker_label = pick_speaker_for_cue(cue, segments)
                                log(
                                    f"Đang tạo giọng đọc cho {request.output_filename} "
                                    f"({speaker_count} người nói, {len(cues)} câu)…"
                                )
                                dub_output = temp_dir / f"clipcutter_dub_{request.index}.wav"
                                built = build_dub_track(
                                    ffmpeg_path, ffprobe_path, cues, segments,
                                    request.duration_seconds, dubber, dub_output,
                                    log=log,
                                )
                                if built:
                                    dub_paths[request.index] = str(dub_output)
                            except Exception as exc:  # noqa: BLE001 - dubbing is best-effort per clip
                                log(
                                    f"Lồng tiếng thất bại cho {request.output_filename} "
                                    f"(giữ nguyên audio gốc cho clip này): {exc}"
                                )
                finally:
                    if dubber is not None:
                        dubber.cleanup()

                if translator.failure_count:
                    log(
                        f"Lưu ý: {translator.failure_count} dòng dịch thất bại "
                        "(mất mạng?) — các dòng đó giữ nguyên ngôn ngữ gốc."
                    )
                if dubbing_enabled and dubber is not None and dubber.failure_count:
                    log(
                        f"Lưu ý: {dubber.failure_count} câu lồng tiếng thất bại "
                        "(mất mạng?) — các câu đó giữ khoảng lặng trong track lồng tiếng."
                    )

        try:
            # Every clip in this run shares the exact same watermark
            # settings (only start/end differ), so composing its
            # [icon+text] badge PNG once, synchronously, right here —
            # before any concurrent cutting starts below — means every
            # cut_clip() call just reads that already-cached file
            # instead of racing to render+save it simultaneously. See
            # ClipCutter.warm_watermark_cache()'s own docstring for why
            # that race would otherwise be a real risk, not a
            # theoretical one.
            cutter.warm_watermark_cache(watermark)

            worker_count = _default_worker_count(total)
            log(
                f"Cutting {total} clip(s) using up to {worker_count} in "
                "parallel…"
            )

            # Results must come back in the original request order
            # regardless of which clip's FFmpeg process happens to
            # finish first — pre-sized list + write-by-index, rather
            # than appending in completion order.
            results = [None] * total  # type: ignore[list-item]
            progress_lock = Lock()
            completed_count = 0
            cancelled_logged = False

            def cut_one(i: int, request: ClipRequest) -> ClipResult:
                if is_cancelled and is_cancelled():
                    result = ClipResult(request=request, status=ClipStatus.FAILED)
                    result.error_message = "Cancelled."
                    return result

                result = cutter.cut_clip(
                    source_path, request, output_dir, watermark=watermark,
                    audio=replace(audio, dub_path=dub_paths.get(request.index)),
                    subtitle_path=subtitle_paths.get(request.index),
                )

                if transcript is not None:
                    result.transcript = transcript.excerpt(
                        request.start_seconds, request.end_seconds
                    ) or None
                    result.transcript_is_auto = transcript.is_auto_generated
                    result.transcript_language = transcript.language

                return result

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_to_index: dict[Future, int] = {
                    executor.submit(cut_one, i, request): i
                    for i, request in enumerate(clip_requests)
                }

                for future in as_completed(future_to_index):
                    i = future_to_index[future]
                    request = clip_requests[i]
                    result = future.result()
                    results[i] = result

                    if on_clip_updated:
                        on_clip_updated(result)

                    if result.status == ClipStatus.DONE:
                        suffix = " (re-encoded)" if result.used_reencode else ""
                        log(f"{request.output_filename} created successfully{suffix}.")
                    elif result.error_message == "Cancelled.":
                        if not cancelled_logged:
                            log("Cancelled — remaining clips skipped.")
                            cancelled_logged = True
                    else:
                        log(f"{request.output_filename} FAILED: {result.error_message}")

                    with progress_lock:
                        completed_count += 1
                        overall_pct = 90 + int((completed_count / total) * 10)
                        progress(overall_pct, f"Cut {completed_count}/{total} clips")
        finally:
            cutter.cleanup()

        if merge_clips_enabled:
            self._merge_successful_clips(
                cutter, results, output_dir, merge_output_filename, merge_only,
                log, progress, on_clip_updated,
            )

        log("Source video kept in session cache for reuse on the next cut.")
        progress(100, "Done")
        return results

    @staticmethod
    def _merge_successful_clips(
        cutter: ClipCutter,
        results: list[ClipResult],
        output_dir: Path,
        merge_output_filename: str,
        merge_only: bool,
        log: Callable[[str], None],
        progress: Callable[[int, str], None],
        on_clip_updated: ClipUpdateCallback | None,
    ) -> None:
        """Concatenate every successfully-cut clip (in request order)
        into one combined file, appended to `results` as one more
        ClipResult — mutates `results` in place rather than returning a
        new list, since ClipPipeline.run() just falls through to
        `return results` right after calling this.

        Runs after every individual clip is already done (not
        interleaved with the parallel cutting above) since it needs
        every clip's final output_path to exist first — merging is
        comparatively quick (usually just a stream-copy concat, no
        re-encoding) next to the cutting step itself, so doing it as a
        separate sequential pass afterward doesn't meaningfully add to
        the total run time.

        If `merge_only` is True and the merge succeeds, every
        individual clip's file is deleted from output_dir and its
        ClipResult dropped from `results` entirely — only the merged
        video is left, on disk and in what's returned. If the merge
        instead FAILS, the individual clips are always kept regardless
        of merge_only (so a merge failure can never silently lose the
        user's already-successfully-cut clips).
        """
        done = [r for r in results if r.status == ClipStatus.DONE and r.output_path]
        if len(done) < 2:
            if len(done) == 1:
                log("Chỉ có 1 clip cắt thành công — bỏ qua bước gộp (không cần gộp).")
            return

        log(f"Đang gộp {len(done)} clip thành 1 video…")
        progress(97, "Merging clips")

        merge_request = ClipRequest(
            index=0, start_seconds=0.0, end_seconds=0.0,
            label="Video gộp tất cả clip",
            output_filename_override=merge_output_filename,
        )
        merge_result = ClipResult(request=merge_request, status=ClipStatus.CUTTING)

        try:
            merge_output_path = output_dir / merge_output_filename
            merge_clips(cutter.ffmpeg_path, [Path(r.output_path) for r in done], merge_output_path)
            merge_result.status = ClipStatus.DONE
            merge_result.output_path = str(merge_output_path)
            merge_result.file_size_bytes = merge_output_path.stat().st_size
            merge_result.actual_duration_seconds = cutter.probe_duration(merge_output_path)
            log(f"Đã gộp thành công: {merge_output_filename}")

            if merge_only:
                for r in done:
                    try:
                        Path(r.output_path).unlink(missing_ok=True)
                    except OSError as exc:
                        log(f"Không xoá được clip lẻ {r.request.output_filename}: {exc}")
                log(f"Đã xoá {len(done)} clip lẻ — chỉ giữ lại video đã gộp.")
                done_ids = {id(r) for r in done}
                results[:] = [r for r in results if id(r) not in done_ids]
        except CutError as exc:
            merge_result.status = ClipStatus.FAILED
            merge_result.error_message = exc.message
            log(f"Gộp clip thất bại: {exc.message}")

        results.append(merge_result)
        if on_clip_updated:
            on_clip_updated(merge_result)

    @staticmethod
    def _fetch_transcript_safe(
        downloader: YouTubeDownloader,
        source_path: Path,
        temp_dir: Path,
        log: Callable[[str], None],
    ) -> Transcript | None:
        """Fetch the transcript, swallowing any failure.

        If the video was just freshly downloaded, its info dict (already
        fetched, no extra request needed) is used to locate and download
        captions in a single additional request. If the video was reused
        from cache instead, no info dict is available — the transcript is
        loaded straight from its own on-disk cache with zero network
        requests. Either way, this never makes a redundant metadata
        request, which is what previously triggered YouTube's HTTP 429
        rate limiting.

        Transcript display is a convenience feature — a captions fetch
        failure (no captions, rate limiting, parsing issue) must never
        abort the clip-cutting run itself.
        """
        try:
            transcript_downloader = TranscriptDownloader()
            if downloader.last_info is not None:
                return transcript_downloader.fetch_from_info(downloader.last_info, temp_dir)

            video_id = video_id_from_source_path(source_path)
            if video_id:
                return transcript_downloader.load_cached(video_id, temp_dir)
            return None
        except Exception as exc:  # noqa: BLE001 - never let this break cutting
            log(f"Transcript fetch failed (continuing without it): {exc}")
            return None
