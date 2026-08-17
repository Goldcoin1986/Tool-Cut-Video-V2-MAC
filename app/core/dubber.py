"""
Turns a clip's (already-translated) subtitle cues into one Vietnamese
AI-dubbed audio track, spoken in a voice that stays consistent per
speaker (up to 4, both genders) using app.core.diarization's speaker
labels.

Pipeline, per clip:
  1. Each TranslatedCue (see app.core.subtitles) is matched to whichever
     DiarizedSegment (see app.core.diarization) overlaps it the most —
     "who said this line".
  2. Each speaker is assigned a fixed edge-tts voice (see
     assign_speaker_voices()): the 1st female speaker gets
     vi-VN-HoaiMyNeural, the 1st male gets vi-VN-NamMinhNeural. edge-tts
     only ships ONE Vietnamese neural voice per gender, so a 2nd
     speaker of the same gender reuses that same base voice but
     pitch-shifted via edge-tts's own SSML `pitch` parameter — not a
     different voice, just enough to sound distinguishable rather than
     identical to the first same-gender speaker.
  3. Adjacent cues from the SAME speaker are first grouped into "dub
     windows" (see _build_dub_windows()) — one full natural sentence's
     worth of cues, capped by a sentence-pause gap, cue count, and word
     count, exactly the same idea as vi_naturalizer's translation
     windows (build_translation_windows), just applied a second time on
     the already-translated vi_text and additionally speaker-aware so a
     window never crosses a speaker change. Each WINDOW (not each raw
     cue) is then synthesized once via edge-tts (a free, no-API-key
     Microsoft Edge Read Aloud endpoint — same "unofficial, can break
     anytime" caveat as translator.py's Google Translate endpoint; see
     Dubber's docstring for how failures degrade gracefully), up to
     Dubber._MAX_CONCURRENT_SYNTHESES windows at once (see
     Dubber.synthesize_many()). This does two things at once: (a) the
     TTS engine gets to read a whole sentence in one go instead of
     several disjointed fragments each spoken/time-stretched on their
     own, which is what "nói đầy đủ theo câu tự nhiên nhất có thể"
     needs — a sentence chopped into 3 separately-synthesized pieces
     never sounds as natural as one continuous utterance, no matter how
     good the fit-to-slot step is; and (b) since a single edge-tts
     request is a multi-second network round trip, cutting the NUMBER
     of requests (typically 2-6x fewer — one per sentence instead of
     one per subtitle line) is a far bigger speed win than raising
     concurrency alone could give, on top of still running the
     remaining requests a handful at a time (see below) rather than one
     at a time, which is what made "lồng tiếng" by far the slowest part
     of cutting a clip in the first place.
  4. Vietnamese is almost always longer than the source-language line
     it translates, so each synthesized line is time-stretched/
     compressed (FFmpeg's `atempo` filter) to fit its own
     [start, end] subtitle slot as closely as reasonably possible
     without sounding unnatural (clamped range — see _MIN/MAX_ATEMPO).
  5. All lines are laid out on a silent base track the exact length of
     the clip (gaps where nobody's speaking stay silent) and mixed down
     into one .wav — this is the file AudioSettings.dub_path points
     ClipCutter._run_ffmpeg_cut() at.

Every stage here degrades gracefully: a clip that fails diarization
gets one default voice for the whole clip; a line that fails TTS
synthesis is simply left as silence in the final track (matching how
translator.py falls back to the original-language line on a translate
failure) rather than aborting the clip's dubbing — and a clip whose
dubbing fails ENTIRELY still gets cut normally, just without a dub
track (see ClipPipeline.run()'s dubbing-prep stage).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.diarization import DiarizedSegment
from app.core.subtitles import TranslatedCue

logger = logging.getLogger("clip_cutter")

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

VOICE_FEMALE = "vi-VN-HoaiMyNeural"
VOICE_MALE = "vi-VN-NamMinhNeural"

_SAME_GENDER_PITCH_TABLE = ["+0Hz", "-14Hz", "-28Hz", "-42Hz"]
"""Pitch assigned to the 1st/2nd/3rd/4th speaker of a given gender, in
first-appearance order (indexed by that gender's own running count —
see assign_speaker_voices()).

edge-tts only ships ONE Vietnamese neural voice per gender, so every
same-gender speaker beyond the first has to be told apart from the
others purely via this SSML `pitch` shift, not a different voice. The
1st speaker of each gender always stays at "+0Hz" — the base
vi-VN-NamMinhNeural/HoaiMyNeural voice, completely unmodified — and
every speaker after that gets pitched consistently LOWER (never
higher), by roughly one "sounds ~10 years older" step each time,
rather than jumping around above and below the base pitch.

