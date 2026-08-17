"""
Locates and validates the ffmpeg and ffprobe binaries.

Checks, in order: an explicit user override from AppConfig, a binary
placed directly next to main.py (or the frozen .exe) or in a local
"ffmpeg" subfolder there, then the system PATH. Failing fast here
(before any download starts) avoids wasting the user's time and
bandwidth on a doomed run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from app.utils.exceptions import FFmpegNotFoundError

# app/core/ffmpeg_locator.py -> app/core -> app -> project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _app_base_dir() -> Path:
    """Directory containing main.py when running from source, or the
    directory containing the .exe when running as a frozen build."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return _PROJECT_ROOT


def _local_dirs() -> list[Path]:
    """Directories to search for a locally-placed ffmpeg/ffprobe, checked
    before falling back to the system PATH: the app's own folder, and an
    "ffmpeg" subfolder inside it (both are convenient drop-in locations
    that need no PATH configuration).

    When running as a PyInstaller --onefile build, this ALSO checks
    sys._MEIPASS (and its "ffmpeg" subfolder) — the hidden temp
    directory PyInstaller extracts --add-data files into at every
    startup. That directory is NOT the same as the folder containing
    the running .exe (_app_base_dir() / sys.executable's parent), so a
    ffmpeg bundled via `--add-data "ffmpeg;ffmpeg"` would otherwise be
    invisible to this locator even though it was packaged correctly.
    """
    base = _app_base_dir()
    dirs = [base, base / "ffmpeg"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        meipass_path = Path(meipass)
        dirs.extend([meipass_path, meipass_path / "ffmpeg"])
    return dirs


def _validate_binary(path: str) -> bool:
    """Return True if `path -version` runs successfully."""
    try:
        result = subprocess.run(
            [path, "-version"],
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _locate_one(exe_name: str, override: str = "") -> str:
    """Locate a single binary (ffmpeg or ffprobe) by name."""
    candidates: list[str] = []

    if override:
        candidates.append(override)

    suffix = ".exe" if sys.platform == "win32" else ""
    for local_dir in _local_dirs():
        candidates.append(str(local_dir / f"{exe_name}{suffix}"))

    on_path = shutil.which(exe_name)
    if on_path:
        candidates.append(on_path)

    for candidate in candidates:
        if candidate and Path(candidate).exists() and _validate_binary(candidate):
            return candidate

    raise FFmpegNotFoundError(
        f"Could not find a working '{exe_name}' binary.",
        details=(
            f"Place {exe_name}.exe next to main.py (or in an 'ffmpeg' "
            "subfolder there), or install FFmpeg and add it to your "
            "system PATH."
        ),
    )


def locate_ffmpeg(
    ffmpeg_override: str = "", ffprobe_override: str = ""
) -> tuple[str, str]:
    """Locate both ffmpeg and ffprobe.

    Args:
        ffmpeg_override: Optional explicit path from AppConfig.
        ffprobe_override: Optional explicit path from AppConfig.

    Returns:
        (ffmpeg_path, ffprobe_path)

    Raises:
        FFmpegNotFoundError: If either binary cannot be located or fails
            to run.
    """
    ffmpeg_path = _locate_one("ffmpeg", ffmpeg_override)
    ffprobe_path = _locate_one("ffprobe", ffprobe_override)
    return ffmpeg_path, ffprobe_path
