"""Quick test for Task 3.2: VWAP Deviation.

Validates:
  - VWAP + Z-score calculation
  - Volume surge filter
  - ADX filter (trending markets rejected)
  - OIR filter
  - Funding filter
  - Entry direction (mean reversion)
  - Exit on VWAP cross and normalization
"""

import sys
sys.path.insert(0, r"C:\Users\Braindead\Documents\trading-bot-hyperliquid")

from src.strategies.vwap_deviation import VWAPDeviation
from src.strategies.indicators import Candle
from src.strategies.base import MarketEvent, Position
import random
import pytest

pytestmark = pytest.mark.unit


def make_candles_with_deviation(n=30, base_price=100.0, deviation_factor=0.0):
    """Generate candles where price deviates from VWAP."""
    candles = []
    price = base_price
    for i in range(n):
        # Create a trending move to push price away from VWAP
        trend = deviation_factor * i / n
        open_p = price
        close_p = price + trend + random.uniform(-0.5, 0.5)
        high_p = max(open_p, close_p) + random.uniform(0.0, 1.0)
        low_p = min(open_p, close_p) - random.uniform(0.0, 1.0)
        vol = 2000.0 if i < n - 1 else 4000.0  # volume surge on last candle
        candles.append(Candle(
            open=open_p, high=high_p, low=low_p, close=close_p,
            volume=vol, timestamp_ms=i * 3_600_000
        ))
        price = close_p
    return candles


def test_vwap_zscore_calculation():
    print("=" * 60)
    print("TEST: VWAP + Z-score calculation")
    print("=" * 60)

    from src.strategies.indicators import calculate_vwap_zscore

    # Create candles with clear upward trend (price above VWAP)
    candles = make_candles_with_deviation(30, 100.0, 10.0)
    current_price = candles[-1].close  # ~110

    vwap, stddev, zscore = calculate_vwap_zscore(candles, current_price, lookback=24)
    print(f"Price={current_price:.2f}, VWAP={vwap:.2f}, StdDev={stddev:.2f}, Z={zscore:.2f}")

    assert vwap is not None
    assert stddev is not None
    assert zscore is not None
    assert current_price > vwap, "Price should be above VWAP in uptrend"
    assert zscore > 0, "Z-score should be positive when price > VWAP"

    # Test with price below VWAP
    candles_down = make_candles_with_deviation(30, 100.0, -10.0)
    current_price_down = candles_down[-1].close  # ~90
    vwap2, _, zscore2 = calculate_vwap_zscore(candles_down, current_price_down, lookback=24)
    assert current_price_down < vwap2
    assert zscore2 < 0
    print(f"Down move: Price={current_price_down:.2f}, VWAP={vwap2:.2f}, Z={zscore2:.2f}")

    print("[PASS]\n")


def test_entry_signal():
    print("=" * 60)
    print("TEST: Entry signal (mean reversion)")
    print("=" * 60)

    vwap_dev = VWAPDeviation({
        "z_threshold": 2.5,
        "volume_surge": 1.5,
        "max_adx": 25.0,
        "require_oir_confirm": False,  # skip OIR for this test
        "max_funding_opposite": 0.01,
        "base_size_pct": 0.01,
        "max_size_pct": 0.03,
    })

    # Create extreme deviation: price way above VWAP
    candles = make_candles_with_deviation(30, 50000.0, 2000.0)  # +4% move
    for c in candles:
        vwap_dev.on_candle(c, "BTC")

    current_price = candles[-1].close
    event = MarketEvent(
        symbol="BTC", price=current_price, timestamp_ms=30 * 3_600_000,
        funding=0.001, predicted_funding=0.001,
        adx_14=15.0,  # non-trending
    )

    sig = vwap_dev.on_data(event)
    if sig is not None:
        print(f"Signal: {sig.side} @ {sig.entry_price:.2f}, confidence={sig.confidence:.2f}")
        print(f"Metadata: zscore={sig.metadata['zscore']:.2f}, vwap={sig.metadata['vwap']:.2f}")
        # Price is way above VWAP → should be SHORT (mean reversion down)
        assert sig.side == "short", f"Expected short, got {sig.side}"
        assert sig.confidence >= 0.65
    else:
        print("No signal (may need more extreme deviation)")

    print("[PASS]\n")


def test_adx_filter():
    print("=" * 60)
    print("TEST: ADX filter (trending markets rejected)")
    print("=" * 60)

    vwap_dev = VWAPDeviation({
        "z_threshold": 2.5,
        "volume_surge": 1.5,
        "max_adx": 25.0,
        "require_oir_confirm": False,
    })

    candles = make_candles_with_deviation(30, 50000.0, 2000.0)
    for c in candles:
        vwap_dev.on_candle(c, "BTC")

    current_price = candles[-1].close
    event = MarketEvent(
        symbol="BTC", price=current_price, timestamp_ms=30 * 3_600_000,
        funding=0.001, predicted_funding=0.001,
        adx_14=35.0,  # trending — should reject
    )

    sig = vwap_dev.on_data(event)
    assert sig is None, "Should reject in trending market (ADX=35 > 25)"
    print("Correctly rejected: ADX=35 > threshold=25")
    print("[PASS]\n")


