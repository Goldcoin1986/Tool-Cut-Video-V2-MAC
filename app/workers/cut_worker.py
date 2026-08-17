"""
QThread wrapper around ClipPipeline. This is the ONLY thread the app
spins up (timestamp parsing runs synchronously on the UI thread since
it's regex-only and effectively instant). Download + cutting both run
here so the GUI never freezes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.core.models import ClipRequest, ClipResult
from app.core.pipeline import ClipPipeline
from app.utils.exceptions import AppError

# Same logger the rest of the app writes to (see app/utils/logger.py).
# Logging through here — instead of only through the log_message signal
# below, which nothing has ever connected to a widget — is what actually
# gets a line into the Activity Log, since ActivityLogWidget is fed by
# log_bus, which is fed by this logger's Qt-bus handler.
logger = logging.getLogger("clip_cutter")


class CutWorker(QThread):
    """Runs the download-and-cut pipeline on a background thread.

    Signals:
        progress(int, str): Overall percent complete (0-100) and status text.
        log_message(str): A line describing pipeline progress. Kept for any
            future direct listener, but note the Activity Log itself is fed
            by the shared logger (see `logger` above), not this signal.
        clip_updated(object): A ClipResult as each clip finishes (success or fail).
        finished_ok(list): All ClipResult objects, emitted on successful completion.
        failed(object): The AppError (with its `.details`, e.g. the raw
            yt-dlp failure reason) raised if the pipeline aborted before
            completing (e.g. bad URL, FFmpeg missing, download failed).
    """

    progress = Signal(int, str)
    log_message = Signal(str)
    clip_updated = Signal(object)
    finished_ok = Signal(list)
    failed = Signal(object)

    def __init__(
        self,
        url: str,
        clip_requests: list[ClipRequest],
        output_dir: Path,
        temp_dir: Path,
        ffmpeg_override: str = "",
        ffprobe_override: str = "",
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
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._clip_requests = clip_requests
        self._output_dir = output_dir
        self._temp_dir = temp_dir
        self._preferred_height = preferred_height
        self._cookies_from_browser = cookies_from_browser
        self._cookies_file = cookies_file
        self._watermark_text = watermark_text
        self._watermark_position = watermark_position
        self._watermark_text_color = watermark_text_color
        self._watermark_box_enabled = watermark_box_enabled
        self._watermark_box_color = watermark_box_color
        self._watermark_font_size = watermark_font_size
        self._watermark_platform = watermark_platform
        self._watermark_use_brand_color = watermark_use_brand_color
        self._watermark_fixed_color_logo = watermark_fixed_color_logo
        self._audio_remove_mode = audio_remove_mode
        self._audio_music_paths = audio_music_paths or []
        self._audio_music_volume = audio_music_volume
        self._merge_clips_enabled = merge_clips_enabled
        self._merge_output_filename = merge_output_filename
        self._merge_only = merge_only
        self._subtitles_enabled = subtitles_enabled
        self._dubbing_enabled = dubbing_enabled
        self._pipeline = ClipPipeline(ffmpeg_override, ffprobe_override)
        self._cancelled = False

    def cancel(self) -> None:
        """Request cooperative cancellation before the next clip starts."""
        self._cancelled = True

    def run(self) -> None:  # noqa: D102 - QThread override
        try:
            results: list[ClipResult] = self._pipeline.run(
                url=self._url,
                clip_requests=self._clip_requests,
                output_dir=self._output_dir,
                temp_dir=self._temp_dir,
                on_progress=lambda pct, msg: self.progress.emit(pct, msg),
                on_log=lambda msg: self.log_message.emit(msg),
                on_clip_updated=lambda result: self.clip_updated.emit(result),
                is_cancelled=lambda: self._cancelled,
                preferred_height=self._preferred_height,
                cookies_from_browser=self._cookies_from_browser,
                cookies_file=self._cookies_file,
                watermark_text=self._watermark_text,
                watermark_position=self._watermark_position,
                watermark_text_color=self._watermark_text_color,
                watermark_box_enabled=self._watermark_box_enabled,
                watermark_box_color=self._watermark_box_color,
                watermark_font_size=self._watermark_font_size,
                watermark_platform=self._watermark_platform,
                watermark_use_brand_color=self._watermark_use_brand_color,
                watermark_fixed_color_logo=self._watermark_fixed_color_logo,
                audio_remove_mode=self._audio_remove_mode,
                audio_music_paths=self._audio_music_paths,
                audio_music_volume=self._audio_music_volume,
                merge_clips_enabled=self._merge_clips_enabled,
                merge_output_filename=self._merge_output_filename,
                merge_only=self._merge_only,
                subtitles_enabled=self._subtitles_enabled,
                dubbing_enabled=self._dubbing_enabled,
            )
            self.finished_ok.emit(results)
        except AppError as exc:
            # Log the real reason (exc.details — e.g. yt-dlp's actual
            # message) to the Activity Log, not just the short, generic
            # exc.message. Previously exc.details was silently dropped
            # here, so the Activity Log (and the error dialog) never
            # showed *why* a download/cut actually failed.
            if exc.details:
                logger.error("%s\nChi tiết: %s", exc.message, exc.details)
            else:
                logger.error("%s", exc.message)
            self.log_message.emit(f"ERROR: {exc.message}")
            # Emit the AppError itself (not just its .message string) so
            # the GUI can show exc.details in the error dialog too.
            self.failed.emit(exc)
        except Exception as exc:  # noqa: BLE001 - never let the thread die silently
            logger.error("Unexpected error: %s", exc)
            self.log_message.emit(f"UNEXPECTED ERROR: {exc}")
            self.failed.emit(AppError(f"An unexpected error occurred: {exc}"))
