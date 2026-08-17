"""
Rule-based (offline, no API/LLM) cleanup for raw transcript cue text
before translation.

This module is intentionally conservative: every transformation here is
a plain regex/heuristic, not an AI call, so it runs instantly and for
free, but it also has a real ceiling — it will not catch every filler
word or restarted sentence a human editor would. When in doubt it
LEAVES text alone rather than risking deleting something meaningful
(see each function's docstring for the exact heuristic used to decide
"safe to remove" vs "keep").

Used by app.core.subtitles.translate_clip_cues() as a pre-translation
pass, so both the burned-in subtitle and the AI dub (which reuse the
same TranslatedCue list) are built from the same cleaned text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------
# 1. ">>"  and raw speaker-label markers (see subtitles.py requirement:
#    a final subtitle must never contain a literal ">>").
# ---------------------------------------------------------------------

_MARKER_SPLIT_RE = re.compile(r">>+")
# ">> Joe:" / ">> Reuben:" with nothing else on the segment -> that
# segment is just a speaker-change marker, no actual spoken content.
_BARE_SPEAKER_LABEL_RE = re.compile(
    r"^[A-Z][A-Za-z.'-]{1,24}(?:\s[A-Z][A-Za-z.'-]{1,24}){0,2}\s*:\s*$"
)
# "Joe: This is important." -> strip the "Joe:" prefix, keep the rest.
_SPEAKER_LABEL_PREFIX_RE = re.compile(
    r"^[A-Z][A-Za-z.'-]{1,24}(?:\s[A-Z][A-Za-z.'-]{1,24}){0,2}\s*:\s+"
)


def strip_speaker_markers(text: str) -> str:
    """Remove ">>" markers and bare/prefix speaker-name labels,
    wherever they occur in `text` — not just at the very start.

    This matters because Transcript.clip_cues() (see
    transcript_downloader.py) already merges short adjacent raw
    caption lines into one cue before this ever runs, so a single cue
    handed to this function can look like
    ">> Joe: >> Reuben: This is a very important point." — three
    original caption lines' worth of ">>"/name markers concatenated
    into one string, with the real ">>" markers no longer only at
    position 0. Splitting on EVERY ">>" occurrence and cleaning each
    resulting segment independently (dropping segments that are pure
    speaker labels, stripping a leading "Name:" from segments that
    have real content after it) handles that merged case the same way
    as a plain ">> This is..." single-marker cue.

    Returns "" if the whole cue turns out to be nothing but speaker
    labels (e.g. ">> Joe: >> Reuben:") — the caller should drop such a
    cue entirely rather than translate/subtitle an empty line.
    """
    segments = _MARKER_SPLIT_RE.split(text)
    kept: list[str] = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        if _BARE_SPEAKER_LABEL_RE.match(seg):
            continue  # pure "Name:" marker, no content
        seg = _SPEAKER_LABEL_PREFIX_RE.sub("", seg, count=1).strip()
        if seg:
            kept.append(seg)
    return " ".join(kept)


# ---------------------------------------------------------------------
# 2. Filler words — only stripped when they behave like a discourse
#    marker (comma-bounded / sentence-initial-with-comma), never when
#    they're load-bearing part of the sentence. See module docstring.
# ---------------------------------------------------------------------

# Filler tokens that are NEVER meaningful on their own -> always safe
# to remove wherever they appear as a standalone word.
_PURE_INTERJECTION_FILLERS = ("uh", "um", "erm", "hmm", "ah")

# Filler phrases that CAN carry real meaning depending on context, so
# these are only removed when comma-bounded (i.e. clearly used as a
# parenthetical discourse marker, not as the sentence's actual verb/
# subject) — e.g. "I mean, this is important." (marker, safe to drop)
# vs. "I mean what I said." (literal, must be kept).
_CONTEXTUAL_FILLER_PHRASES = (
    "you know", "i mean", "well", "basically", "actually", "like",
)


def _build_interjection_re() -> re.Pattern:
    alt = "|".join(re.escape(w) for w in _PURE_INTERJECTION_FILLERS)
    # Standalone word (word-boundary), optional trailing comma, with
    # the surrounding whitespace collapsed so removal doesn't leave
    # double spaces or a stray leading comma behind.
    return re.compile(rf"(?i)\s*\b(?:{alt})\b\s*,?\s*")


def _build_start_re() -> re.Pattern:
    alt = "|".join(re.escape(p) for p in sorted(_CONTEXTUAL_FILLER_PHRASES, key=len, reverse=True))
    # Sentence/clause-initial, followed by a comma —
    #   "I mean, this is important."  ->  "This is important."
    return re.compile(rf"(?:^|(?<=[.!?])\s+)(?:{alt})\s*,\s*", re.IGNORECASE)


def _build_mid_re() -> re.Pattern:
    alt = "|".join(re.escape(p) for p in sorted(_CONTEXTUAL_FILLER_PHRASES, key=len, reverse=True))
    # Parenthetical, comma on both sides mid-sentence —
    #   "This is, like, really cool." ->  "This is really cool."
    return re.compile(rf",\s*(?:{alt})\s*,", re.IGNORECASE)


_INTERJECTION_RE = _build_interjection_re()
_START_FILLER_RE = _build_start_re()
_MID_FILLER_RE = _build_mid_re()


def _capitalize_first(text: str) -> str:
    """Re-capitalize the first letter after a sentence-initial filler
    was removed (e.g. "I mean, this is important." losing "I mean, "
    would otherwise leave a lowercase "this" starting the sentence)."""
    for i, ch in enumerate(text):
        if ch.isalpha():
            return text[:i] + ch.upper() + text[i + 1:]
        if ch not in " \t":
            break
    return text


def remove_filler_words(text: str) -> tuple[str, int]:
    """Strip filler words/phrases per the rules above.

    Returns (cleaned_text, removed_count) — removed_count feeds the
    "[CLEAN] Removed filler words: N" style summary log.
    """
    removed = 0
    interjection_at_start = bool(_INTERJECTION_RE.match(text))
    cleaned, n = _INTERJECTION_RE.subn(" ", text)
    removed += n
    if interjection_at_start:
        cleaned = _capitalize_first(cleaned)

    was_sentence_start = bool(_START_FILLER_RE.match(cleaned))
    cleaned, n = _START_FILLER_RE.subn("", cleaned)
    removed += n
    if was_sentence_start:
        cleaned = _capitalize_first(cleaned)

    cleaned, n = _MID_FILLER_RE.subn(" ", cleaned)
    removed += n

    cleaned = re.sub(r"\s+,", ",", cleaned)  # stray space before comma
    cleaned = re.sub(r",\s*,", ",", cleaned)  # double comma
    cleaned = re.sub(r"^\s*,\s*", "", cleaned)  # leading comma
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, removed


# ---------------------------------------------------------------------
# 3. Stutters / accidental word-repetition ("I, I think", "the, the
#    plan", "very, very, very important" -> collapse the whole run
#    down to one occurrence, matching every example in the spec).
# ---------------------------------------------------------------------

_MAX_REPEAT_PHRASE_WORDS = 3
# Longest phrase length tried first (greedy), down to a single word —
# so "I think, I think" collapses as ONE 2-word-phrase match rather
# than (incorrectly) as two separate 1-word matches ("I, I" + "think,
# think" both being technically "repeats" but not what was said).
_REPEAT_PHRASE_RES = [
    re.compile(r"\b(\w+(?:\s+\w+){%d})\b(?:[,]?\s+\1\b)+" % (n - 1), re.IGNORECASE)
    for n in range(_MAX_REPEAT_PHRASE_WORDS, 0, -1)
]


def dedupe_repeated_words(text: str) -> tuple[str, int]:
    """Collapse immediate runs of the same word OR short phrase ("I,
    I", "the, the", "I think, I think", "very, very, very") down to a
    single occurrence. Only touches IMMEDIATELY adjacent repeats (never
    words/phrases that just happen to repeat later in the sentence),
    so it can't accidentally eat intentional non-adjacent repetition
    ("no no no" said seconds apart in different cues is unaffected —
    this only ever looks within one cue's text).

    Tries the longest phrase length first (3 words, then 2, then 1) so
    a repeated multi-word phrase collapses as one clean match instead
    of leaving fragments behind; runs the whole pass twice since
    collapsing a longer phrase repeat can newly expose a shorter one
    right next to it (rare, but cheap to guard against).

    Returns (cleaned_text, removed_count) — removed_count is how many
    EXTRA copies were dropped (a triple repeat removes 2, a double
    removes 1), for the "[CLEAN] Removed repetitions: N" summary log.
    """
    removed = 0

    def _make_sub(base_ref: list[str]):
        def _sub(m: re.Match) -> str:
            nonlocal removed
            base = m.group(1)
            base_ref.append(base)
            occurrences = len(re.findall(rf"\b{re.escape(base)}\b", m.group(0), re.IGNORECASE))
            removed += max(0, occurrences - 1)
            return base
        return _sub

    cleaned = text
    for _pass in range(2):
        for pattern in _REPEAT_PHRASE_RES:
            base_ref: list[str] = []
            before = cleaned
            cleaned = pattern.sub(_make_sub(base_ref), cleaned)
            if cleaned != before:
                # A phrase-level match fires _sub once per collapsed
                # run; each call already incremented `removed` above.
                pass

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, removed


# ---------------------------------------------------------------------
# 4. Public entry point used by subtitles.py
# ---------------------------------------------------------------------


@dataclass
class CleanResult:
    text: str
    filler_removed: int
    repetitions_removed: int
    dropped: bool  # True if the cue had real content that reduced to nothing


def clean_cue_text(raw_text: str) -> CleanResult:
    """Full cleanup pass for one transcript cue: strip >>/speaker
    labels, remove filler words, collapse stutters/repeats.

    Never raises. A cue that turns out to be pure noise (e.g. just
    ">> Joe:") comes back with text="" and dropped=True — callers
    should skip such cues rather than translate/subtitle an empty
    line.
    """
    text = strip_speaker_markers(raw_text)
    if not text:
        return CleanResult(text="", filler_removed=0, repetitions_removed=0, dropped=True)

    text, filler_removed = remove_filler_words(text)
    text, repetitions_removed = dedupe_repeated_words(text)
    text = text.strip()

    return CleanResult(
        text=text,
        filler_removed=filler_removed,
        repetitions_removed=repetitions_removed,
        dropped=not text,
    )
