"""
MainWindow: pure layout + wiring. No business logic lives here — it
creates widgets, connects their signals to worker start calls, and
connects worker signals back to widget updates.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import AppConfig
from app.core.models import ClipRequest, ClipResult, ClipStatus
from app.core.pot_server import get_shared_manager
from app.core.timestamp_parser import parse_clip_data
from app.core import update_checker
from app.gui.dialogs.error_dialog import show_error
from app.gui.widgets.activity_log import ActivityLogWidget
from app.gui.widgets.audio_picker import AudioPicker
from app.gui.widgets.clip_table import ClipTableWidget
from app.gui.widgets.cookies_picker import CookiesPicker
from app.gui.widgets.output_folder_picker import OutputFolderPicker
from app.gui.widgets.progress_status import ProgressStatusWidget
from app.gui.widgets.resolution_picker import ResolutionPicker
from app.gui.widgets.summary_table import SummaryTableWidget
from app.gui.widgets.transcript_input import ClipDataInputWidget
from app.gui.widgets.update_status_widget import UpdateStatusWidget
from app.gui.widgets.url_input import UrlInputWidget
from app.gui.widgets.watermark_picker import WatermarkPicker
from app.utils.exceptions import AppError, NoTimestampsFoundError
from app.utils.file_utils import clear_temp_dir, open_folder
from app.utils.logger import get_logger, log_bus
from app.workers.cut_worker import CutWorker

logger = logging.getLogger("clip_cutter")

_AUTO_ANALYZE_DEBOUNCE_MS = 400


class MainWindow(QMainWindow):
    """Top-level application window."""

    _update_check_finished = Signal(str, str)
    """Emitted from the background update-check thread with
    (kind, text) — see _check_ytdlp_update_now() and
    _run_startup_update_check(). A Qt Signal, NOT QTimer.singleShot():
    singleShot's timer belongs to whichever thread calls it, so a
    singleShot fired from a plain threading.Thread (no Qt event loop
    of its own) would just sit there and never fire, silently — which
    was exactly the "Đang kiểm tra…" pill stuck forever" bug (the
    Activity Log still updated fine because log_bus above is already a
    real Signal). A Signal, connected with a normal .connect() the way
    log_bus is, is automatically delivered via a queued connection
    whenever emit() happens on a different thread than the receiver's
    own — no manual thread-hopping needed, and it actually arrives."""

    def __init__(self) -> None:
        super().__init__()
        self._config = AppConfig.load()
        self._clip_requests: list[ClipRequest] = []
        self._worker: CutWorker | None = None

        self.setWindowTitle("Tool Cut Video V1")
        self.resize(self._config.window_width, self._config.window_height)

        self._build_ui()
        self._wire_signals()

        get_logger()
        log_bus.message_logged.connect(self._activity_log.append_log)
        self._update_check_finished.connect(self._on_update_check_finished)

        # Start the PO Token HTTP server (see app/core/pot_server.py) in
        # the background so downloads aren't capped at 360p. Runs off
        # the GUI thread since it involves launching a subprocess and
        # polling for it to become ready (up to ~10s) — logging from
        # this thread is safe, it goes through the same Qt signal bus
        # as everything else. Best-effort: if it fails (Node/plugin not
        # installed), download() in downloader.py still works, yt-dlp
        # just falls back to script mode / lower resolutions and logs
        # why, same as before this feature existed.
        self._pot_server = get_shared_manager()
        threading.Thread(
            target=self._pot_server.start, name="pot-server-startup", daemon=True
        ).start()

        # Check once (at most every 24h — see app/core/update_checker.py)
        # whether a newer yt-dlp is out. YouTube changes its extraction
        # scheme often enough that an otherwise-untouched install can
        # start failing on its own; this catches that early instead of
        # the user just seeing a download suddenly break one day with no
        # obvious cause. Same fire-and-forget background-thread pattern
        # as the PO Token server above — logs through the same Qt bus,
        # never blocks the UI, never raises into this thread.
        threading.Thread(
            target=self._run_startup_update_check,
            name="ytdlp-update-check",
            daemon=True,
        ).start()

    def _run_startup_update_check(self) -> None:
        result = update_checker.check_and_maybe_update(self._config)
        if result.kind != "idle":  # idle = throttled, nothing new to show
            self._update_check_finished.emit(result.kind, result.text)

    def _on_update_check_finished(self, kind: str, text: str) -> None:
        """Slot for _update_check_finished — always runs on the GUI
        thread (Qt queues the delivery automatically since the signal
        is emitted from a background thread), so it's safe to touch
        widgets here directly."""
        self._update_status.set_checking(False)
        self._update_status.set_status(kind, text)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        self._url_input = UrlInputWidget()
        self._clip_data_input = ClipDataInputWidget()
        self._output_folder = OutputFolderPicker(self._config.last_output_folder)
        self._resolution_picker = ResolutionPicker()
        self._cookies_picker = CookiesPicker(
            self._config.cookies_from_browser, self._config.cookies_file
        )
        self._watermark_picker = WatermarkPicker(
            self._config.watermark_text,
            self._config.watermark_position,
            self._hex_from_ffmpeg_color(self._config.watermark_text_color),
            self._config.watermark_box_enabled,
            self._hex_from_ffmpeg_color(self._config.watermark_box_color),
            self._config.watermark_font_size,
            self._config.watermark_platform or None,
            self._config.watermark_use_brand_color,
            self._config.watermark_fixed_color_logo,
        )
        self._audio_picker = AudioPicker(
            self._config.audio_remove_mode,
            self._config.audio_music_paths,
            self._config.audio_music_volume,
            self._config.dubbing_enabled,
        )
        self._merge_checkbox = QCheckBox("Gộp tất cả clip thành 1 video")
        self._merge_checkbox.setToolTip(
            "Sau khi cắt xong, nối tất cả clip lại thành 1 file video "
            "duy nhất (theo đúng thứ tự), lưu cùng thư mục output."
        )
        self._merge_checkbox.setChecked(self._config.merge_clips_enabled)
        self._merge_checkbox.toggled.connect(self._on_merge_toggled)

        self._merge_only_checkbox = QCheckBox("Chỉ giữ video đã gộp (không giữ từng clip)")
        self._merge_only_checkbox.setToolTip(
            "Sau khi gộp thành công, xoá các clip lẻ — thư mục output chỉ "
            "còn lại 1 video đã gộp, không cần tải/giữ từng clip."
        )
        self._merge_only_checkbox.setChecked(self._config.merge_only)
        self._merge_only_checkbox.setContentsMargins(20, 0, 0, 0)
        self._merge_only_checkbox.setVisible(self._merge_checkbox.isChecked())

        self._subtitles_checkbox = QCheckBox("Phụ đề tiếng Việt (dịch tự động từ phụ đề tiếng Anh)")
        self._subtitles_checkbox.setToolTip(
            "Dịch máy (miễn phí, không cần API key) từ phụ đề tiếng Anh có "
            "sẵn của video sang tiếng Việt, rồi in cứng vào clip. Cần video "
            "gốc có phụ đề tiếng Anh (thủ công hoặc tự động) — nếu không "
            "có, bước này sẽ tự bỏ qua. Đây là dịch máy nên có thể chưa tự "
            "nhiên 100% với câu đùa/thành ngữ, nhưng đủ để hiểu nội dung."
        )
        self._subtitles_checkbox.setChecked(self._config.subtitles_enabled)

        button_row = QHBoxLayout()
        self._analyze_btn = QPushButton("Analyze")
        self._cut_btn = QPushButton("Cut Clips")
        self._cut_btn.setProperty("role", "primary")
        self._cut_btn.setEnabled(False)
        self._clear_btn = QPushButton("Clear")
        self._update_status = UpdateStatusWidget()
        button_row.addWidget(self._analyze_btn)
        button_row.addWidget(self._cut_btn)
        button_row.addWidget(self._clear_btn)
        button_row.addStretch(1)
        button_row.addWidget(self._update_status)

        clips_label = QLabel("DETECTED CLIPS")
        clips_label.setProperty("role", "section-title")
        self._clip_table = ClipTableWidget()
        self._clip_table.setMinimumHeight(160)

        self._progress_status = ProgressStatusWidget()
        self._activity_log = ActivityLogWidget()

        summary_label = QLabel("SUMMARY")
        summary_label.setProperty("role", "section-title")
        self._summary_table = SummaryTableWidget()
        self._summary_table.setMinimumHeight(140)
        self._summary_table.setVisible(False)
        summary_label.setVisible(False)
        self._summary_label = summary_label

        self._open_folder_btn = QPushButton("Open Output Folder")
        self._open_folder_btn.setEnabled(False)

        root.addWidget(self._url_input)
        root.addWidget(self._clip_data_input)
        root.addWidget(self._output_folder)
        root.addWidget(self._resolution_picker)
        root.addWidget(self._cookies_picker)
        root.addWidget(self._watermark_picker)
        root.addWidget(self._audio_picker)
        root.addLayout(button_row)
        root.addWidget(self._merge_checkbox)
        root.addWidget(self._merge_only_checkbox)
        root.addWidget(self._subtitles_checkbox)
        root.addWidget(clips_label)
        root.addWidget(self._clip_table)
        root.addWidget(self._progress_status)
        root.addWidget(self._activity_log)
        root.addWidget(summary_label)
        root.addWidget(self._summary_table)
        root.addWidget(self._open_folder_btn)

        scroll.setWidget(content)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self._auto_analyze_timer = QTimer(self)
        self._auto_analyze_timer.setSingleShot(True)
        self._auto_analyze_timer.setInterval(_AUTO_ANALYZE_DEBOUNCE_MS)

    def _wire_signals(self) -> None:
        self._analyze_btn.clicked.connect(self._analyze)
        self._cut_btn.clicked.connect(self._start_cutting)
        self._clear_btn.clicked.connect(self._clear_all)
        self._open_folder_btn.clicked.connect(self._open_output_folder)
        self._update_status.check_now_requested.connect(self._check_ytdlp_update_now)

        self._clip_data_input.text_changed.connect(self._on_clip_data_changed)
        self._auto_analyze_timer.timeout.connect(lambda: self._analyze(silent=True))

    # ------------------------------------------------------------------
    # Analyze (Detected Clips / Smart Clip Preview)
    # ------------------------------------------------------------------
    def _on_clip_data_changed(self, _text: str) -> None:
        self._auto_analyze_timer.start()

    def _analyze(self, silent: bool = False) -> None:
        text = self._clip_data_input.get_text()
        try:
            self._clip_requests = parse_clip_data(text)
        except NoTimestampsFoundError as exc:
            self._clip_requests = []
            self._clip_table.clear_table()
            self._cut_btn.setEnabled(False)
            if not silent:
                show_error(self, exc)
            return

        self._clip_table.populate(self._clip_requests)
        self._cut_btn.setEnabled(True)
        self._progress_status.set_status(
            f"{len(self._clip_requests)} clip(s) detected. Ready to cut."
        )

    # ------------------------------------------------------------------
    # Cut Clips
    # ------------------------------------------------------------------
    def _start_cutting(self) -> None:
        if not self._clip_requests:
            self._analyze()
            if not self._clip_requests:
                return

        if not self._url_input.is_valid():
            show_error(
                self,
                AppError("Please enter a valid YouTube URL before cutting clips."),
            )
            return

        output_dir = Path(self._output_folder.get_path())
        self._config.last_output_folder = str(output_dir)
        self._config.cookies_from_browser = self._cookies_picker.get_browser()
        self._config.cookies_file = self._cookies_picker.get_cookies_file()
        self._config.watermark_text = self._watermark_picker.get_text()
        self._config.watermark_position = self._watermark_picker.get_position()
        self._config.watermark_text_color = self._watermark_picker.get_text_color()
        self._config.watermark_box_enabled = self._watermark_picker.get_box_enabled()
        self._config.watermark_box_color = self._watermark_picker.get_box_color()
        self._config.watermark_font_size = self._watermark_picker.get_font_size()
        self._config.watermark_platform = self._watermark_picker.get_platform() or ""
        self._config.watermark_use_brand_color = self._watermark_picker.get_use_brand_color()
        self._config.watermark_fixed_color_logo = self._watermark_picker.get_fixed_color_logo()
        self._config.audio_remove_mode = self._audio_picker.get_remove_mode()
        self._config.audio_music_paths = self._audio_picker.get_music_paths()
        self._config.audio_music_volume = self._audio_picker.get_music_volume()
        self._config.dubbing_enabled = self._audio_picker.get_dubbing_enabled()
        self._config.merge_clips_enabled = self._merge_checkbox.isChecked()
        self._config.merge_only = self._merge_only_checkbox.isChecked()
        self._config.subtitles_enabled = self._subtitles_checkbox.isChecked()
        self._config.save()

        self._set_busy(True)
        self._summary_table.setVisible(False)
        self._summary_label.setVisible(False)
        self._open_folder_btn.setEnabled(False)
        self._activity_log.clear()
        self._progress_status.set_progress(0, "Starting…")

        self._worker = CutWorker(
            url=self._url_input.get_url(),
            clip_requests=self._clip_requests,
            output_dir=output_dir,
            temp_dir=Path(self._config.temp_dir),
            ffmpeg_override=self._config.ffmpeg_path_override,
            ffprobe_override=self._config.ffprobe_path_override,
            preferred_height=self._resolution_picker.get_target_height(),
            cookies_from_browser=self._cookies_picker.get_browser(),
            cookies_file=self._cookies_picker.get_cookies_file(),
            watermark_text=self._watermark_picker.get_text(),
            watermark_position=self._watermark_picker.get_position(),
            watermark_text_color=self._watermark_picker.get_text_color(),
            watermark_box_enabled=self._watermark_picker.get_box_enabled(),
            watermark_box_color=self._watermark_picker.get_box_color(),
            watermark_font_size=self._watermark_picker.get_font_size(),
            watermark_platform=self._watermark_picker.get_platform() or "",
            watermark_use_brand_color=self._watermark_picker.get_use_brand_color(),
            watermark_fixed_color_logo=self._watermark_picker.get_fixed_color_logo(),
            audio_remove_mode=self._audio_picker.get_remove_mode(),
            audio_music_paths=self._audio_picker.get_music_paths(),
            audio_music_volume=self._audio_picker.get_music_volume(),
            merge_clips_enabled=self._merge_checkbox.isChecked(),
            merge_only=self._merge_only_checkbox.isChecked(),
            subtitles_enabled=self._subtitles_checkbox.isChecked(),
            dubbing_enabled=self._audio_picker.get_dubbing_enabled(),
        )
        self._worker.progress.connect(self._progress_status.set_progress)
        self._worker.clip_updated.connect(self._on_clip_updated)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_clip_updated(self, result: ClipResult) -> None:
        self._clip_table.update_status(result)

    def _on_finished(self, results: list[ClipResult]) -> None:
        self._set_busy(False)

        succeeded = [r for r in results if r.status == ClipStatus.DONE]
        failed = [r for r in results if r.status == ClipStatus.FAILED]

        self._summary_table.populate(results)
        self._summary_table.setVisible(True)
        self._summary_label.setVisible(True)

        if succeeded:
            self._open_folder_btn.setEnabled(True)

        self._progress_status.set_progress(
            100, f"Finished: {len(succeeded)} succeeded, {len(failed)} failed."
        )

    def _on_failed(self, error: AppError) -> None:
        self._set_busy(False)
        show_error(self, error)

    def _set_busy(self, busy: bool) -> None:
        self._cut_btn.setEnabled(not busy and bool(self._clip_requests))
        self._analyze_btn.setEnabled(not busy)
        self._clear_btn.setEnabled(not busy)
        self._url_input.set_enabled(not busy)
        self._clip_data_input.set_enabled(not busy)
        self._output_folder.set_enabled(not busy)
        self._resolution_picker.set_enabled(not busy)
        self._cookies_picker.set_enabled(not busy)
        self._watermark_picker.set_enabled(not busy)
        self._audio_picker.set_enabled(not busy)
        self._merge_checkbox.setEnabled(not busy)
        self._merge_only_checkbox.setEnabled(not busy and self._merge_checkbox.isChecked())
        self._subtitles_checkbox.setEnabled(not busy)

    # ------------------------------------------------------------------
    # Clear / Open Folder
    # ------------------------------------------------------------------
    def _clear_all(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            confirm = QMessageBox.question(
                self,
                "Cutting in Progress",
                "A cut is currently running. Clear anyway?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        self._clip_requests = []
        self._clip_data_input.clear()
        self._clip_table.clear_table()
        self._summary_table.clear_table()
        self._summary_table.setVisible(False)
        self._summary_label.setVisible(False)
        self._open_folder_btn.setEnabled(False)
        self._cut_btn.setEnabled(False)
        self._progress_status.reset()
        self._activity_log.clear()

    def _on_merge_toggled(self, checked: bool) -> None:
        """"Chỉ giữ video đã gộp" only makes sense once merging is
        actually on — hide it entirely rather than just greying it out,
        so it doesn't clutter the form when irrelevant."""
        self._merge_only_checkbox.setVisible(checked)

    def _open_output_folder(self) -> None:
        try:
            open_folder(self._output_folder.get_path())
        except AppError as exc:
            show_error(self, exc)

    def _check_ytdlp_update_now(self) -> None:
        """Manual "check now" — bypasses the 24h throttle (see
        app/core/update_checker.py) but still runs off the GUI thread
        and reports through both the Activity Log (via logger.* calls
        inside check_and_maybe_update itself) and the compact status
        pill, so a slow/offline PyPI request never freezes the window.
        """
        self._update_status.set_checking(True)

        def run_and_report() -> None:
            result = update_checker.check_and_maybe_update(self._config, force=True)
            # Emit a real Signal (see _update_check_finished's
            # docstring) rather than QTimer.singleShot from this
            # background thread — that used to silently never fire,
            # since a timer created on a thread with no Qt event loop
            # of its own never gets to run its callback, leaving the
            # pill stuck on "Đang kiểm tra…" forever even though the
            # Activity Log (a real Signal) already showed the result.
            self._update_check_finished.emit(result.kind, result.text)

        threading.Thread(
            target=run_and_report, name="ytdlp-update-check-manual", daemon=True
        ).start()

    @staticmethod
    def _hex_from_ffmpeg_color(ffmpeg_color: str) -> str:
        """'0xRRGGBB' -> '#RRGGBB' for QColorDialog/swatch buttons.
        Falls back to white on anything unrecognized rather than
        raising, since this only feeds a UI default color."""
        value = ffmpeg_color.strip()
        if value.lower().startswith("0x") and len(value) == 8:
            return "#" + value[2:].upper()
        if value.startswith("#") and len(value) == 7:
            return value.upper()
        return "#FFFFFF"

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        self._config.save()
        clear_temp_dir(self._config.temp_dir)
        self._pot_server.stop()
        super().closeEvent(event)
