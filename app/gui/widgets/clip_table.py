"""
Detected Clips table (Smart Clip Preview).

Display-only table populated after Analyze with columns:
Clip | Start | End | Length | Status. Status cells update live as
CutWorker reports progress on each clip.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from app.core.models import ClipRequest, ClipResult, ClipStatus
from app.gui import theme
from app.utils.time_utils import format_duration

_COLUMNS = ["Clip", "Start", "End", "Length", "Status"]

_STATUS_COLORS = {
    ClipStatus.PENDING: theme.TEXT_SECONDARY,
    ClipStatus.DOWNLOADING: theme.WARNING,
    ClipStatus.CUTTING: theme.WARNING,
    ClipStatus.DONE: theme.SUCCESS,
    ClipStatus.FAILED: theme.ERROR,
}


class ClipTableWidget(QTableWidget):
    """Read-only table showing every detected clip and its live status."""

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(_COLUMNS), parent)
        self.setHorizontalHeaderLabels(_COLUMNS)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setAlternatingRowColors(False)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)

        self._row_by_index: dict[int, int] = {}

    def populate(self, requests: list[ClipRequest]) -> None:
        """Fill the table from freshly analyzed clip requests, all Pending."""
        self.setRowCount(0)
        self._row_by_index.clear()

        for row, request in enumerate(requests):
            self.insertRow(row)
            self._row_by_index[request.index] = row

            self.setItem(row, 0, self._item(request.label))
            self.setItem(row, 1, self._item(format_duration(request.start_seconds)))
            self.setItem(row, 2, self._item(format_duration(request.end_seconds)))
            self.setItem(row, 3, self._item(format_duration(request.duration_seconds)))
            self.setItem(row, 4, self._status_item(ClipStatus.PENDING))

    def update_status(self, result: ClipResult) -> None:
        """Update a single row's Status cell as CutWorker reports progress."""
        row = self._row_by_index.get(result.index)
        if row is None:
            return
        self.setItem(row, 4, self._status_item(result.status))

    def clear_table(self) -> None:
        self.setRowCount(0)
        self._row_by_index.clear()

    @staticmethod
    def _item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    @staticmethod
    def _status_item(status: ClipStatus) -> QTableWidgetItem:
        item = QTableWidgetItem(status.value)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setForeground(Qt.GlobalColor.white)
        color = _STATUS_COLORS.get(status, theme.TEXT_PRIMARY)
        from PySide6.QtGui import QColor
        item.setForeground(QColor(color))
        return item