This replaces an earlier version of this table that alternated
direction and used much larger shifts (down to -55Hz) to force the 3rd
and 4th same-gender speaker to sound clearly distinct from each other.
That made those voices sound synthetic/robotic — edge-tts's pitch
shift is a straightforward re-sampling of the SAME base voice, and
pushing it far enough from where that voice was actually recorded is
exactly what starts to sound artificial, which is worse than two
characters sounding only moderately different. A smaller, one-
directional "getting older" shift keeps every voice sounding like a
real human being while still giving each speaker its own identity."""

_MAX_RETRIES = 2
_RETRY_DELAY_SECONDS = 1.5
_MIN_DELAY_BETWEEN_CALLS_SECONDS = 0.2
"""Light self-throttling between edge-tts calls, same philosophy as
translator.py's own throttle — this is also an unofficial free
endpoint, no reason to hammer it faster than necessary."""

_SYNTHESIS_TIMEOUT_SECONDS = 22.0
"""edge-tts is a network call to an unofficial Microsoft endpoint
(same caveat as above) with NO built-in timeout of its own — unlike
every other network/subprocess call in this codebase (see
translator.py's urlopen(timeout=...), ffmpeg_cutter.py's and this same
file's subprocess.run(timeout=...) calls, diarization.py's model
download), which all bound how long they'll wait. Without one here, a
flaky connection, a firewall silently dropping packets, or the
endpoint simply hanging mid-response leaves `communicate.save()`
awaiting forever with no exception and nothing further logged — from
the user's side this looks exactly like "app treo ở bước tạo giọng
đọc, không chạy tiếp" with no way to recover short of killing the
process. Wrapping the call in asyncio.wait_for() below turns that
silent hang into an ordinary, already-handled failure (same retry/
graceful-degrade path as any other synthesis error), so one bad
network moment costs at most ~1 retried line, never the whole run.
22s comfortably covers a long line plus a slow connection without
tying up the whole clip over one stuck request."""

_MAX_CONCURRENT_SYNTHESES = 6
"""How many edge-tts requests Dubber.synthesize_many() fires off at
once. edge-tts's real bottleneck is per-request network round-trip
time (each line commonly takes several seconds), not local CPU work,
so running a handful of requests concurrently finishes a whole clip's
worth of lines several times faster than one at a time — doing them
strictly sequentially is exactly what made dubbing feel painfully slow
for anything more than a couple of lines (and, combined with only one
Activity Log line before the whole batch, made it LOOK hung for
minutes even once _SYNTHESIS_TIMEOUT_SECONDS above stopped it from
hanging FOREVER). Raised from 4 to 6 now that build_dub_track() also
synthesizes per SPEAKER-GROUPED SENTENCE WINDOW instead of per raw cue
(see _build_dub_windows()) — there are already several times fewer
requests in flight in total, so a slightly higher concurrency cap here
still comfortably avoids reading as hammering an unofficial free
endpoint that could rate-limit or block the app for being too
aggressive; same throttling philosophy as
_MIN_DELAY_BETWEEN_CALLS_SECONDS below, just applied per-slot instead
of globally serial."""

_DUB_WINDOW_MAX_CUES = 6
_DUB_WINDOW_MAX_WORDS = 40
_DUB_WINDOW_MAX_GAP_SECONDS = 1.5
"""Same thresholds/philosophy as vi_naturalizer.build_translation_windows'
_MAX_WINDOW_CUES/_MAX_WINDOW_WORDS/_MAX_GAP_SECONDS (kept as separate
constants here rather than imported, since they gate a different
concern — grouping already-TRANSLATED vi_text back into one TTS
request — and dubber.py shouldn't reach into vi_naturalizer's private
names). Cues that were originally translated together as one sentence
almost always regroup back into the same dub window here too (same gap
size, similar cue/word caps), so the sentence read out loud usually
matches the sentence Google Translate actually saw — that alignment is
what makes the dubbed audio sound like one continuous thought instead
of Google Translate's fragment-by-fragment output stitched back
together with pauses.

IMPORTANT: these three caps are now used as-is ONLY by the no-diarization
fallback path (_build_dub_windows_fallback(), when `segments` is empty).
When real diarization data IS available, _build_dub_windows_from_segments()
below treats a shared DiarizedSegment as the primary, authoritative signal
for "still the same continuous utterance" and only falls back to these
caps at a genuine inter-cue pause inside that segment — see that
function's docstring for why (auto-caption line breaks are not reliable
evidence of the speaker actually pausing, but a VAD-derived
DiarizedSegment boundary is)."""

_DUB_WINDOW_SOFT_CUT_MIN_GAP_SECONDS = 0.03
"""Used only inside _build_dub_windows_from_segments(): once a window has
already grown past _DUB_WINDOW_MAX_CUES/_DUB_WINDOW_MAX_WORDS but is still
inside the SAME DiarizedSegment (i.e. the speaker never really stopped
talking, per real VAD data), we still need *some* place to cut it so
atempo isn't forced to stretch/compress a very long sentence to fit its
slot (see _MIN_ATEMPO/_MAX_ATEMPO). The only acceptable cut point in that
situation is one where the underlying cue timestamps themselves show even
a small gap (prev cue's end < next cue's start) — that's the closest
proxy this module has to "there was a brief breath/pause here" without
re-running VAD at word granularity. A gap of essentially 0s between two
auto-caption lines almost always means the caption was split arbitrarily
mid-flow (not a real pause), so cutting there is exactly the bug this
whole rework fixes — hence a small positive threshold here rather than
">= 0"."""

_MIN_ATEMPO = 0.90
_MAX_ATEMPO = 1.15
"""How far a synthesized line can be time-stretched/compressed with
FFmpeg's `atempo` filter to fit its subtitle slot before this just
accepts the leftover mismatch (silence-padded if now shorter than the
slot, trimmed if still longer) instead of pushing atempo further —
atempo technically supports 0.5-2.0, but a stretch/compression that
extreme starts to sound obviously unnatural (chipmunk/slow-motion),
which would make the dub worse, not better.

