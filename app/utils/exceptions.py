"""
Custom exception hierarchy for Tool Cut Video V1.

Every error the application can raise inherits from AppError so the GUI
layer (see app/gui/dialogs/error_dialog.py) can catch a single base type
and still present a specific, friendly message per subclass.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for all application-specific errors.

    Attributes:
        message: A short, user-facing description of what went wrong.
        details: Optional technical detail (e.g. underlying exception text)
            useful for the activity log but not necessarily shown in a
            dialog headline.
    """

    def __init__(self, message: str, details: str | None = None) -> None:
        self.message = message
        self.details = details
        super().__init__(message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.details:
            return f"{self.message} ({self.details})"
        return self.message


class InvalidURLError(AppError):
    """Raised when the provided YouTube URL is missing or malformed."""


class NoTimestampsFoundError(AppError):
    """Raised when the Clip Data text contains no parsable timestamps."""


class InvalidTimestampRangeError(AppError):
    """Raised when a clip's end time is not after its start time."""


class FFmpegNotFoundError(AppError):
    """Raised when the ffmpeg or ffprobe binary cannot be located."""


class DownloadError(AppError):
    """Raised when yt-dlp fails to download the source video."""


class VideoUnavailableError(AppError):
    """Raised when the requested video is private, deleted, or region-locked."""


class NetworkError(AppError):
    """Raised when a network failure interrupts a download."""


class CutError(AppError):
    """Raised when FFmpeg fails to produce a valid clip, even after retrying
    with re-encoding."""


class ClipValidationError(AppError):
    """Raised when a produced clip fails post-cut validation
    (missing, empty, or too short)."""


class PermissionDeniedError(AppError):
    """Raised when the app cannot write to the chosen output folder or
    cannot delete the temporary downloaded video."""


class OutputFolderError(AppError):
    """Raised when the configured output folder is invalid or inaccessible."""
