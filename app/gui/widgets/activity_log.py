"""Scrolling, color-coded activity log panel fed by the logging signal bus."""

from __future__ import annotations

import html

from PySide6.QtWidgets import QLabel, QTextEdit, QVBoxLayout, QWidget

from app.gui import theme

_LEVEL_COLORS = {
    "DEBUG": theme.TEXT_SECONDARY,
    "INFO": theme.TEXT_PRIMARY,
    "WARNING": theme.WARNING,
    "ERROR": theme.ERROR,
    "CRITICAL": theme.ERROR,
}


class ActivityLogWidget(QWidget):
    """Read-only, auto-scrolling log panel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        title = QLabel("ACTIVITY LOG")
        title.setProperty("role", "section-title")

        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setMinimumHeight(120)
        self._text_edit.setStyleSheet(
            f"font-family: Consolas, 'Courier New', monospace; font-size: 12px; "
            f"background-color: {theme.SURFACE};"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(title)
        layout.addWidget(self._text_edit)

    def append_log(self, level: str, message: str) -> None:
        color = _LEVEL_COLORS.get(level, theme.TEXT_PRIMARY)
        # Messages can now include literal '<', '&', etc. from raw
        # yt-dlp/ffmpeg error text, plus embedded '\n' (e.g. the
        # "Chi tiết: ..." detail line appended in cut_worker.py) — since
        # this widget renders as HTML, both need explicit handling or
        # they either break the markup or silently collapse onto one line.
        safe_message = html.escape(message).replace("\n", "<br>")
        self._text_edit.append(f'<span style="color:{color};">{safe_message}</span>')
        scrollbar = self._text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear(self) -> None:
        self._text_edit.clear()
