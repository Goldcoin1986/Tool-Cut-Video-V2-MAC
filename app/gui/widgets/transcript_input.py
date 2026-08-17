"""
'Clip Data' input — separate, labeled boxes ("Clip 1", "Clip 2", ...)
for pasted AI output (ChatGPT, Claude, Gemini, DeepSeek, Perplexity, or
any other tool that produces Start Time / End Time style output).

Why separate boxes instead of one shared text area: with everything in
one box, it's easy to lose track of which Start Time belongs to which
End Time once more than one or two clips are pasted in, especially
when editing after the fact. Giving each clip its own box makes the
boundary between clips visually unambiguous — paste one clip's
"Start Time / End Time" block into its own box, and there's nothing to
mix up. Three boxes are shown by default (covers the common case); a
"+ Thêm Clip" button adds more for longer sessions.

Internally this still reuses the exact same parsing logic
(parse_clip_data) as before: get_text() joins every non-empty box's
text together in order, and MainWindow parses that combined text
exactly as it always did — so this is a pure UI change, nothing about
how timestamps are extracted or validated is different.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.gui import theme

_DEFAULT_BOX_COUNT = 3
_BOX_HEIGHT = 100

_PLACEHOLDER = "Start Time: 07:55\nEnd Time: 08:51"


class _ClipBox(QWidget):
    """One labeled 'Clip N' text box, with an optional remove button."""

    text_changed = Signal()
    remove_requested = Signal(object)  # emits this _ClipBox instance

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._label = QLabel()
        self._label.setProperty("role", "field-label")

        self._remove_button = QPushButton("Đóng")
        self._remove_button.setToolTip("Xoá khung clip này")
        self._remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        self._remove_button.setVisible(False)
        self._remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remove_button.setStyleSheet(
            f"QPushButton {{"
            f"color: {theme.TEXT_SECONDARY};"
            f"background: transparent;"
            f"border: 1px solid {theme.BORDER};"
            "border-radius: 5px;"
            "padding: 1px 10px;"
            "font-size: 11px;"
            "}}"
            f"QPushButton:hover {{"
            f"color: {theme.ERROR};"
            f"border: 1px solid {theme.ERROR};"
            "}}"
        )

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        header.addWidget(self._label, stretch=1)
        header.addWidget(self._remove_button)

        self._text_edit = QTextEdit()
        self._text_edit.setPlaceholderText(_PLACEHOLDER)
        self._text_edit.setFixedHeight(_BOX_HEIGHT)
        self._text_edit.textChanged.connect(self.text_changed.emit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(header)
        layout.addWidget(self._text_edit)

    def set_label(self, text: str) -> None:
        self._label.setText(text)

    def set_removable(self, removable: bool) -> None:
        self._remove_button.setVisible(removable)

    def get_text(self) -> str:
        return self._text_edit.toPlainText()

    def clear(self) -> None:
        self._text_edit.clear()

    def set_enabled(self, enabled: bool) -> None:
        self._text_edit.setEnabled(enabled)


class ClipDataInputWidget(QWidget):
    """'CLIP DATA' section: a stack of per-clip boxes + an 'add' button.

    Public API is unchanged from the old single-textarea version
    (get_text / clear / set_enabled / text_changed signal), so nothing
    outside this file needs to change.
    """

    text_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        title = QLabel("CLIP DATA")
        title.setProperty("role", "section-title")

        hint = QLabel("Dán mỗi clip (Start Time / End Time) vào một khung riêng bên dưới.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")

        self._boxes_layout = QHBoxLayout()
        self._boxes_layout.setContentsMargins(0, 0, 0, 0)
        self._boxes_layout.setSpacing(8)

        self._add_button = QPushButton("+ Thêm Clip")
        self._add_button.clicked.connect(lambda: self._add_box())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addLayout(self._boxes_layout)
        layout.addWidget(self._add_button)

        self._boxes: list[_ClipBox] = []
        for _ in range(_DEFAULT_BOX_COUNT):
            self._add_box(emit_signal=False)
        self._renumber()

    # ------------------------------------------------------------------
    # Box management
    # ------------------------------------------------------------------
    def _add_box(self, emit_signal: bool = True) -> None:
        box = _ClipBox()
        box.text_changed.connect(self._on_any_box_changed)
        box.remove_requested.connect(self._remove_box)
        self._boxes.append(box)
        self._boxes_layout.addWidget(box, stretch=1)
        self._renumber()
        if emit_signal:
            self._on_any_box_changed()

    def _remove_box(self, box: "_ClipBox") -> None:
        # Always keep at least one box on screen — removing the last
        # one would leave no way to enter a clip at all.
        if len(self._boxes) <= 1:
            return
        self._boxes.remove(box)
        self._boxes_layout.removeWidget(box)
        box.deleteLater()
        self._renumber()
        self._on_any_box_changed()

    def _renumber(self) -> None:
        for i, box in enumerate(self._boxes, start=1):
            box.set_label(f"Clip {i}")
            # Only offer removal for boxes beyond the default set, so
            # the first few always stay visible/stable.
            box.set_removable(len(self._boxes) > 1 and i > _DEFAULT_BOX_COUNT)

    def _on_any_box_changed(self) -> None:
        self.text_changed.emit(self.get_text())

    # ------------------------------------------------------------------
    # Public API (unchanged from the previous single-textarea version)
    # ------------------------------------------------------------------
    def get_text(self) -> str:
        """Combine every non-empty box's text, in order, into one block
        — parse_clip_data() then extracts Start/End pairs from it
        exactly as it always did with a single pasted blob."""
        parts = [box.get_text().strip() for box in self._boxes if box.get_text().strip()]
        return "\n\n".join(parts)

    def clear(self) -> None:
        """Clear all boxes' text and collapse back down to the default
        number of (empty) boxes."""
        for box in list(self._boxes):
            if box is not self._boxes[0]:
                self._boxes_layout.removeWidget(box)
                box.deleteLater()
        self._boxes = self._boxes[:1]
        while len(self._boxes) < _DEFAULT_BOX_COUNT:
            self._add_box(emit_signal=False)
        for box in self._boxes:
            box.clear()
        self._renumber()

    def set_enabled(self, enabled: bool) -> None:
        for box in self._boxes:
            box.set_enabled(enabled)
        self._add_button.setEnabled(enabled)