Narrowed from an earlier 0.75-1.35 range: that wider range let one
line get compressed 25% (fast) and the very next one stretched 35%
(slow) just because their subtitle slots happened to be tight or
loose, which is what made the reading speed noticeably inconsistent
line to line — fast at first, then dragging — even though every line
individually stayed inside the "still sounds natural" bound.
+-10-15% keeps every line's rate much closer to edge-tts's own natural
pace, at the cost of accepting a slightly bigger leftover slot
mismatch (silence-padded/trimmed, same as before) on lines whose
translated length is furthest from their original slot."""

_TARGET_SAMPLE_RATE = 44100


@dataclass
class SpeakerVoice:
    voice: str
    pitch: str  # edge-tts SSML pitch string, e.g. "+0Hz" or "+18Hz"


class Dubber:
    """Stateful wrapper around `edge-tts`, styled after
    app.core.translator.Translator: caches identical (text, voice,
    pitch) syntheses within one run, self-throttles between network
    calls, and degrades gracefully on failure (failure_count/
    success_count mirror Translator's, for the same end-of-run summary
    log line pattern).

    `edge-tts` is an unofficial wrapper around the same endpoint the
    Microsoft Edge browser's "Read Aloud" feature uses — free, no API
    key, but (like Google Translate's free endpoint) not a published,
    versioned API, so it can be rate-limited or change without notice.
    A synthesis failure here must never be able to break clip cutting
    — see build_dub_track()'s per-cue handling below.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], Path] = {}
        self._last_call = 0.0
        self.failure_count = 0
        self.success_count = 0
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="clipcutter_dub_tts_"))
        self._seg_counter = 0
        # Guards _last_call (the self-throttle), success_count/
        # failure_count, and _seg_counter — the only pieces of shared
        # state _synthesize_uncached() touches. synthesize_many() below
        # runs several _synthesize_uncached() calls truly concurrently
        # (via asyncio.to_thread, i.e. real OS threads, not just
        # interleaved coroutines), so these plain read-then-write
        # updates need a lock or two calls landing at the same instant
        # could stomp on each other (e.g. two threads both reading the
        # same _seg_counter value and writing the same output
        # filename, silently overwriting one line's audio with
        # another's). synthesize()'s single-item path below still goes
        # through the same lock, just with essentially no contention.
        self._lock = threading.Lock()

    def synthesize(self, text: str, voice: str, pitch: str = "+0Hz") -> Path | None:
        """Returns a path to a synthesized .mp3 for `text`, or None if
        synthesis failed (never raises). For dubbing a whole clip's
        worth of lines at once, prefer synthesize_many() below instead
        — it runs several of these concurrently rather than one at a
        time."""
        text = text.strip()
        if not text:
            return None
        key = (text, voice, pitch)
        if key in self._cache:
            return self._cache[key]
        result = self._synthesize_uncached(text, voice, pitch)
        if result is not None:
            self._cache[key] = result
        return result

    def synthesize_many(
        self,
        items: list[tuple[str, str, str]],
        on_done: Callable[[int, Path | None], None] | None = None,
    ) -> list[Path | None]:
        """Synthesizes every (text, voice, pitch) in `items`, up to
        _MAX_CONCURRENT_SYNTHESES at once, and returns one Path|None
        per item in the SAME order as `items` (never raises — a failed
        item is simply None in its slot, exactly like synthesize()).

        `on_done(i, result)`, if given, fires once per item as it
        finishes — in COMPLETION order, not input order, since that's
        the whole point of doing this concurrently — so a caller like
        build_dub_track() can log real-time "N/total done" progress
        as lines actually finish instead of only before/after the
        whole batch (which is what made a clip with many lines look
        stuck for minutes even though it was quietly working the whole
        time).
        """
        results: list[Path | None] = [None] * len(items)

        # Already-cached items need no network call and no thread —
        # resolve those immediately so on_done still reports them, and
        # only hand genuinely new items to the concurrent batch below.
        # Items that are new but DUPLICATE each other within this same
        # batch (e.g. the same short line repeated twice in one clip)
        # share a single synthesis task too — dispatching one edge-tts
        # request per unique (text, voice, pitch) rather than one per
        # cue avoids paying for the same network round trip twice.
        pending_by_key: dict[tuple[str, str, str], list[int]] = {}
        for i, (text, voice, pitch) in enumerate(items):
            stripped = text.strip()
            if not stripped:
                if on_done:
                    on_done(i, None)
                continue
            key = (stripped, voice, pitch)
            cached = self._cache.get(key)
            if cached is not None:
                results[i] = cached
                if on_done:
                    on_done(i, cached)
            else:
                pending_by_key.setdefault(key, []).append(i)

        if pending_by_key:
            async def _run_batch() -> None:
                semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SYNTHESES)

                async def _one(key: tuple[str, str, str], indices: list[int]) -> None:
                    stripped, voice, pitch = key
                    async with semaphore:
                        # _synthesize_uncached() is a plain, blocking
                        # (sync) method — including its own internal
                        # asyncio.run() per attempt (see that method's
                        # comment on why that's still fine per-thread).
                        # asyncio.to_thread() runs it in a worker
                        # thread so several can genuinely overlap
                        # instead of one fully finishing before the
                        # next even starts, without having to rewrite
                        # its retry/timeout/throttle logic in async
                        # form.
                        result = await asyncio.to_thread(
                            self._synthesize_uncached, stripped, voice, pitch
                        )
                    if result is not None:
                        self._cache[key] = result
                    for i in indices:
                        results[i] = result
                        if on_done:
                            on_done(i, result)

                await asyncio.gather(*(_one(key, idxs) for key, idxs in pending_by_key.items()))

            asyncio.run(_run_batch())

        return results

    def _synthesize_uncached(self, text: str, voice: str, pitch: str) -> Path | None:
        import edge_tts  # noqa: PLC0415 - lazy import, see diarization.py's Diarizer for the same pattern

        for attempt in range(_MAX_RETRIES + 1):
            with self._lock:
                elapsed = time.monotonic() - self._last_call
                if elapsed < _MIN_DELAY_BETWEEN_CALLS_SECONDS:
                    time.sleep(_MIN_DELAY_BETWEEN_CALLS_SECONDS - elapsed)
                self._last_call = time.monotonic()
                self._seg_counter += 1
                out_path = self._tmp_dir / f"seg_{self._seg_counter:04d}.mp3"
            try:
                communicate = edge_tts.Communicate(text, voice, pitch=pitch)
                # asyncio.run() spins up a brand-new event loop for this
                # call and tears it down when done (same as before) — so
                # wrapping the save() coroutine in wait_for() here just
                # adds a deadline to that same fresh loop. Each attempt
                # (and, when called via synthesize_many() above, each
                # concurrent item) gets its OWN loop this way — separate
                # loops on separate threads don't conflict with each
                # other. See _SYNTHESIS_TIMEOUT_SECONDS above for why
                # this timeout exists at all.
                asyncio.run(
                    asyncio.wait_for(communicate.save(str(out_path)), timeout=_SYNTHESIS_TIMEOUT_SECONDS)
                )
                if out_path.exists() and out_path.stat().st_size > 0:
                    with self._lock:
                        self.success_count += 1
                    return out_path
                raise RuntimeError("edge-tts trả về file rỗng")
            except Exception as exc:  # noqa: BLE001 - network/endpoint failures are expected occasionally; this broad catch also covers asyncio.TimeoutError from wait_for() above (a hung request), routing it through the exact same retry/graceful-degrade path as any other synthesis failure
                logger.debug(
                    "edge-tts attempt %d/%d failed: %s", attempt + 1, _MAX_RETRIES + 1, exc,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY_SECONDS)

        with self._lock:
            self.failure_count += 1
        logger.debug("Lồng tiếng thất bại cho 1 câu, giữ khoảng lặng: %r", text[:60])
        return None

    def cleanup(self) -> None:
        """Delete this Dubber's temp synthesis cache. Best-effort, like
        ClipCutter.cleanup()."""
        shutil.rmtree(self._tmp_dir, ignore_errors=True)


