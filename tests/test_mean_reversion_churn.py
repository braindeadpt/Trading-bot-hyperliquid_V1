"""Anti-churn tests for FundingExtreme mean reversion exits."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.strategies.base import MarketEvent, Position
from src.strategies.mean_reversion import MeanReversion
import pytest

pytestmark = pytest.mark.unit


def _mr(**overrides: object) -> MeanReversion:
    cfg = {
        "min_funding_exit_hold_ms": 900_000,
        "min_profit_before_funding_exit_pct": 0.001,
        "min_strong_threshold_abs": 0.00006,
        "max_hold_minutes": 60,
        "reentry_cooldown_ms": 600_000,
    }
    cfg.update(overrides)
    return MeanReversion(cfg)


def _long_position(entry_ms: int = 0, entry_funding: float = 0.0002) -> Position:
    return Position(
        symbol="BTC",
        side="long",
        entry_price=50_000.0,
        size=0.02,
        entry_time_ms=entry_ms,
        metadata={"entry_funding": entry_funding},
    )


def test_heuristic_oi_ratio_does_not_trigger_oi_normalizing_exit() -> None:
    """Fallback oi_ratio=0.5 must not close a long right after min-hold."""
    strat = _mr()
    hold_ms = strat.MIN_FUNDING_EXIT_HOLD_MS + 1
    pos = _long_position()
    event = MarketEvent(
        symbol="BTC",
        price=50_050.0,
        timestamp_ms=hold_ms,
        oi_total=1_000_000.0,
        funding=0.00015,
        predicted_funding=0.00015,
    )
    ratio, is_real = strat._estimate_oi_ratio(event)
    assert ratio == 0.5
    assert is_real is False

    exit_sig = strat.on_position(pos, event)
    assert exit_sig is None


def test_real_oi_ratio_can_trigger_oi_normalizing_exit() -> None:
    """Real Binance LS ratio above 0.4 still exits long positions."""
    strat = _mr()
    hold_ms = strat.MIN_FUNDING_EXIT_HOLD_MS + 1
    pos = _long_position()
    event = MarketEvent(
        symbol="BTC",
        price=50_050.0,
        timestamp_ms=hold_ms,
        oi_long_ratio=0.45,
        oi_total=1_000_000.0,
        funding=0.00015,
        predicted_funding=0.00015,
    )
    exit_sig = strat.on_position(pos, event)
    assert exit_sig is not None
    assert exit_sig.reason == "oi_normalizing_after_shorts"


def test_weak_entry_funding_does_not_trigger_funding_reverted() -> None:
    """Near-zero entry funding never qualifies for funding_reverted exit."""
    strat = _mr()
    hold_ms = strat.MIN_FUNDING_EXIT_HOLD_MS + 1
    pos = _long_position(entry_funding=0.00003)  # below min_strong_threshold_abs
    event = MarketEvent(
        symbol="BTC",
        price=50_100.0,  # +0.2% profit would otherwise pass min_profit gate
        timestamp_ms=hold_ms,
        oi_total=1_000_000.0,
        funding=0.00001,
        predicted_funding=0.00001,
    )
    exit_sig = strat.on_position(pos, event)
    assert exit_sig is None


def test_reentry_cooldown_blocks_immediate_new_entry() -> None:
    """After exit signal, on_data stays silent until cooldown elapses."""
    strat = _mr(reentry_cooldown_ms=300_000)
    state = strat._get_state("BTC")
    state.last_exit_ms = 1_000_000
    event = MarketEvent(
        symbol="BTC",
        price=50_000.0,
        timestamp_ms=1_100_000,  # 100s later < 300s cooldown
        funding=-0.001,
        predicted_funding=-0.001,
        oi_total=1_000_000.0,
        oi_delta=-100.0,
        oi_long_ratio=0.30,
    )
    assert strat.on_data(event) is None


def main() -> None:
    test_heuristic_oi_ratio_does_not_trigger_oi_normalizing_exit()
    test_real_oi_ratio_can_trigger_oi_normalizing_exit()
    test_weak_entry_funding_does_not_trigger_funding_reverted()
    test_reentry_cooldown_blocks_immediate_new_entry()
    print("test_mean_reversion_churn: all passed")


if __name__ == "__main__":
    main()
