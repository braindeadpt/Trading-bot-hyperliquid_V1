"""Collector pause flag — packaging-only kill switch for disk pressure.

`deploy/linux/disk_guard.sh` creates `PAUSE_COLLECTORS` inside the data root
when the filesystem crosses the stop threshold (default 85%). Research
collectors consult `collectors_paused()` before every write batch and skip
persistence while the flag exists; reads, shadow ledgers and feed-health
heartbeats are unaffected.

The flag directory resolves to, in order:
  1. $BOT_DATA_ROOT
  2. dirname($BOT_RESEARCH_DATABASE_PATH)
  3. ./data/research relative to the project root
"""

import os
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_FLAG_NAME = "PAUSE_COLLECTORS"
_last_log_mono = 0.0
_LOG_EVERY_SEC = 300.0


def flag_path() -> Path:
    root = (os.environ.get("BOT_DATA_ROOT") or "").strip()
    if not root:
        root = str(Path(os.environ.get("BOT_RESEARCH_DATABASE_PATH", "data/research/hyperliquid.db")).parent)
    return Path(root) / _FLAG_NAME


def collectors_paused() -> bool:
    global _last_log_mono
    if not flag_path().exists():
        return False
    now = time.monotonic()
    if now - _last_log_mono >= _LOG_EVERY_SEC:
        _last_log_mono = now
        logger.warning(
            "PAUSE_COLLECTORS present at %s — research writes suspended "
            "(reads, shadows and heartbeats unaffected)", flag_path(),
        )
    return True
