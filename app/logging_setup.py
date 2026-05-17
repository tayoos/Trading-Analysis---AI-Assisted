"""
Central logging configuration for gunicorn workers and dev server.

Logs are written to /data/logs/ (mount this directory on Unraid for persistence).
Format is line-oriented for easy grep/tail:

  2026-05-17 19:53:53 | INFO     | app.prices | Live prices refreshed: 12 fetched
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

_CONFIGURED = False

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Noisy third-party loggers → only warnings and above in app.log
_QUIET_LOGGERS = (
    "urllib3",
    "urllib3.connectionpool",
    "requests",
    "werkzeug",
    "yfinance",
    "peewee",
    "apscheduler",
)


def log_directory() -> str:
    return os.getenv("LOG_DIR", "/data/logs")


def configure_logging(force: bool = False) -> str:
    """
    Attach structured handlers to the root logger. Safe to call multiple times.
    Returns the log directory path used.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return log_directory()

    log_dir = log_directory()
    os.makedirs(log_dir, exist_ok=True)

    tz_name = os.getenv("TZ", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    class LocalFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):  # noqa: N802
            from datetime import datetime
            dt = datetime.fromtimestamp(record.created, tz=tz)
            if datefmt:
                return dt.strftime(datefmt)
            return dt.strftime(DATE_FORMAT)

    formatter = LocalFormatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    try:
        app_file = RotatingFileHandler(
            os.path.join(log_dir, "app.log"),
            maxBytes=int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024))),
            backupCount=int(os.getenv("LOG_BACKUP_COUNT", "5")),
            encoding="utf-8",
        )
        app_file.setFormatter(formatter)
        root.addHandler(app_file)
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not open app.log: %s", exc)

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "Logging configured → %s (app.log, timezone %s)", log_dir, tz_name
    )
    return log_dir
