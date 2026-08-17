"""
Entry point for Tool Cut Video V1.

Run with:  python main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app.gui.main_window import MainWindow
from app.gui.theme import STYLESHEET
from app.utils.logger import configure_logging


def _resolve_app_icon_path() -> Path | None:
    """Locates assets/icons/app.ico, both running from source and as a
    frozen PyInstaller build — same _MEIPASS lookup pattern already
    used by app.core.watermark_composer._icons_dir() and
    app.core.ffmpeg_locator for their own bundled files.

    This is the SAME image build.bat already passes to PyInstaller's
    own --icon (the .exe file's resource icon / what Explorer and the
    taskbar show) and, once bundled here too, is now also set
    explicitly at runtime via setWindowIcon() below — using one single
    consistent icon image everywhere (exe resource, splash, and the
    live running window) instead of 3 separate un-coordinated places
    that could each show something slightly different and read as
    "multiple different logos" flashing in sequence on startup.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "assets" / "icons" / "app.ico"
        return candidate if candidate.is_file() else None
    candidate = Path(__file__).resolve().parent / "assets" / "icons" / "app.ico"
    return candidate if candidate.is_file() else None


def _close_pyinstaller_splash() -> None:
    """Closes the PyInstaller --splash bootloader splash screen (see
    build.bat's SPLASH_ARG), if one is currently showing.

    THIS is the piece that was missing before: PyInstaller's --splash
    does NOT auto-close itself just because MainWindow.show() ran. The
    bootloader opens its own separate splash process/window the instant
    the .exe is double-clicked (during self-extraction, before Python
    even starts), and it stays on screen — as a static image layered on
    top of everything else, including MainWindow once that image itself
    finishes drawing — until something explicitly calls
    `pyi_splash.close()`. Without this call, the splash keeps sitting on
    top of the real window indefinitely (this is exactly the bug
    reported: splash still covering the corner of "DETECTED CLIPS" and
    part of the Activity Log while the app is already fully running).

    `pyi_splash` only exists inside a PyInstaller --splash build (it's
    injected by the bootloader, not a real importable package) — running
    via `python main.py` in a normal dev environment never has it, so
    this is wrapped in try/except ImportError and is a silent no-op
    there, exactly like every other PyInstaller-only code path in this
    app.
    """
    try:
        import pyi_splash  # type: ignore[import-not-found]
    except ImportError:
        return
    pyi_splash.close()


def main() -> int:
    configure_logging()

    app = QApplication(sys.argv)
    app.setApplicationName("Tool Cut Video V1")
    app.setStyleSheet(STYLESHEET)

    icon_path = _resolve_app_icon_path()
    if icon_path is not None:
        # Set once, on the QApplication — every window (including
        # MainWindow below) inherits it as its default windowIcon,
        # which is also what Windows uses for the taskbar entry. This
        # is the fix for "logo hiện, rồi logo nhỏ hơn khác đi" reading
        # as two different logos in a row: without this call, the
        # bootloader splash shows assets/splash/splash.png while the
        # LIVE running window/taskbar entry had no explicit icon of
        # its own, so Qt/Windows could fall back to a generic default
        # icon for a moment right as the splash hands off to the real
        # window — a visibly different, smaller icon appearing right
        # after the big splash image, before settling. Using the exact
        # same app.ico everywhere removes that mismatch.
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()
    window.show()

    # Make sure MainWindow has actually finished painting on screen
    # BEFORE closing the splash — otherwise there's a small window
    # where the splash is already gone but the real window hasn't
    # fully drawn yet, which reads as a second, blank flash between
    # "logo" and "real app" instead of one clean handoff.
    app.processEvents()

    # Close the bootloader splash only once MainWindow is actually
    # showing on screen — this is the "MainWindow đã show()" moment the
    # splash is supposed to hand off to, not any earlier point during
    # import/init where the window isn't visible yet.
    _close_pyinstaller_splash()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