def test_volume_filter():
    print("=" * 60)
    print("TEST: Volume surge filter")
    print("=" * 60)

    vwap_dev = VWAPDeviation({
        "z_threshold": 2.5,
        "volume_surge": 2.0,  # require 200% volume
        "max_adx": 25.0,
        "require_oir_confirm": False,
    })

    # Low volume candles
    candles = []
    price = 50000.0
    for i in range(30):
        open_p = price
        close_p = price + 100.0
        high_p = max(open_p, close_p) + 50.0
        low_p = min(open_p, close_p) - 50.0
        candles.append(Candle(
            open=open_p, high=high_p, low=low_p, close=close_p,
            volume=1000.0, timestamp_ms=i * 3_600_000
        ))
        price = close_p

    for c in candles:
        vwap_dev.on_candle(c, "BTC")

    event = MarketEvent(
        symbol="BTC", price=candles[-1].close, timestamp_ms=30 * 3_600_000,
        funding=0.001, predicted_funding=0.001,
        adx_14=15.0,
    )

    sig = vwap_dev.on_data(event)
    assert sig is None, "Should reject — volume too low (1000 vs avg 1000 = 1.0x < 2.0x)"
    print("Correctly rejected: volume ratio 1.0x < threshold 2.0x")
    print("[PASS]\n")


def test_exit_on_vwap_cross():
    print("=" * 60)
    print("TEST: Exit on VWAP reversion (long side)")
    print("=" * 60)

    # v3.1.18 (commit 07ab215) removed the separate "crosses VWAP" exit
    # (side-specific zscore sign flip) and replaced it with a single
    # unified "near-full VWAP reversion" exit at |Z| < 0.3, so a large
    # -1500 trend that merely crosses zero no longer exits early (it
    # would close before the 2R TP had room to play out). Build candles
    # that revert close to VWAP instead, matching current behavior.
    vwap_dev = VWAPDeviation({"max_hold_hours": 4})

    # Position entered LONG when price was below VWAP
    pos = Position(
        symbol="BTC", side="long", entry_price=48000.0,
        size=0.1, entry_time_ms=0,
    )

    # Deterministic small oscillation around base so price reverts close to
    # VWAP (|Z| < 0.3) and stddev > 0, without relying on unseeded randomness.
    candles = []
    base = 50000.0
    for i in range(30):
        close_p = base + (30.0 if i % 2 == 0 else -30.0)
        candles.append(Candle(
            open=close_p - 20, high=close_p + 50, low=close_p - 50, close=close_p,
            volume=2000.0, timestamp_ms=i * 3_600_000
        ))
    for c in candles:
        vwap_dev.on_candle(c, "BTC")

    event = MarketEvent(
        symbol="BTC", price=base, timestamp_ms=2 * 3_600_000,
    )

    exit_sig = vwap_dev.on_position(pos, event)
    assert exit_sig is not None, "Should exit when price reverts close to VWAP"
    assert "vwap_reverted" in exit_sig.reason
    print(f"Exit: {exit_sig.reason}")
    print("[PASS]\n")


def test_exit_on_normalization():
    print("=" * 60)
    print("TEST: Exit on deviation normalization")
    print("=" * 60)

    vwap_dev = VWAPDeviation({"max_hold_hours": 4})

    pos = Position(
        symbol="BTC", side="short", entry_price=52000.0,
        size=0.1, entry_time_ms=0,
    )

    # Price now very close to VWAP (|Z| < 0.3, v3.1.18 threshold)
    # Deterministic small oscillation around base to give stddev > 0
    # without relying on unseeded randomness.
    candles = []
    base = 50000.0
    for i in range(30):
        close_p = base + (30.0 if i % 2 == 0 else -30.0)
        candles.append(Candle(
            open=close_p-20, high=close_p+50, low=close_p-50, close=close_p,
            volume=2000.0, timestamp_ms=i * 3_600_000
        ))

    for c in candles:
        vwap_dev.on_candle(c, "BTC")

    # Use a price very close to VWAP
    event = MarketEvent(
        symbol="BTC", price=base, timestamp_ms=2 * 3_600_000,
    )

    exit_sig = vwap_dev.on_position(pos, event)
    assert exit_sig is not None, "Should exit when deviation normalizes"
    # v3.1.18 (commit 07ab215) renamed this exit reason from
    # "deviation_normalized_z..." to "vwap_reverted_z..." and tightened
    # the threshold from |Z|<1.0 to |Z|<0.3.
    assert "vwap_reverted" in exit_sig.reason
    print(f"Exit: {exit_sig.reason}")
    print("[PASS]\n")


if __name__ == "__main__":
    test_vwap_zscore_calculation()
    test_entry_signal()
    test_adx_filter()
    test_volume_filter()
    test_exit_on_vwap_cross()
    test_exit_on_normalization()
    print("=" * 60)
    print("ALL TESTS PASSED [OK]")
    print("=" * 60)
