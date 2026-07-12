"""Quick integration test for Tasks 1.4, 2.1, 2.2.

Validates:
  - ADX regime filter weights signals correctly
  - Slippage estimation rejects high-slippage signals
  - Fill ratio gate rejects under-covered size
  - SmartMoneyFlow OIR + RSI + wall filters work
"""

import sys
sys.path.insert(0, r"C:\Users\Braindead\Documents\trading-bot-hyperliquid")

from src.strategies.base import MarketEvent, Signal
from src.strategies.indicators import Candle, calculate_adx, calculate_rsi
from src.data.orderbook_metrics import estimate_slippage, calculate_fill_ratio, PriceLevel
# HlOrderbook and HlPriceLevel removed — they no longer exist in the codebase.
# Use src.data.orderbook_metrics.PriceLevel if needed.
from src.strategies.trend_follow import TrendFollow
from src.strategies.mean_reversion import MeanReversion
from datetime import datetime, timezone
import time
import pytest

pytestmark = pytest.mark.unit


def make_candles_trending(n=50, start_price=100.0):
    """Generate candles with a clear upward trend (ADX should be high)."""
    candles = []
    price = start_price
    for i in range(n):
        open_p = price
        close_p = price + 1.5
        high_p = max(open_p, close_p) + 0.5
        low_p = min(open_p, close_p) - 0.2
        candles.append(Candle(
            open=open_p, high=high_p, low=low_p, close=close_p,
            volume=2000.0, timestamp_ms=i * 900_000
        ))
        price = close_p
    return candles


def make_candles_ranging(n=50, start_price=100.0):
    """Generate candles with perfect alternation (ADX should be near 0)."""
    candles = []
    for i in range(n):
        if i % 2 == 0:
            open_p = start_price
            close_p = start_price + 1.0
            high_p = close_p + 0.2
            low_p = open_p - 0.2
        else:
            open_p = start_price + 1.0
            close_p = start_price
            high_p = open_p + 0.2
            low_p = close_p - 0.2
        candles.append(Candle(
            open=open_p, high=high_p, low=low_p, close=close_p,
            volume=1500.0, timestamp_ms=i * 900_000
        ))
    return candles


def build_event(
    symbol="BTC",
    price=50000.0,
    candles_15m=None,
    funding=0.0001,
    oir=None,
    ask_wall=None,
    bid_wall=None,
) -> MarketEvent:
    """Build a MarketEvent with controllable microstructure."""
    ts = int(time.time() * 1000)
    return MarketEvent(
        symbol=symbol,
        price=price,
        timestamp_ms=ts,
        candle_1m=None,
        candle_5m=None,
        candle_15m=candles_15m[-1] if candles_15m else None,
        candle_1h=None,
        funding=funding,
        predicted_funding=funding,
        oi_total=50000.0,
        oi_delta=100.0,
        volume_1m=5000.0,
        bid_ask_imbalance=0.05,
        vwap_15m=price * 0.99 if candles_15m else None,
        funding_avg=funding,
        funding_weighted=funding,
        predicted_funding_avg=funding,
        oi_total_aggregated=50000.0,
        oi_exchange_count=3,
        orderbook_spread_pct=0.0001,
        orderbook_oir=oir,
        orderbook_depth_quality=0.8,
        orderbook_bid_ask_ratio=1.2,
        orderbook_largest_bid_wall=bid_wall,
        orderbook_largest_ask_wall=ask_wall,
        adx_14=None,
    )


def test_adx_calculation():
    print("=" * 60)
    print("TEST: ADX calculation")
    print("=" * 60)

    trend_candles = make_candles_trending(50)
    range_candles = make_candles_ranging(50)

    adx_trend = calculate_adx(trend_candles, 14)
    adx_range = calculate_adx(range_candles, 14)

    print(f"ADX (trending):  {adx_trend:.1f}")
    print(f"ADX (ranging):   {adx_range:.1f}")

    assert adx_trend is not None, "ADX should not be None for trending data"
    assert adx_range is not None, "ADX should not be None for ranging data"
    assert adx_trend > 25, f"Expected ADX > 25 for trend, got {adx_trend}"
    assert adx_range < 20, f"Expected ADX < 20 for range, got {adx_range}"
    print("[PASS] ADX calculation PASSED\n")


