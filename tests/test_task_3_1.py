"""Quick test for Task 3.1: Funding Arbitrage.

Validates:
  - Pair selection finds best spread
  - Entry thresholds respected
  - OI stability filter works
  - Exit signals fire on reversion and max hold
"""

import sys
sys.path.insert(0, r"C:\Users\Braindead\Documents\trading-bot-hyperliquid")

from src.strategies.funding_arbitrage import FundingArbitrage
from src.strategies.base import MarketEvent, Position
import pytest

pytestmark = pytest.mark.unit


def test_pair_selection():
    print("=" * 60)
    print("TEST: Pair selection")
    print("=" * 60)

    arb = FundingArbitrage({
        "min_funding_spread": 0.012,
        "min_individual_funding": 0.005,
        "confidence": 0.75,
    })

    # Simulate funding data for 4 assets
    funding_map = {
        "BTC": -0.008,   # most negative
        "ETH": 0.003,
        "SOL": 0.001,
        "XRP": 0.010,    # most positive
    }
    oi_delta_map = {k: 100.0 for k in funding_map}

    pair = arb.scan_pair_opportunity(funding_map, oi_delta_map, timestamp_ms=0)
    assert pair is not None, "Should find a pair"
    long_sig, short_sig = pair

    print(f"LONG:  {long_sig.symbol} @ funding={long_sig.metadata['funding']:.4f}")
    print(f"SHORT: {short_sig.symbol} @ funding={short_sig.metadata['funding']:.4f}")
    print(f"Spread: {short_sig.metadata['spread']:.4f}")

    assert long_sig.symbol == "BTC", f"Expected BTC as long, got {long_sig.symbol}"
    assert short_sig.symbol == "XRP", f"Expected XRP as short, got {short_sig.symbol}"
    assert long_sig.side == "long"
    assert short_sig.side == "short"
    assert long_sig.metadata["pair"] == "funding_arb"
    assert short_sig.metadata["pair"] == "funding_arb"

    print("[PASS]\n")


def test_spread_threshold():
    print("=" * 60)
    print("TEST: Spread threshold")
    print("=" * 60)

    arb = FundingArbitrage({
        "min_funding_spread": 0.015,  # high threshold
        "min_individual_funding": 0.005,
    })

    # Spread = 0.012 < 0.015 → no arb
    funding_map = {
        "BTC": -0.006,
        "ETH": 0.006,
    }
    oi_delta_map = {k: 100.0 for k in funding_map}

    pair = arb.scan_pair_opportunity(funding_map, oi_delta_map, timestamp_ms=0)
    assert pair is None, "Should NOT find pair — spread too small"
    print("Correctly rejected: spread 1.2% < threshold 1.5%")
    print("[PASS]\n")


def test_individual_threshold():
    print("=" * 60)
    print("TEST: Individual funding threshold")
    print("=" * 60)

    arb = FundingArbitrage({
        "min_funding_spread": 0.005,
        "min_individual_funding": 0.010,  # high individual threshold
    })

    # Spread = 0.012 > 0.005, but individual values are 0.006 < 0.010
    funding_map = {
        "BTC": -0.006,
        "ETH": 0.006,
    }
    oi_delta_map = {k: 100.0 for k in funding_map}

    pair = arb.scan_pair_opportunity(funding_map, oi_delta_map, timestamp_ms=0)
    assert pair is None, "Should NOT find pair — individual funding too small"
    print("Correctly rejected: individual 0.6% < threshold 1.0%")
    print("[PASS]\n")


def test_oi_stability_filter():
    print("=" * 60)
    print("TEST: OI stability filter")
    print("=" * 60)

    arb = FundingArbitrage({
        "min_funding_spread": 0.005,
        "min_individual_funding": 0.005,
        "require_oi_stable": True,
        "oi_delta_max": 500.0,
    })

    funding_map = {
        "BTC": -0.010,
        "ETH": 0.010,
    }
    # OI surging on one leg
    oi_delta_map = {"BTC": 2000.0, "ETH": 100.0}

    pair = arb.scan_pair_opportunity(funding_map, oi_delta_map, timestamp_ms=0)
    assert pair is None, "Should NOT find pair — OI surging on long leg"
    print("Correctly rejected: OI_delta=2000 > max=500")
    print("[PASS]\n")


def test_exit_on_reversion():
    print("=" * 60)
    print("TEST: Exit on funding reversion")
    print("=" * 60)

    arb = FundingArbitrage({"exit_threshold": 0.002})

    # Position with extreme funding
    pos = Position(
        symbol="BTC", side="long", entry_price=50000.0,
        size=0.1, entry_time_ms=0,
    )

    # Funding reverts to 0.1% — below 0.2% threshold
    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=3_600_000,
        funding=0.001, predicted_funding=0.001,
    )

    exit_sig = arb.on_position(pos, event)
    assert exit_sig is not None, "Should exit when funding reverts"
    assert "reverted" in exit_sig.reason
    print(f"Exit signal: {exit_sig.reason}")
    print("[PASS]\n")


def test_exit_on_max_hold():
    print("=" * 60)
    print("TEST: Exit on max hold time")
    print("=" * 60)

    arb = FundingArbitrage({"max_hold_hours": 8})

    pos = Position(
        symbol="BTC", side="long", entry_price=50000.0,
        size=0.1, entry_time_ms=0,
    )

    # 9 hours later — beyond 8h max hold
    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=9 * 3_600_000,
        funding=0.005,  # still extreme
    )

    exit_sig = arb.on_position(pos, event)
    assert exit_sig is not None, "Should exit at max hold"
    assert "max_hold" in exit_sig.reason
    print(f"Exit signal: {exit_sig.reason}")
    print("[PASS]\n")


if __name__ == "__main__":
    test_pair_selection()
    test_spread_threshold()
    test_individual_threshold()
    test_oi_stability_filter()
    test_exit_on_reversion()
    test_exit_on_max_hold()
    print("=" * 60)
    print("ALL TESTS PASSED [OK]")
    print("=" * 60)
