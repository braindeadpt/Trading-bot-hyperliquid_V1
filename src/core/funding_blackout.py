"""Time-of-day entry filter (funding-reset blackout).

Blocks new entries in a configurable window around scheduled CEX
funding resets. Liquidity is thinnest and order-book imbalances largest
in the minutes around 8h-funding ticks, which makes entries more
likely to face slippage / adverse selection.

Default: block 5 minutes before and after each of 00:00, 08:00, 16:00 UTC.
Hyperliquid itself uses 1h funding, but the legacy CEX 8h cadence is
where most of the cross-venue arb action and cascade risk concentrates.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FundingBlackoutConfig:
    enabled: bool = True
    minutes_before: int = 5
    minutes_after: int = 5
    resets_utc: List[str] = field(default_factory=lambda: ["00:00", "08:00", "16:00"])


class FundingBlackoutFilter:
    """Return True if entries are currently blacked out."""

    def __init__(self, config: Optional[FundingBlackoutConfig] = None) -> None:
        self._cfg = config or FundingBlackoutConfig()
        self._reset_minutes: List[int] = []
        self._parse_resets()

    @classmethod
    def from_config_dict(cls, cfg: dict) -> "FundingBlackoutFilter":
        return cls(FundingBlackoutConfig(
            enabled=bool(cfg.get("enabled", True)),
            minutes_before=int(cfg.get("minutes_before", 5)),
            minutes_after=int(cfg.get("minutes_after", 5)),
            resets_utc=list(cfg.get("resets_utc", ["00:00", "08:00", "16:00"])),
        ))

    def _parse_resets(self) -> None:
        parsed: List[int] = []
        for spec in self._cfg.resets_utc:
            try:
                hh, mm = spec.split(":")
                parsed.append(int(hh) * 60 + int(mm))
            except (ValueError, AttributeError) as exc:
                logger.warning("Invalid funding reset spec %r — skipping: %s", spec, exc)
        self._reset_minutes = sorted(parsed)

    def is_blocked(self, ts_ms: Optional[int] = None) -> bool:
        """Return True if *ts_ms* (default: now) falls inside a blackout window."""
        if not self._cfg.enabled or not self._reset_minutes:
            return False
        now = datetime.fromtimestamp(
            (ts_ms / 1000.0) if ts_ms else time.time(), tz=timezone.utc
        )
        minutes_into_day = now.hour * 60 + now.minute
        before = self._cfg.minutes_before
        after = self._cfg.minutes_after
        for reset in self._reset_minutes:
            window_start = (reset - before) % (24 * 60)
            window_end = (reset + after) % (24 * 60)
            if window_start <= window_end:
                if window_start <= minutes_into_day < window_end:
                    return True
            else:
                # Window wraps midnight (e.g. 23:55 → 00:05)
                if minutes_into_day >= window_start or minutes_into_day < window_end:
                    return True
        return False

    def next_reset_remaining_sec(self, ts_ms: Optional[int] = None) -> int:
        """Return seconds until the *next* blackout window opens (0 if inside)."""
        if not self._cfg.enabled or not self._reset_minutes:
            return 0
        now = datetime.fromtimestamp(
            (ts_ms / 1000.0) if ts_ms else time.time(), tz=timezone.utc
        )
        minutes_into_day = now.hour * 60 + now.minute
        seconds_into_day = minutes_into_day * 60 + now.second

        for reset in self._reset_minutes:
            blackout_start_sec = ((reset - self._cfg.minutes_before) % (24 * 60)) * 60
            diff = (blackout_start_sec - seconds_into_day) % 86_400
            return int(diff)
        return 0
