"""
QComboBox ignores focus by default when it comes to wheel events: Qt
lets a combo box change its selected value just from the mouse wheel
passing over it, even if the user never clicked it and is only trying
to scroll the page it sits in. On a form with several combo boxes (the
resolution picker, the cookies/login picker), this makes normal page
scrolling accidentally change unrelated settings — exactly the
"cuộn chuột thì các mục tự đổi" complaint this class exists to fix.

Fix: only accept wheel events while the combo box actually has focus
(i.e. the user just clicked into it on purpose). Otherwise, ignore the
event so it bubbles up to the parent QScrollArea and scrolls the page
like the user expects.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QComboBox, QSpinBox, QWidget


class NoScrollComboBox(QComboBox):
    """A QComboBox that only responds to the mouse wheel when focused."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Only gain focus via click or Tab — never just from the mouse
        # passing over it while scrolling — so the "has focus" check in
        # wheelEvent() below actually reflects deliberate interaction.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt override signature
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoScrollSpinBox(QSpinBox):
    """A QSpinBox with the same 'ignore wheel unless focused' fix as
    NoScrollComboBox above — spin boxes have the identical problem."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt override signature
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()
