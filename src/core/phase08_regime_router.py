"""Phase 08 hard regime router — VB vs VWAP mutual exclusion by ADX regime."""

from __future__ import annotations

import logging
from typing import Dict, List, Literal, Optional, Tuple

from src.core.regime import regime_strategy_name
from src.strategies.base import Signal

logger = logging.getLogger(__name__)

MarketRegime = Literal["unknown", "range", "low_vol", "expansion", "trend"]

VB_STRATEGY = "VolatilityBreakout"
VWAP_STRATEGY = "VWAPDeviation"

VB_REGIMES = frozenset({"trend", "expansion"})
VWAP_REGIMES = frozenset({"range", "low_vol"})


def classify_market_regime(
    adx: Optional[float],
    *,
    adx_range_threshold: float = 20.0,
    adx_trend_threshold: float = 25.0,
) -> MarketRegime:
    """Classify ADX into range/low_vol / expansion / trend."""
    if adx is None:
        return "unknown"
    if adx < adx_range_threshold:
        return "low_vol"
    if adx > adx_trend_threshold:
        return "trend"
    return "expansion"


def regime_allows_strategy(strategy_name: str, regime: MarketRegime) -> bool:
    """Hard gate: VB in trend/expansion; VWAP in range/low_vol."""
    if regime == "unknown":
        return False
    if strategy_name == VB_STRATEGY:
        return regime in VB_REGIMES
    if strategy_name == VWAP_STRATEGY:
        return regime in VWAP_REGIMES
    return True


class SequentialContradictionGuard:
    """Block opposite-side signals on the same symbol within a time window."""

    def __init__(self, block_ms: int = 3_600_000) -> None:
        self._block_ms = max(60_000, int(block_ms))
        self._last_side: Dict[str, str] = {}
        self._last_ts: Dict[str, int] = {}

    def check(
        self,
        symbol: str,
        side: str,
        timestamp_ms: int,
    ) -> Optional[str]:
        """Return rejection reason if sequential flip is blocked."""
        prev_side = self._last_side.get(symbol)
        prev_ts = self._last_ts.get(symbol, 0)
        if (
            prev_side is not None
            and prev_side != side
            and timestamp_ms - prev_ts < self._block_ms
        ):
            return "sequential_contradictory_signal"
        return None

    def record(self, symbol: str, side: str, timestamp_ms: int) -> None:
        """Record an accepted entry side for sequential tracking."""
        self._last_side[symbol] = side
        self._last_ts[symbol] = timestamp_ms


def route_phase08_signals(
    signals: List[Signal],
    adx: Optional[float],
    *,
    adx_range_threshold: float = 20.0,
    adx_trend_threshold: float = 25.0,
    symbol: str = "",
    seq_guard: Optional[SequentialContradictionGuard] = None,
    timestamp_ms: int = 0,
) -> Tuple[List[Signal], Optional[str]]:
    """Filter signals by regime and reject contradictory entries.

    Returns (allowed_signals, reject_reason).
    """
    if not signals:
        return [], None

    regime = classify_market_regime(
        adx,
        adx_range_threshold=adx_range_threshold,
        adx_trend_threshold=adx_trend_threshold,
    )
    allowed: List[Signal] = []
    for sig in signals:
        name = regime_strategy_name(sig)
        if regime_allows_strategy(name, regime):
            allowed.append(sig)
        else:
            logger.info(
                "Phase08 regime router BLOCK %s %s — %s not allowed in %s (ADX=%s)",
                symbol,
                sig.side,
                name,
                regime,
                f"{adx:.1f}" if adx is not None else "?",
            )

    if not allowed:
        return [], f"regime_{regime}_no_allowed_strategies"

    sides = {s.side for s in allowed}
    if len(sides) > 1:
        names = [regime_strategy_name(s) for s in allowed]
        logger.warning(
            "Phase08 regime router REJECT contradictory %s — %s",
            symbol,
            list(zip(names, [s.side for s in allowed])),
        )
        return [], "contradictory_simultaneous_signals"

    if seq_guard is not None and allowed:
        seq_reason = seq_guard.check(symbol, allowed[0].side, timestamp_ms)
        if seq_reason:
            logger.info(
                "Phase08 sequential block %s %s (prev=%s)",
                symbol,
                allowed[0].side,
                seq_guard._last_side.get(symbol),
            )
            return [], seq_reason

    return allowed, None
