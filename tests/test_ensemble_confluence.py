"""Tests for ensemble confluence gating (min_agreeing + threshold)."""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.strategies.base import MarketEvent, Position, Signal, Strategy
from src.strategies.ensemble import StrategyEnsemble, StrategyWeight


class _StubStrategy(Strategy):
    """Minimal strategy stub that returns a fixed signal."""

    def __init__(self, name: str, signal: Optional[Signal]) -> None:
        self._name = name
        self._signal = signal

    @property
    def name(self) -> str:
        return self._name

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        return self._signal

    def on_position(self, position: Position, event: MarketEvent) -> None:
        return None


def _event() -> MarketEvent:
    return MarketEvent(
        symbol="BTC",
        price=50_000.0,
        timestamp_ms=int(time.time() * 1000),
    )


def _sig(
    strategy: str,
    side: str = "long",
    confidence: float = 0.55,
    size_pct: float = 0.01,
) -> Signal:
    return Signal(
        strategy=strategy,
        symbol="BTC",
        side=side,
        confidence=confidence,
        size_pct=size_pct,
        entry_price=50_000.0,
        stop_loss_pct=0.01,
    )


def _ensemble(*strategies: Strategy) -> StrategyEnsemble:
    weights = [
        StrategyWeight(s.name, 0.15, min_confidence=0.0)
        for s in strategies
    ]
    return StrategyEnsemble(
        strategies=list(strategies),
        weights=weights,
        threshold=0.25,
        min_strategies_agreeing=2,
        high_conviction_threshold=0.80,
        high_conviction_enabled=True,
        high_conviction_exclude=["VolatilityBreakout", "VWAPDeviation", "FundingExtreme"],
    )


def test_single_low_conviction_signal_rejected() -> None:
    """One sub-strategy at conf=0.55 cannot enter without high-conviction bypass."""
    solo = _StubStrategy("CVDOrderFlow", _sig("CVDOrderFlow", confidence=0.55))
    result = _ensemble(solo).on_market_event(_event())
    assert result is None


def test_single_below_high_conviction_even_with_high_score_weight_rejected() -> None:
    """Excluded strategy never bypasses; sub-threshold confidence also blocks."""
    solo = _StubStrategy(
        "VolatilityBreakout",
        _sig("VolatilityBreakout", confidence=0.75),
    )
    result = _ensemble(solo).on_market_event(_event())
    assert result is None


def test_two_agreeing_signals_above_threshold_enter() -> None:
    """Two concordant strategies with combined score >= threshold produce entry."""
    a = _StubStrategy("CVDOrderFlow", _sig("CVDOrderFlow", confidence=0.85))
    b = _StubStrategy("DonchianBreakout", _sig("DonchianBreakout", confidence=0.85))
    result = _ensemble(a, b).on_market_event(_event())
    assert result is not None
    assert result.side == "long"
    assert result.metadata.get("high_conviction_bypass") is not True
    assert len(result.metadata.get("strategies_agreeing", [])) == 2
    assert result.metadata.get("ensemble_score", 0.0) >= 0.25


def test_single_high_conviction_non_excluded_enters() -> None:
    """Single strategy at conf >= 0.80 (non-excluded) uses high-conviction path."""
    solo = _StubStrategy("CVDOrderFlow", _sig("CVDOrderFlow", confidence=0.82))
    result = _ensemble(solo).on_market_event(_event())
    assert result is not None
    assert result.metadata.get("high_conviction_bypass") is True
    assert result.metadata.get("original_strategy") == "CVDOrderFlow"


def test_two_agreeing_but_below_threshold_rejected() -> None:
    """Agreement alone is insufficient when combined score < threshold."""
    a = _StubStrategy("CVDOrderFlow", _sig("CVDOrderFlow", confidence=0.60))
    b = _StubStrategy("DonchianBreakout", _sig("DonchianBreakout", confidence=0.60))
    # 0.60 * 0.15 * 2 = 0.18 < 0.25
    result = _ensemble(a, b).on_market_event(_event())
    assert result is None


def main() -> None:
    test_single_low_conviction_signal_rejected()
    test_single_below_high_conviction_even_with_high_score_weight_rejected()
    test_two_agreeing_signals_above_threshold_enter()
    test_single_high_conviction_non_excluded_enters()
    test_two_agreeing_but_below_threshold_rejected()
    print("test_ensemble_confluence: all passed")


if __name__ == "__main__":
    main()
