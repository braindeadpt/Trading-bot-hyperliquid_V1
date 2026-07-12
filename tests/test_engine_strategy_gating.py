"""Tests for TradingEngine top-level strategy is_active() gating."""

from __future__ import annotations

import os
import sys
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.engine import TradingEngine
from src.strategies.base import MarketEvent, Signal
import pytest

pytestmark = pytest.mark.unit


class _InactiveStrategy:
    name = "InactiveStub"
    calls = 0

    def is_active(self) -> bool:
        return False

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        _InactiveStrategy.calls += 1
        return None


class _ActiveStrategy:
    name = "ActiveStub"
    calls = 0

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        _ActiveStrategy.calls += 1
        return None


def test_strategy_is_operational_respects_is_active() -> None:
    assert TradingEngine._strategy_is_operational(_InactiveStrategy()) is False
    assert TradingEngine._strategy_is_operational(_ActiveStrategy()) is True


def test_entry_loop_skips_inactive_strategy() -> None:
    """Mirror engine entry loop: inactive strategies never get on_data."""
    _InactiveStrategy.calls = 0
    _ActiveStrategy.calls = 0
    event = MarketEvent(symbol="BTC", price=50_000.0, timestamp_ms=1)
    strategies = [_InactiveStrategy(), _ActiveStrategy()]

    for strategy in strategies:
        if not TradingEngine._strategy_is_operational(strategy):
            continue
        strategy.on_data(event)

    assert _InactiveStrategy.calls == 0
    assert _ActiveStrategy.calls == 1


def main() -> None:
    test_strategy_is_operational_respects_is_active()
    test_entry_loop_skips_inactive_strategy()
    print("test_engine_strategy_gating: all passed")


if __name__ == "__main__":
    main()
