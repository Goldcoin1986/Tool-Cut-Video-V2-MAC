"""
Best-effort, rule-based (no LLM) helpers that push the free Google
Translate output at app.core.translator.Translator a little closer to
natural spoken Vietnamese, and that give it enough CONTEXT to translate
coherently in the first place.

Two independent pieces:
  1. `naturalize_opener()` — a small curated dictionary of English
     discourse-openers ("Obviously,", "Honestly,", ...) that Google
     Translate tends to render too literally/stiffly in Vietnamese.
     These are NOT in text_cleaner's filler list (they usually DO carry
     real rhetorical weight, so they shouldn't be deleted), but their
     natural Vietnamese equivalent is well-known and fixed enough to
     hardcode rather than trust the literal MT output for.
  2. `build_translation_windows()` — groups adjacent TranscriptCues
     into small "one complete thought" windows (ending at sentence
     punctuation, a speaker-pause gap, or a size cap) so the whole
     window's text can be sent to Translator as ONE string instead of
     translating each fragment independently. Google Translate (like
     any MT) produces far more coherent output when it can see a full
     sentence instead of a chopped 2-3 word fragment — this is the
     no-LLM way to get the "translate cue 1+2+3 as one idea" behavior
     the spec asks for while still keeping each cue's own timestamp.

Ceiling/limitations: this is pattern-matching, not comprehension — it
will not rewrite awkward MT phrasing it doesn't have a rule for. That
tradeoff (no API key, no per-call cost, instant) was a deliberate
choice; see README/CHANGELOG for the option to swap in an LLM-based
Translator later without touching callers (translate_clip_cues()'s
public shape doesn't change either way).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.transcript_downloader import TranscriptCue

# English discourse-opener (lowercased, no trailing comma) -> the
# natural Vietnamese opener to force in its place. Matched only when
# the opener is immediately followed by a comma at the very start of
# the (already cleaned) cue/window text -- i.e. clearly used as a
# rhetorical opener, not as a normal adverb inside the sentence
# ("Obviously," at the start vs "...is obviously true" mid-sentence,
# which this deliberately leaves for Google Translate as normal).
_OPENER_NATURAL_VI: dict[str, str] = {
    "obviously": "Tất nhiên",
    "of course": "Tất nhiên",
    "clearly": "Rõ ràng là",
    "honestly": "Thật ra",
    "frankly": "Thẳng thắn mà nói",
    "to be fair": "Công bằng mà nói",
    "in fact": "Thực ra",
    "look": "Này",
}

_OPENER_RE = re.compile(
    r"(?i)^\s*(" + "|".join(re.escape(k) for k in sorted(_OPENER_NATURAL_VI, key=len, reverse=True)) + r")\s*,\s*"
)


def split_opener(text: str) -> tuple[str | None, str]:
    """If `text` starts with a known discourse opener + comma, return
    (natural_vi_opener, remaining_english_text_to_translate_normally).
    Otherwise (None, text) unchanged — the vast majority of cues take
    this path and are completely unaffected."""
    m = _OPENER_RE.match(text)
    if not m:
        return None, text
    key = m.group(1).lower()
    natural = _OPENER_NATURAL_VI.get(key)
    if not natural:
        return None, text
    return natural, text[m.end():]


def apply_opener(natural_opener: str | None, translated_remainder: str) -> str:
    """Recombine a forced natural opener (from split_opener) with the
    Google-translated remainder. No-op (returns translated_remainder
    unchanged) if natural_opener is None."""
    if not natural_opener:
        return translated_remainder
    remainder = translated_remainder.strip()
    if not remainder:
        return f"{natural_opener}."
    return f"{natural_opener}, {remainder[0].lower()}{remainder[1:]}"


# ---------------------------------------------------------------------
# Context windows for grouped translation
# ---------------------------------------------------------------------

_SENTENCE_END_RE = re.compile(r"[.!?]\s*$")
_MAX_WINDOW_CUES = 6
_MAX_WINDOW_WORDS = 40
_MAX_GAP_SECONDS = 1.5
"""A pause this long between two cues usually means a new thought (or
even a new speaker) started -- don't merge across it even if the
previous cue had no closing punctuation (auto-generated captions often
lack punctuation entirely)."""


@dataclass
class TranslationWindow:
    """One or more adjacent (already-cleaned) cues that together form
    a single coherent idea, to be translated as ONE string."""

    cue_indices: list[int]  # indices into the caller's cue list
    texts: list[str]  # cleaned text for each of those cues, same order

    @property
    def joined_text(self) -> str:
        return " ".join(t for t in self.texts if t)


def build_translation_windows(
    cues: list[TranscriptCue], cleaned_texts: list[str],
) -> list[TranslationWindow]:
    """Group `cues` (with their already-cleaned `cleaned_texts`, same
    length/order, empty string for a dropped cue) into
    TranslationWindows.

    A window closes (and a new one starts) when any of these happen:
      - the accumulated text already ends in '.', '!' or '?'
      - the window has reached _MAX_WINDOW_CUES cues or
        _MAX_WINDOW_WORDS words (avoid sending Google Translate one
        giant paragraph, and keep proportional timestamp-splitting
        reasonably accurate)
      - the gap to the next cue's start exceeds _MAX_GAP_SECONDS

    Dropped (empty-text) cues are skipped entirely -- they never start
    or extend a window and never appear in any window's cue_indices.
    """
    windows: list[TranslationWindow] = []
    cur_indices: list[int] = []
    cur_texts: list[str] = []
    cur_words = 0
    prev_end: float | None = None

    def _flush() -> None:
        nonlocal cur_indices, cur_texts, cur_words
        if cur_indices:
            windows.append(TranslationWindow(cue_indices=cur_indices, texts=cur_texts))
        cur_indices, cur_texts, cur_words = [], [], 0

    for i, (cue, text) in enumerate(zip(cues, cleaned_texts)):
        if not text:
            continue

        gap_too_large = (
            prev_end is not None and cur_indices and (cue.start_seconds - prev_end) > _MAX_GAP_SECONDS
        )
        if gap_too_large:
            _flush()

        cur_indices.append(i)
        cur_texts.append(text)
        cur_words += len(text.split())
        prev_end = cue.end_seconds

        joined_so_far = " ".join(cur_texts)
        if (
            _SENTENCE_END_RE.search(joined_so_far)
            or len(cur_indices) >= _MAX_WINDOW_CUES
            or cur_words >= _MAX_WINDOW_WORDS
        ):
            _flush()

    _flush()
    return windows


def split_translation_by_word_ratio(translated_text: str, source_texts: list[str]) -> list[str]:
    """Split one window's translated Vietnamese text back across its
    member cues, proportionally to each cue's ORIGINAL (source-
    language) word count -- the standard approximation professional
    subtitle tools use when a full sentence has to be re-chopped back
    onto several timed lines: exact per-word alignment isn't available
    without a word-aligned MT model, but word-count-ratio keeps each
    cue's slice roughly proportional to how much of the idea it
    actually carried, which is far closer than "assign the whole
    window's translation to the first cue and leave the rest empty".

    If there's only one source cue (the overwhelmingly common case —
    most cues are already a complete short sentence on their own),
    this returns [translated_text] unchanged, so behavior for the
    common case is identical to the old direct cue-by-cue translation.
    """
    n = len(source_texts)
    if n <= 1:
        return [translated_text]

    words = translated_text.split()
    if not words:
        return ["" for _ in source_texts]

    total_source_words = sum(max(1, len(t.split())) for t in source_texts) or 1
    counts: list[int] = []
    allocated = 0
    for t in source_texts[:-1]:
        share = max(1, round(len(words) * max(1, len(t.split())) / total_source_words))
        share = min(share, len(words) - allocated - (n - len(counts) - 1))
        share = max(share, 0)
        counts.append(share)
        allocated += share
    counts.append(max(0, len(words) - allocated))

    parts: list[str] = []
    idx = 0
    for c in counts:
        parts.append(" ".join(words[idx: idx + c]))
        idx += c
    return parts
