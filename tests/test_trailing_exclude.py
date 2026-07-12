"""Trailing stop exclude-by-sub-strategy tests."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.engine import TradingEngine
from src.strategies.base import Position
from src.utils.config import Config
import pytest

pytestmark = pytest.mark.unit


def _engine_with_trailing_exclude() -> TradingEngine:
    cfg = Config({
        "assets": ["BTC"],
        "execution": {
            "trailing_stop": {
                "enabled": True,
                "exclude_strategies": ["VolatilityBreakout"],
            },
        },
    })
    return TradingEngine(
        config=cfg,
        db=MagicMock(),
        data_bus=MagicMock(),
        strategies=[],
        risk_manager=MagicMock(),
        executor=MagicMock(),
    )


def test_trailing_skipped_for_excluded_sub_strategy() -> None:
    engine = _engine_with_trailing_exclude()
    pos = Position(
        symbol="SOL",
        side="short",
        entry_price=70.0,
        size=1.0,
        entry_time_ms=0,
        metadata={"sub_strategy": "VolatilityBreakout"},
    )
    assert engine._trailing_excluded_for_position(pos) is True


def test_trailing_applies_when_not_excluded() -> None:
    engine = _engine_with_trailing_exclude()
    pos = Position(
        symbol="BTC",
        side="long",
        entry_price=100_000.0,
        size=0.01,
        entry_time_ms=0,
        metadata={"sub_strategy": "SmartMoneyFlow"},
    )
    assert engine._trailing_excluded_for_position(pos) is False


if __name__ == "__main__":
    test_trailing_skipped_for_excluded_sub_strategy()
    test_trailing_applies_when_not_excluded()
    print("ALL TRAILING EXCLUDE TESTS PASSED [OK]")