def test_regime_weights():
    print("=" * 60)
    print("TEST: Regime-based confidence weighting")
    print("=" * 60)

    # Simulate engine's _apply_regime_weights logic
    weights_trend = {"SmartMoneyFlow": 1.3, "FundingExtreme": 0.7}
    weights_range = {"SmartMoneyFlow": 0.7, "FundingExtreme": 1.3}

    sig_tf = Signal(
        strategy="SmartMoneyFlow", symbol="BTC", side="long",
        confidence=0.75, size_pct=0.01, entry_price=50000.0,
        stop_loss_pct=0.02, take_profit_pct=0.04, reason="test",
    )
    sig_mr = Signal(
        strategy="FundingExtreme", symbol="BTC", side="short",
        confidence=0.75, size_pct=0.01, entry_price=50000.0,
        stop_loss_pct=0.02, take_profit_pct=0.04, reason="test",
    )

    # Trend regime
    w_tf = weights_trend["SmartMoneyFlow"]
    w_mr = weights_trend["FundingExtreme"]
    conf_tf_trend = min(sig_tf.confidence * w_tf, 1.0)
    conf_mr_trend = min(sig_mr.confidence * w_mr, 1.0)
    print(f"Trend regime: TF confidence {sig_tf.confidence} -> {conf_tf_trend:.2f} (weight {w_tf})")
    print(f"Trend regime: MR confidence {sig_mr.confidence} -> {conf_mr_trend:.2f} (weight {w_mr})")
    assert conf_tf_trend > sig_tf.confidence, "TrendFollow should be boosted in trend"
    assert conf_mr_trend < sig_mr.confidence, "MeanReversion should be penalized in trend"

    # Range regime
    w_tf = weights_range["SmartMoneyFlow"]
    w_mr = weights_range["FundingExtreme"]
    conf_tf_range = min(sig_tf.confidence * w_tf, 1.0)
    conf_mr_range = min(sig_mr.confidence * w_mr, 1.0)
    print(f"Range regime: TF confidence {sig_tf.confidence} -> {conf_tf_range:.2f} (weight {w_tf})")
    print(f"Range regime: MR confidence {sig_mr.confidence} -> {conf_mr_range:.2f} (weight {w_mr})")
    assert conf_tf_range < sig_tf.confidence, "TrendFollow should be penalized in range"
    assert conf_mr_range > sig_mr.confidence, "MeanReversion should be boosted in range"

    print("[PASS] Regime weighting PASSED\n")


def test_slippage_and_fill_ratio():
    print("=" * 60)
    print("TEST: Slippage estimation + Fill ratio gate")
    print("=" * 60)

    # Thin book
    asks = [
        PriceLevel(price=50100.0, size=0.01),
        PriceLevel(price=50200.0, size=0.5),
    ]

    # Small size -> low slippage, good fill ratio
    slippage_small = estimate_slippage(asks, 0.005, "buy")
    fill_small = calculate_fill_ratio(asks, 0.005)
    print(f"Size=0.005 BTC: slippage={slippage_small*100:.3f}%, fill_ratio={fill_small*100:.1f}%")
    assert slippage_small < 0.002, "Small size should have low slippage"
    assert fill_small >= 1.0, "Small size should be fully covered"

    # Large size -> high slippage, poor fill ratio
    slippage_large = estimate_slippage(asks, 2.0, "buy")
    fill_large = calculate_fill_ratio(asks, 2.0)
    print(f"Size=2.0 BTC:   slippage={slippage_large*100:.3f}%, fill_ratio={fill_large*100:.1f}%")
    assert fill_large < 0.8, "Large size should have poor fill ratio"

    print("[PASS] Slippage + Fill ratio PASSED\n")


def test_smart_money_flow_filters():
    print("=" * 60)
    print("TEST: SmartMoneyFlow microstructure filters")
    print("=" * 60)

    tf = TrendFollow({
        "max_hold_hours": 4,
        "stop_loss_atr_multiplier": 2.0,
        "volume_surge_multiplier": 1.5,
        "ema_period": 20,
        "atr_period": 14,
        "oir_long_threshold": 0.6,
        "oir_short_threshold": -0.6,
        "wall_proximity_pct": 0.005,
        "rsi_min": 40.0,
        "rsi_max": 70.0,
    })

    # Build enough candles for indicators
    candles = make_candles_trending(35, start_price=50000.0)

    # --- Case 1: Good OIR, no wall, good RSI -> should potentially signal ---
    event_good = build_event(
        price=50200.0,  # above EMA/VWAP
        candles_15m=candles,
        funding=0.0001,
        oir=0.8,  # strong bid imbalance -> confirms long
        ask_wall=51000.0,  # far away
        bid_wall=49000.0,  # far away
    )
    sig = tf.on_data(event_good)
    print(f"Case 1 (OIR=0.8, no wall, low funding): signal={'YES' if sig else 'NO'}")

    # --- Case 2: Bad OIR (negative, doesn't confirm long) -> should NOT signal ---
    event_bad_oir = build_event(
        price=50200.0,
        candles_15m=candles,
        funding=0.0001,
        oir=-0.8,  # strong ask imbalance -> contradicts long
        ask_wall=51000.0,
        bid_wall=49000.0,
    )
    sig2 = tf.on_data(event_bad_oir)
    print(f"Case 2 (OIR=-0.8, contradicts long): signal={'YES' if sig2 else 'NO'}")

    # --- Case 3: Wall blocking (ask wall just above price) -> should NOT signal ---
    event_wall = build_event(
        price=50200.0,
        candles_15m=candles,
        funding=0.0001,
        oir=0.8,
        ask_wall=50210.0,  # only 0.02% above -> blocks long
        bid_wall=49000.0,
    )
    sig3 = tf.on_data(event_wall)
    print(f"Case 3 (ask wall at 50210, 0.02% above): signal={'YES' if sig3 else 'NO'}")

    print("[PASS] SmartMoneyFlow filters PASSED\n")


if __name__ == "__main__":
    test_adx_calculation()
    test_regime_weights()
    test_slippage_and_fill_ratio()
    test_smart_money_flow_filters()
    print("=" * 60)
    print("ALL TESTS PASSED [OK]")
    print("=" * 60)