def assign_speaker_voices(segments: list[DiarizedSegment]) -> dict[str, SpeakerVoice]:
    """Map each distinct speaker_label seen in `segments` to a fixed
    SpeakerVoice, in first-appearance order (Diarizer already numbers
    "Speaker 1"/"Speaker 2"/... that way — see its docstring).

    Every same-gender speaker (up to 4, per this feature's stated "up to
    4, 2+2" target case) gets its own distinct pitch from
    _SAME_GENDER_PITCH_TABLE, indexed by that gender's own running
    count — not just the 1st vs. "everyone else after it", which used
    to make the 3rd and 4th same-gender speaker sound identical to each
    other. A 5th+ speaker of one gender (beyond this feature's target
    case) clamps to the table's last entry rather than crashing."""
    voices: dict[str, SpeakerVoice] = {}
    female_count = 0
    male_count = 0
    seen: list[str] = []
    last_index = len(_SAME_GENDER_PITCH_TABLE) - 1
    for seg in segments:
        if seg.speaker_label in voices:
            continue
        seen.append(seg.speaker_label)
        if seg.gender == "male":
            pitch = _SAME_GENDER_PITCH_TABLE[min(male_count, last_index)]
            voices[seg.speaker_label] = SpeakerVoice(VOICE_MALE, pitch)
            male_count += 1
        else:
            pitch = _SAME_GENDER_PITCH_TABLE[min(female_count, last_index)]
            voices[seg.speaker_label] = SpeakerVoice(VOICE_FEMALE, pitch)
            female_count += 1
    return voices


def pick_speaker_for_cue(cue: TranslatedCue, segments: list[DiarizedSegment]) -> str | None:
    """Public alias of _pick_speaker_for_cue(), for callers outside
    this module (e.g. ClipPipeline.run(), which sets
    TranslatedCue.speaker_label right after diarization so the cue
    carries its own speaker identity — see TranslatedCue's docstring)
    that want the exact same overlap-matching logic build_dub_track()
    already uses, instead of a second, possibly-diverging
    implementation."""
    return _pick_speaker_for_cue(cue, segments)


