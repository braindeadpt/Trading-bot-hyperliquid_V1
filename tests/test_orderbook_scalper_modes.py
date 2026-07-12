"""Tests for OrderBookScalper mode (momentum/fade) and anti-spoof filter."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.strategies.base import MarketEvent
from src.strategies.orderbook_scalper import OrderBookScalper
import pytest

pytestmark = pytest.mark.unit


def _scalper(**overrides: object) -> OrderBookScalper:
    cfg = {
        "enabled": True,
        "auto_enable": False,
        "bid_ask_ratio_long": 1.5,
        "bid_ask_ratio_short": 0.67,
        "spoof_wall_proximity_pct": 0.001,
        "spoof_depth_skew_min": 0.65,
    }
    cfg.update(overrides)
    return OrderBookScalper(cfg)


def _event(
    ratio: float,
    *,
    price: float = 100_000.0,
    depth_q: float = 0.5,
    bid_wall: float | None = None,
    ask_wall: float | None = None,
    ts: int = 1,
) -> MarketEvent:
    return MarketEvent(
        symbol="BTC",
        price=price,
        timestamp_ms=ts,
        orderbook_bid_ask_ratio=ratio,
        orderbook_spread_pct=0.0002,
        orderbook_depth_quality=depth_q,
        orderbook_largest_bid_wall=bid_wall,
        orderbook_largest_ask_wall=ask_wall,
    )


def test_momentum_mode_long_on_bid_heavy() -> None:
    strat = _scalper(mode="momentum")
    sig = strat.on_data(_event(1.6))
    assert sig is not None
    assert sig.side == "long"
    assert sig.metadata["mode"] == "momentum"


def test_fade_mode_short_on_bid_heavy() -> None:
    strat = _scalper(mode="fade")
    sig = strat.on_data(_event(1.6, ts=10))
    assert sig is not None
    assert sig.side == "short"
    assert sig.metadata["mode"] == "fade"


def test_fade_mode_long_on_ask_heavy() -> None:
    strat = _scalper(mode="fade")
    sig = strat.on_data(_event(0.6, ts=20))
    assert sig is not None
    assert sig.side == "long"
    assert sig.metadata["imbalance_side"] == "ask"


def test_spoof_filter_rejects_near_bid_wall_with_skewed_depth() -> None:
    """Momentum long blocked when bid wall is tight to mid and book is bid-heavy."""
    strat = _scalper(mode="momentum")
    # Wall 0.05% below mid, depth_quality 0.72 (bid-heavy)
    sig = strat.on_data(
        _event(
            1.6,
            price=100_000.0,
            depth_q=0.72,
            bid_wall=99_950.0,
            ts=30,
        )
    )
    assert sig is None


def test_spoof_filter_allows_distant_wall() -> None:
    strat = _scalper(mode="momentum")
    sig = strat.on_data(
        _event(
            1.6,
            depth_q=0.72,
            bid_wall=99_000.0,  # 1% away
            ts=40,
        )
    )
    assert sig is not None
    assert sig.side == "long"


def test_spoof_filter_allows_balanced_depth_near_wall() -> None:
    strat = _scalper(mode="momentum")
    sig = strat.on_data(
        _event(
            1.6,
            depth_q=0.55,
            bid_wall=99_950.0,
            ts=50,
        )
    )
    assert sig is not None


def test_spoof_filter_rejects_fade_long_near_ask_wall() -> None:
    strat = _scalper(mode="fade")
    sig = strat.on_data(
        _event(
            0.6,
            depth_q=0.30,
            ask_wall=100_050.0,
            ts=60,
        )
    )
    assert sig is None


def main() -> None:
    test_momentum_mode_long_on_bid_heavy()
    test_fade_mode_short_on_bid_heavy()
    test_fade_mode_long_on_ask_heavy()
    test_spoof_filter_rejects_near_bid_wall_with_skewed_depth()
    test_spoof_filter_allows_distant_wall()
    test_spoof_filter_allows_balanced_depth_near_wall()
    test_spoof_filter_rejects_fade_long_near_ask_wall()
    print("test_orderbook_scalper_modes: all passed")


if __name__ == "__main__":
    main()
