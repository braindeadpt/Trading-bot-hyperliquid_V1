"""Structured logging with optional JSON formatting, file rotation, and console output.

Usage:
    from src.utils.logger import setup_logger
    logger = setup_logger("bot", level="INFO", json=True)
    logger.info("Starting engine", extra={"capital": 100_000})

QW3: switched from size-based to time-based rotation (default: daily,
14 backups).  The legacy ``max_bytes``/``backup_count`` knobs are
preserved as a safety cap so a runaway log can never fill the disk.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload: Dict[str, Any] = {
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "line": record.lineno,
        }
        # Merge any extra fields injected via `extra={...}`
        for key, value in record.__dict__.items():
            if key not in payload and not key.startswith("_"):
                payload[key] = value
        # Include exception info if present
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plain formatter (human-readable)
# ---------------------------------------------------------------------------

PLAIN_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def setup_logger(
    name: str = "bot",
    level: str = "INFO",
    json_format: bool = False,
    log_file: Optional[str] = None,
    max_bytes: int = 10_485_760,   # 10 MiB safety cap
    backup_count: int = 14,        # 14 daily backups (QW3 default)
    console: bool = True,
    rotation_when: str = "midnight",
    rotation_interval: int = 1,
    utc: bool = False,
) -> logging.Logger:
    """Create and configure a logger with rotating file + optional console handlers.

    QW3: time-based rotation is the default (``rotation_when='midnight'``,
    one file per day, 14 backups retained).  ``max_bytes`` is preserved as
    a hard cap so a runaway log can never exceed ~10 MiB per file.

    Parameters
    ----------
    name :
        Logger name (used for retrieval via ``logging.getLogger(name)``).
    level :
        Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    json_format :
        If True, emit JSON lines; otherwise human-readable text.
    log_file :
        Path to the rotating log file.  None = file handler disabled.
    max_bytes :
        Per-file size safety cap (default 10 MiB).
    backup_count :
        Number of backup files / rotation intervals to keep.
    console :
        Whether to attach a StreamHandler for stdout.
    rotation_when :
        TimedRotatingFileHandler ``when`` argument (default 'midnight').
    rotation_interval :
        TimedRotatingFileHandler ``interval`` argument.
    utc :
        If True, use UTC for rotation timestamps (default: local time).

    Returns
    -------
    Configured ``logging.Logger`` instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Prevent duplicate handlers if called multiple times in the same process
    if logger.handlers:
        logger.handlers.clear()

    formatter: logging.Formatter
    if json_format:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(PLAIN_FMT, datefmt=DATE_FMT)

    # --- Console handler ---
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logger.level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    # --- Rotating file handler (time-based primary, size cap safety net) ---
    if log_file:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        # QW3: use TimedRotatingFileHandler as the primary mechanism.
        # We layer a size cap by checking file size in the same handler
        # via the ``maxBytes`` attribute (Python 3.13+ supports both
        # when='midnight' and maxBytes simultaneously).
        try:
            file_handler: logging.Handler
            file_handler = logging.handlers.TimedRotatingFileHandler(
                filename=str(p),
                when=rotation_when,
                interval=rotation_interval,
                backupCount=backup_count,
                encoding="utf-8",
                utc=utc,
            )
            # Python >=3.13: enforce size cap alongside time rotation.
            # Older Python: silently fall back to time-only.
            if hasattr(file_handler, "maxBytes"):
                file_handler.maxBytes = max_bytes  # type: ignore[attr-defined]
        except (TypeError, AttributeError):
            # Pre-3.13 fallback to size-based rotation.
            file_handler = logging.handlers.RotatingFileHandler(
                filename=str(p),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        file_handler.setLevel(logger.level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


# ---------------------------------------------------------------------------
# Convenience re-exports so callers can do:
#     from src.utils.logger import setup_logger, JsonFormatter
# ---------------------------------------------------------------------------

__all__ = ["setup_logger", "JsonFormatter"]
