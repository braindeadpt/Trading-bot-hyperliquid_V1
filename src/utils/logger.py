"""Structured logging with optional JSON formatting, file rotation, and console output.

Usage:
    from src.utils.logger import setup_logger
    logger = setup_logger("bot", level="INFO", json=True)
    logger.info("Starting engine", extra={"capital": 100_000})
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
    max_bytes: int = 10_485_760,   # 10 MiB
    backup_count: int = 5,
    console: bool = True,
) -> logging.Logger:
    """Create and configure a logger with rotating file + optional console handlers.

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
        Rollover threshold for the rotating file.
    backup_count :
        Number of backup files to keep.
    console :
        Whether to attach a StreamHandler for stdout.

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

    # --- Rotating file handler ---
    if log_file:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
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
