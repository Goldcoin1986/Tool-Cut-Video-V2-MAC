"""
Application configuration: persisted user settings (last output folder,
window size, optional ffmpeg path override) stored as JSON in the
per-user app-data directory.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.utils.file_utils import get_app_data_dir, get_default_temp_dir

logger = logging.getLogger("clip_cutter")

_SETTINGS_FILENAME = "settings.json"


@dataclass
class AppConfig:
    """Persisted + runtime configuration for the application."""

    window_width: int = 1100
    window_height: int = 750
    last_output_folder: str = str(Path.home() / "Downloads" / "PodcastClips")
    ffmpeg_path_override: str = ""
    ffprobe_path_override: str = ""
    temp_dir: str = str(get_default_temp_dir())
    cookies_from_browser: str = ""
    """Browser to pull YouTube login cookies from (e.g. 'chrome', 'firefox',
    'edge', 'brave'). Empty string = don't use browser cookies. Using a
    logged-in browser's cookies is often required to get YouTube to serve
    high-resolution (720p/1080p+) formats — without it, YouTube frequently
    limits anonymous requests to low-resolution formats only. Ignored when
    cookies_file is set (file mode takes priority — see below)."""

    cookies_file: str = ""
    """Path to a Netscape-format cookies.txt file exported from a browser
    while logged into YouTube. Preferred over cookies_from_browser because
    it reads a static file instead of a live browser's cookie database, so
    it never fails just because that browser happens to be running (which
    locks the database file, e.g. Chrome/Edge/Brave). Empty string = not
    using a cookies file."""

    watermark_text: str = ""
    """Text (e.g. a handle like '@toniboiboi') burned into a corner of
    every cut clip. Empty string = no watermark."""

    watermark_position: str = "bottom-right"
    """Corner the watermark text is drawn in: 'top-left', 'top-right',
    'bottom-left', or 'bottom-right'."""

    watermark_text_color: str = "0xFFFFFF"
    """FFmpeg color spec (e.g. '0xRRGGBB') for the watermark text."""

    watermark_box_enabled: bool = True
    """Whether the watermark text has a solid background box behind it
    for readability. If False, an outline stroke is used instead."""

    watermark_box_color: str = "0x000000"
    """FFmpeg color spec for the watermark's background box, if enabled."""

    watermark_font_size: int = 28
    """Font size (px) for the watermark text."""

    watermark_platform: str = ""
    """Optional platform key ('x', 'facebook', 'tiktok', 'youtube') whose
    real logo glyph is drawn before the watermark text. Empty string =
    no icon, i.e. plain text only (the original behaviour)."""

    watermark_fixed_color_logo: bool = False
    """Only relevant when watermark_platform is set. If True, the
    icon burned in is that platform's real fixed full-color logo
    artwork, unaffected by watermark_text_color or
    watermark_use_brand_color. Takes priority over
    watermark_use_brand_color."""

    watermark_use_brand_color: bool = False
    """Only relevant when watermark_platform is set (and
    watermark_fixed_color_logo is False). If True, the icon
    is tinted with that platform's own official brand color(s) instead
    of watermark_text_color. Default False keeps the original
    behaviour (icon always matches the text color)."""

    last_ytdlp_update_check: str = ""
    """ISO-8601 UTC timestamp of the last time app.core.update_checker
    checked PyPI for a newer yt-dlp release. Empty string = never
    checked yet (a check is due immediately on next launch). Persisted
    here — not just kept in memory — so the once-per-24h throttle
    survives the app being closed and reopened; see
    update_checker.is_check_due()."""

    audio_remove_mode: str = "none"
    """Persisted state of the "Tắt tiếng gốc" radio group — one of
    "none"/"voice"/"background"/"both". See
    app.core.ffmpeg_cutter.AudioSettings.remove_mode."""
    audio_music_paths: list[str] = field(default_factory=list)
    """Persisted paths to the last-picked custom music file(s), in pick
    order. Empty list = no custom music. Not validated on load — if a
    file has since moved/been deleted, AudioPicker just flags it as
    missing and the cut simply proceeds without it (same graceful-
    degradation pattern as cookies_file elsewhere in this config)."""
    audio_music_volume: float = 1.0
    """Persisted gain multiplier for audio_music_paths — see
    AudioSettings.music_volume."""
    merge_clips_enabled: bool = False
    """Persisted state of the "Gộp tất cả clip thành 1 video" checkbox."""
    merge_only: bool = False
    """Persisted state of the "Chỉ giữ video đã gộp" sub-checkbox — see
    ClipPipeline.run()'s merge_only parameter. Only meaningful when
    merge_clips_enabled is also True."""
    subtitles_enabled: bool = False
    """Persisted state of the "Phụ đề tiếng Việt (dịch tự động)"
    checkbox — see ClipPipeline.run()'s subtitles_enabled parameter."""
    dubbing_enabled: bool = False
    """Persisted state of the "Lồng tiếng tự động (AI)" checkbox — see
    ClipPipeline.run()'s dubbing_enabled parameter and
    app.core.dubber's module docstring."""

    @classmethod
    def load(cls) -> "AppConfig":
        """Load settings from disk, falling back to defaults on any issue."""
        settings_path = get_app_data_dir() / _SETTINGS_FILENAME
        if not settings_path.exists():
            return cls()
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            defaults = asdict(cls())
            defaults.update({k: v for k, v in data.items() if k in defaults})
            return cls(**defaults)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to load settings, using defaults: %s", exc)
            return cls()

    def save(self) -> None:
        """Persist current settings to disk. Failures are logged, not raised."""
        settings_path = get_app_data_dir() / _SETTINGS_FILENAME
        try:
            settings_path.write_text(
                json.dumps(asdict(self), indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("Failed to save settings: %s", exc)
