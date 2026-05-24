"""Phase 4 tests: maker order routing, TCA with maker fees, CI smoke."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dataclasses import dataclass

from src.core.order_router import resolve_order_routing
from src.core.tca import passes_tca_check, round_trip_cost_from_spec
from src.strategies.base import Signal
from src.utils.config import Config


@dataclass
class _Level:
    price: float
    size: float


@dataclass
class _Book:
    bids: list
    asks: list


def _cfg() -> Config:
    return Config({
        "risk": {"taker_fee_pct": 0.035, "paper_slippage_pct": 0.05},
        "execution": {
            "maker_orders": {
                "enabled": True,
                "maker_fee_pct": 0.01,
                "strategies": ["OrderBookScalper", "VWAPDeviation"],
            },
        },
    })


def test_maker_routing_for_scalper() -> None:
    signal = Signal(
        strategy="StrategyEnsemble",
        symbol="BTC",
        side="long",
        confidence=0.7,
        size_pct=0.005,
        entry_price=100_000.0,
        stop_loss_pct=0.002,
        take_profit_pct=0.0025,
        metadata={"original_strategy": "OrderBookScalper", "calculated_size": 0.01},
    )
    book = _Book(bids=[_Level(99_950.0, 1.0)], asks=[_Level(100_050.0, 1.0)])
    enriched, spec = resolve_order_routing(signal, _cfg(), book)
    assert spec.order_type == "limit_maker"
    assert enriched.entry_price == 99_950.0
    assert spec.entry_fee_pct == 0.0001
    assert spec.entry_slippage_pct == 0.0


def test_market_routing_for_trend() -> None:
    signal = Signal(
        strategy="StrategyEnsemble",
        symbol="BTC",
        side="long",
        confidence=0.7,
        size_pct=0.01,
        metadata={"original_strategy": "SmartMoneyFlow"},
    )
    enriched, spec = resolve_order_routing(signal, _cfg(), None)
    assert spec.order_type == "market"
    assert abs(spec.entry_fee_pct - 0.00035) < 1e-10


def test_tca_passes_with_maker_entry_taker_exit() -> None:
    cost = round_trip_cost_from_spec(0.0001, 0.00035, 0.0, 0.0005)
    assert cost < 0.0025
    signal = Signal(
        strategy="OrderBookScalper",
        symbol="BTC",
        side="long",
        confidence=0.6,
        size_pct=0.005,
        take_profit_pct=0.0025,
        metadata={
            "order_type": "limit_maker",
            "entry_fee_pct": 0.0001,
            "exit_fee_pct": 0.00035,
            "entry_slippage_pct": 0.0,
            "exit_slippage_pct": 0.0005,
        },
    )
    ok, reason = passes_tca_check(signal, 0.00035, 0.0005, 0.0005)
    assert ok, reason


if __name__ == "__main__":
    test_maker_routing_for_scalper()
    test_market_routing_for_trend()
    test_tca_passes_with_maker_entry_taker_exit()
    print("ALL PHASE 4 TESTS PASSED [OK]")
