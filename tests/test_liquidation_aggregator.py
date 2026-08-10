"""Unit tests for multi-venue liquidation aggregator + provenance."""

from __future__ import annotations

import inspect

import pytest

from src.exchanges.liquidation_aggregator import (
    _bybit_side,
    _okx_side,
    parse_hl_trade_liquidation,
)
from src.exchanges.liquidation_event import (
    is_proxy_liquidation_source,
    is_real_liquidation_source,
)
from src.strategies.checklist_meta import ChecklistMeta
from src.strategies.liquidation_catcher import LiquidationCatcher

pytestmark = pytest.mark.unit


def test_real_provenance_accepts_venues_and_rollup() -> None:
    assert is_real_liquidation_source("real")
    assert is_real_liquidation_source("okx")
    assert is_real_liquidation_source("bybit")
    assert is_real_liquidation_source("hl")
    assert is_real_liquidation_source("binance")
    assert not is_real_liquidation_source("proxy")
    assert not is_real_liquidation_source(None)
    assert not is_real_liquidation_source("coinalyze")
    assert is_proxy_liquidation_source("proxy")


def test_okx_and_bybit_side_mapping() -> None:
    assert _okx_side({"posSide": "long", "side": "sell"}) == "long"
    assert _okx_side({"posSide": "short", "side": "buy"}) == "short"
    assert _okx_side({"side": "sell"}) == "long"
    assert _bybit_side("Buy") == "long"
    assert _bybit_side("Sell") == "short"


def test_hl_parse_requires_liquidation_object() -> None:
    plain = {"coin": "BTC", "px": "100", "sz": "1", "side": "A", "time": 1}
    assert parse_hl_trade_liquidation(plain) is None
    liq = {
        "coin": "BTC",
        "px": "100",
        "sz": "2",
        "side": "A",
        "time": 1,
        "dir": "Close Long",
        "liquidation": {"liquidatedUser": "0xabc", "markPx": 100, "method": "market"},
    }
    ev = parse_hl_trade_liquidation(liq)
    assert ev is not None
    assert ev.source == "hl"
    assert ev.side == "long"
    assert ev.notional_usd == pytest.approx(200.0)


def test_catcher_accepts_real_rollup() -> None:
    from src.strategies.base import MarketEvent

    c = LiquidationCatcher(
        {
            "enabled": True,
            "require_real_liquidation_data": True,
            "min_notional_usd": 1_000,
            "min_liquidation_count": 1,
            "require_oi_decreasing": False,
            "min_confidence": 0.0,
        }
    )
    ev = MarketEvent(
        symbol="BTC",
        timestamp_ms=1_000_000,
        price=50_000.0,
        liquidation_notional_5m=2_000_000.0,
        liquidation_side_5m="long",
        liquidation_count_5m=5,
        liquidation_data_source="real",
        liquidation_feed_ready=True,
    )
    sig = c.on_data(ev)
    assert sig is not None


def test_catcher_rejects_proxy() -> None:
    from src.strategies.base import MarketEvent

    c = LiquidationCatcher(
        {
            "enabled": True,
            "require_real_liquidation_data": True,
            "min_notional_usd": 1_000,
            "min_liquidation_count": 1,
            "require_oi_decreasing": False,
        }
    )
    ev = MarketEvent(
        symbol="BTC",
        timestamp_ms=1_000_000,
        price=50_000.0,
        liquidation_notional_5m=2_000_000.0,
        liquidation_side_5m="long",
        liquidation_count_5m=5,
        liquidation_data_source="proxy",
        liquidation_feed_ready=True,
    )
    assert c.on_data(ev) is None


def test_okx_ws_payload_sample_parses() -> None:
    """Regression against a live OKX WS frame captured 2026-08-09."""
    from src.exchanges.hyperliquid_ws import DataBus
    from src.exchanges.liquidation_aggregator import MultiVenueLiquidationAggregator

    bus = DataBus()
    got = []
    agg = MultiVenueLiquidationAggregator(
        bus,
        symbols=["BTC", "ETH"],
        enable_okx=False,
        enable_bybit=False,
        enable_coinalyze_check=False,
        on_event=got.append,
    )
    # Force map entry for a non-major so we prove parser without waiting for BTC
    agg._okx_inst_to_base["GIGGLE-USDT-SWAP"] = "BTC"  # map under test symbol
    agg._allowed.add("BTC")
    raw = {
        "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
        "data": [
            {
                "details": [
                    {
                        "bkLoss": "0",
                        "bkPx": "35.43",
                        "posSide": "short",
                        "side": "buy",
                        "sz": "71",
                        "ts": "1786309199641",
                    }
                ],
                "instId": "GIGGLE-USDT-SWAP",
                "instType": "SWAP",
            }
        ],
    }
    import json

    agg._on_okx_message(json.dumps(raw))
    assert len(got) == 1
    assert got[0].source == "okx"
    assert got[0].side == "short"
    assert got[0].notional_usd == pytest.approx(35.43 * 71)


def test_bybit_payload_uses_v_as_size() -> None:
    from src.exchanges.hyperliquid_ws import DataBus
    from src.exchanges.liquidation_aggregator import MultiVenueLiquidationAggregator

    bus = DataBus()
    got = []
    agg = MultiVenueLiquidationAggregator(
        bus,
        symbols=["BTC"],
        enable_okx=False,
        enable_bybit=False,
        enable_coinalyze_check=False,
        on_event=got.append,
    )
    raw = {
        "topic": "allLiquidation.BTCUSDT",
        "data": [
            {
                "T": 1786309199641,
                "s": "BTCUSDT",
                "S": "Buy",
                "v": "0.01",
                "p": "65000",
            }
        ],
    }
    import json

    agg._on_bybit_message(json.dumps(raw))
    assert len(got) == 1
    assert got[0].source == "bybit"
    assert got[0].side == "long"
    assert got[0].notional_usd == pytest.approx(650.0)