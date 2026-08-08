"""Historical replay substitute for live WS / feed-health gates.

Checks continuity (1m gaps), coverage over the run window, and
freshness of funding/OI auxiliary series at each bar timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.strategies.base import MarketEvent
from src.utils.config import Config
from src.utils.helpers import safe_float


@dataclass(frozen=True)
class SymbolReplayAudit:
    """Pre-computed quality metrics for one symbol over the backtest window."""

    symbol: str
    coverage_pct: float
    max_gap_ms: int
    bar_count: int
    expected_bars: int
    funding_available: bool
    oi_available: bool


class ReplayDataQualityGate:
    """Replay-time gate equivalent to live feed-health blocking."""

    def __init__(
        self,
        *,
        min_coverage_pct: float = 0.95,
        max_bar_gap_ms: int = 120_000,
        max_funding_stale_ms: int = 300_000,
        max_oi_stale_ms: int = 300_000,
        require_funding: bool = True,
        require_oi: bool = False,
        parity_mode: bool = False,
    ) -> None:
        self._min_coverage = min_coverage_pct
        self._max_gap_ms = max_bar_gap_ms
        self._max_funding_stale = max_funding_stale_ms
        self._max_oi_stale = max_oi_stale_ms
        self._require_funding = require_funding
        self._require_oi = require_oi
        # Live has no window-coverage / multi-day-gap entry gate. In parity
        # mode those replay-only kills are disabled; missing bars are simply
        # absent from the timeline. Funding/OI freshness still apply.
        self._parity_mode = parity_mode

    @classmethod
    def from_config(cls, config: Config) -> "ReplayDataQualityGate":
        qc = config.get("backtest.replay_data_quality", {}) or {}
        return cls(
            min_coverage_pct=safe_float(qc.get("min_coverage_pct", 95.0)) / 100.0,
            max_bar_gap_ms=int(qc.get("max_bar_gap_ms", 120_000)),
            max_funding_stale_ms=int(qc.get("max_funding_stale_ms", 300_000)),
            max_oi_stale_ms=int(qc.get("max_oi_stale_ms", 300_000)),
            require_funding=bool(qc.get("require_funding", True)),
            require_oi=bool(qc.get("require_oi", False)),
            # Default True: live/replay parity is the primary backtest goal.
            parity_mode=bool(qc.get("parity_mode", True)),
        )

    @staticmethod
    def audit_symbol_window(
        symbol: str,
        candles_1m: List[Any],
        start_ms: Optional[int],
        end_ms: Optional[int],
        *,
        funding_ts: Optional[List[Tuple[int, Any, ...]]] = None,
        oi_ts: Optional[List[Tuple[int, Any, ...]]] = None,
    ) -> SymbolReplayAudit:
        """Compute coverage and worst gap for one symbol."""
        if not candles_1m:
            return SymbolReplayAudit(
                symbol=symbol,
                coverage_pct=0.0,
                max_gap_ms=0,
                bar_count=0,
                expected_bars=0,
                funding_available=False,
                oi_available=False,
            )

        ts_list = sorted(int(c.timestamp_ms) for c in candles_1m)
        lo = start_ms if start_ms is not None else ts_list[0]
        hi = end_ms if end_ms is not None else ts_list[-1]
        expected = max(1, int((hi - lo) / 60_000) + 1)
        in_window = [t for t in ts_list if lo <= t <= hi]
        max_gap = 0
        for i in range(1, len(in_window)):
            gap = in_window[i] - in_window[i - 1]
            if gap > max_gap:
                max_gap = gap
        coverage = len(in_window) / expected if expected > 0 else 0.0
        return SymbolReplayAudit(
            symbol=symbol,
            coverage_pct=coverage,
            max_gap_ms=max_gap,
            bar_count=len(in_window),
            expected_bars=expected,
            funding_available=bool(funding_ts),
            oi_available=bool(oi_ts),
        )

    def check_entry(
        self,
        symbol: str,
        event: MarketEvent,
        *,
        audit: Optional[SymbolReplayAudit],
        last_bar_ts: Optional[int],
        funding_ts_at: Optional[int],
        oi_ts_at: Optional[int],
    ) -> Optional[str]:
        """Return rejection reason or None if replay data is acceptable."""
        if audit is None:
            return "replay_quality_no_audit"

        if not self._parity_mode:
            if audit.coverage_pct < self._min_coverage:
                return (
                    f"replay_coverage_low:{audit.coverage_pct * 100:.1f}%"
                    f"<{self._min_coverage * 100:.1f}%"
                )

            if last_bar_ts is not None:
                gap = event.timestamp_ms - last_bar_ts
                if gap > self._max_gap_ms:
                    return f"replay_bar_gap:{gap}ms>{self._max_gap_ms}ms"

        if self._require_funding:
            if not audit.funding_available:
                return "replay_funding_missing"
            if funding_ts_at is None:
                return "replay_funding_stale:no_series"
            stale = event.timestamp_ms - funding_ts_at
            if stale > self._max_funding_stale:
                return f"replay_funding_stale:{stale}ms"

        if self._require_oi:
            if not audit.oi_available:
                return "replay_oi_missing"
            if oi_ts_at is None:
                return "replay_oi_stale:no_series"
            stale = event.timestamp_ms - oi_ts_at
            if stale > self._max_oi_stale:
                return f"replay_oi_stale:{stale}ms"

        return None
