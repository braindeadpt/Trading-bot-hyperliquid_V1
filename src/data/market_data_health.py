"""Market data feed health snapshots for monitoring and dashboard."""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class PollRecord:
    """Single health sample."""

    timestamp_ms: int
    symbol: str
    cex_ok: bool
    hl_ok: bool
    status: str  # green | yellow | red


class MarketDataHealthTracker:
    """Rolling window of poll outcomes for failure-rate metrics."""

    def __init__(self, window_sec: float = 3600.0) -> None:
        self._window_sec = float(window_sec)
        self._records: Deque[PollRecord] = collections.deque()

    def record(
        self,
        symbol: str,
        *,
        cex_ok: bool,
        hl_ok: bool,
        status: str,
        timestamp_ms: Optional[int] = None,
    ) -> None:
        ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        self._records.append(
            PollRecord(
                timestamp_ms=ts,
                symbol=symbol,
                cex_ok=cex_ok,
                hl_ok=hl_ok,
                status=status,
            )
        )
        self._prune(ts)

    def _prune(self, now_ms: int) -> None:
        cutoff = now_ms - int(self._window_sec * 1000)
        while self._records and self._records[0].timestamp_ms < cutoff:
            self._records.popleft()

    def symbol_stats(self, symbol: str) -> Tuple[int, int, float]:
        """Return (total_polls, failed_polls, failure_rate) in the window."""
        now_ms = int(time.time() * 1000)
        self._prune(now_ms)
        cutoff = now_ms - int(self._window_sec * 1000)
        total = 0
        failed = 0
        for rec in self._records:
            if rec.symbol != symbol or rec.timestamp_ms < cutoff:
                continue
            total += 1
            if rec.status == "red":
                failed += 1
        rate = (failed / total) if total > 0 else 0.0
        return total, failed, rate

    def overall_stats(self) -> Tuple[int, int, float, str]:
        """Aggregate stats across all symbols in window."""
        now_ms = int(time.time() * 1000)
        self._prune(now_ms)
        cutoff = now_ms - int(self._window_sec * 1000)
        total = 0
        failed = 0
        statuses: List[str] = []
        for rec in self._records:
            if rec.timestamp_ms < cutoff:
                continue
            total += 1
            statuses.append(rec.status)
            if rec.status == "red":
                failed += 1
        rate = (failed / total) if total > 0 else 0.0
        if not statuses:
            return 0, 0, 0.0, "red"
        if any(s == "red" for s in statuses):
            overall = "red"
        elif any(s == "yellow" for s in statuses):
            overall = "yellow"
        else:
            overall = "green"
        return total, failed, rate, overall


@dataclass
class SymbolFeedHealth:
    """Per-symbol market data quality."""

    symbol: str
    cex_ok: bool = False
    cex_stale: bool = False
    cex_age_sec: float = 0.0
    cex_exchanges: List[str] = field(default_factory=list)
    cex_exchange_count: int = 0
    hl_predicted_ok: bool = False
    hl_predicted_stale: bool = False
    hl_predicted_age_sec: float = 0.0
    hl_venues: List[str] = field(default_factory=list)
    status: str = "red"  # green | yellow | red
    polls_1h: int = 0
    failed_polls_1h: int = 0
    failure_rate_1h: float = 0.0
    funding_hl_ws: Optional[float] = None
    funding_hl_predicted_8h: Optional[float] = None
    funding_cex_avg_8h: Optional[float] = None
    oi_cex_usd: Optional[float] = None
    oi_hl: Optional[float] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "cex_ok": self.cex_ok,
            "cex_stale": self.cex_stale,
            "cex_age_sec": round(self.cex_age_sec, 1),
            "cex_exchanges": self.cex_exchanges,
            "cex_exchange_count": self.cex_exchange_count,
            "hl_predicted_ok": self.hl_predicted_ok,
            "hl_predicted_stale": self.hl_predicted_stale,
            "hl_predicted_age_sec": round(self.hl_predicted_age_sec, 1),
            "hl_venues": self.hl_venues,
            "status": self.status,
            "polls_1h": self.polls_1h,
            "failed_polls_1h": self.failed_polls_1h,
            "failure_rate_1h": round(self.failure_rate_1h * 100, 1),
            "funding_hl_ws": self.funding_hl_ws,
            "funding_hl_predicted_8h": self.funding_hl_predicted_8h,
            "funding_cex_avg_8h": self.funding_cex_avg_8h,
            "oi_cex_usd": self.oi_cex_usd,
            "oi_hl": self.oi_hl,
        }


@dataclass
class MarketDataHealthSummary:
    """Fleet-wide health rollup."""

    overall: str = "red"
    symbols: Dict[str, SymbolFeedHealth] = field(default_factory=dict)
    polls_1h: int = 0
    failed_polls_1h: int = 0
    failure_rate_1h: float = 0.0
    red_since_sec: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "overall": self.overall,
            "polls_1h": self.polls_1h,
            "failed_polls_1h": self.failed_polls_1h,
            "failure_rate_1h": round(self.failure_rate_1h * 100, 1),
            "red_since_sec": round(self.red_since_sec, 1),
            "symbols": {k: v.to_dict() for k, v in self.symbols.items()},
        }


def compute_feed_status(
    *,
    cex_ok: bool,
    cex_stale: bool,
    cex_exchange_count: int,
    min_exchanges: int,
    hl_ok: bool,
    hl_stale: bool,
) -> str:
    """green / yellow / red."""
    if not cex_ok and not hl_ok:
        return "red"
    if cex_stale or hl_stale:
        return "yellow"
    if cex_ok and cex_exchange_count < min_exchanges:
        return "yellow"
    if cex_ok or hl_ok:
        return "green"
    return "red"
