"""Output folder selector: text field showing the path + a Browse button."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class OutputFolderPicker(QWidget):
    """Label + path field + Browse button for choosing the clips output folder."""

    folder_changed = Signal(str)

    def __init__(self, default_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        title = QLabel("OUTPUT FOLDER")
        title.setProperty("role", "section-title")

        self._line_edit = QLineEdit(default_path)
        self._line_edit.textChanged.connect(self.folder_changed.emit)

        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._on_browse)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._line_edit, stretch=1)
        row.addWidget(browse_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(title)
        layout.addLayout(row)

    def _on_browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", self._line_edit.text()
        )
        if folder:
            self._line_edit.setText(folder)

    def get_path(self) -> str:
        return self._line_edit.text().strip()

    def set_enabled(self, enabled: bool) -> None:
        self._line_edit.setEnabled(enabled)
