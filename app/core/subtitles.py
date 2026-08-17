"""
Builds a translated Vietnamese .srt subtitle file for one clip from its
corresponding Transcript — ties together Transcript.clip_cues() (clean,
timed English cues) and Translator (machine translation) into a ready
file for ClipCutter.cut_clip()'s subtitle_path to burn in with FFmpeg.

translate_clip_cues() is split out from write_srt()/build_translated_srt()
so callers that need BOTH burned-in subtitles AND AI dubbing for the
same clip (see app.core.dubber) can translate each cue exactly once and
reuse the same TranslatedCue list for both, instead of paying for two
separate Google Translate calls per line.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.text_cleaner import clean_cue_text
from app.core.transcript_downloader import Transcript
from app.core.translator import Translator
from app.core.vi_naturalizer import (
    apply_opener,
    build_translation_windows,
    split_opener,
    split_translation_by_word_ratio,
)

logger = logging.getLogger("clip_cutter")


@dataclass
class TranslatedCue:
    """One subtitle line, already translated, with timestamps relative
    to the CLIP's own start (0:00 = the clip's first frame) — the same
    timeline app.core.diarization.DiarizedSegment uses, so
    app.core.dubber can overlap-match the two directly with no offset
    math."""

    start_seconds: float
    end_seconds: float
    vi_text: str
    en_text: str
    """Original-language text (despite the name — kept for logging/
    debugging; may be any source language, not just English, see
    ClipPipeline.run()'s Translator(source_lang=transcript.language))."""
    speaker_label: str | None = None
    """Optional "Speaker N" label (see app.core.diarization), filled
    in by the caller AFTER diarization runs — translate_clip_cues()
    itself always leaves this None, since diarization happens later
    and only when dubbing is enabled. Purely additive: existing
    callers that never set this keep working exactly as before."""


def _format_srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    total_millis = int(round(seconds * 1000))
    hours, rem = divmod(total_millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def translate_clip_cues(
    transcript: Transcript,
    clip_start_seconds: float,
    clip_end_seconds: float,
    translator: Translator,
    log: Callable[[str], None] | None = None,
) -> list[TranslatedCue]:
    """Translate every transcript cue overlapping this clip into
    Vietnamese, with timestamps rebased to the clip's own 0:00.

    Before translation, each cue's raw text goes through
    app.core.text_cleaner (strip ">>"/speaker labels, drop filler
    words, collapse stutters/repeats) — a cue that turns out to be
    pure noise (e.g. just ">> Joe:") is dropped entirely rather than
    translated as an empty line.

    Adjacent cleaned cues are then grouped into "one complete idea"
    context windows (app.core.vi_naturalizer.build_translation_windows)
    and translated as ONE string per window instead of fragment-by-
    fragment, so a sentence YouTube's captions split across 2-3 cues
    still gets translated coherently — each cue keeps its own original
    timestamp; the window's translated text is split back across its
    member cues proportionally by word count
    (split_translation_by_word_ratio).

    `log`, if given, receives short progress/summary lines (filler-word
    and repetition counts, how many windows are being translated) —
    purely cosmetic, translation still proceeds identically without it.

    Returns [] if the transcript has no cues at all in this clip's time
    range, or if every cue in range turns out to be pure noise after
    cleaning — callers should treat that the same way as "no
    subtitles/dubbing possible for this clip" rather than an error.
    """
    cues = transcript.clip_cues(clip_start_seconds, clip_end_seconds)
    if not cues:
        return []

    cleaned_texts: list[str] = []
    total_filler = 0
    total_repeats = 0
    for cue in cues:
        result = clean_cue_text(cue.text)
        cleaned_texts.append(result.text)
        total_filler += result.filler_removed
        total_repeats += result.repetitions_removed

    if log and (total_filler or total_repeats):
        log(
            f"  Đã làm sạch transcript: bỏ {total_filler} từ đệm, "
            f"gộp {total_repeats} từ/cụm bị lặp."
        )

    windows = build_translation_windows(cues, cleaned_texts)
    if not windows:
        return []

    if log:
        log(f"  Đang dịch {len(windows)} cụm câu (từ {len(cues)} dòng transcript gốc)…")

    translated_cues: list[TranslatedCue] = []
    for window in windows:
        natural_opener, remainder = split_opener(window.joined_text)
        vi_translated = translator.translate(remainder)
        vi_full = apply_opener(natural_opener, vi_translated)

        vi_parts = split_translation_by_word_ratio(vi_full, window.texts)

        for cue_idx, vi_part in zip(window.cue_indices, vi_parts):
            cue = cues[cue_idx]
            vi_text = vi_part.strip() or cleaned_texts[cue_idx]
            translated_cues.append(
                TranslatedCue(
                    start_seconds=cue.start_seconds - clip_start_seconds,
                    end_seconds=cue.end_seconds - clip_start_seconds,
                    vi_text=vi_text,
                    en_text=cue.text,
                )
            )

    translated_cues.sort(key=lambda c: c.start_seconds)
    return translated_cues


def write_srt(cues: list[TranslatedCue], output_path: Path) -> bool:
    """Write already-translated cues out as a .srt file. Returns True
    if at least one block was written, False (and writes nothing) if
    `cues` is empty — callers should skip passing subtitle_path to
    cut_clip() in that case rather than burn in an empty, pointless
    file."""
    if not cues:
        return False

    blocks = [
        f"{i}\n{_format_srt_timestamp(cue.start_seconds)} --> "
        f"{_format_srt_timestamp(cue.end_seconds)}\n{cue.vi_text}\n"
        for i, cue in enumerate(cues, start=1)
    ]
    output_path.write_text("\n".join(blocks), encoding="utf-8")
    return True


def build_translated_srt(
    transcript: Transcript,
    clip_start_seconds: float,
    clip_end_seconds: float,
    translator: Translator,
    output_path: Path,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Convenience wrapper kept for callers that only need the .srt
    file and don't care about reusing the translated cues elsewhere
    (translate_clip_cues() + write_srt() do the actual work) — same
    behavior as before this module was split, including timestamps
    relative to the CLIP's own start.

    Returns True if at least one subtitle line was written, False if
    the transcript has no cues at all in this clip's time range.
    """
    cues = translate_clip_cues(transcript, clip_start_seconds, clip_end_seconds, translator, log=log)
    return write_srt(cues, output_path)