def _pick_speaker_for_cue(cue: TranslatedCue, segments: list[DiarizedSegment]) -> str | None:
    """Whichever DiarizedSegment overlaps `cue` the most in time, by
    label — None if there's no overlap at all (e.g. diarization found
    no speech there, only VAD noise elsewhere in the clip) or if
    `segments` is empty."""
    best_label: str | None = None
    best_overlap = 0.0
    for seg in segments:
        overlap = min(cue.end_seconds, seg.end_seconds) - max(cue.start_seconds, seg.start_seconds)
        if overlap > best_overlap:
            best_overlap = overlap
            best_label = seg.speaker_label
    return best_label


@dataclass
class _DubWindow:
    """One or more adjacent cues from the SAME speaker, joined back
    into one full sentence's worth of vi_text — see
    _build_dub_windows()."""

    cue_indices: list[int]
    text: str
    start_seconds: float
    end_seconds: float
    speaker_label: str | None


def _pick_segment_index_for_cue(cue: TranslatedCue, segments: list[DiarizedSegment]) -> int | None:
    """Same overlap-matching as _pick_speaker_for_cue(), but returns the
    INDEX into `segments` instead of just its speaker_label string.

    _build_dub_windows_from_segments() needs the index, not the label,
    because two DIFFERENT DiarizedSegments can share the same label
    (e.g. "Speaker 1" talks, "Speaker 2" talks, then "Speaker 1" talks
    again) — grouping by label alone would wrongly glue that speaker's
    two separate turns together into one TTS request across an
    intervening turn. Grouping by segment index keeps each real,
    continuous utterance in its own window, which is the whole point
    of this rework (see module docstring / _build_dub_windows_from_segments()).
    """
    best_idx: int | None = None
    best_overlap = 0.0
    for idx, seg in enumerate(segments):
        overlap = min(cue.end_seconds, seg.end_seconds) - max(cue.start_seconds, seg.start_seconds)
        if overlap > best_overlap:
            best_overlap = overlap
            best_idx = idx
    return best_idx


def _build_dub_windows(
    cues: list[TranslatedCue],
    speaker_for_cue: list[str | None],
    segments: list[DiarizedSegment],
) -> list[_DubWindow]:
    """Group `cues` into _DubWindows so build_dub_track() can synthesize
    one full natural-sounding sentence per edge-tts request instead of
    one request per raw subtitle line.

    Dispatches to one of two strategies:
      - `segments` non-empty (the normal case whenever diarization
        succeeded): _build_dub_windows_from_segments() below, which
        uses the real VAD-derived DiarizedSegment boundaries — not
        subtitle-line timestamps — as the authoritative signal for
        "did the speaker actually pause here". This is the fix for
        dubbed audio sounding "khựng giữa câu" (choppy mid-sentence):
        auto-captions frequently split one continuous sentence into
        several short lines with no real pause between them, and the
        old purely-timestamp/word/cue-cap logic would cut a fresh TTS
        request at those artificial line breaks anyway.
      - `segments` empty (diarization failed or wasn't run):
        _build_dub_windows_fallback() below, the original
        timestamp/word/cue-cap-only logic — there's no VAD data to
        fall back on in that case, so this preserves the previous
        behavior exactly.
    """
    if not segments:
        return _build_dub_windows_fallback(cues, speaker_for_cue)
    return _build_dub_windows_from_segments(cues, segments)


def _build_dub_windows_fallback(
    cues: list[TranslatedCue], speaker_for_cue: list[str | None],
) -> list[_DubWindow]:
    """Original grouping logic, used only when there's no diarization
    data to work with (`segments` is empty — see _build_dub_windows()).

    A window closes (a new one starts) when any of these happen:
      - the next cue belongs to a DIFFERENT speaker (never blend two
        speakers' lines into one TTS request — voice/pitch consistency
        per speaker matters more than a slightly longer sentence)
      - the gap to the next cue's start exceeds
        _DUB_WINDOW_MAX_GAP_SECONDS (a pause that long usually means a
        new thought, or even a new speaker diarization mislabeled,
        started)
      - the window has reached _DUB_WINDOW_MAX_CUES cues or
        _DUB_WINDOW_MAX_WORDS words (keep each edge-tts request and its
        atempo fit-to-slot reasonable, and avoid over-stretching one
        synthesized clip across a very long span of the timeline)

    Cues with empty/whitespace-only vi_text (e.g. a cue text_cleaner
    reduced to nothing) are skipped entirely, same as
    vi_naturalizer.build_translation_windows() does for cleaned_texts.
    """
    windows: list[_DubWindow] = []
    cur_indices: list[int] = []
    cur_texts: list[str] = []
    cur_words = 0
    cur_speaker: str | None = None
    cur_start: float | None = None
    prev_end: float | None = None

    def _flush() -> None:
        nonlocal cur_indices, cur_texts, cur_words, cur_speaker, cur_start
        if cur_indices:
            windows.append(
                _DubWindow(
                    cue_indices=cur_indices,
                    text=" ".join(t for t in cur_texts if t),
                    start_seconds=cur_start,  # type: ignore[arg-type]
                    end_seconds=prev_end,  # type: ignore[arg-type]
                    speaker_label=cur_speaker,
                )
            )
        cur_indices, cur_texts, cur_words = [], [], 0
        cur_speaker, cur_start = None, None

    for i, cue in enumerate(cues):
        text = cue.vi_text.strip()
        if not text:
            continue
        label = speaker_for_cue[i]

        gap_too_large = (
            prev_end is not None and cur_indices and (cue.start_seconds - prev_end) > _DUB_WINDOW_MAX_GAP_SECONDS
        )
        speaker_changed = bool(cur_indices) and label != cur_speaker
        if gap_too_large or speaker_changed:
            _flush()

        if not cur_indices:
            cur_start = cue.start_seconds
            cur_speaker = label

        cur_indices.append(i)
        cur_texts.append(text)
        cur_words += len(text.split())
        prev_end = cue.end_seconds

        if len(cur_indices) >= _DUB_WINDOW_MAX_CUES or cur_words >= _DUB_WINDOW_MAX_WORDS:
            _flush()

    _flush()
    return windows


