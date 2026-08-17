"""Dark theme: color palette and QSS stylesheet for the whole application."""

from __future__ import annotations

BACKGROUND = "#1e1f26"
SURFACE = "#282a36"
SURFACE_ALT = "#31333f"
BORDER = "#3d3f4c"
TEXT_PRIMARY = "#e6e6e6"
TEXT_SECONDARY = "#9a9cae"
ACCENT = "#7c5cff"
ACCENT_HOVER = "#8f74ff"
SUCCESS = "#4caf7d"
WARNING = "#e0a63f"
ERROR = "#e5555f"
INFO_BLUE = "#3b82f6"
"""Kept as the "reference" blue for anything that only needs a border/
accent-sized touch of blue. UpdateStatusWidget's pill itself now uses
the darker INFO_BLUE_SOLID_BG below instead (see that constant) — a
solid fill needs to be dark enough for its own text to read on top of
it, which this lighter blue isn't."""

INFO_BLUE_SOLID_BG = "#60a5fa"
INFO_BLUE_SOLID_BG_HOVER = "#4f8ef7"
"""Solid (not tinted/translucent) blue fill for UpdateStatusWidget's
whole pill. Previously (#1d4ed8, a dark mid-blue) this was deliberately
darker than INFO_BLUE itself specifically so light text/icon colors on
top of it would stay readable — see the removed comment that used to
sit here. That pill still looked wrong in practice: every QLabel Qt
draws (self._dot/self._caption/self._text in update_status_widget.py)
only had its `color` set in its own stylesheet, never its
`background`, so the app-wide `QWidget {{ background-color: BACKGROUND
}}` rule (near-black, see below) painted through underneath each
label, showing up as a dark rectangle in the middle of the pill instead
of letting the pill's own blue show through — that's the actual "ô bị
đen ở giữa" bug, not a color choice at all (fixed for good now by
adding `background: transparent` to each of those labels' stylesheets
in update_status_widget.py; changing colors here alone would NOT have
fixed it). Since the underlying transparency bug is now fixed
separately, this pill's fill was also moved to a genuinely LIGHTER
blue per request — but that means every foreground color sitting on
it (dot colors in _DOT_COLORS, the status text) had to be re-checked
and mostly DARKENED for contrast, the opposite direction from before:
a light fill needs dark foreground colors, not light/bright ones. See
_DOT_COLORS and the status-text color in update_status_widget.py for
the results of that recheck. _HOVER here is one step DARKER than the
base (not lighter, unlike the rest of the theme's hover states) simply
because the base is already close to the lightest shade that still
reads clearly as "blue" rather than washing out to near-white — going
lighter on hover had nowhere good left to go."""

STATUS_YELLOW = "#fde047"
"""No longer used by UpdateStatusWidget (see
_STATUS_TEXT_ON_LIGHT_BLUE in update_status_widget.py) now that the
pill's fill moved to a lighter blue — this bright, cool yellow read
fine on the OLD dark-blue INFO_BLUE_SOLID_BG fill but nearly disappears
on the new light one (dark-on-light needs a dark color, not a bright
one). Left defined here only as an unused historical reference /
in case some future dark-fill pill wants exactly this yellow again;
nothing currently imports it."""

