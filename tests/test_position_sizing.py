"""Tests for conviction + ATR risk ceiling position sizing."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.risk_manager import RiskManager
from src.strategies.base import Signal


def _make_rm() -> RiskManager:
    return RiskManager({"risk.per_trade_risk_pct": 1.0}, None)


def _signal(size_pct: float, entry_price: float = 50_000.0) -> Signal:
    return Signal(
        strategy="TestStrategy",
        symbol="BTC",
        side="long",
        confidence=0.85,
        size_pct=size_pct,
        entry_price=entry_price,
    )


def test_small_size_pct_reduces_notional() -> None:
    """Conviction sizing below the risk ceiling yields a smaller position."""
    rm = _make_rm()
    capital = 10_000.0
    atr_pct = 0.01

    size_low = rm.calculate_position_size(_signal(0.005), capital, atr_pct)
    size_high = rm.calculate_position_size(_signal(0.03), capital, atr_pct)

    assert size_low > 0.0
    assert size_high > size_low
    assert size_high / size_low == 6.0  # 3% / 0.5%


def test_identical_signals_different_size_pct_produce_different_sizes() -> None:
    """Acceptance: 0.5% vs 3% size_pct on otherwise identical signals."""
    rm = _make_rm()
    capital = 10_000.0
    atr_pct = 0.01

    s_small = rm.calculate_position_size(_signal(0.005), capital, atr_pct)
    s_large = rm.calculate_position_size(_signal(0.03), capital, atr_pct)

    assert s_small != s_large


def test_atr_risk_ceiling_respected() -> None:
    """High conviction cannot exceed the ATR-derived risk notional."""
    rm = _make_rm()
    capital = 10_000.0
    atr_pct = 0.05  # wide stop → lower risk ceiling

    risk_amount = capital * rm._per_trade_risk_pct
    stop_distance = max(2.0 * atr_pct, rm.MIN_STOP_DISTANCE_PCT)
    notional_risk = risk_amount / stop_distance
    max_notional = capital * rm.MAX_POSITION_SIZE_PCT
    expected_cap = min(notional_risk, max_notional)

    size = rm.calculate_position_size(_signal(0.50), capital, atr_pct)
    actual_notional = size * 50_000.0

    assert actual_notional == expected_cap
    assert actual_notional < capital * 0.50


def test_max_position_size_pct_never_exceeded() -> None:
    """Hard cap at max_position_size_pct of capital."""
    rm = _make_rm()
    capital = 10_000.0
    atr_pct = 0.001  # tight stop → risk ceiling above max cap

    size = rm.calculate_position_size(_signal(0.50), capital, atr_pct)
    notional = size * 50_000.0
    max_allowed = capital * rm.MAX_POSITION_SIZE_PCT

    assert notional <= max_allowed + 1e-9


def test_kelly_multiplier_reduces_size() -> None:
    """Kelly < 1 applied via size_pct (as engine does) halves conviction notional."""
    rm = _make_rm()
    capital = 10_000.0
    atr_pct = 0.01
    base_pct = 0.02
    kelly = 0.5

    size_full = rm.calculate_position_size(_signal(base_pct), capital, atr_pct)
    size_kelly = rm.calculate_position_size(_signal(base_pct * kelly), capital, atr_pct)

    assert size_kelly == size_full * kelly


def main() -> None:
    test_small_size_pct_reduces_notional()
    test_identical_signals_different_size_pct_produce_different_sizes()
    test_atr_risk_ceiling_respected()
    test_max_position_size_pct_never_exceeded()
    test_kelly_multiplier_reduces_size()
    print("test_position_sizing: all passed")


if __name__ == "__main__":
    main()
