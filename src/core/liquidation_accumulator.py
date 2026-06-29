"""Rolling 5-minute liquidation window — shared by live engine and backtest."""

from __future__ import annotations

import collections
from typing import Deque, Optional, Tuple


class LiquidationAccumulator:
    """Track liquidation notional in a rolling time window."""

    def __init__(self, window_ms: int = 300_000) -> None:
        self.window_ms = window_ms
        self.events: Deque[Tuple[int, float, str]] = collections.deque()
        self.source: Optional[str] = None

    def prune(self, now_ms: int) -> None:
        while self.events and self.events[0][0] < now_ms - self.window_ms:
            self.events.popleft()

    def record(
        self,
        timestamp_ms: int,
        notional: float,
        side: str,
        source: str,
    ) -> None:
        self.prune(timestamp_ms)
        self.events.append((timestamp_ms, notional, side))
        self.source = source

    def stats(self) -> Tuple[Optional[float], Optional[str], Optional[int]]:
        """Return (dominant_notional_5m, dominant_side, count_on_side)."""
        if not self.events:
            return None, None, None

        total_long = 0.0
        total_short = 0.0
        count_long = 0
        count_short = 0

        for _, notional, side in self.events:
            if side == "long":
                total_long += notional
                count_long += 1
            else:
                total_short += notional
                count_short += 1

        if total_long >= total_short and total_long > 0:
            return total_long, "long", count_long
        if total_short > 0:
            return total_short, "short", count_short
        return None, None, None
