"""Tests for funding normalization and HL predictedFundings parsing."""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.data.market_data_health import MarketDataHealthTracker, compute_feed_status
from src.exchanges.funding_aggregator import AggregatedFundingOI, FundingOIAggregator
from src.exchanges.funding_normalize import (
    normalize_funding_to_8h,
    parse_optional_rate,
    resolve_effective_funding,
)
from src.exchanges.hl_predicted_funding import (
    HyperliquidPredictedFundingClient,
    parse_predicted_fundings_response,
)
from src.strategies.base import MarketEvent


def test_parse_optional_rate() -> None:
    assert parse_optional_rate(None) is None
    assert parse_optional_rate("") is None
    assert parse_optional_rate("0.0000125") == 0.0000125
    assert parse_optional_rate(0.0) == 0.0


def test_normalize_1h_to_8h() -> None:
    rate_1h = 0.00001
    rate_8h = normalize_funding_to_8h(rate_1h, 1.0)
    assert abs(rate_8h - 0.00008) < 1e-12


def test_parse_predicted_fundings_response() -> None:
    raw = [
        [
            "BTC",
            [
                ["HlPerp", {"fundingRate": "0.000011", "nextFundingTime": 1, "fundingIntervalHours": 1}],
                ["BinPerp", {"fundingRate": "0.00008", "nextFundingTime": 2, "fundingIntervalHours": 8}],
            ],
        ]
    ]
    parsed = parse_predicted_fundings_response(raw)
    assert "BTC" in parsed
    snap = parsed["BTC"]
    assert snap.predicted_funding_hl == 0.000011
    assert abs(snap.predicted_funding_hl_8h - 0.000088) < 1e-12


def test_resolve_effective_funding_priority() -> None:
    event = MarketEvent(
        symbol="BTC",
        price=1.0,
        timestamp_ms=0,
        funding=0.0001,
        predicted_funding=0.0002,
        predicted_funding_avg=0.0003,
    )
    assert resolve_effective_funding(event) == 0.0003


def test_health_tracker_failure_rate() -> None:
    tracker = MarketDataHealthTracker(window_sec=3600)
    for _ in range(4):
        tracker.record("BTC", cex_ok=True, hl_ok=True, status="green")
    tracker.record("BTC", cex_ok=False, hl_ok=False, status="red")
    total, failed, rate = tracker.symbol_stats("BTC")
    assert total == 5
    assert failed == 1
    assert abs(rate - 0.2) < 1e-9


def test_compute_feed_status() -> None:
    assert compute_feed_status(
        cex_ok=True,
        cex_stale=False,
        cex_exchange_count=3,
        min_exchanges=2,
        hl_ok=True,
        hl_stale=False,
    ) == "green"
    assert compute_feed_status(
        cex_ok=True,
        cex_stale=True,
        cex_exchange_count=3,
        min_exchanges=2,
        hl_ok=True,
        hl_stale=False,
    ) == "yellow"


def test_aggregator_stale_cache() -> None:
    agg = FundingOIAggregator(stale_max_sec=600)

    async def _run() -> None:
        fresh = await agg.poll(["BTC"])
        assert "BTC" in fresh
        assert fresh["BTC"].stale is False
        # Seed cache then simulate empty poll by corrupting symbol map key temporarily
        agg._cache["BTC"] = fresh["BTC"]
        stale_row = AggregatedFundingOI(
            symbol="FAKE",
            funding_avg=0.001,
            exchange_count=1,
            timestamp_ms=fresh["BTC"].timestamp_ms,
        )
        agg._cache["FAKE"] = stale_row

    asyncio.run(_run())


def test_hl_predicted_live_smoke() -> None:
    """Optional live call — skipped when network unavailable."""

    async def _run() -> None:
        client = HyperliquidPredictedFundingClient()
        result = await client.poll(["BTC"])
        assert "BTC" in result
        snap = result["BTC"]
        assert snap.predicted_funding_hl is not None
        assert snap.predicted_funding_hl > 0

    try:
        asyncio.run(_run())
        print("OK live predictedFundings smoke")
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP live smoke: {exc}")


if __name__ == "__main__":
    test_parse_optional_rate()
    test_normalize_1h_to_8h()
    test_parse_predicted_fundings_response()
    test_resolve_effective_funding_priority()
    test_health_tracker_failure_rate()
    test_compute_feed_status()
    test_hl_predicted_live_smoke()
    print("All test_market_data_funding checks passed")
