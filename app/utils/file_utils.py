"""
Filesystem helpers: safe filenames, human-readable sizes, cross-platform
"open folder in file explorer", and safe temp-file cleanup.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from pathlib import Path

from app.utils.exceptions import OutputFolderError, PermissionDeniedError

_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """Strip characters that are invalid in Windows/macOS/Linux filenames."""
    cleaned = _INVALID_CHARS_RE.sub("", name).strip()
    return cleaned or "clip"


def format_filesize(size_bytes: int | None) -> str:
    """Format a byte count as a human-readable string (e.g. '12.4 MB')."""
    if size_bytes is None:
        return "-"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def ensure_dir(path: str | Path) -> Path:
    """Create a directory (and parents) if it doesn't exist.

    Raises:
        OutputFolderError: If the directory cannot be created.
    """
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputFolderError(
            f"Could not create output folder: {p}", details=str(exc)
        ) from exc
    return p


def delete_file_safe(path: str | Path) -> None:
    """Delete a file if it exists, raising PermissionDeniedError on failure.

    Used to remove the temporary downloaded video after cutting completes.
    Silently no-ops if the file is already gone.
    """
    p = Path(path)
    if not p.exists():
        return
    try:
        p.unlink()
    except OSError as exc:
        raise PermissionDeniedError(
            f"Could not delete temporary file: {p.name}", details=str(exc)
        ) from exc


def open_folder(path: str | Path) -> None:
    """Open the given folder in the OS's default file explorer.

    Supports Windows (primary target), macOS, and Linux.

    Raises:
        OutputFolderError: If the folder does not exist or cannot be opened.
    """
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise OutputFolderError(f"Output folder does not exist: {p}")

    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", str(p)], check=True)
        else:
            subprocess.run(["xdg-open", str(p)], check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OutputFolderError(
            f"Could not open output folder: {p}", details=str(exc)
        ) from exc


def get_app_data_dir() -> Path:
    """Return (and create) a per-user app-data directory for settings/logs."""
    if platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    app_dir = base / "AIPodcastClipCutter"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def get_default_temp_dir() -> Path:
    """Return (and create) the temp directory used for downloaded source video."""
    temp_dir = get_app_data_dir() / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def clear_temp_dir(temp_dir: str | Path) -> None:
    """Delete every file in the temp/cache directory.

    Called once when the application closes, since downloaded source
    videos are kept for the whole session (reused if the user cuts more
    clips from the same URL) rather than deleted after every run.
    Failures are logged-worthy but never raised, so a locked file can't
    prevent the app from closing.
    """
    p = Path(temp_dir)
    if not p.exists():
        return
    for item in p.iterdir():
        if item.is_file():
            try:
                item.unlink()
            except OSError:
                pass