def _build_dub_windows_from_segments(
    cues: list[TranslatedCue], segments: list[DiarizedSegment],
) -> list[_DubWindow]:
    """Group `cues` into _DubWindows using real diarization data as the
    primary "same continuous utterance" signal — this is what actually
    fixes dubbed audio sounding choppy mid-sentence (see module
    docstring and _build_dub_windows()'s).

    Each DiarizedSegment already IS one continuous speaking turn from
    one speaker, built from real VAD on the original audio
    (app.core.diarization.Diarizer.diarize()), with short pauses
    (≤ diarization._MAX_MERGE_GAP_SECONDS) already bridged. So: every
    cue that overlaps the SAME DiarizedSegment is, by construction,
    part of the same uninterrupted turn of speech — regardless of how
    YouTube's auto-captions happened to chop that turn into separate
    subtitle lines. Those cues are grouped into one window and read by
    edge-tts as one continuous sentence, no matter how many cues or
    words that is, UNLESS a genuine pause shows up between two of its
    cues (see _DUB_WINDOW_SOFT_CUT_MIN_GAP_SECONDS) once the group has
    already grown past _DUB_WINDOW_MAX_CUES/_DUB_WINDOW_MAX_WORDS — that
    still-rare cut keeps atempo from having to stretch one very long
    synthesized line across a very long slot (see _MIN_ATEMPO/
    _MAX_ATEMPO), while never cutting at a fake, gap-less line break.

    A window always closes when the next cue maps to a DIFFERENT
    DiarizedSegment (different speaker, OR the same speaker after a
    real pause long enough that diarization itself treated it as a new
    turn) — this also naturally keeps the old hard rule "never blend
    two different speakers into one TTS request" intact, since two
    different speakers are always two different segments.

    Cues that don't overlap any DiarizedSegment at all (e.g. diarization
    found no speech where auto-captions did) fall back to the old
    gap/word/cue-cap grouping among themselves, same spirit as
    _build_dub_windows_fallback() — there's no VAD evidence to lean on
    for those specific cues either way.

    Cues with empty/whitespace-only vi_text are skipped entirely, same
    as _build_dub_windows_fallback().
    """
    windows: list[_DubWindow] = []
    cur_indices: list[int] = []
    cur_texts: list[str] = []
    cur_words = 0
    cur_seg_idx: int | None = None
    cur_start: float | None = None
    prev_end: float | None = None

    def _flush() -> None:
        nonlocal cur_indices, cur_texts, cur_words, cur_start
        if cur_indices:
            speaker_label = segments[cur_seg_idx].speaker_label if cur_seg_idx is not None else None
            windows.append(
                _DubWindow(
                    cue_indices=cur_indices,
                    text=" ".join(t for t in cur_texts if t),
                    start_seconds=cur_start,  # type: ignore[arg-type]
                    end_seconds=prev_end,  # type: ignore[arg-type]
                    speaker_label=speaker_label,
                )
            )
        cur_indices, cur_texts, cur_words = [], [], 0
        cur_start = None

    for i, cue in enumerate(cues):
        text = cue.vi_text.strip()
        if not text:
            continue
        seg_idx = _pick_segment_index_for_cue(cue, segments)
        gap = (cue.start_seconds - prev_end) if (prev_end is not None and cur_indices) else None

        seg_changed = bool(cur_indices) and seg_idx != cur_seg_idx
        if seg_changed:
            _flush()

        if not cur_indices:
            cur_seg_idx = seg_idx
            cur_start = cue.start_seconds

        if cur_indices and cur_seg_idx is None:
            # No diarization evidence for this stretch of cues — fall
            # back to the old pause-based cut (same threshold/spirit as
            # _build_dub_windows_fallback(), just without a speaker
            # label to compare, since there's no segment to read one
            # from here).
            if gap is not None and gap > _DUB_WINDOW_MAX_GAP_SECONDS:
                _flush()
                cur_seg_idx = None
                cur_start = cue.start_seconds
        elif cur_indices and cur_seg_idx is not None:
            # Same real DiarizedSegment as the cues gathered so far —
            # only cut early if the group is already oversized AND
            # there's a genuine (non-zero) pause right here; otherwise
            # keep this whole continuous utterance in one TTS request
            # even past the usual caps (see docstring above).
            over_cap = len(cur_indices) >= _DUB_WINDOW_MAX_CUES or cur_words >= _DUB_WINDOW_MAX_WORDS
            has_real_gap = gap is not None and gap > _DUB_WINDOW_SOFT_CUT_MIN_GAP_SECONDS
            if over_cap and has_real_gap:
                _flush()
                cur_seg_idx = seg_idx
                cur_start = cue.start_seconds

        cur_indices.append(i)
        cur_texts.append(text)
        cur_words += len(text.split())
        prev_end = cue.end_seconds

        # Unconditional cap cut ONLY applies to the no-segment fallback
        # group — a real DiarizedSegment is never force-cut here (it
        # only cuts above, and only at a genuine pause), because
        # cutting a continuous VAD-verified utterance at an arbitrary
        # word count is exactly the choppiness bug this rework fixes.
        if cur_seg_idx is None and (len(cur_indices) >= _DUB_WINDOW_MAX_CUES or cur_words >= _DUB_WINDOW_MAX_WORDS):
            _flush()

    _flush()
    return windows


