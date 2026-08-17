"""Standardized error dialog: maps each AppError subclass to a friendly,
specific message and icon."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from app.utils.exceptions import (
    AppError,
    CutError,
    DownloadError,
    FFmpegNotFoundError,
    InvalidURLError,
    NetworkError,
    NoTimestampsFoundError,
    OutputFolderError,
    PermissionDeniedError,
    VideoUnavailableError,
)

_TITLES: dict[type, str] = {
    InvalidURLError: "Invalid YouTube URL",
    NoTimestampsFoundError: "No Timestamps Found",
    FFmpegNotFoundError: "FFmpeg Not Found",
    DownloadError: "Download Failed",
    VideoUnavailableError: "Video Unavailable",
    NetworkError: "Network Error",
    CutError: "Clip Cutting Failed",
    PermissionDeniedError: "Permission Denied",
    OutputFolderError: "Output Folder Error",
}


def show_error(parent: QWidget | None, error: AppError) -> None:
    """Display a QMessageBox appropriate to the given AppError subclass."""
    title = _TITLES.get(type(error), "Error")
    text = error.message
    detail = error.details or ""

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    if detail:
        box.setDetailedText(detail)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()
