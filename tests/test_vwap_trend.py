"""Smoke unit tests for experimental VWAPTrend strategy."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from src.strategies.base import MarketEvent, Position
from src.strategies.indicators import Candle
from src.strategies.vwap_trend import VWAPTrend

pytestmark = pytest.mark.unit

DAY0 = 1_747_526_400_000  # 2026-05-18 00:00 UTC
M15 = 900_000


def _c(close: float, ts: int, volume: float = 10.0) -> Candle:
    return Candle(
        open=close, high=close * 1.001, low=close * 0.999,
        close=close, volume=volume, timestamp_ms=ts,
    )


def test_disabled_by_default():
    s = VWAPTrend({})
    assert s.is_active() is False
    ev = MarketEvent(symbol="BTC", price=100.0, timestamp_ms=DAY0 + M15)
    assert s.on_data(ev) is None


def test_long_above_anchored_vwap():
    s = VWAPTrend({
        "enabled": True,
        "vwap_confirm_tf": "15m",
        "vwap_cross_buffer_pct": 0.001,
        "min_flip_interval_minutes": 0,
        "min_session_bars": 2,
        "signal_throttle_ms": 0,
    })
    # Build rising session: VWAP lags below last close
    prices = [100.0, 101.0, 102.0, 104.0]
    last_sig = None
    for i, px in enumerate(prices):
        ts = DAY0 + (i + 1) * M15
        candle = _c(px, ts)
        ev = MarketEvent(
            symbol="BTC", price=px, timestamp_ms=ts + 60_000,
            candle_15m=candle,
        )
        last_sig = s.on_data(ev)
    assert last_sig is not None
    assert last_sig.side == "long"
    assert last_sig.strategy == "VWAPTrend"


def test_exit_on_opposite_close():
    s = VWAPTrend({
        "enabled": True,
        "vwap_confirm_tf": "15m",
        "vwap_cross_buffer_pct": 0.001,
        "min_flip_interval_minutes": 0,
        "min_session_bars": 2,
        "close_on_utc_rollover": False,
        "max_hold_hours": 48,
    })
    # Seed long bias
    for i, px in enumerate([100.0, 101.0, 103.0]):
        ts = DAY0 + (i + 1) * M15
        s.on_data(MarketEvent(
            symbol="BTC", price=px, timestamp_ms=ts + 1_000,
            candle_15m=_c(px, ts),
        ))

    pos = Position(
        symbol="BTC", side="long", entry_price=103.0, size=1.0,
        entry_time_ms=DAY0 + 3 * M15,
    )
    # Hard dump below VWAP
    dump_ts = DAY0 + 4 * M15
    dump = _c(90.0, dump_ts, volume=50.0)
    exit_sig = s.on_position(
        pos,
        MarketEvent(symbol="BTC", price=90.0, timestamp_ms=dump_ts + 1_000, candle_15m=dump),
    )
    assert exit_sig is not None
    assert "vwap_opposite" in exit_sig.reason


def test_session_filter_blocks_off_hours():
    s = VWAPTrend({
        "enabled": True,
        "use_session_filter": True,
        "session_hours_utc": [13, 14, 15],
        "vwap_confirm_tf": "15m",
        "min_session_bars": 1,
        "signal_throttle_ms": 0,
        "min_flip_interval_minutes": 0,
    })
    # 02:00 UTC on day 0 — outside allow-list
    ts = DAY0 + 2 * 3_600_000
    sig = s.on_data(MarketEvent(
        symbol="ETH", price=2000.0, timestamp_ms=ts + 1_000,
        candle_15m=_c(2000.0, ts),
    ))
    assert sig is None
