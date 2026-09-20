"""Deluxe Sync logging shared by the Calibre plugin."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from calibre.constants import config_dir


LOG_PREFIX = "[Deluxe-Sync]"
REDACTED = "[REDACTED]"
LOG_PATH = Path(config_dir) / "deluxe-sync.log"
_BASE_LOGGER_NAME = "deluxe_sync"
_MAX_LOG_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3


def _configure_base_logger() -> logging.Logger:
    logger = logging.getLogger(_BASE_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if getattr(logger, "_deluxe_sync_configured", False):
        return logger

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG)
    console.setFormatter(
        logging.Formatter(f"{LOG_PREFIX} %(levelname)s %(message)s")
    )
    logger.addHandler(console)

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                f"%(asctime)s {LOG_PREFIX} %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(file_handler)
    except OSError as error:
        logger.warning(
            "Persistent log file could not be opened (%s: %s)",
            type(error).__name__,
            error,
        )

    logger._deluxe_sync_configured = True
    return logger


def get_logger(component: str | None = None) -> logging.Logger:
    """Return a DEBUG logger writing to Calibre's console and deluxe-sync.log."""

    _configure_base_logger()
    if not component:
        return logging.getLogger(_BASE_LOGGER_NAME)
    return logging.getLogger(f"{_BASE_LOGGER_NAME}.{component}")


def log_path() -> str:
    """Return the persistent log path for diagnostics/UI."""

    return str(LOG_PATH)
