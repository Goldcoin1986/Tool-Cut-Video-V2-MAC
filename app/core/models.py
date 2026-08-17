"""
Core data models shared across the entire application.

These dataclasses are the single vocabulary used by the parser, the
downloader, the cutter, the worker threads, and the GUI widgets. Keeping
them here (with no Qt or I/O dependencies) means every other module can
import freely without circular-import risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ClipStatus(str, Enum):
    """Lifecycle status of a single clip, mirrored in the GUI table."""

    PENDING = "Pending"
    DOWNLOADING = "Downloading"
    CUTTING = "Cutting"
    DONE = "Done"
    FAILED = "Failed"


@dataclass
class ClipRequest:
    """A single clip the user wants cut, as parsed from Clip Data.

    Attributes:
        index: 1-based position, used for clip_01.mp4 style naming and
            table row ordering.
        start_seconds: Start offset into the source video, in seconds.
        end_seconds: End offset into the source video, in seconds.
        label: Optional human-readable label extracted from the source
            text (e.g. "Clip #1"). Falls back to "Clip {index}" if none
            was found.
    """

    index: int
    start_seconds: float
    end_seconds: float
    label: str = ""
    output_filename_override: str | None = None
    """Optional. When set, `output_filename` returns this exact name
    instead of the normal clip_NN.mp4 pattern below. Used only for the
    single synthetic "merged" ClipRequest ClipPipeline.run() adds after
    combining every successful clip into one file (see
    merge_clips_enabled) — that entry isn't a real numbered clip and
    needs its own filename, but everything else about it (how it's
    displayed in the clip table / summary table, its ClipResult shape)
    should work exactly like a normal one."""

    def __post_init__(self) -> None:
        if not self.label:
            self.label = f"Clip {self.index}"

    @property
    def duration_seconds(self) -> float:
        """Length of the requested clip in seconds (never negative)."""
        return max(0.0, self.end_seconds - self.start_seconds)

    @property
    def output_filename(self) -> str:
        """Deterministic output filename, e.g. clip_01.mp4."""
        if self.output_filename_override:
            return self.output_filename_override
        return f"clip_{self.index:02d}.mp4"


@dataclass
class ClipResult:
    """Outcome of attempting to cut a single ClipRequest.

    Produced by ClipCutter and consumed by both the Detected Clips table
    (live status updates) and the post-run Summary table (final
    filename / duration / size / status).
    """

    request: ClipRequest
    status: ClipStatus = ClipStatus.PENDING
    output_path: str | None = None
    file_size_bytes: int | None = None
    actual_duration_seconds: float | None = None
    used_reencode: bool = False
    error_message: str | None = None
    transcript: str | None = None
    transcript_is_auto: bool = False
    transcript_language: str | None = None

    @property
    def index(self) -> int:
        return self.request.index

    @property
    def label(self) -> str:
        return self.request.label

    @property
    def filename(self) -> str:
        return self.request.output_filename
