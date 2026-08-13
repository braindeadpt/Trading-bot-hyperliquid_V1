"""Unit tests for scripts/vb_long_followthrough_variant.py.

Pins the follow-through mechanics on the forensic CSV: BB band parity with the
strategy implementation, breakout-signal reproduction, confirmation-candle
semantics, and the delayed-entry PnL adjustment.
"""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.vb_long_followthrough_variant as v  # noqa: E402
from src.strategies.indicators import calculate_bollinger_bands  # noqa: E402


def test_bollinger_bands_matches_strategy_implementation():
    prices = [100.0 + (i % 7) * 0.5 for i in range(30)]
    a = v.bollinger_bands(prices)
    b = calculate_bollinger_bands(prices, v.BB_PERIOD, v.BB_STD)
    assert a[0] == pytest.approx(b[0])
    assert a[1] == pytest.approx(b[1])
    assert a[2] == pytest.approx(b[2])


def test_bollinger_bands_requires_period():
    assert v.bollinger_bands([1.0] * 10) == (None, None, None)


def _candles(close_values, open_values=None):
    """Build open-stamped 15m bars (ts, open, high, low, close)."""
    opens = open_values or close_values
    bars = []
    for i, c in enumerate(close_values):
        o = opens[i]
        bars.append((i * 900_000, o, max(o, c) + 0.1, min(o, c) - 0.1, c))
    return bars


def _trade(side, entry_price, exit_price, pnl_pct, pnl_usd, entry_ts, exit_reason="x", regime="trend"):
    return {
        "entry_time": entry_ts, "symbol": "BTC", "side": side,
        "entry_price": entry_price, "exit_price": exit_price,
        "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "r_multiple": 0.0,
        "exit_reason": exit_reason, "regime": regime, "adx": None, "hold_min": 60.0,
    }


def test_enrich_confirms_follow_through_long():
    # 40 bars; entry bar at index 20 (close 110 > band ~100), confirm bar 21
    # closes 111 (beyond band) -> FT True. Bars are flat 100 before the break.
    closes = [100.0] * 20 + [110.0, 111.0, 112.0] + [112.0] * 17
    bars = {"BTC": _candles(closes)}
    t = _trade("long", 110.0, 115.0, 4.5, 10.0, entry_ts=20 * 900_000)
    (out, ok, repro), = (v.enrich([t], bars),)
    assert ok == 1 and repro == 1
    assert out[0]["_ft"] is True


def test_enrich_rejects_failed_confirmation():
    # Entry bar 20 closes 110 beyond band; confirm bar 21 closes back BELOW
    # the band (99) -> the candle after the breakout did NOT hold -> FT False.
    closes = [100.0] * 20 + [110.0, 99.0, 100.0] + [100.0] * 17
    bars = {"BTC": _candles(closes)}
    t = _trade("long", 110.0, 105.0, -4.5, -10.0, entry_ts=20 * 900_000)
    out, ok, repro = v.enrich([t], bars)
    assert ok == 1 and repro == 1
    assert out[0]["_ft"] is False


def test_enrich_short_follow_through_direction():
    # Short: band ~100; entry bar closes 90 (below band), confirm bar 91
    # (still below band) -> FT True for the short.
    closes = [100.0] * 20 + [90.0, 91.0, 92.0] + [92.0] * 17
    bars = {"BTC": _candles(closes)}
    t = _trade("short", 90.0, 85.0, 5.5, 12.0, entry_ts=20 * 900_000)
    out, ok, repro = v.enrich([t], bars)
    assert ok == 1 and repro == 1
    assert out[0]["_ft"] is True


def test_enrich_delayed_entry_pnl_adjustment_long():
    # Entry close 110, exit 115 -> orig pnl% +4.5%. Delayed open (bar 22) is
    # 112 (higher -> worse for long): adjusted pnl% = (115-112)/112 = +2.68.
    closes = [100.0] * 20 + [110.0, 111.0, 112.0] + [112.0] * 17
    bars = {"BTC": _candles(closes)}
    t = _trade("long", 110.0, 115.0, 4.5, 10.0, entry_ts=20 * 900_000)
    out, ok, _ = v.enrich([t], bars)
    assert out[0]["_delayed_entry"] == pytest.approx(112.0)
    expected = 10.0 * ((115.0 - 112.0) / 112.0 * 100.0) / 4.5
    assert out[0]["_pnl_usd_delay"] == pytest.approx(expected)
    # gap is positive when the delayed entry is higher for a long
    assert out[0]["_entry_gap_pct"] > 0


def test_enrich_gap_sign_inverts_for_short():
    # Short entry close 90, delayed open 88 (lower -> better for short):
    # gap (o_n2 - entry)/entry * sgn must be POSITIVE (good fill).
    closes = [100.0] * 20 + [90.0, 91.0, 88.0] + [88.0] * 17
    bars = {"BTC": _candles(closes)}
    t = _trade("short", 90.0, 85.0, 5.5, 12.0, entry_ts=20 * 900_000)
    out, ok, _ = v.enrich([t], bars)
    assert out[0]["_entry_gap_pct"] > 0
    assert out[0]["_delayed_entry"] == pytest.approx(88.0)


def test_enrich_skips_trades_without_context():
    t = _trade("long", 110.0, 115.0, 4.5, 10.0, entry_ts=999_999_999_999)
    bars = {"BTC": _candles([100.0] * 40)}
    out, ok, repro = v.enrich([t], bars)
    assert ok == 0 and repro == 0
    assert out[0]["_ft"] is None and out[0]["_delayed_entry"] is None
