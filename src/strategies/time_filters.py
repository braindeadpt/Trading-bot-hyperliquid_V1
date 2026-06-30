"""Shared time-based filters for strategies (v3.1.36).

Weekday / hour block: prevents new entries during low-probability windows
(e.g. Friday afternoon — no time for move to develop before weekend).

Used by strategies via a single config block:
  use_weekday_filter: true
  weekday_blocked_days: [5]            # 0=Mon ... 6=Sun (UTC)
  weekday_blocked_start_h: 18          # UTC hour
  weekday_blocked_end_h: 24            # exclusive (24 = midnight rollover)

Multiple blocks can be configured via ``weekday_blocks``:
  weekday_blocks:
    - days: [5]
      start_h: 18
      end_h: 24
    - days: [6, 0]
      start_h: 0
      end_h: 6
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class WeekdayBlock:
    days: Tuple[int, ...]   # 0=Mon ... 6=Sun
    start_h: int            # 0..24
    end_h: int              # 1..24 (exclusive)

    def is_blocked(self, ts_ms: int) -> bool:
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        weekday = dt.weekday()  # Monday=0 ... Sunday=6
        if weekday not in self.days:
            return False
        h = dt.hour
        if self.start_h <= self.end_h:
            return self.start_h <= h < self.end_h
        # wrap-around (e.g. 22 -> 4)
        return h >= self.start_h or h < self.end_h


def parse_weekday_blocks(cfg: Dict[str, Any]) -> List[WeekdayBlock]:
    """Parse weekday_blocks from a strategy config dict.

    Supports either:
      - ``weekday_blocks: [{days: [5], start_h: 18, end_h: 24}, ...]`` (preferred)
      - or flat ``weekday_blocked_days: [5]``, ``weekday_blocked_start_h: 18``,
        ``weekday_blocked_end_h: 24`` (single block)
    """
    blocks: List[WeekdayBlock] = []
    raw_blocks = cfg.get("weekday_blocks")
    if raw_blocks and isinstance(raw_blocks, list):
        for b in raw_blocks:
            if not isinstance(b, dict):
                continue
            days = b.get("days") or []
            days_t = tuple(int(d) for d in days if 0 <= int(d) <= 6)
            if not days_t:
                continue
            sh = int(b.get("start_h", 0))
            eh = int(b.get("end_h", 24))
            if sh == eh:
                continue
            blocks.append(WeekdayBlock(days=days_t, start_h=sh, end_h=eh))
        return blocks

    # Flat single-block config
    days = cfg.get("weekday_blocked_days")
    if days is None:
        return []
    days_t = tuple(int(d) for d in days if 0 <= int(d) <= 6)
    if not days_t:
        return []
    sh = int(cfg.get("weekday_blocked_start_h", 0))
    eh = int(cfg.get("weekday_blocked_end_h", 24))
    if sh == eh:
        return []
    blocks.append(WeekdayBlock(days=days_t, start_h=sh, end_h=eh))
    return blocks


def is_weekday_blocked(ts_ms: int, blocks: List[WeekdayBlock]) -> bool:
    """Return True if ``ts_ms`` falls inside any weekday block."""
    return any(b.is_blocked(ts_ms) for b in blocks)
