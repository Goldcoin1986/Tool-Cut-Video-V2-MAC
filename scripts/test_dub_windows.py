"""
Test độc lập (không cần GUI, không cần mạng) cho VẤN ĐỀ 1: giọng lồng
tiếng AI bị ngắt/khựng giữa câu.

Kiểm tra hàm app.core.dubber._build_dub_windows() — hàm gộp các
TranslatedCue liên tiếp cùng người nói thành 1 "cụm câu" trước khi gọi
edge-tts. Test này KHÔNG gọi mạng, KHÔNG cần edge-tts thật: nó chỉ dựng
dữ liệu TranslatedCue/DiarizedSegment giả rồi gọi thẳng _build_dub_windows()
để kiểm tra logic gộp cụm.

Chạy: python scripts/test_dub_windows.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.diarization import DiarizedSegment
from app.core.dubber import (
    _DUB_WINDOW_MAX_CUES,
    _DUB_WINDOW_MAX_WORDS,
    _build_dub_windows,
)
from app.core.subtitles import TranslatedCue

_FAILURES: list[str] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _FAILURES.append(name)


def test_long_sentence_one_segment_stays_one_window() -> None:
    """1 câu dài bị YouTube tách thành 5 dòng phụ đề ngắn, NẰM TRỌN
    trong 1 DiarizedSegment duy nhất (VAD không thấy khoảng lặng thật
    nào giữa các dòng) → phải chỉ tạo ĐÚNG 1 cụm câu (1 lần gọi TTS),
    dù tổng số từ vượt _DUB_WINDOW_MAX_WORDS (40) và số dòng vượt
    _DUB_WINDOW_MAX_CUES (6)."""
    # 8 dòng phụ đề ngắn, mỗi dòng cách dòng trước ĐÚNG 0.0s (auto-caption
    # tách câu dài ra, không có khoảng lặng thật nào giữa các dòng) —
    # tổng cộng ~48 từ, vượt cả 2 ngưỡng cũ.
    words_per_line = ["đây là một câu rất dài được"] * 8
    cues: list[TranslatedCue] = []
    t = 0.0
    for line in words_per_line:
        dur = 1.2
        cues.append(
            TranslatedCue(
                start_seconds=t,
                end_seconds=t + dur,
                vi_text=line,
                en_text="",
            )
        )
        t += dur  # gap = 0.0s giữa 2 dòng liên tiếp — không có khoảng lặng thật

    total_words = sum(len(c.vi_text.split()) for c in cues)
    assert total_words >= _DUB_WINDOW_MAX_WORDS, "test setup phải vượt ngưỡng từ cũ"
    assert len(cues) > _DUB_WINDOW_MAX_CUES, "test setup phải vượt ngưỡng số dòng cũ"

    # 1 DiarizedSegment duy nhất bao trọn toàn bộ 8 dòng trên — đây là dữ
    # liệu VAD thật xác nhận người nói KHÔNG hề dừng lời.
    segments = [
        DiarizedSegment(
            start_seconds=0.0,
            end_seconds=t,
            speaker_label="Speaker 1",
            gender="female",
        )
    ]
    speaker_for_cue = ["Speaker 1"] * len(cues)

    windows = _build_dub_windows(cues, speaker_for_cue, segments)

    _check(
        "1 DiarizedSegment dài -> đúng 1 cụm câu (1 lần gọi TTS)",
        len(windows) == 1,
        f"got {len(windows)} windows: {[w.cue_indices for w in windows]}",
    )
    if windows:
        _check(
            "cụm câu chứa đủ cả 8 dòng phụ đề gốc",
            windows[0].cue_indices == list(range(len(cues))),
            f"got {windows[0].cue_indices}",
        )
        _check(
            "text của cụm câu nối đủ cả 8 dòng (không bị bỏ sót)",
            windows[0].text.count("đây là một câu rất dài được") == len(cues),
        )


def test_real_pause_inside_long_segment_can_still_cut() -> None:
    """Trong 1 DiarizedSegment RẤT dài (vượt ngưỡng nhiều lần), nếu có
    1 khoảng lặng thật (dù nhỏ) giữa 2 dòng phụ đề, hàm được PHÉP cắt ở
    đúng chỗ đó (để atempo không phải co giãn quá mức) — nhưng KHÔNG
    được cắt ở những chỗ gap = 0."""
    cues: list[TranslatedCue] = []
    t = 0.0
    # 10 dòng đầu, gap = 0 giữa các dòng (không phải chỗ để cắt)
    for _ in range(10):
        dur = 1.0
        cues.append(TranslatedCue(t, t + dur, "một hai ba bốn năm sáu bảy tám", ""))
        t += dur
    # 1 khoảng lặng NHỎ nhưng CÓ THẬT (0.2s) — điểm cắt hợp lệ duy nhất
    t += 0.2
    # thêm 2 dòng nữa sau khoảng lặng đó, vẫn trong cùng 1 DiarizedSegment
    for _ in range(2):
        dur = 1.0
        cues.append(TranslatedCue(t, t + dur, "chín mười mười một mười hai", ""))
        t += dur

    segments = [DiarizedSegment(0.0, t, "Speaker 1", "male")]
    speaker_for_cue = ["Speaker 1"] * len(cues)

    windows = _build_dub_windows(cues, speaker_for_cue, segments)

    _check(
        "đoạn quá dài với 1 khoảng lặng thật -> được phép cắt (không bắt buộc còn 1 cụm)",
        len(windows) >= 1,
    )
    # Không có cụm nào được cắt ở giữa 2 dòng có gap = 0 (chỉ số dòng bất kỳ
    # trong 10 dòng đầu không bao giờ đứng cuối 1 cụm trừ khi là dòng 9
    # (index 9), vì đó là dòng ngay trước khoảng lặng thật).
    for w in windows:
        last_idx = w.cue_indices[-1]
        if last_idx == len(cues) - 1:
            continue  # cụm cuối cùng, luôn hợp lệ
        _check(
            f"cụm kết thúc ở dòng {last_idx} chỉ xảy ra tại khoảng lặng thật",
            last_idx == 9,
            f"cụm bị cắt sai chỗ (gap=0) ở dòng {last_idx}",
        )


def test_speaker_change_always_splits_even_within_tight_timing() -> None:
    """Không bao giờ được gộp 2 người nói khác nhau vào chung 1 cụm câu,
    kể cả khi timing sát nhau (bug cũ này không được phá khi sửa)."""
    cues = [
        TranslatedCue(0.0, 1.0, "xin chào", ""),
        TranslatedCue(1.0, 2.0, "tôi khỏe", ""),  # người nói khác, liền ngay sau
    ]
    segments = [
        DiarizedSegment(0.0, 1.0, "Speaker 1", "female"),
        DiarizedSegment(1.0, 2.0, "Speaker 2", "male"),
    ]
    speaker_for_cue = ["Speaker 1", "Speaker 2"]

    windows = _build_dub_windows(cues, speaker_for_cue, segments)

    _check("2 người nói khác nhau -> 2 cụm câu riêng biệt", len(windows) == 2)
    if len(windows) == 2:
        _check("cụm 1 đúng người nói 1", windows[0].speaker_label == "Speaker 1")
        _check("cụm 2 đúng người nói 2", windows[1].speaker_label == "Speaker 2")


def test_empty_segments_falls_back_to_old_behavior() -> None:
    """segments rỗng (diarization thất bại) -> vẫn dùng logic cũ dựa
    trên gap/word/cue cap theo timestamp phụ đề (hành vi fallback)."""
    cues = []
    t = 0.0
    for _ in range(_DUB_WINDOW_MAX_CUES + 2):
        cues.append(TranslatedCue(t, t + 1.0, "một hai ba", ""))
        t += 1.0
    speaker_for_cue = [None] * len(cues)

    windows = _build_dub_windows(cues, speaker_for_cue, [])

    _check(
        "segments rỗng -> vẫn cắt cụm theo ngưỡng cue cũ (fallback)",
        len(windows) >= 2,
        f"got {len(windows)} windows",
    )


if __name__ == "__main__":
    test_long_sentence_one_segment_stays_one_window()
    test_real_pause_inside_long_segment_can_still_cut()
    test_speaker_change_always_splits_even_within_tight_timing()
    test_empty_segments_falls_back_to_old_behavior()

    print()
    if _FAILURES:
        print(f"THẤT BẠI: {len(_FAILURES)} kiểm tra không đạt -> {_FAILURES}")
        sys.exit(1)
    print("Tất cả kiểm tra đều đạt.")
