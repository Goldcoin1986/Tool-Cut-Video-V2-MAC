"""YouTube URL input field with a live validity indicator."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget

from app.core.downloader import is_valid_youtube_url
from app.gui import theme


class UrlInputWidget(QWidget):
    """Label + QLineEdit for the YouTube URL, with inline validity feedback."""

    url_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        title = QLabel("YOUTUBE URL")
        title.setProperty("role", "section-title")

        self._line_edit = QLineEdit()
        self._line_edit.setPlaceholderText("https://www.youtube.com/watch?v=…")
        self._line_edit.textChanged.connect(self._on_text_changed)

        self._hint = QLabel(" ")
        self._hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(title)
        layout.addWidget(self._line_edit)
        layout.addWidget(self._hint)

    def _on_text_changed(self, text: str) -> None:
        text = text.strip()
        if not text:
            self._hint.setText(" ")
            self._line_edit.setStyleSheet("")
        elif is_valid_youtube_url(text):
            self._hint.setText("✓ Valid YouTube URL")
            self._hint.setStyleSheet(f"color: {theme.SUCCESS}; font-size: 11px;")
        else:
            self._hint.setText("⚠ This doesn't look like a YouTube URL")
            self._hint.setStyleSheet(f"color: {theme.WARNING}; font-size: 11px;")
        self.url_changed.emit(text)

    def get_url(self) -> str:
        return self._line_edit.text().strip()

    def is_valid(self) -> bool:
        return is_valid_youtube_url(self.get_url())

    def set_enabled(self, enabled: bool) -> None:
        self._line_edit.setEnabled(enabled)
