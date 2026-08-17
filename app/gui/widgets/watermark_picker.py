"""
Watermark / logo-text picker — lets the user type a handle/logo text
(e.g. "@toniboiboi"), pick a corner, choose the text color and an
optional background box color (or turn the background off entirely),
and that gets burned into every cut clip. No need to open CapCut
afterwards just to add a watermark.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.gui.theme import ACCENT, BORDER, SURFACE, SURFACE_ALT, TEXT_SECONDARY
from app.gui.widgets.no_scroll_combo import NoScrollComboBox, NoScrollSpinBox

# Display key -> (button label, bundled icon filename under
# app/assets/icons/). "none" has no icon file — it's the "no platform
# logo, just the typed text" option and is drawn as a plain button.
# Keep the keys in sync with app.core.watermark_composer.PLATFORM_ICON_FILES.
PLATFORM_OPTIONS: list[tuple[str, str, str | None]] = [
    ("none", "Không", None),
    ("x", "X", "x.png"),
    ("facebook", "Facebook", "facebook.png"),
    ("tiktok", "TikTok", "tiktok.png"),
    ("youtube", "YouTube", "youtube.png"),
]

_ICONS_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "icons"
# Full-color logo artwork used for the platform picker buttons below —
# picking a platform is just a recognition task, so the real brand
# colors read far more clearly at a glance than the plain white
# silhouettes (those live in _ICONS_DIR and are only used, tinted, in
# the actual burned-in watermark — see app.core.watermark_composer).
_COLOR_ICONS_DIR = _ICONS_DIR / "color"

# Display label -> internal position key (matches
# app.core.ffmpeg_cutter._WATERMARK_POSITIONS).
POSITION_OPTIONS: list[tuple[str, str]] = [
    ("Dưới - Phải", "bottom-right"),
    ("Dưới - Trái", "bottom-left"),
    ("Trên - Phải", "top-right"),
    ("Trên - Trái", "top-left"),
]

_DEFAULT_TEXT_COLOR = "#FFFFFF"
_DEFAULT_BOX_COLOR = "#000000"

_MIN_FONT_SIZE = 12
_MAX_FONT_SIZE = 96

# Swatch chrome: a fixed, neutral frame drawn *around* the color chip so
# the control stays clearly visible and clickable no matter which color
# is picked — including white or near-black, which used to blend
# straight into the dark theme and effectively disappear.
_SWATCH_BORDER = "#6b6e7c"
_SWATCH_BORDER_HOVER = ACCENT


def _to_ffmpeg_color(hex_color: str) -> str:
    """'#RRGGBB' -> '0xRRGGBB', the color spec FFmpeg's drawtext expects."""
    return "0x" + hex_color.lstrip("#").upper()


