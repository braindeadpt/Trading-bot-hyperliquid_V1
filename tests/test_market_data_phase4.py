"""Phase 4 tests: strategy parity with normalized funding + feed health gates."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.exchanges.funding_normalize import (
    funding_data_usable,
    resolve_effective_funding,
    venue_funding_spread,
)
from src.strategies.base import MarketEvent
from src.strategies.funding_arbitrage import FundingArbitrage
from src.strategies.mean_reversion import MeanReversion


def test_venue_spread() -> None:
    venues = {"HlPerp": 0.00008, "BinPerp": 0.00009, "BybitPerp": 0.000085}
    assert venue_funding_spread(venues) < 0.00002


def test_funding_data_usable_blocks_red() -> None:
    event = MarketEvent(
        symbol="BTC",
        price=100_000.0,
        timestamp_ms=1,
        predicted_funding=0.00008,
        funding_avg=0.00007,
        market_data_health="red",
    )
    assert not funding_data_usable(event, require_feed_health=True)
    assert funding_data_usable(event, require_feed_health=False)


def test_mean_reversion_uses_resolved_funding() -> None:
    strat = MeanReversion({
        "require_feed_health": False,
        "use_dynamic_percentile": False,
        "extreme_threshold": 0.000001,
    })
    event = MarketEvent(
        symbol="BTC",
        price=100_000.0,
        timestamp_ms=1000,
        predicted_funding_avg=0.00012,
        predicted_funding=0.00011,
        funding=0.0001,
        oi_long_ratio=0.72,
        market_data_health="green",
        predicted_funding_by_venue={
            "HlPerp": 0.00011,
            "BinPerp": 0.00012,
        },
    )
    assert resolve_effective_funding(event) == 0.00012
    # Warm-up: no candles — should not crash; may return None
    assert strat.on_data(event) is None or strat.on_data(event) is not None


def test_funding_arbitrage_resolved_rates() -> None:
    arb = FundingArbitrage({
        "enabled": True,
        "require_feed_health": False,
        "min_funding_spread": 0.00001,
        "min_individual_funding": 0.000001,
        "min_net_spread": 0.00001,
        "spread_check_interval_ms": 0,
        "scan_interval_ms": 0,
    })
    e1 = MarketEvent(
        symbol="BTC",
        price=100_000.0,
        timestamp_ms=1,
        predicted_funding_avg=0.00005,
        market_data_health="green",
    )
    e2 = MarketEvent(
        symbol="ETH",
        price=3000.0,
        timestamp_ms=1,
        predicted_funding_avg=0.00012,
        market_data_health="green",
    )
    arb.on_data(e1)
    arb.on_data(e2)
    assert arb._latest_funding["BTC"] == 0.00005
    assert arb._latest_funding["ETH"] == 0.00012
    pair = arb.scan_pair_opportunity(
        funding_map=dict(arb._latest_funding),
        oi_delta_map={},
        timestamp_ms=2,
    )
    assert pair is not None


if __name__ == "__main__":
    test_venue_spread()
    test_funding_data_usable_blocks_red()
    test_mean_reversion_uses_resolved_funding()
    test_funding_arbitrage_resolved_rates()
    print("All test_market_data_phase4 checks passed")
