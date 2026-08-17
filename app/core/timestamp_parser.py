"""
Parses AI-generated "Clip Data" text (from ChatGPT, Claude, Gemini,
DeepSeek, Perplexity, or any similar tool) into a list of ClipRequest
objects.

The format is deliberately loose: it looks for "Start Time" / "End Time"
labels (with or without a colon, on the same line or the next line) and
pairs them up in the order they appear. Everything else — medal emoji,
"Clip #N" headers, markdown, blank lines — is ignored.
"""

from __future__ import annotations

import logging
import re

from app.core.models import ClipRequest
from app.utils.exceptions import NoTimestampsFoundError
from app.utils.time_utils import parse_timestamp

logger = logging.getLogger("clip_cutter")

_TIME_TOKEN = r"(\d{1,2}(?::\d{2}){1,2})"

_START_RE = re.compile(
    rf"start\s*time\s*[:\-]?\s*\n?\s*{_TIME_TOKEN}", re.IGNORECASE
)
_END_RE = re.compile(
    rf"end\s*time\s*[:\-]?\s*\n?\s*{_TIME_TOKEN}", re.IGNORECASE
)
_LABEL_RE = re.compile(
    r"clip\s*#?\s*(\d+)", re.IGNORECASE
)


def parse_clip_data(text: str) -> list[ClipRequest]:
    """Extract ClipRequest objects from free-form AI output text.

    Start Time and End Time occurrences are matched positionally: the
    Nth start time is paired with the Nth end time. Pairs with an
    invalid range (end <= start) or an unparsable timestamp are skipped
    with a warning rather than aborting the whole batch, so one bad
    entry doesn't block the rest.

    Args:
        text: Raw text pasted by the user.

    Returns:
        A list of valid ClipRequest objects, 1-indexed in the order
        they were found.

    Raises:
        NoTimestampsFoundError: If no valid Start/End pairs could be
            extracted from the text at all.
    """
    if not text or not text.strip():
        raise NoTimestampsFoundError(
            "Clip Data is empty. Paste AI-generated timestamps to analyze."
        )

    starts = _START_RE.findall(text)
    ends = _END_RE.findall(text)
    labels = _LABEL_RE.findall(text)

    if not starts or not ends:
        raise NoTimestampsFoundError(
            "No 'Start Time' / 'End Time' pairs were found in the pasted text."
        )

    pair_count = min(len(starts), len(ends))
    if len(starts) != len(ends):
        logger.warning(
            "Mismatched Start/End timestamp counts (%d starts, %d ends); "
            "using the first %d pairs.",
            len(starts),
            len(ends),
            pair_count,
        )

    requests: list[ClipRequest] = []
    next_index = 1
    for i in range(pair_count):
        raw_start, raw_end = starts[i], ends[i]
        try:
            start_seconds = parse_timestamp(raw_start)
            end_seconds = parse_timestamp(raw_end)
        except ValueError as exc:
            logger.warning("Skipping unparsable timestamp pair (%s -> %s): %s",
                            raw_start, raw_end, exc)
            continue

        if end_seconds <= start_seconds:
            logger.warning(
                "Skipping clip with invalid range: %s -> %s (end must be after start)",
                raw_start, raw_end,
            )
            continue

        label = f"Clip {labels[i]}" if i < len(labels) else f"Clip {next_index}"
        requests.append(
            ClipRequest(
                index=next_index,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                label=label,
            )
        )
        next_index += 1

    if not requests:
        raise NoTimestampsFoundError(
            "Timestamps were found but none had a valid Start/End range."
        )

    return requests
