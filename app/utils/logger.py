"""
Centralized logging.

Writes to a rotating log file on disk AND emits every message through a
Qt signal bus, so the GUI's Activity Log widget can mirror log output
live without the logging system needing to import Qt widgets directly
(avoids circular imports and keeps core/ Qt-free).
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from app.utils.file_utils import get_app_data_dir

_LOGGER_NAME = "clip_cutter"


class LogBus(QObject):
    """Qt signal bus that broadcasts log records to any connected widget.

    Signal payload: (level_name: str, message: str)
    """

    message_logged = Signal(str, str)


log_bus = LogBus()


class _QtBusHandler(logging.Handler):
    """A logging.Handler that forwards records onto the Qt signal bus."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            log_bus.message_logged.emit(record.levelname, msg)
        except Exception:  # pragma: no cover - logging must never crash the app
            self.handleError(record)


def configure_logging(log_dir: Path | None = None) -> logging.Logger:
    """Configure and return the application's shared logger.

    Idempotent: calling this more than once will not duplicate handlers.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    log_dir = log_dir or get_app_data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    file_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.DEBUG)

    bus_formatter = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    bus_handler = _QtBusHandler()
    bus_handler.setFormatter(bus_formatter)
    bus_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.addHandler(bus_handler)
    return logger


def get_logger() -> logging.Logger:
    """Return the shared application logger, configuring it on first use."""
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        return configure_logging()
    return logger