STYLESHEET = f"""
QWidget {{
    background-color: {BACKGROUND};
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}

/* QColorDialog (opened from the watermark text/background color
picker) keeps its own native, light background regardless of this
app's dark theme — but the blanket QWidget text-color rule above
still applied our light theme text color on top of it, making every
label/button inside the dialog nearly invisible (light text on a
light background). Resetting it to the OS's own default palette here
makes the dialog render normally, matching how it looks in any other
application. */
QColorDialog, QColorDialog * {{
    background-color: palette(window);
    color: palette(window-text);
}}

QMainWindow {{
    background-color: {BACKGROUND};
}}

QLabel[role="section-title"] {{
    font-size: 13px;
    font-weight: 600;
    color: {TEXT_SECONDARY};
    letter-spacing: 0.5px;
    text-transform: uppercase;
    padding-top: 6px;

}}

QLabel[role="field-label"] {{
    font-size: 12px;
    font-weight: 600;
    color: {TEXT_SECONDARY};
}}

QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px;
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT};
}}

QLineEdit:focus, QTextEdit:focus {{
    border: 1px solid {ACCENT};
}}

QPushButton {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px 16px;
    color: {TEXT_PRIMARY};
    font-weight: 500;
}}

QPushButton:hover {{
    background-color: {BORDER};
}}

QPushButton:disabled {{
    color: {TEXT_SECONDARY};
    background-color: {SURFACE};
}}

QPushButton[role="primary"] {{
    background-color: {ACCENT};
    border: none;
    color: white;
}}

QPushButton[role="primary"]:hover {{
    background-color: {ACCENT_HOVER};
}}

QPushButton[role="primary"]:disabled {{
    background-color: {SURFACE_ALT};
    color: {TEXT_SECONDARY};
}}

QTableWidget {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    gridline-color: {BORDER};
    selection-background-color: {ACCENT};
}}

QHeaderView::section {{
    background-color: {SURFACE_ALT};
    color: {TEXT_SECONDARY};
    padding: 6px;
    border: none;
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
}}

QProgressBar {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    text-align: center;
    color: {TEXT_PRIMARY};
    height: 20px;
}}

QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 5px;
}}

QScrollBar:vertical {{
    background: {SURFACE};
    width: 10px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 5px;
    min-height: 20px;
}}

/* Radio buttons (currently only the "Kiểu icon" mode picker in the
watermark card) — the default OS indicator is a tiny, low-contrast dot
that's easy to miss against the dark theme. Give it an explicit, high-
contrast style instead: a clearly visible hollow ring when unchecked,
and a bold solid green dot when checked (SUCCESS green, same one used
everywhere else in the app for "done state" so the selected option
reads as unmistakably "on"), so which mode is active is obvious at a
glance — including while the whole picker is disabled during a cut
(the ":checked:disabled" rule below), which previously lost its dot
entirely because the plain ":disabled" rule was declared after
":checked" and won the conflict for every disabled radio regardless
of its checked state. */
QRadioButton {{
    spacing: 6px;
}}

QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 9px;
    border: 2px solid {TEXT_SECONDARY};
    background-color: {SURFACE};
}}

QRadioButton::indicator:hover {{
    border: 2px solid {SUCCESS};
}}

QRadioButton::indicator:checked {{
    border: 2px solid {SUCCESS};
    background-color: {SUCCESS};
    image: none;
}}

QRadioButton::indicator:checked:hover {{
    border: 2px solid {SUCCESS};
}}

QRadioButton:disabled {{
    color: {TEXT_SECONDARY};
}}

QRadioButton::indicator:disabled {{
    border: 2px solid {BORDER};
    background-color: {SURFACE};
}}

QRadioButton::indicator:checked:disabled {{
    border: 2px solid {SUCCESS};
    background-color: {SUCCESS};
}}

/* Checkboxes (merge/subtitle toggles in the main window, "Thêm nhạc
của tôi" / "Lồng tiếng tự động (AI)" in the audio picker, etc.) had
the exact same low-contrast problem the radio buttons above already
got fixed for: the platform's default check indicator is a thin,
small tick that barely shows up against this dark theme, so it's easy
to glance at a row of checkboxes and not be able to tell which are
actually ticked. Same fix, square instead of round: a clearly visible
hollow box when unchecked, and a bold solid SUCCESS-green fill when
checked (no separate tick glyph drawn on top — the filled-vs-hollow
box itself is the "on" signal, exactly like the radio dot above), so
"is this ticked?" is answerable at a glance rather than a squint. The
":checked:disabled" rule is repeated after ":disabled" for the same
reason as the radio buttons' equivalent rule: QSS applies whichever
matching rule is declared LAST when a widget matches more than one
selector, so a disabled-but-checked box needs its own rule declared
after the plain ":disabled" one or it silently loses its filled state
while disabled (e.g. "Chỉ giữ video đã gộp" while a cut is running). */
QCheckBox {{
    spacing: 6px;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 2px solid {TEXT_SECONDARY};
    background-color: {SURFACE};
}}

QCheckBox::indicator:hover {{
    border: 2px solid {SUCCESS};
}}

QCheckBox::indicator:checked {{
    border: 2px solid {SUCCESS};
    background-color: {SUCCESS};
    image: none;
}}

QCheckBox::indicator:checked:hover {{
    border: 2px solid {SUCCESS};
}}

QCheckBox:disabled {{
    color: {TEXT_SECONDARY};
}}

QCheckBox::indicator:disabled {{
    border: 2px solid {BORDER};
    background-color: {SURFACE};
}}

QCheckBox::indicator:checked:disabled {{
    border: 2px solid {SUCCESS};
    background-color: {SUCCESS};
}}
"""
