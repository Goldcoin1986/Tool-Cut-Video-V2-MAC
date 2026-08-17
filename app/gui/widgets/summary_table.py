"""
Post-run Summary table: shown after cutting finishes, columns
Filename | Duration | File Size | Status | Transcript — lets the user
verify every clip was created successfully at a glance, and read the
caption text spoken during that clip.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from app.core.models import ClipResult, ClipStatus
from app.gui import theme
from app.utils.file_utils import format_filesize
from app.utils.time_utils import format_duration

_COLUMNS = ["Filename", "Duration", "File Size", "Status", "Transcript"]
_TRANSCRIPT_PREVIEW_CHARS = 80

_STATUS_COLORS = {
    ClipStatus.DONE: theme.SUCCESS,
    ClipStatus.FAILED: theme.ERROR,
}


class SummaryTableWidget(QTableWidget):
    """Read-only table summarizing the final outcome of every clip."""

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(_COLUMNS), parent)
        self.setHorizontalHeaderLabels(_COLUMNS)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)

    def populate(self, results: list[ClipResult]) -> None:
        """Fill the table with the final outcome of every clip in the batch."""
        self.setRowCount(0)
        for row, result in enumerate(results):
            self.insertRow(row)
            self.setItem(row, 0, self._item(result.filename))
            duration_text = (
                format_duration(result.actual_duration_seconds)
                if result.actual_duration_seconds is not None
                else "-"
            )
            self.setItem(row, 1, self._item(duration_text))
            self.setItem(row, 2, self._item(format_filesize(result.file_size_bytes)))
            self.setItem(row, 3, self._status_item(result))
            self.setItem(row, 4, self._transcript_item(result))

    def clear_table(self) -> None:
        self.setRowCount(0)

    @staticmethod
    def _item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    @staticmethod
    def _status_item(result: ClipResult) -> QTableWidgetItem:
        label = "✓ Done" if result.status == ClipStatus.DONE else "✗ Failed"
        item = QTableWidgetItem(label)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        color = _STATUS_COLORS.get(result.status, theme.TEXT_PRIMARY)
        item.setForeground(QColor(color))
        if result.error_message:
            item.setToolTip(result.error_message)
        return item

    @staticmethod
    def _transcript_item(result: ClipResult) -> QTableWidgetItem:
        if not result.transcript:
            item = QTableWidgetItem("—")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setForeground(QColor(theme.TEXT_SECONDARY))
            return item

        lang_label = (result.transcript_language or "?").upper()
        tag = f"[{lang_label} · Tự động] " if result.transcript_is_auto else f"[{lang_label}] "

        preview = result.transcript
        if len(preview) > _TRANSCRIPT_PREVIEW_CHARS:
            preview = preview[:_TRANSCRIPT_PREVIEW_CHARS].rstrip() + "…"

        item = QTableWidgetItem(tag + preview)
        item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        if result.transcript_is_auto:
            item.setForeground(QColor(theme.WARNING))

        language_names = {"vi": "Tiếng Việt", "en": "Tiếng Anh"}
        language_name = language_names.get(result.transcript_language or "", lang_label)
        tooltip_header = f"Ngôn ngữ: {language_name}"
        if result.transcript_is_auto:
            tooltip_header += " (phụ đề tự động - có thể không chính xác)"
        item.setToolTip(f"{tooltip_header}\n\n{result.transcript}")
        return item
