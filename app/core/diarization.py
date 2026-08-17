"""
Best-effort speaker diarization for ONE clip's audio (not the whole
source video) — used by app.core.dubber to figure out which of up to 4
speakers said each subtitle line, so each one can get its own
consistent TTS voice.

Deliberately avoids `pyannote.audio`: it's the "correct"/most accurate
option, but its pretrained pipelines are gated behind a Hugging Face
account + access token, which is a non-starter for a desktop .exe an
end user just downloads and runs — there's no sane place to ask a
non-technical user for an HF token, and baking one into the app would
leak the developer's own token to every copy of the .exe. Instead this
uses a lighter, fully-local, no-account stack:
  - `webrtcvad` for voice-activity detection (which stretches of the
    clip have someone talking at all).
  - `resemblyzer` for per-utterance speaker embeddings (its pretrained
    weights ship INSIDE the pip package itself — no network download
    on first use, unlike some other options that pull a model from a
    hub the first time they run).
  - `scikit-learn`'s AgglomerativeClustering to group utterances into
    up to 4 speakers, and `librosa` (pYIN) to estimate each cluster's
    average pitch for a male/female guess.

Accuracy caveat (see the feature's top-level docstring in dubber.py for
the full picture): this is run on a single ~2-minute clip with limited
samples per speaker, so it's a reasonable prototype-quality guess, NOT
studio-grade diarization — two speakers of the same gender with similar
voices, or heavy cross-talk/overlap, can get misattributed. Good enough
to make dubbed multi-speaker clips sound noticeably more natural than
one flat voice for everyone; not a promise of perfect attribution.

Heavy-dependency note: resemblyzer/librosa/scikit-learn/webrtcvad are
all imported LAZILY inside the functions that need them (not at module
import time) — see Diarizer._ensure_loaded()'s docstring for why.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.utils.exceptions import CutError

logger = logging.getLogger("clip_cutter")

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_VAD_SAMPLE_RATE = 16000
_VAD_FRAME_MS = 30  # webrtcvad only accepts 10/20/30ms frames
_VAD_AGGRESSIVENESS = 2  # 0 (least aggressive) .. 3 (most aggressive)
_MIN_SPEECH_SEGMENT_SECONDS = 0.35
_MAX_MERGE_GAP_SECONDS = 0.30  # bridge short pauses within one utterance
_MAX_SPEAKERS = 4
_MALE_FEMALE_F0_THRESHOLD_HZ = 165.0
"""Rough dividing line between typical adult male and female average
speaking F0 — standard, widely-cited approximate cutoff (typical male
range ~85-180Hz, typical female range ~165-255Hz); good enough for a
binary best-effort guess, not a clinical measurement."""


@dataclass
class DiarizedSegment:
    """One speech utterance within a clip, in the CLIP's own relative
    timeline (0:00 = clip start — matches build_translated_srt()'s
    timestamps so app.core.dubber can overlap-match cues against these
    directly without any offset math)."""

    start_seconds: float
    end_seconds: float
    speaker_label: str  # "Speaker 1".."Speaker 4", stable within one clip
    gender: str  # "male" or "female" (best-effort guess, never "unknown"
    # — see Diarizer._guess_gender's docstring for why a guess is always
    # returned rather than a 3rd "unsure" value that dubber.py would
    # then have to special-case)


def extract_clip_audio(
    ffmpeg_path: str,
    source_path: Path,
    clip_start_seconds: float,
    clip_end_seconds: float,
    output_path: Path,
) -> None:
    """Extract just this ONE clip's audio (not the whole source video)
    into a mono 16kHz WAV at `output_path` — the format both webrtcvad
    and resemblyzer expect. This is a cheap, fast ffmpeg call (a few
    seconds of audio decode), nowhere near the cost of diarizing the
    entire source video would be, which is why diarization only ever
    runs on the small per-clip slice rather than once for the whole
    download.
    """
    from app.utils.time_utils import format_timestamp_for_ffmpeg

    duration = max(0.0, clip_end_seconds - clip_start_seconds)
    cmd = [
        ffmpeg_path, "-y",
        "-ss", format_timestamp_for_ffmpeg(clip_start_seconds),
        "-t", format_timestamp_for_ffmpeg(duration),
        "-i", str(source_path),
        "-vn", "-ac", "1", "-ar", str(_VAD_SAMPLE_RATE),
        "-f", "wav", str(output_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            creationflags=_CREATION_FLAGS,
        )
    except subprocess.SubprocessError as exc:
        raise CutError("Không trích được audio của clip để tách giọng.", details=str(exc)) from exc
    if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        raise CutError(
            "Không trích được audio của clip để tách giọng.",
            details=proc.stderr[-500:] if proc.stderr else None,
        )


class Diarizer:
    """Loads its (heavy) ML dependencies lazily, once, on first real
    use — NOT at import time and NOT in __init__ — so simply
    constructing a ClipPipeline (which happens on every app launch,
    dubbing on or off) never pays resemblyzer/librosa/scikit-learn's
    import cost. The cost is only paid the first time a user actually
    runs a cut with dubbing enabled, and only once per app process
    after that (the loaded VoiceEncoder is cached on `self`).

    Not thread-safe to call diarize() concurrently from multiple
    threads on the SAME Diarizer instance — see ClipPipeline.run()'s
    module-level note on why the dubbing-prep stage runs one clip at a
    time rather than in ClipCutter's usual thread pool.
    """

    def __init__(self) -> None:
        self._encoder = None  # resemblyzer.VoiceEncoder, loaded lazily

    def _ensure_loaded(self) -> None:
        if self._encoder is not None:
            return
        logger.info(
            "Đang tải mô hình tách giọng theo người nói (chỉ lần đầu "
            "dùng tính năng lồng tiếng trong phiên này)…"
        )
        from resemblyzer import VoiceEncoder  # noqa: PLC0415 - intentional lazy import
        # device="cpu": this app has no GPU dependency anywhere else and
        # can't assume the end user's machine has a CUDA-capable GPU or
        # the matching PyTorch build installed — CPU inference on a
        # ~2-minute clip's worth of short utterances is fast enough
        # (well under a second per utterance) not to need one.
        self._encoder = VoiceEncoder(device="cpu")

    def diarize(self, wav_path: Path, max_speakers: int = _MAX_SPEAKERS) -> list[DiarizedSegment]:
        """Diarize one clip's mono 16kHz WAV (see extract_clip_audio)
        into up to `max_speakers` speakers.

        Returns [] (never raises) if the audio has no detectable speech
        at all, or if diarization fails for any reason — dubber.py
        treats an empty list as "assign everything to one default
        voice" rather than this being a fatal error for the whole cut.
        """
        try:
            return self._diarize_uncached(wav_path, max_speakers)
        except Exception as exc:  # noqa: BLE001 - diarization is best-effort
            logger.warning("Tách giọng theo người nói thất bại, dùng 1 giọng mặc định: %s", exc)
            return []

    def _diarize_uncached(self, wav_path: Path, max_speakers: int) -> list[DiarizedSegment]:
        import numpy as np  # noqa: PLC0415
        import librosa  # noqa: PLC0415
        from resemblyzer import preprocess_wav  # noqa: PLC0415

        self._ensure_loaded()

        y, _sr = librosa.load(str(wav_path), sr=_VAD_SAMPLE_RATE, mono=True)
        if y.size == 0:
            return []

        raw_segments = self._voice_activity_segments(y)
        if not raw_segments:
            return []

        embeddings: list["np.ndarray"] = []
        kept_segments: list[tuple[float, float]] = []
        for start_s, end_s in raw_segments:
            chunk = y[int(start_s * _VAD_SAMPLE_RATE):int(end_s * _VAD_SAMPLE_RATE)]
            if chunk.size < int(_MIN_SPEECH_SEGMENT_SECONDS * _VAD_SAMPLE_RATE):
                continue
            try:
                processed = preprocess_wav(chunk, source_sr=_VAD_SAMPLE_RATE)
                if processed.size == 0:
                    continue
                embeddings.append(self._encoder.embed_utterance(processed))
                kept_segments.append((start_s, end_s))
            except Exception as exc:  # noqa: BLE001 - one bad utterance shouldn't kill the whole clip
                logger.debug("Bỏ qua 1 đoạn khi tách giọng (lỗi embedding): %s", exc)

        if not kept_segments:
            return []

        labels = self._cluster(embeddings, max_speakers)

        # Stable "Speaker N" naming: numbered by FIRST time each cluster
        # is heard, not by raw cluster id (which has no meaningful
        # order) — makes voice assignment in dubber.py deterministic and
        # matches how a human would naturally refer to "the first
        # person who spoke" as Speaker 1.
        first_seen: dict[int, float] = {}
        for (start_s, _end_s), cluster_id in zip(kept_segments, labels):
            first_seen.setdefault(cluster_id, start_s)
        ordered_clusters = sorted(first_seen, key=lambda c: first_seen[c])
        label_names = {cluster_id: f"Speaker {i + 1}" for i, cluster_id in enumerate(ordered_clusters)}

        genders = self._guess_genders(y, kept_segments, labels, ordered_clusters)

        return [
            DiarizedSegment(
                start_seconds=start_s,
                end_seconds=end_s,
                speaker_label=label_names[cluster_id],
                gender=genders[cluster_id],
            )
            for (start_s, end_s), cluster_id in zip(kept_segments, labels)
        ]

    @staticmethod
    def _voice_activity_segments(y) -> list[tuple[float, float]]:
        """Frame-level webrtcvad pass -> merged (start, end) speech
        segments, in seconds. Short pauses shorter than
        _MAX_MERGE_GAP_SECONDS are bridged into the surrounding
        utterance (natural mid-sentence breathing pauses shouldn't
        fragment one utterance into several tiny ones); segments
        shorter than _MIN_SPEECH_SEGMENT_SECONDS after merging are
        dropped as noise/too-short-to-embed-reliably.
        """
        import numpy as np  # noqa: PLC0415
        import webrtcvad  # noqa: PLC0415

        vad = webrtcvad.Vad(_VAD_AGGRESSIVENESS)
        frame_len = int(_VAD_SAMPLE_RATE * _VAD_FRAME_MS / 1000)

        pcm16 = (np.clip(y, -1.0, 1.0) * 32767.0).astype(np.int16)
        n_frames = len(pcm16) // frame_len
        if n_frames == 0:
            return []

        voiced_flags: list[bool] = []
        for i in range(n_frames):
            frame = pcm16[i * frame_len:(i + 1) * frame_len]
            try:
                voiced_flags.append(vad.is_speech(frame.tobytes(), _VAD_SAMPLE_RATE))
            except Exception:  # noqa: BLE001 - a single malformed frame shouldn't abort VAD
                voiced_flags.append(False)

        frame_seconds = _VAD_FRAME_MS / 1000.0
        raw: list[tuple[float, float]] = []
        seg_start: float | None = None
        for i, voiced in enumerate(voiced_flags):
            t = i * frame_seconds
            if voiced and seg_start is None:
                seg_start = t
            elif not voiced and seg_start is not None:
                raw.append((seg_start, t))
                seg_start = None
        if seg_start is not None:
            raw.append((seg_start, n_frames * frame_seconds))

        merged: list[tuple[float, float]] = []
        for start_s, end_s in raw:
            if merged and start_s - merged[-1][1] <= _MAX_MERGE_GAP_SECONDS:
                merged[-1] = (merged[-1][0], end_s)
            else:
                merged.append((start_s, end_s))

        return [(s, e) for s, e in merged if e - s >= _MIN_SPEECH_SEGMENT_SECONDS]

    @staticmethod
    def _cluster(embeddings: list, max_speakers: int) -> list[int]:
        """Cluster utterance embeddings into up to `max_speakers`
        speakers.

        The true speaker count isn't known ahead of time, so this
        tries k = 1..max_speakers and picks whichever k has the best
        silhouette score (a standard, simple "how well-separated are
        these clusters" metric) — falling back to a single speaker (k=1)
        if no k>=2 clusters clearly better than treating everyone as
        one speaker (silhouette below a low sanity threshold, or too
        few utterances to even try k>=2). This is a deliberately simple
        heuristic appropriate for a prototype with only a handful of
        utterances per clip — not a substitute for a proper speaker-
        count-estimation algorithm, but good enough to usually get the
        right number of speakers for the common 1-4 speaker podcast
        case this feature targets.
        """
        import numpy as np  # noqa: PLC0415
        from sklearn.cluster import AgglomerativeClustering  # noqa: PLC0415
        from sklearn.metrics import silhouette_score  # noqa: PLC0415

        n = len(embeddings)
        if n <= 1:
            return [0] * n

        X = np.stack(embeddings)
        max_k = min(max_speakers, n - 1)
        if max_k < 2:
            return [0] * n

        best_k = 1
        best_score = -1.0
        for k in range(2, max_k + 1):
            try:
                model = AgglomerativeClustering(n_clusters=k, metric="cosine", linkage="average")
                labels = model.fit_predict(X)
                score = silhouette_score(X, labels, metric="cosine")
            except Exception:  # noqa: BLE001 - a bad k shouldn't abort the whole search
                continue
            if score > best_score:
                best_score = score
                best_k = k

        _MIN_USEFUL_SILHOUETTE = 0.15  # below this, clusters aren't meaningfully separated
        if best_k < 2 or best_score < _MIN_USEFUL_SILHOUETTE:
            return [0] * n

        model = AgglomerativeClustering(n_clusters=best_k, metric="cosine", linkage="average")
        return list(model.fit_predict(X))

    @staticmethod
    def _guess_genders(
        y, kept_segments: list[tuple[float, float]], labels: list[int], ordered_clusters: list[int],
    ) -> dict[int, str]:
        """Best-effort male/female guess per cluster, via mean F0
        (pitch) across all of that cluster's utterance audio, using
        librosa's pYIN pitch tracker. Always returns SOME gender per
        cluster (never "unknown") — see DiarizedSegment.gender's
        docstring for why; a wrong guess just means that speaker gets
        the "wrong" edge-tts voice family, which is a much smaller
        problem for a dubbing feature than dubber.py having to handle
        a 3-way male/female/unknown branch everywhere downstream.
        """
        import numpy as np  # noqa: PLC0415
        import librosa  # noqa: PLC0415

        genders: dict[int, str] = {}
        for cluster_id in ordered_clusters:
            chunks = [
                y[int(s * _VAD_SAMPLE_RATE):int(e * _VAD_SAMPLE_RATE)]
                for (s, e), lbl in zip(kept_segments, labels)
                if lbl == cluster_id
            ]
            if not chunks:
                genders[cluster_id] = "female"
                continue
            audio = np.concatenate(chunks)
            try:
                f0, voiced_flag, _voiced_prob = librosa.pyin(
                    audio, fmin=65.0, fmax=400.0, sr=_VAD_SAMPLE_RATE,
                )
                voiced_f0 = f0[voiced_flag] if voiced_flag is not None else f0
                voiced_f0 = voiced_f0[~np.isnan(voiced_f0)]
                mean_f0 = float(np.mean(voiced_f0)) if voiced_f0.size else None
            except Exception:  # noqa: BLE001 - pitch tracking is best-effort
                mean_f0 = None

            if mean_f0 is None:
                # No reliable pitch estimate — default to the more
                # common edge-tts default voice family (female) rather
                # than leaving this cluster unassigned.
                genders[cluster_id] = "female"
            else:
                genders[cluster_id] = "male" if mean_f0 < _MALE_FEMALE_F0_THRESHOLD_HZ else "female"
        return genders
