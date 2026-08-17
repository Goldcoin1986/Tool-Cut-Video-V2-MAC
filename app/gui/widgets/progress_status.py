"""Combined progress bar + status label, updated during download/cut."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget


class ProgressStatusWidget(QWidget):
    """Progress bar with an adjacent status text label."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)

        self._status_label = QLabel("Ready")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._progress_bar)
        layout.addWidget(self._status_label)

    def set_progress(self, percent: int, message: str) -> None:
        self._progress_bar.setValue(max(0, min(100, percent)))
        self._status_label.setText(message)

    def set_status(self, message: str) -> None:
        self._status_label.setText(message)

    def reset(self) -> None:
        self._progress_bar.setValue(0)
        self._status_label.setText("Ready")
