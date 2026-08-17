"""Resolution selector for the downloaded source video."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.gui.widgets.no_scroll_combo import NoScrollComboBox

# Display label -> target max height in pixels (None = best available).
RESOLUTION_OPTIONS: list[tuple[str, int | None]] = [
    ("Tốt nhất có sẵn (Best)", None),
    ("1080p", 1080),
    ("720p", 720),
    ("480p", 480),
    ("360p", 360),
]


class ResolutionPicker(QWidget):
    """Label + QComboBox for choosing the target download resolution."""

    resolution_changed = Signal(object)  # emits int | None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        title = QLabel("ĐỘ PHÂN GIẢI")
        title.setProperty("role", "section-title")

        self._combo = NoScrollComboBox()
        for label, _height in RESOLUTION_OPTIONS:
            self._combo.addItem(label)
        self._combo.setCurrentIndex(0)
        self._combo.currentIndexChanged.connect(
            lambda _idx: self.resolution_changed.emit(self.get_target_height())
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(title)
        layout.addWidget(self._combo)

    def get_target_height(self) -> int | None:
        """Return the selected max height in pixels, or None for 'Best'."""
        return RESOLUTION_OPTIONS[self._combo.currentIndex()][1]

    def set_enabled(self, enabled: bool) -> None:
        self._combo.setEnabled(enabled)
