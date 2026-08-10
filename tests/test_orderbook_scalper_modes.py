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
        "spoof_wall_fraction_min": 0.21,
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
    bid_wall_size: float | None = 10.0,
    ask_wall_size: float | None = 10.0,
    bid_depth: float | None = 100.0,
    ask_depth: float | None = 100.0,
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
        orderbook_largest_bid_wall_size=bid_wall_size,
        orderbook_largest_ask_wall_size=ask_wall_size,
        orderbook_bid_depth_1pct=bid_depth,
        orderbook_ask_depth_1pct=ask_depth,
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


def test_spoof_filter_rejects_near_dominant_bid_wall() -> None:
    """Momentum long blocked when near-mid bid wall is a large fraction of bid depth."""
    strat = _scalper(mode="momentum")
    sig = strat.on_data(
        _event(
            1.6,
            price=100_000.0,
            depth_q=0.72,  # old filter would use this; new filter ignores it
            bid_wall=99_950.0,
            bid_wall_size=50.0,
            bid_depth=100.0,  # wall_frac=0.50 >= 0.21
            ts=30,
        )
    )
    assert sig is None


def test_spoof_filter_allows_near_wall_small_fraction() -> None:
    """Near wall that is only 10% of side depth is NOT spoof — must allow signal.

    This is the critical regression: old depth_q filter blocked 100% of ask/bid
    imbalance signals; wall_frac must be orthogonal.
    """
    strat = _scalper(mode="momentum")
    sig = strat.on_data(
        _event(
            1.6,
            depth_q=0.72,
            bid_wall=99_950.0,
            bid_wall_size=10.0,
            bid_depth=100.0,  # wall_frac=0.10 < 0.21
            ts=35,
        )
    )
    assert sig is not None
    assert sig.side == "long"


def test_spoof_filter_allows_ask_heavy_with_small_wall_frac() -> None:
    """Ask-heavy short must NOT be auto-blocked by low depth_q alone."""
    strat = _scalper(mode="momentum")
    sig = strat.on_data(
        _event(
            0.5,
            depth_q=0.25,  # old filter: always spoof on ask side
            ask_wall=100_050.0,
            ask_wall_size=12.0,
            ask_depth=100.0,  # frac=0.12
            ts=36,
        )
    )
    assert sig is not None
    assert sig.side == "short"


def test_spoof_filter_allows_distant_wall() -> None:
    strat = _scalper(mode="momentum")
    sig = strat.on_data(
        _event(
            1.6,
            depth_q=0.72,
            bid_wall=99_000.0,  # 1% away
            bid_wall_size=80.0,
            bid_depth=100.0,
            ts=40,
        )
    )
    assert sig is not None
    assert sig.side == "long"


def test_spoof_filter_fail_open_without_sizes() -> None:
    """Missing wall sizes must not recreate the 100% block."""
    strat = _scalper(mode="momentum")
    sig = strat.on_data(
        _event(
            1.6,
            depth_q=0.72,
            bid_wall=99_950.0,
            bid_wall_size=None,
            bid_depth=None,
            ts=50,
        )
    )
    assert sig is not None


def test_spoof_filter_rejects_fade_long_near_dominant_ask_wall() -> None:
    strat = _scalper(mode="fade")
    sig = strat.on_data(
        _event(
            0.6,
            depth_q=0.30,
            ask_wall=100_050.0,
            ask_wall_size=60.0,
            ask_depth=100.0,
            ts=60,
        )
    )
    assert sig is None
