"""
Time parsing and formatting helpers.

Handles conversion between the human-entered MM:SS / HH:MM:SS timestamp
formats used in Clip Data and the float-seconds representation used
internally by the parser, cutter, and pipeline.
"""

from __future__ import annotations

import re

_TIMESTAMP_RE = re.compile(r"^\d{1,2}(:\d{2}){1,2}$")


def parse_timestamp(text: str) -> float:
    """Parse a MM:SS or HH:MM:SS timestamp string into total seconds.

    Args:
        text: A timestamp such as "07:55" or "1:02:09".

    Returns:
        Total number of seconds as a float.

    Raises:
        ValueError: If the text is not a valid MM:SS or HH:MM:SS timestamp.
    """
    text = text.strip()
    if not _TIMESTAMP_RE.match(text):
        raise ValueError(f"'{text}' is not a valid MM:SS or HH:MM:SS timestamp")

    parts = [int(p) for p in text.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        hours = 0
    else:
        hours, minutes, seconds = parts

    if seconds >= 60 or minutes >= 60:
        raise ValueError(f"'{text}' has an out-of-range minutes/seconds component")

    return float(hours * 3600 + minutes * 60 + seconds)


def format_duration(total_seconds: float) -> str:
    """Format a duration in seconds as MM:SS or H:MM:SS for display.

    Durations under an hour are shown as MM:SS; longer durations include
    the hour component.
    """
    total_seconds = max(0, int(round(total_seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_timestamp_for_ffmpeg(total_seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm, the format FFmpeg's -ss/-to expect."""
    total_seconds = max(0.0, total_seconds)
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"
