"""
Cookies selector for unlocking full-quality YouTube downloads.

YouTube frequently restricts anonymous requests to low-resolution
formats. Two ways to authenticate are offered:

  1. Browser cookies — read directly from a locally installed browser's
     cookie database. Simple, but fails whenever that browser is still
     running (Chrome/Edge/Brave lock their cookie database file while
     open — see the "still running in the background" Settings option),
     which forces this app to silently fall back to anonymous downloads
     and lose access to higher resolutions.

  2. cookies.txt file — a static, exported Netscape-format cookie file.
     Immune to the browser-lock problem entirely, since nothing here
     reads a live browser process; this is what most professional
     download tools recommend for reliability. Export one with a
     browser extension such as "Get cookies.txt LOCALLY" while logged
     into YouTube, then point this app at the exported file.
"""

from __future__ import annotations

from pathlib import Path

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

from app.gui import theme
from app.gui.widgets.no_scroll_combo import NoScrollComboBox

# Display label -> yt-dlp browser keyword. The last entry is handled
# specially (file mode) rather than passed to yt-dlp directly.
BROWSER_OPTIONS: list[tuple[str, str]] = [
    ("Không dùng (ẩn danh)", ""),
    ("Chrome", "chrome"),
    ("Firefox", "firefox"),
    ("Edge", "edge"),
    ("Brave", "brave"),
    ("File cookies.txt…", "__file__"),
]

_FILE_SENTINEL = "__file__"


class CookiesPicker(QWidget):
    """Label + mode selector (anonymous / browser cookies / cookies.txt
    file) for supplying YouTube login cookies, to unlock higher-
    resolution downloads."""

    settings_changed = Signal()

    def __init__(
        self,
        initial_browser: str = "",
        initial_cookies_file: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        title = QLabel("ĐĂNG NHẬP YOUTUBE (tùy chọn — giúp tải chất lượng cao hơn)")
        title.setProperty("role", "section-title")

        self._combo = NoScrollComboBox()
        for label, _value in BROWSER_OPTIONS:
            self._combo.addItem(label)
        self._combo.currentIndexChanged.connect(self._on_mode_changed)

        # --- File-mode row: path field + Browse button (hidden unless
        # "File cookies.txt…" is the selected mode) ---
        self._file_edit = QLineEdit(initial_cookies_file)
        self._file_edit.setPlaceholderText("Đường dẫn tới file cookies.txt…")
        self._file_edit.textChanged.connect(self._on_file_text_changed)

        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._on_browse)

        self._file_row = QWidget()
        file_row_layout = QHBoxLayout(self._file_row)
        file_row_layout.setContentsMargins(0, 0, 0, 0)
        file_row_layout.setSpacing(8)
        file_row_layout.addWidget(self._file_edit, stretch=1)
        file_row_layout.addWidget(browse_button)

        self._file_hint = QLabel(" ")
        self._file_hint.setWordWrap(True)
        self._file_hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(title)
        layout.addWidget(self._combo)
        layout.addWidget(self._file_row)
        layout.addWidget(self._file_hint)

        # Apply the saved settings now that every widget referenced by
        # _update_file_row_visibility()/_refresh_file_hint() exists.
        self._set_initial(initial_browser, initial_cookies_file)
        self._update_file_row_visibility()
        self._refresh_file_hint()

    # ------------------------------------------------------------------
    # Initial state
    # ------------------------------------------------------------------
    def _set_initial(self, browser: str, cookies_file: str) -> None:
        # A saved cookies-file path takes priority over any saved browser
        # keyword, since only one mode can be active at a time and having
        # a file path saved means the user last chose file mode.
        self._select_value(_FILE_SENTINEL if cookies_file else browser)

    def _select_value(self, value: str) -> None:
        for i, (_label, v) in enumerate(BROWSER_OPTIONS):
            if v == value:
                self._combo.setCurrentIndex(i)
                return
        self._combo.setCurrentIndex(0)

    def _current_value(self) -> str:
        return BROWSER_OPTIONS[self._combo.currentIndex()][1]

    def _is_file_mode(self) -> bool:
        return self._current_value() == _FILE_SENTINEL

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------
    def _on_mode_changed(self, _idx: int) -> None:
        self._update_file_row_visibility()
        self._refresh_file_hint()
        self.settings_changed.emit()

    def _on_file_text_changed(self, _text: str) -> None:
        self._refresh_file_hint()
        self.settings_changed.emit()

    def _update_file_row_visibility(self) -> None:
        is_file_mode = self._is_file_mode()
        self._file_row.setVisible(is_file_mode)
        self._file_hint.setVisible(is_file_mode)

    def _refresh_file_hint(self) -> None:
        if not self._is_file_mode():
            return
        path_text = self._file_edit.text().strip()
        if not path_text:
            self._file_hint.setText(
                "Xuất file cookies.txt bằng tiện ích mở rộng trình duyệt (ví dụ "
                "\"Get cookies.txt LOCALLY\") trong lúc đã đăng nhập YouTube, "
                "rồi chọn file đó ở đây."
            )
            self._file_hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
        elif Path(path_text).is_file():
            self._file_hint.setText(f"✓ Sẽ dùng cookie từ: {path_text}")
            self._file_hint.setStyleSheet(f"color: {theme.SUCCESS}; font-size: 11px;")
        else:
            self._file_hint.setText("⚠ Không tìm thấy file này trên máy.")
            self._file_hint.setStyleSheet(f"color: {theme.WARNING}; font-size: 11px;")

    def _on_browse(self) -> None:
        start_dir = self._file_edit.text().strip() or str(Path.home())
        path, _filter = QFileDialog.getOpenFileName(
            self, "Chọn file cookies.txt", start_dir, "Cookies (*.txt);;Tất cả file (*)"
        )
        if path:
            self._file_edit.setText(path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_browser(self) -> str:
        """Return the selected yt-dlp browser keyword, or '' when not in
        browser-cookie mode (anonymous mode or file mode)."""
        value = self._current_value()
        return value if value != _FILE_SENTINEL else ""

    def get_cookies_file(self) -> str:
        """Return the selected cookies.txt path, or '' when not in
        file-cookie mode."""
        return self._file_edit.text().strip() if self._is_file_mode() else ""

    def set_enabled(self, enabled: bool) -> None:
        self._combo.setEnabled(enabled)
        self._file_edit.setEnabled(enabled)