def _probe_duration_seconds(ffprobe_path: str, path: Path) -> float | None:
    import json

    cmd = [
        ffprobe_path, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            creationflags=_CREATION_FLAGS,
        )
        if proc.returncode != 0:
            return None
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (subprocess.SubprocessError, KeyError, ValueError):
        return None


def _fit_segment_to_slot(
    ffmpeg_path: str, ffprobe_path: str, raw_path: Path, target_seconds: float, out_path: Path,
) -> bool:
    """Time-stretch/compress `raw_path` (a synthesized line) with
    FFmpeg's `atempo` so its duration is as close as reasonable to
    `target_seconds` (the cue's own [start, end] slot width), writing
    a mono 44.1kHz WAV to `out_path`. Returns False (never raises) if
    ffprobe/ffmpeg fail for any reason — the caller then just skips
    this line (silence) rather than losing the whole dub track over
    one bad segment.
    """
    raw_duration = _probe_duration_seconds(ffprobe_path, raw_path)
    if not raw_duration or raw_duration <= 0 or target_seconds <= 0:
        atempo = 1.0
    else:
        atempo = max(_MIN_ATEMPO, min(_MAX_ATEMPO, raw_duration / target_seconds))

    cmd = [
        ffmpeg_path, "-y", "-i", str(raw_path),
        "-filter:a", f"atempo={atempo:.4f}",
        "-ar", str(_TARGET_SAMPLE_RATE), "-ac", "1",
        str(out_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            creationflags=_CREATION_FLAGS,
        )
    except subprocess.SubprocessError:
        return False
    return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


def build_dub_track(
    ffmpeg_path: str,
    ffprobe_path: str,
    cues: list[TranslatedCue],
    segments: list[DiarizedSegment],
    clip_duration_seconds: float,
    dubber: Dubber,
    output_path: Path,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Build one clip-length Vietnamese dub track at `output_path`
    (mono WAV, exact clip duration) from `cues` (already-translated
    subtitle lines) and `segments` (diarization result, [] if
    diarization failed/found nothing — every line then falls back to
    one default female voice for the whole clip).

    `log`, if given, receives one short progress line per synthesized
    WINDOW as it actually finishes (see Dubber.synthesize_many()'s
    on_done) — without this, a clip with many lines produced exactly
    ONE Activity Log line ("Đang tạo giọng đọc cho X…") and then
    nothing for as long as all its lines took combined, which reads
    identically to a hang even when everything is working fine.

    Returns True if at least one window was successfully synthesized
    and placed (so the caller should set AudioSettings.dub_path to
    `output_path`), False if nothing could be dubbed at all (e.g. every
    single TTS call failed) — the caller should then leave dub_path
    unset and let the clip cut proceed with its normal audio, per this
    feature's graceful-degradation policy.
    """
    if not cues:
        return False

    voices = assign_speaker_voices(segments)
    default_voice = SpeakerVoice(VOICE_FEMALE, "+0Hz")

    # Reuse cue.speaker_label if the caller already populated it (see
    # TranslatedCue's docstring — ClipPipeline.run() sets this right
    # after diarization) instead of re-running the same overlap-match
    # a second time; cues from an older caller that never set it
    # (speaker_label is None) fall back to computing it here exactly
    # as before, so this stays fully backward compatible.
    speaker_for_cue = [
        cue.speaker_label if cue.speaker_label is not None else _pick_speaker_for_cue(cue, segments)
        for cue in cues
    ]

    # Group cues from the same speaker back into full-sentence windows
    # (see _build_dub_windows()'s docstring) — this is the key change
    # for both speed (far fewer edge-tts requests) and naturalness (a
    # whole sentence spoken/time-stretched as one continuous clip
    # instead of several disjointed per-cue fragments).
    windows = _build_dub_windows(cues, speaker_for_cue, segments)
    if not windows:
        return False

    voice_for_window = [
        voices.get(w.speaker_label, default_voice) if w.speaker_label else default_voice
        for w in windows
    ]
    synth_items = [(w.text, v.voice, v.pitch) for w, v in zip(windows, voice_for_window)]

    done_count = 0

    def _on_synth_done(_index: int, _result: Path | None) -> None:
        nonlocal done_count
        done_count += 1
        if log:
            log(f"  Đã tạo giọng đọc {done_count}/{len(windows)} cụm câu…")

    raw_paths = dubber.synthesize_many(synth_items, on_done=_on_synth_done if log else None)

    placed: list[tuple[float, Path]] = []
    tmp_dir = output_path.parent
    for i, (window, raw_path) in enumerate(zip(windows, raw_paths)):
        if raw_path is None:
            continue

        fitted_path = tmp_dir / f"{output_path.stem}_seg{i:03d}.wav"
        target = max(0.05, window.end_seconds - window.start_seconds)
        if not _fit_segment_to_slot(ffmpeg_path, ffprobe_path, raw_path, target, fitted_path):
            logger.debug("Không khớp được thời lượng cho 1 cụm câu lồng tiếng, bỏ qua cụm đó.")
            continue

        placed.append((window.start_seconds, fitted_path))

    if not placed:
        return False

    return _mix_dub_segments(ffmpeg_path, placed, clip_duration_seconds, output_path, log=log)


def _mix_dub_segments(
    ffmpeg_path: str,
    placed: list[tuple[float, Path]],
    clip_duration_seconds: float,
    output_path: Path,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Lay every (start_seconds, wav_path) segment onto a silent base
    track exactly `clip_duration_seconds` long and mix them all down
    into `output_path`.

    This used to be ONE ffmpeg command with an (N+1)-input filter_complex
    (silence + every segment at once, `amix=inputs=N+1`) bounded by a
    single 180s timeout. In practice, on at least one real machine, that
    single command hung well past 180s for a clip with under 20 short
    lines — nowhere near enough audio to justify that on CPU alone, so
    the more likely cause is environment-specific (antivirus scanning
    each newly-written temp .wav as ffmpeg opens it, disk I/O, etc. —
    something outside this app's control) rather than a bug in the
    filter graph itself. Either way, one giant command means one slow
    or stuck moment loses the ENTIRE dub track, and a single 180s ceiling
    can't be safely raised much without risking genuinely long clips
    (many cues) tying up the whole cut for that long over one line.

    So this now mixes segments in ONE AT A TIME instead: each step is
    its own small ffmpeg call (silence-or-current-mix + exactly one
    segment), with its own short timeout. This trades one big command
    for several small, fast, independently-bounded ones — a slow or
    stuck moment on ONE segment now costs that one segment (skipped,
    left silent, exactly like a failed synthesis) instead of the whole
    track, and normal-case total time is comparable since each 2-input
    mix of small mono clips is fast.
    """
    duration = max(0.1, clip_duration_seconds)
    tmp_dir = output_path.parent
    buf_a = tmp_dir / f"{output_path.stem}_mixA.wav"
    buf_b = tmp_dir / f"{output_path.stem}_mixB.wav"

    silence_cmd = [
        ffmpeg_path, "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=mono:sample_rate={_TARGET_SAMPLE_RATE}",
        "-t", f"{duration:.3f}",
        "-ar", str(_TARGET_SAMPLE_RATE), "-ac", "1",
        str(buf_a),
    ]
    try:
        proc = subprocess.run(
            silence_cmd, capture_output=True, text=True, timeout=30,
            creationflags=_CREATION_FLAGS,
        )
    except subprocess.SubprocessError as exc:
        logger.warning("Tạo track lồng tiếng thất bại (không tạo được nền im lặng): %s", exc)
        return False
    if proc.returncode != 0 or not buf_a.exists() or buf_a.stat().st_size == 0:
        logger.warning(
            "Tạo track lồng tiếng thất bại (không tạo được nền im lặng): %s",
            proc.stderr[-500:] if proc.stderr else "unknown error",
        )
        return False

    if log:
        log(f"  Đang ghép {len(placed)} câu lồng tiếng vào 1 track…")

    current = buf_a
    other = buf_b
    placed_count = 0
    for start, seg_path in placed:
        delay_ms = max(0, int(round(start * 1000)))
        mix_cmd = [
            ffmpeg_path, "-y",
            "-i", str(current), "-i", str(seg_path),
            "-filter_complex",
            f"[1:a]adelay={delay_ms}:all=1[s1];[0:a][s1]amix=inputs=2:"
            "duration=first:dropout_transition=0:normalize=0[dubout]",
            "-map", "[dubout]",
            "-ar", str(_TARGET_SAMPLE_RATE), "-ac", "1",
            str(other),
        ]
        try:
            proc = subprocess.run(
                mix_cmd, capture_output=True, text=True, timeout=30,
                creationflags=_CREATION_FLAGS,
            )
        except subprocess.SubprocessError as exc:
            logger.debug("Ghép 1 câu vào track lồng tiếng thất bại, bỏ qua câu đó: %s", exc)
            continue
        if proc.returncode != 0 or not other.exists() or other.stat().st_size == 0:
            logger.debug(
                "Ghép 1 câu vào track lồng tiếng thất bại, bỏ qua câu đó: %s",
                proc.stderr[-300:] if proc.stderr else "unknown error",
            )
            continue

        current, other = other, current
        placed_count += 1

    if placed_count == 0:
        # Nothing actually mixed in — this is functionally the same
        # "couldn't dub this clip at all" case build_dub_track already
        # returns False for when every synthesis/fit fails, so treat
        # it identically here rather than shipping a silent dub track.
        return False

    shutil.copyfile(current, output_path)
    for buf in (buf_a, buf_b):
        if buf.exists():
            buf.unlink(missing_ok=True)
    return output_path.exists() and output_path.stat().st_size > 0
