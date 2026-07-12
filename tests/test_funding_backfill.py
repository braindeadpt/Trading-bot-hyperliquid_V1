"""Tests for funding/OI backfill and candle buy/sell volume."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.candle_backfill import kline_to_candle
import pytest

pytestmark = pytest.mark.unit


def test_kline_to_candle_buy_sell_split() -> None:
    k = [
        1_700_000_000_000,  # open time
        "100", "110", "90", "105",  # OHLC
        "10.0",  # volume (base)
        1_700_000_059_999,  # close time
        "1000.0",  # quote volume
        100,  # trades
        "6.0",  # taker buy base
        "600.0",  # taker buy quote
        "0",
    ]
    c = kline_to_candle(k, "BTC")
    assert c.buy_volume == 6.0
    assert c.sell_volume == 4.0
    assert c.trade_count == 100
    assert c.timestamp_ms == 1_700_000_059_999


def test_liquidation_accumulator_stats() -> None:
    from src.core.liquidation_accumulator import LiquidationAccumulator

    acc = LiquidationAccumulator(window_ms=300_000)
    acc.record(1_000_000, 2_000_000.0, "long", "proxy")
    acc.record(1_060_000, 1_000_000.0, "long", "proxy")
    n, side, count = acc.stats()
    assert n == 3_000_000.0 and side == "long" and count == 2


def test_derive_liquidation_proxy() -> None:
    from src.data.database import Candle
    from src.data.external_feeds_backfill import derive_liquidation_proxy_events

    candles = [
        Candle("BTC", 1_000_000, 100, 101, 99, 100, 100.0),
        Candle("BTC", 1_060_000, 100, 101, 94, 95, 200.0),
    ]
    oi = [(1_060_000, 1e9, -1e6)]
    events = derive_liquidation_proxy_events("BTC", candles, oi, min_notional_usd=1000.0)
    assert len(events) == 1
    assert events[0].side == "long"
    assert events[0].source == "proxy"


if __name__ == "__main__":
    test_kline_to_candle_buy_sell_split()
    test_liquidation_accumulator_stats()
    test_derive_liquidation_proxy()
    print("ALL EXTERNAL FEEDS TESTS PASSED [OK]")
