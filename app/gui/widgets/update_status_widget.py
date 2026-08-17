"""
Compact "yt-dlp update status" pill — replaces what used to be a
full-width "Kiểm tra cập nhật yt-dlp" button with a small, glanceable
status indicator (colored dot + short text) plus a tiny refresh button,
sized to its content instead of stretching across the button row.

MainWindow drives this widget's state from app.core.update_checker's
result — this widget itself has zero network/update logic, it only
displays whatever state it's told and emits check_now_requested when
the refresh button is clicked.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QWidget

from app.gui.theme import (
    INFO_BLUE_SOLID_BG,
    INFO_BLUE_SOLID_BG_HOVER,
    TEXT_SECONDARY,
)

_STATUS_TEXT_ON_LIGHT_BLUE = "#5b3a00"
"""Replaces the old STATUS_YELLOW (#fde047) for self._text now that
the pill's fill (INFO_BLUE_SOLID_BG, see theme.py) moved from a dark
mid-blue to a lighter blue: a bright yellow that read clearly on the
old dark fill nearly disappears on a light one, since both are
light/high-luminance colors. This dark, warm amber/brown keeps enough
of "vàng" (yellow/gold) character to still read as a status highlight
rather than plain body text, while actually being legible against the
new lighter blue — dark-on-light needs a dark color, the same way
light-on-dark needed a light one before."""

# status kind -> dot color. These are deliberately NOT the theme's
# plain SUCCESS/ERROR/TEXT_SECONDARY constants — this pill's background
# is a solid blue (INFO_BLUE_SOLID_BG, see theme.py), and checking each
# color against that specific blue (not against BACKGROUND, which is
# what SUCCESS/ERROR/TEXT_SECONDARY were originally chosen for) is what
# decided these. The fill moved from a dark mid-blue to a lighter blue
# (see theme.py's INFO_BLUE_SOLID_BG comment) — light dot colors that
# used to stand out against a DARK fill would now nearly vanish against
# a LIGHT one, so every dot color below is deliberately a DARK, fairly
# saturated shade instead:
#   - idle/checking: dark slate instead of light gray.
#   - ok: dark green instead of a bright/pastel green.
#   - update: dark orange/rust instead of the theme's WARNING (#e0a63f,
#     itself a mid-toned orange that would also wash out on this fill)
#     — kept as its own color here rather than reusing WARNING, since
#     WARNING is shared app-wide and needs to keep meaning "warning" at
#     its own brightness everywhere else.
#   - error: dark red instead of a light red/pink tint.
_DOT_COLORS: dict[str, str] = {
    "idle": "#334155",
    "checking": "#334155",
    "ok": "#14532d",
    "update": "#7c2d12",
    "error": "#991b1b",
}

# status kind -> short, human-readable pill label. The raw `text`
# update_checker.py passes in (e.g. plain "yt-dlp" for idle, or
# "yt-dlp mới nhất (2026.07.04)" for ok) is written for the Activity
# Log, where there's a whole line of context around it — inside this
# small pill on its own, "yt-dlp" reads as a meaningless label to
# anyone who doesn't already know what yt-dlp is. These short labels
# are what's ALWAYS shown in the pill; the original detailed `text` is
# never lost, it just moves to the tooltip (hover to see exactly which
# version, why, etc.) — see set_status().
_DISPLAY_LABELS: dict[str, str] = {
    "idle": "Tự động cập nhật",
    "checking": "Đang kiểm tra…",
    "ok": "Đã cập nhật",
    "update": "Upgrade",
    "error": "Lỗi cập nhật",
}

# Pill fill/border is always solid blue (INFO_BLUE_SOLID_BG, not tied
# to status kind) — this pill is meant to always read as a clickable
# button first, with the dot color (above) carrying the actual status
# meaning.


class UpdateStatusWidget(QWidget):
    """Small rounded pill: [●][status text][↻] — the whole pill (not
    just the small ↻ button) is clickable and triggers an immediate
    check, since a blue-bordered box reads as "clickable" on its own;
    requiring the tiny icon specifically would be an easy miss."""

    check_now_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._pill = QFrame(self)
        self._pill.setObjectName("updateStatusPill")
        self._pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pill.setToolTip(
            "YouTube thay đổi liên tục khiến yt-dlp cũ có thể tự nhiên tải "
            "video/cắt clip bị lỗi. App tự kiểm tra 1 lần/24h ở nền — bấm "
            "vào đây để kiểm tra và tự cập nhật ngay."
        )
        self._pill.setStyleSheet(
            f"QFrame#updateStatusPill {{"
            f"background-color: {INFO_BLUE_SOLID_BG};"
            f"border: 2px solid {INFO_BLUE_SOLID_BG};"
            "border-radius: 13px;"
            "}}"
            f"QFrame#updateStatusPill:hover {{"
            f"background-color: {INFO_BLUE_SOLID_BG_HOVER};"
            f"border: 2px solid {INFO_BLUE_SOLID_BG_HOVER};"
            "}}"
        )
        self._pill.mousePressEvent = self._on_pill_clicked  # type: ignore[method-assign]

        # Static label with no click behavior of its own — its only job
        # is to tell someone unfamiliar with "yt-dlp" what this pill
        # even is at a glance, without needing to hover for the
        # tooltip.
        # Every QLabel below explicitly sets `background: transparent`
        # in its own stylesheet — without it, the app-wide `QWidget {
        # background-color: BACKGROUND }` rule (see theme.py) paints
        # each label's own near-black background rectangle on top of
        # this pill's blue fill, which is exactly the "ở giữa ô vẫn
        # màu đen" bug: a QLabel only setting `color` never overrides
        # the inherited `background-color`, it just adds to it.
        self._caption = QLabel("Cập nhật:")
        self._caption.setStyleSheet(
            f"color: {TEXT_SECONDARY}; background: transparent; font-size: 11px;"
        )

        self._dot = QLabel("●")
        self._dot.setFixedWidth(12)
        self._dot.setStyleSheet(
            f"color: {_DOT_COLORS['idle']}; background: transparent; font-size: 10px;"
        )

        self._text = QLabel(_DISPLAY_LABELS["idle"])
        self._text.setStyleSheet(
            f"color: {_STATUS_TEXT_ON_LIGHT_BLUE}; background: transparent; "
            "font-size: 11px; font-weight: 600;"
        )

        self._refresh_btn = QToolButton()
        self._refresh_btn.setText("↻")
        self._refresh_btn.setToolTip("Kiểm tra và cập nhật ngay")
        self._refresh_btn.setFixedSize(20, 20)
        self._refresh_btn.setStyleSheet(
            # Explicit base color (not just inherited from the app-wide
            # QWidget rule) because that inherited color was picked for
            # legibility against BACKGROUND, not against this pill's
            # new solid blue fill — white still reads fine on this
            # blue, so it's kept, just made explicit here.
            "QToolButton { border: none; background: transparent; font-size: 13px; color: #ffffff; }"
            "QToolButton:hover { color: #4ade80; }"
            f"QToolButton:disabled {{ color: {TEXT_SECONDARY}; }}"
        )
        self._refresh_btn.clicked.connect(self.check_now_requested.emit)

        pill_layout = QHBoxLayout(self._pill)
        pill_layout.setContentsMargins(12, 4, 8, 4)
        pill_layout.setSpacing(7)
        pill_layout.addWidget(self._dot)
        pill_layout.addWidget(self._text)
        pill_layout.addWidget(self._refresh_btn)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.addWidget(self._caption)
        outer.addWidget(self._pill)

    def _on_pill_clicked(self, _event) -> None:
        if self._refresh_btn.isEnabled():
            self.check_now_requested.emit()

    def set_status(self, kind: str, text: str) -> None:
        """kind: one of 'idle', 'checking', 'ok', 'update', 'error'.

        The pill's VISIBLE label is always one of the short, human-
        readable _DISPLAY_LABELS above — never the raw `text` this
        method receives (that raw text — e.g. bare "yt-dlp" for idle,
        or "yt-dlp mới nhất (2026.07.04)" for ok — is written for the
        Activity Log, where a full line of context makes it clear;
        alone in a small pill it just reads as a random label to
        anyone who doesn't already recognize "yt-dlp"). The full
        original `text` is never discarded — it's always set as the
        tooltip, one hover away, complete with whichever version/
        reason update_checker.py composed it with.
        """
        color = _DOT_COLORS.get(kind, TEXT_SECONDARY)
        self._dot.setStyleSheet(f"color: {color}; background: transparent; font-size: 10px;")
        self._text.setText(_DISPLAY_LABELS.get(kind, text))
        self._text.setToolTip(text)

    def set_checking(self, checking: bool) -> None:
        self._refresh_btn.setEnabled(not checking)
        if checking:
            self.set_status("checking", "Đang kiểm tra…")
