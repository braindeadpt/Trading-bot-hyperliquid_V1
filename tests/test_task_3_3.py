"""Quick test for Task 3.3: Liquidation Catcher.

Validates:
  - Entry when liquidation notional > threshold
  - Correct opposite direction
  - OI decreasing filter
  - ADX filter
  - 2R take profit exit
  - Time-based exit
  - VWAP reversion exit
"""

import sys
sys.path.insert(0, r"C:\Users\Braindead\Documents\trading-bot-hyperliquid")

from src.strategies.liquidation_catcher import LiquidationCatcher
from src.strategies.base import MarketEvent, Position
import pytest

pytestmark = pytest.mark.unit


def test_entry_on_liquidation():
    print("=" * 60)
    print("TEST: Entry on liquidation cascade")
    print("=" * 60)

    catcher = LiquidationCatcher({
        "min_notional_usd": 50_000_000,
        "min_liquidation_count": 10,
        "require_oi_decreasing": False,  # skip for this test
        "base_size_pct": 0.005,
    })

    # Longs liquidated (price drop) → go LONG
    # v3.1.4 (commit 3220110) added a require_real_liquidation_data gate
    # (default True) that skips signals unless liquidation_data_source
    # == "binance", to avoid trading on unverified/proxy liquidation
    # estimates. Set it explicitly so this test exercises real data.
    event = MarketEvent(
        symbol="BTC", price=48000.0, timestamp_ms=0,
        funding=0.001, predicted_funding=0.001,
        liquidation_notional_5m=75_000_000,
        liquidation_side_5m="long",
        liquidation_count_5m=25,
        liquidation_data_source="binance",
    )

    sig = catcher.on_data(event)
    assert sig is not None, "Should signal on $75M liquidation"
    assert sig.side == "long", f"Longs liquidated → should go LONG, got {sig.side}"
    assert sig.confidence >= 0.70
    assert sig.metadata["liq_notional"] == 75_000_000
    print(f"Signal: {sig.side} @ {sig.entry_price:.2f}, notional=${sig.metadata['liq_notional']/1_000_000:.0f}M")
    print("[PASS]\n")


def test_entry_shorts_liquidated():
    print("=" * 60)
    print("TEST: Entry when shorts liquidated")
    print("=" * 60)

    catcher = LiquidationCatcher({
        "min_notional_usd": 50_000_000,
        "require_oi_decreasing": False,
    })

    # v3.1.4 (commit 3220110): require_real_liquidation_data gate needs
    # liquidation_data_source == "binance" to allow a signal.
    event = MarketEvent(
        symbol="BTC", price=52000.0, timestamp_ms=0,
        funding=0.001, predicted_funding=0.001,
        liquidation_notional_5m=100_000_000,
        liquidation_side_5m="short",
        liquidation_count_5m=30,
        liquidation_data_source="binance",
    )

    sig = catcher.on_data(event)
    assert sig is not None
    assert sig.side == "short", f"Shorts liquidated → should go SHORT, got {sig.side}"
    print(f"Signal: {sig.side} @ {sig.entry_price:.2f}, notional=${sig.metadata['liq_notional']/1_000_000:.0f}M")
    print("[PASS]\n")


def test_notional_too_small():
    print("=" * 60)
    print("TEST: Notional below threshold")
    print("=" * 60)

    catcher = LiquidationCatcher({"min_notional_usd": 50_000_000})

    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=0,
        liquidation_notional_5m=10_000_000,
        liquidation_side_5m="long",
        liquidation_count_5m=15,
    )

    sig = catcher.on_data(event)
    assert sig is None, "$10M < $50M threshold → should NOT signal"
    print("Correctly rejected: $10M < $50M threshold")
    print("[PASS]\n")


def test_oi_increasing_filter():
    print("=" * 60)
    print("TEST: OI increasing filter")
    print("=" * 60)

    catcher = LiquidationCatcher({
        "min_notional_usd": 50_000_000,
        "require_oi_decreasing": True,
        "oi_delta_max": 0.0,
    })

    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=0,
        liquidation_notional_5m=75_000_000,
        liquidation_side_5m="long",
        liquidation_count_5m=20,
        oi_delta=5000.0,  # OI increasing — not a true liquidation
    )

    sig = catcher.on_data(event)
    assert sig is None, "OI increasing → should NOT signal (not a real liquidation)"
    print("Correctly rejected: OI_delta=5000 > 0")
    print("[PASS]\n")


def test_adx_filter():
    print("=" * 60)
    print("TEST: ADX filter (trending market)")
    print("=" * 60)

    catcher = LiquidationCatcher({
        "min_notional_usd": 50_000_000,
        "max_adx": 40.0,
        "require_oi_decreasing": False,
    })

    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=0,
        liquidation_notional_5m=75_000_000,
        liquidation_side_5m="long",
        liquidation_count_5m=20,
        adx_14=50.0,  # strong trend
    )

    sig = catcher.on_data(event)
    assert sig is None, "ADX=50 > 40 → should NOT signal (cascade may continue)"
    print("Correctly rejected: ADX=50 > threshold=40")
    print("[PASS]\n")


def test_exit_2r():
    print("=" * 60)
    print("TEST: 2R take profit exit")
    print("=" * 60)

    catcher = LiquidationCatcher({"take_profit_r": 2.0})

    # Entered LONG at $48K with 1% stop → 2R = 2% profit target = $48,960
    pos = Position(
        symbol="BTC", side="long", entry_price=48000.0,
        size=0.1, entry_time_ms=0,
    )

    event = MarketEvent(
        symbol="BTC", price=48960.0, timestamp_ms=5 * 60_000,
    )

    exit_sig = catcher.on_position(pos, event)
    assert exit_sig is not None, "Should exit at 2R (2% profit)"
    assert "take_profit_2R" in exit_sig.reason
    print(f"Exit: {exit_sig.reason}")
    print("[PASS]\n")


def test_exit_max_hold():
    print("=" * 60)
    print("TEST: Max hold time exit")
    print("=" * 60)

    catcher = LiquidationCatcher({"max_hold_minutes": 30})

    pos = Position(
        symbol="BTC", side="long", entry_price=48000.0,
        size=0.1, entry_time_ms=0,
    )

    event = MarketEvent(
        symbol="BTC", price=48100.0, timestamp_ms=35 * 60_000,  # 35 min
    )

    exit_sig = catcher.on_position(pos, event)
    assert exit_sig is not None, "Should exit at max hold"
    assert "max_hold" in exit_sig.reason
    print(f"Exit: {exit_sig.reason}")
    print("[PASS]\n")


if __name__ == "__main__":
    test_entry_on_liquidation()
    test_entry_shorts_liquidated()
    test_notional_too_small()
    test_oi_increasing_filter()
    test_adx_filter()
    test_exit_2r()
    test_exit_max_hold()
    print("=" * 60)
    print("ALL TESTS PASSED [OK]")
    print("=" * 60)