class _ColorSwatchButton(QPushButton):
    """A small button that shows the currently-picked color as its
    background and opens a QColorDialog when clicked.

    Always keeps a solid, fixed-color frame (independent of the chosen
    fill) plus a hover highlight, so the swatch never blends into the
    surrounding dark UI and stays obviously clickable."""

    color_changed = Signal(str)  # emits '#RRGGBB'

    def __init__(self, initial_hex: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(38, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName(f"colorSwatch_{id(self)}")
        self._hex = initial_hex
        self._apply_swatch()
        self.clicked.connect(self._pick_color)

    def _apply_swatch(self) -> None:
        # Scoped to this exact widget instance via #objectName so it can
        # never be picked up by any other button's selector, no matter
        # what the app-wide theme stylesheet defines for QPushButton.
        self.setStyleSheet(
            f"QPushButton#{self.objectName()} {{"
            f"background-color: {self._hex};"
            f"border: 2px solid {_SWATCH_BORDER};"
            "border-radius: 6px;"
            "}}"
            f"QPushButton#{self.objectName()}:hover {{"
            f"border: 2px solid {_SWATCH_BORDER_HOVER};"
            "}}"
        )
        self.setToolTip(f"Chọn màu ({self._hex})")

    def _pick_color(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._hex), self, "Chọn màu")
        if chosen.isValid():
            self._hex = chosen.name().upper()
            self._apply_swatch()
            self.color_changed.emit(self._hex)

    def hex_color(self) -> str:
        return self._hex

    def set_hex_color(self, hex_color: str) -> None:
        self._hex = hex_color
        self._apply_swatch()


def _step_button(text: str) -> QPushButton:
    """A small square +/- button for the font-size stepper."""
    btn = QPushButton(text)
    btn.setFixedSize(26, 26)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(
        f"QPushButton {{"
        f"background-color: {SURFACE_ALT};"
        f"border: 1px solid {BORDER};"
        "border-radius: 5px;"
        "font-weight: 700;"
        "padding: 0;"
        "}}"
        f"QPushButton:hover {{ border: 1px solid {ACCENT}; color: {ACCENT}; }}"
        f"QPushButton:disabled {{ color: {TEXT_SECONDARY}; }}"
    )
    return btn


class WatermarkPicker(QWidget):
    """Label + text field + position/color controls for the optional
    burned-in watermark/logo text, laid out as a single compact card."""

    settings_changed = Signal()

    def __init__(
        self,
        initial_text: str = "",
        initial_position: str = "bottom-right",
        initial_text_color: str = _DEFAULT_TEXT_COLOR,
        initial_box_enabled: bool = True,
        initial_box_color: str = _DEFAULT_BOX_COLOR,
        initial_font_size: int = 28,
        initial_platform: str | None = None,
        initial_use_brand_color: bool = False,
        initial_fixed_color_logo: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        # --- Card shell -------------------------------------------------
        # Everything lives inside a bordered "card" so the section reads
        # as one compact, self-contained control instead of loose rows
        # floating in the page background.
        card = QFrame(self)
        card.setObjectName("watermarkCard")
        card.setStyleSheet(
            f"QFrame#watermarkCard {{"
            f"background-color: {SURFACE};"
            f"border: 1px solid {BORDER};"
            "border-radius: 8px;"
            "}}"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 10, 14, 12)
        card_layout.setSpacing(8)

        title = QLabel("LOGO / CHỮ CHÈN VIDEO")
        title.setProperty("role", "section-title")
        title.setStyleSheet("padding-top: 0px;")  # tighter than the default section title

        # --- Row 0: platform logo picker ---------------------------------
        # Lets the typed handle (e.g. "@toniboiboi") be prefixed with a
        # real X / Facebook / TikTok / YouTube glyph instead of the user
        # having to type a literal "X" that just renders as the letter X
        # in whatever font — so it's actually recognizable as the app's
        # logo at a glance.
        platform_label = QLabel("Biểu tượng app")
        platform_label.setProperty("role", "field-label")

        self._platform_group = QButtonGroup(self)
        self._platform_group.setExclusive(True)
        self._platform_buttons: dict[str, QToolButton] = {}

        platform_row = QHBoxLayout()
        platform_row.setContentsMargins(0, 0, 0, 0)
        platform_row.setSpacing(6)
        platform_row.addWidget(platform_label)
        for key, label, icon_filename in PLATFORM_OPTIONS:
            btn = QToolButton()
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(label)
            if icon_filename is not None:
                icon_path = _COLOR_ICONS_DIR / icon_filename
                if icon_path.is_file():
                    btn.setIcon(QIcon(QPixmap(str(icon_path))))
                    btn.setIconSize(QSize(18, 18))
                else:
                    btn.setText(label)
            else:
                btn.setText(label)
            btn.setFixedHeight(28)
            btn.setMinimumWidth(30 if icon_filename else 52)
            btn.setStyleSheet(
                f"QToolButton {{"
                f"background-color: {SURFACE_ALT};"
                f"border: 1px solid {BORDER};"
                "border-radius: 6px;"
                "padding: 2px 8px;"
                "}}"
                f"QToolButton:hover {{ border: 1px solid {ACCENT}; }}"
                f"QToolButton:checked {{"
                f"background-color: {ACCENT};"
                f"border: 1px solid {ACCENT};"
                "}}"
            )
            self._platform_group.addButton(btn)
            self._platform_buttons[key] = btn
            platform_row.addWidget(btn)
        # Icon color style — three mutually-exclusive modes for how the
        # platform logo glyph gets colored:
        #   - "Theo màu chữ" (auto/text-match): the plain white-silhouette
        #     icon is tinted to match the "Màu chữ" swatch below (default,
        #     original behaviour).
        #   - "Theo màu thương hiệu" (brand tint): the silhouette is
        #     instead tinted with that platform's own official brand
        #     color(s) — X's black, Facebook's blue, YouTube's red, and
        #     TikTok's real 3-layer cyan/magenta/black mark.
        #   - "Logo gốc nhiều màu" (fixed full-color logo): the real,
        #     fixed, multi-color brand artwork is burned in exactly as
        #     published (e.g. Facebook's actual blue circle + white "f"),
        #     completely ignoring both the text color and the brand-tint
        #     option above.
        # All three only mean something once an actual platform icon is
        # selected, so the whole group is disabled while "Không" is picked.
        icon_style_label = QLabel("Kiểu icon")
        icon_style_label.setProperty("role", "field-label")

        self._style_text_radio = QRadioButton("Theo màu chữ")
        self._style_text_radio.setToolTip(
            "Tô biểu tượng theo màu chữ (\"Màu chữ\" bên dưới)"
        )
        self._style_brand_radio = QRadioButton("Theo màu thương hiệu")
        self._style_brand_radio.setToolTip(
            "Tô biểu tượng theo màu gốc của từng nền tảng thay vì theo "
            "màu chữ bên dưới"
        )
        self._style_fixed_radio = QRadioButton("Logo gốc nhiều màu")
        self._style_fixed_radio.setToolTip(
            "Dùng đúng logo gốc nhiều màu của nền tảng (không đổi màu theo "
            "chữ hay theo tuỳ chọn màu thương hiệu)"
        )

        self._icon_style_group = QButtonGroup(self)
        self._icon_style_group.setExclusive(True)
        self._icon_style_group.addButton(self._style_text_radio)
        self._icon_style_group.addButton(self._style_brand_radio)
        self._icon_style_group.addButton(self._style_fixed_radio)

        if initial_fixed_color_logo:
            self._style_fixed_radio.setChecked(True)
        elif initial_use_brand_color:
            self._style_brand_radio.setChecked(True)
        else:
            self._style_text_radio.setChecked(True)

        self._icon_style_group.buttonToggled.connect(
            lambda _b, _c: self.settings_changed.emit()
        )

        icon_style_row = QHBoxLayout()
        icon_style_row.setContentsMargins(0, 0, 0, 0)
        icon_style_row.setSpacing(6)
        icon_style_row.addWidget(icon_style_label)
        icon_style_row.addWidget(self._style_text_radio)
        icon_style_row.addWidget(self._style_brand_radio)
        icon_style_row.addWidget(self._style_fixed_radio)
        icon_style_row.addStretch(1)

        platform_row.addStretch(1)
        self._select_platform(initial_platform)
        self._set_icon_style_enabled(self.get_platform() is not None)
        self._platform_group.buttonToggled.connect(self._on_platform_toggled)

        # --- Row 1: text + position --------------------------------------
        self._text_edit = QLineEdit(initial_text)
        self._text_edit.setPlaceholderText("Ví dụ: @toniboiboi")
        self._text_edit.setMinimumWidth(220)
        self._text_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._text_edit.textChanged.connect(lambda _t: self.settings_changed.emit())

        self._position_combo = NoScrollComboBox()
        self._position_combo.setMinimumWidth(120)
        for label, _value in POSITION_OPTIONS:
            self._position_combo.addItem(label)
        self._select_position(initial_position)
        self._position_combo.currentIndexChanged.connect(
            lambda _idx: self.settings_changed.emit()
        )

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(10)
        top_row.addWidget(self._text_edit, stretch=1)
        top_row.addWidget(self._position_combo)

        # --- Row 2: text color / background color / font size -----------
        text_color_label = QLabel("Màu chữ")
        text_color_label.setProperty("role", "field-label")
        self._text_color_btn = _ColorSwatchButton(initial_text_color)
        self._text_color_btn.color_changed.connect(lambda _c: self.settings_changed.emit())

        self._box_checkbox = QCheckBox("Có nền")
        self._box_checkbox.setChecked(initial_box_enabled)
        self._box_checkbox.toggled.connect(self._on_box_toggled)

        box_color_label = QLabel("Màu nền")
        box_color_label.setProperty("role", "field-label")
        self._box_color_btn = _ColorSwatchButton(initial_box_color)
        self._box_color_btn.color_changed.connect(lambda _c: self.settings_changed.emit())
        self._box_color_btn.setEnabled(initial_box_enabled)

        font_size_label = QLabel("Cỡ chữ")
        font_size_label.setProperty("role", "field-label")

        self._font_size_spin = NoScrollSpinBox()
        self._font_size_spin.setRange(_MIN_FONT_SIZE, _MAX_FONT_SIZE)
        self._font_size_spin.setValue(initial_font_size)
        self._font_size_spin.setFixedWidth(48)
        self._font_size_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Native spin arrows are tiny and easy to miss — replace them
        # with two full-size +/- buttons that are much easier to hit.
        self._font_size_spin.setButtonSymbols(NoScrollSpinBox.ButtonSymbols.NoButtons)
        self._font_size_spin.valueChanged.connect(lambda _v: self.settings_changed.emit())
        self._font_size_spin.valueChanged.connect(self._refresh_step_buttons)

        self._font_minus_btn = _step_button("\u2212")  # minus sign
        self._font_minus_btn.clicked.connect(lambda: self._step_font_size(-1))
        self._font_plus_btn = _step_button("+")
        self._font_plus_btn.clicked.connect(lambda: self._step_font_size(1))

        font_stepper = QHBoxLayout()
        font_stepper.setContentsMargins(0, 0, 0, 0)
        font_stepper.setSpacing(4)
        font_stepper.addWidget(self._font_minus_btn)
        font_stepper.addWidget(self._font_size_spin)
        font_stepper.addWidget(self._font_plus_btn)

        color_row = QHBoxLayout()
        color_row.setContentsMargins(0, 0, 0, 0)
        color_row.setSpacing(8)
        color_row.addWidget(text_color_label)
        color_row.addWidget(self._text_color_btn)
        color_row.addSpacing(16)
        color_row.addWidget(self._box_checkbox)
        color_row.addWidget(box_color_label)
        color_row.addWidget(self._box_color_btn)
        color_row.addSpacing(16)
        color_row.addWidget(font_size_label)
        color_row.addLayout(font_stepper)
        color_row.addStretch(1)

        card_layout.addWidget(title)
        card_layout.addLayout(platform_row)
        card_layout.addLayout(icon_style_row)
        card_layout.addLayout(top_row)
        card_layout.addLayout(color_row)

        # A generous minimum width keeps row 2's controls from being
        # squeezed on top of each other (the "che khuất" overlap) when
        # the window gets narrow — the scroll area will grow horizontally
        # or the label just wraps to a new line before that happens.
        card.setMinimumWidth(460)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self._refresh_step_buttons(self._font_size_spin.value())

    def _on_box_toggled(self, checked: bool) -> None:
        self._box_color_btn.setEnabled(checked)
        self.settings_changed.emit()

    def _on_platform_toggled(self, _btn: QToolButton, checked: bool) -> None:
        if not checked:
            return
        # The icon-style radios only mean something once a real platform
        # icon is selected — grey them out (without losing whatever
        # value the user had picked) whenever "Không" is selected instead.
        self._set_icon_style_enabled(self.get_platform() is not None)
        self.settings_changed.emit()

    def _set_icon_style_enabled(self, enabled: bool) -> None:
        self._style_text_radio.setEnabled(enabled)
        self._style_brand_radio.setEnabled(enabled)
        self._style_fixed_radio.setEnabled(enabled)

    def _step_font_size(self, delta: int) -> None:
        self._font_size_spin.setValue(self._font_size_spin.value() + delta)

    def _refresh_step_buttons(self, value: int) -> None:
        self._font_minus_btn.setEnabled(value > _MIN_FONT_SIZE)
        self._font_plus_btn.setEnabled(value < _MAX_FONT_SIZE)

    def _select_position(self, value: str) -> None:
        for i, (_label, v) in enumerate(POSITION_OPTIONS):
            if v == value:
                self._position_combo.setCurrentIndex(i)
                return
        self._position_combo.setCurrentIndex(0)

    def _select_platform(self, value: str | None) -> None:
        key = value if value in self._platform_buttons else "none"
        self._platform_buttons[key].setChecked(True)

    def get_text(self) -> str:
        return self._text_edit.text().strip()

    def get_platform(self) -> str | None:
        """Selected platform key ('x', 'facebook', 'tiktok', 'youtube'),
        or None when "Không" (no icon) is selected."""
        for key, btn in self._platform_buttons.items():
            if btn.isChecked() and key != "none":
                return key
        return None

    def get_use_brand_color(self) -> bool:
        """Whether the logo glyph should be tinted with that platform's
        own official brand color(s) instead of the text color. Only
        meaningful when get_platform() is not None. Mutually exclusive
        with get_fixed_color_logo()."""
        return self._style_brand_radio.isChecked()

    def get_fixed_color_logo(self) -> bool:
        """Whether the logo glyph should be the platform's real fixed
        full-color artwork, burned in as-is (no tinting at all). Only
        meaningful when get_platform() is not None. Takes priority over
        get_use_brand_color()."""
        return self._style_fixed_radio.isChecked()

    def get_position(self) -> str:
        return POSITION_OPTIONS[self._position_combo.currentIndex()][1]

    def get_text_color(self) -> str:
        return _to_ffmpeg_color(self._text_color_btn.hex_color())

    def get_box_enabled(self) -> bool:
        return self._box_checkbox.isChecked()

    def get_box_color(self) -> str:
        return _to_ffmpeg_color(self._box_color_btn.hex_color())

    def get_font_size(self) -> int:
        return self._font_size_spin.value()

    def set_enabled(self, enabled: bool) -> None:
        for btn in self._platform_buttons.values():
            btn.setEnabled(enabled)
        self._set_icon_style_enabled(enabled and self.get_platform() is not None)
        self._text_edit.setEnabled(enabled)
        self._position_combo.setEnabled(enabled)
        self._text_color_btn.setEnabled(enabled)
        self._box_checkbox.setEnabled(enabled)
        self._box_color_btn.setEnabled(enabled and self._box_checkbox.isChecked())
        self._font_size_spin.setEnabled(enabled)
        self._font_minus_btn.setEnabled(enabled and self._font_size_spin.value() > _MIN_FONT_SIZE)
        self._font_plus_btn.setEnabled(enabled and self._font_size_spin.value() < _MAX_FONT_SIZE)
