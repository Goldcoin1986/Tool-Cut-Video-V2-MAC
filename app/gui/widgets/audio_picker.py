"""
Audio options for cut clips: remove the original voice and/or
background sound, and/or overlay the user's own music (one or several
files, played back to back).

Kept as one card (rather than separate ones) since the options
directly interact — whether the custom music REPLACES or gets MIXED
WITH whatever remains of the original audio depends on the "Tắt tiếng
gốc" mode chosen, so a user picking music needs to see both at once to
understand what they'll actually get.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from app.gui import theme
from app.gui.theme import BORDER, SURFACE

_MUSIC_FILE_FILTER = (
    "Audio files (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus);;All files (*)"
)

# remove_mode value -> radio button label
_REMOVE_MODE_LABELS: list[tuple[str, str]] = [
    ("none", "Giữ nguyên"),
    ("voice", "Bỏ giọng nói (giữ âm thanh nền)"),
    ("background", "Bỏ âm thanh nền (giữ giọng nói)"),
    ("both", "Bỏ cả hai (im lặng hoàn toàn)"),
]

_REMOVE_MODE_TOOLTIPS: dict[str, str] = {
    "none": "Không thay đổi âm thanh gốc.",
    "voice": (
        "Thử loại giọng nói ở giữa kênh trái/phải, giữ lại nhạc/âm thanh "
        "nền. Đây là kỹ thuật xử lý tín hiệu cũ (không phải AI tách "
        "giọng) — hiệu quả tốt với nhạc stereo có giọng hát ở giữa, "
        "nhưng thường KHÔNG hiệu quả với video nói chuyện/podcast dạng "
        "mono thường gặp trên YouTube."
    ),
    "background": (
        "Thử lọc theo dải tần giọng nói + khử nhiễu nhẹ để giữ giọng, "
        "giảm âm thanh nền. Đây là ước lượng gần đúng (không phải AI "
        "tách nhạc nền) — giọng có thể nghe hơi mỏng/như gọi điện, và "
        "âm nền cùng dải tần giọng nói vẫn có thể còn sót lại."
    ),
    "both": "Tắt hẳn âm thanh gốc — cách duy nhất đảm bảo 100% không còn sót gì.",
}


class AudioPicker(QWidget):
    """Card with audio options for the cut: remove voice/background
    (or both) from the original audio, and/or add the user's own
    background music (one or several files)."""

    settings_changed = Signal()

    def __init__(
        self,
        initial_remove_mode: str = "none",
        initial_music_paths: list[str] | None = None,
        initial_music_volume: float = 1.0,
        initial_dubbing_enabled: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        card = QFrame(self)
        card.setObjectName("audioCard")
        card.setStyleSheet(
            f"QFrame#audioCard {{"
            f"background-color: {SURFACE};"
            f"border: 1px solid {BORDER};"
            "border-radius: 8px;"
            "}}"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 10, 14, 12)
        card_layout.setSpacing(6)

        title = QLabel("ÂM THANH")
        title.setProperty("role", "section-title")
        title.setStyleSheet("padding-top: 0px;")

        remove_label = QLabel("Tắt tiếng gốc")
        remove_label.setProperty("role", "field-label")

        self._remove_mode_group = QButtonGroup(self)
        self._remove_mode_group.setExclusive(True)
        self._remove_radios: dict[str, QRadioButton] = {}
        remove_row = QHBoxLayout()
        remove_row.setContentsMargins(0, 0, 0, 0)
        remove_row.setSpacing(10)
        for mode, label in _REMOVE_MODE_LABELS:
            radio = QRadioButton(label)
            radio.setToolTip(_REMOVE_MODE_TOOLTIPS[mode])
            self._remove_mode_group.addButton(radio)
            self._remove_radios[mode] = radio
            remove_row.addWidget(radio)
        remove_row.addStretch(1)
        self._remove_radios.get(initial_remove_mode, self._remove_radios["none"]).setChecked(True)
        self._remove_mode_group.buttonToggled.connect(self._on_changed)

        self._music_checkbox = QCheckBox("Thêm nhạc của tôi")
        self._music_paths: list[str] = list(initial_music_paths or [])
        self._music_checkbox.setChecked(bool(self._music_paths))
        self._music_checkbox.toggled.connect(self._on_music_toggled)

        self._music_path_label = QLabel("Chưa chọn file nhạc")
        self._music_path_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
        self._music_path_label.setWordWrap(True)

        browse_btn = QPushButton("Chọn file nhạc… (chọn được nhiều bài)")
        browse_btn.setToolTip(
            "Chọn nhiều file cùng lúc (Ctrl/Shift + click) để nối nhạc "
            "lại thành một danh sách phát cho clip."
        )
        browse_btn.clicked.connect(self._on_browse)
        self._browse_btn = browse_btn

        clear_btn = QPushButton("Bỏ chọn")
        clear_btn.clicked.connect(self._on_clear_music)
        self._clear_btn = clear_btn

        music_file_row = QHBoxLayout()
        music_file_row.setContentsMargins(0, 0, 0, 0)
        music_file_row.setSpacing(6)
        music_file_row.addWidget(browse_btn)
        music_file_row.addWidget(clear_btn)
        music_file_row.addStretch(1)

        volume_label = QLabel("Âm lượng nhạc")
        self._volume_spin = QDoubleSpinBox()
        self._volume_spin.setRange(0.1, 2.0)
        self._volume_spin.setSingleStep(0.1)
        self._volume_spin.setDecimals(1)
        self._volume_spin.setSuffix("x")
        self._volume_spin.setValue(max(0.1, min(2.0, initial_music_volume)))
        self._volume_spin.valueChanged.connect(self._on_changed)

        volume_row = QHBoxLayout()
        volume_row.setContentsMargins(0, 0, 0, 0)
        volume_row.setSpacing(6)
        volume_row.addWidget(volume_label)
        volume_row.addWidget(self._volume_spin)
        volume_row.addStretch(1)

        self._music_details = QWidget()
        details_layout = QVBoxLayout(self._music_details)
        details_layout.setContentsMargins(24, 2, 0, 0)
        details_layout.setSpacing(4)
        details_layout.addWidget(self._music_path_label)
        details_layout.addLayout(music_file_row)
        details_layout.addLayout(volume_row)

        self._mode_hint = QLabel(" ")
        self._mode_hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
        self._mode_hint.setContentsMargins(0, 0, 0, 0)
        self._mode_hint.setWordWrap(True)

        self._dub_checkbox = QCheckBox("Lồng tiếng tự động (AI)")
        self._dub_checkbox.setToolTip(
            "Tạo giọng đọc tiếng Việt AI cho từng clip, đúng giọng theo "
            "từng người nói thật trong clip (tối đa 4 người, phân biệt "
            "cả giới tính lẫn từng cá nhân). Chạy chậm hơn nhiều so với "
            "chỉ cắt clip — cần tách giọng theo người nói + dịch + tạo "
            "giọng đọc cho từng câu, đặc biệt chậm ở lần dùng đầu tiên "
            "khi cần tải mô hình tách giọng. Chỉ dùng được nếu video có "
            "phụ đề gốc (thủ công hoặc tự động) để dịch."
        )
        self._dub_checkbox.setChecked(initial_dubbing_enabled)
        self._dub_checkbox.toggled.connect(self._on_changed)

        self._dub_hint = QLabel(" ")
        self._dub_hint.setStyleSheet(f"color: {theme.WARNING}; font-size: 11px;")
        self._dub_hint.setContentsMargins(24, 0, 0, 0)
        self._dub_hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(card)

        card_layout.addWidget(title)
        card_layout.addWidget(remove_label)
        card_layout.addLayout(remove_row)
        card_layout.addWidget(self._music_checkbox)
        card_layout.addWidget(self._music_details)
        card_layout.addWidget(self._mode_hint)
        card_layout.addWidget(self._dub_checkbox)
        card_layout.addWidget(self._dub_hint)

        self._refresh_music_path_label()
        self._update_visibility()
        self._refresh_mode_hint()
        self._refresh_dub_hint()

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------
    def _on_changed(self, *_args) -> None:
        self._refresh_mode_hint()
        self._refresh_dub_hint()
        self.settings_changed.emit()

    def _on_music_toggled(self, _checked: bool) -> None:
        self._update_visibility()
        self._refresh_mode_hint()
        self._refresh_dub_hint()
        self.settings_changed.emit()

    def _on_browse(self) -> None:
        start_dir = str(Path(self._music_paths[0]).parent) if self._music_paths else str(Path.home())
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Chọn file nhạc (có thể chọn nhiều bài)", start_dir, _MUSIC_FILE_FILTER
        )
        if paths:
            self._music_paths = paths
            self._refresh_music_path_label()
            self._refresh_mode_hint()
            self._refresh_dub_hint()
            self.settings_changed.emit()

    def _on_clear_music(self) -> None:
        self._music_paths = []
        self._music_checkbox.setChecked(False)
        self._refresh_music_path_label()
        self._update_visibility()
        self._refresh_mode_hint()
        self._refresh_dub_hint()
        self.settings_changed.emit()

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------
    def _update_visibility(self) -> None:
        self._music_details.setVisible(self._music_checkbox.isChecked())

    def _refresh_music_path_label(self) -> None:
        if not self._music_paths:
            self._music_path_label.setText("Chưa chọn file nhạc")
            self._music_path_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
            return
        missing = [p for p in self._music_paths if not Path(p).is_file()]
        names = ", ".join(Path(p).name for p in self._music_paths)
        if missing:
            self._music_path_label.setText(f"⚠ Thiếu {len(missing)} file — {names}")
            self._music_path_label.setStyleSheet(f"color: {theme.WARNING}; font-size: 11px;")
        elif len(self._music_paths) == 1:
            self._music_path_label.setText(f"✓ {names}")
            self._music_path_label.setStyleSheet(f"color: {theme.SUCCESS}; font-size: 11px;")
        else:
            self._music_path_label.setText(f"✓ {len(self._music_paths)} bài (nối lại): {names}")
            self._music_path_label.setStyleSheet(f"color: {theme.SUCCESS}; font-size: 11px;")

    def _refresh_mode_hint(self) -> None:
        has_music = self._music_checkbox.isChecked() and bool(self._music_paths)
        mode = self.get_remove_mode()
        if has_music and mode == "both":
            text = "Nhạc của bạn sẽ THAY THẾ hoàn toàn tiếng gốc."
        elif has_music and mode != "none":
            text = "Nhạc của bạn sẽ được TRỘN cùng phần âm thanh gốc còn lại sau khi lọc."
        elif has_music:
            text = "Nhạc của bạn sẽ được TRỘN cùng tiếng gốc (cả hai đều nghe được)."
        elif mode == "both":
            text = "Clip sẽ hoàn toàn không có âm thanh."
        elif mode != "none":
            text = _REMOVE_MODE_TOOLTIPS[mode]
        else:
            text = " "
        self._mode_hint.setText(text)

    def _refresh_dub_hint(self) -> None:
        # See AudioSettings.dub_path's docstring (app/core/ffmpeg_cutter.py)
        # for why "none"/"background" leave the original spoken voice
        # audible underneath the AI dub — surfaced here as a proactive
        # hint rather than silently overriding the user's chosen mode.
        if not self._dub_checkbox.isChecked():
            self._dub_hint.setText(" ")
            return
        mode = self.get_remove_mode()
        if mode in ("voice", "both"):
            self._dub_hint.setText(" ")
        else:
            self._dub_hint.setText(
                "⚠ Với chế độ \"Tắt tiếng gốc\" hiện tại, giọng nói gốc vẫn "
                "sẽ nghe được cùng lúc với giọng lồng tiếng AI — chọn "
                "\"Bỏ giọng nói\" hoặc \"Bỏ cả hai\" ở trên để có kết quả "
                "sạch hơn."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_remove_mode(self) -> str:
        for mode, radio in self._remove_radios.items():
            if radio.isChecked():
                return mode
        return "none"

    def get_music_paths(self) -> list[str]:
        """Chosen music files in pick order, or [] if the "Thêm nhạc
        của tôi" checkbox is off (even if files were previously picked
        — unchecking it means "don't use them this run", not "forget
        them")."""
        if self._music_checkbox.isChecked():
            return list(self._music_paths)
        return []

    def get_music_volume(self) -> float:
        return self._volume_spin.value()

    def get_dubbing_enabled(self) -> bool:
        return self._dub_checkbox.isChecked()

    def set_enabled(self, enabled: bool) -> None:
        for radio in self._remove_radios.values():
            radio.setEnabled(enabled)
        self._music_checkbox.setEnabled(enabled)
        self._browse_btn.setEnabled(enabled)
        self._clear_btn.setEnabled(enabled)
        self._volume_spin.setEnabled(enabled)
        self._dub_checkbox.setEnabled(enabled)
