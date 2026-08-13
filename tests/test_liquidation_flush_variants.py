"""Unit tests for scripts/liquidation_flush_variants.py.

Pins the variant mechanics to the v2 simulation: the baseline (p90 filter,
1st-bar entry, hold 30m, fade) must reproduce the simulation's cell
exactly, and the sweep variants must apply their stated modifications.
"""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.liquidation_flush_variants import (  # noqa: E402
    entry_index,
    flush_events,
    percentile,
    simulate_hold,
    simulate_trail,
    summarize,
)


def _flush(minute_ms: int, side: str, notional: float) -> dict:
    return {"minute_ms": minute_ms, "dominant_side": side, "notional": notional}


def _candles(seq):
    """Build a candles dict from a list of (open, high, low, close).
    Timestamps at minute END (ts % 60_000 == 59_999), contiguous 1m."""
    out = {}
    for i, (o, h, l, c) in enumerate(seq):
        ts = i * 60_000 + 59_999
        out[ts] = (o, h, l, c)
    return out


class TestEntryIndex:
    def test_first_bar_after_flush_minute(self):
        ts_list = [i * 60_000 + 59_999 for i in range(10)]
        # flush minute m=2 closes at m*60k+60k=180_000 -> first candle ts>=180k is i=3
        assert entry_index(ts_list, 120_000, 0) == 3
        assert entry_index(ts_list, 120_000, 1) == 4  # 2nd bar

    def test_delay_beyond_available_candles(self):
        ts_list = [i * 60_000 + 59_999 for i in range(4)]
        # flush at m=3, 1st bar would be i=4 which is out of range
        assert entry_index(ts_list, 180_000, 0) == 4


class TestFlushEvents:
    def test_dominant_side_bucketing(self):
        events = [
            (120_000, 100.0, "long"),
            (121_000, 200.0, "short"),
            (125_000, 50.0, "long"),   # same minute m=2
            (180_000, 900.0, "short"),
        ]
        fl = flush_events(events)
        assert len(fl) == 2
        assert fl[0]["minute_ms"] == 120_000
        assert fl[0]["dominant_side"] == "short"   # 200 > 150
        assert fl[0]["notional"] == 200.0
        assert fl[1]["dominant_side"] == "short"


class TestBaselineParity:
    """The baseline cell must match the v2 simulation on synthetic data:
    p90 filter, 1st-bar entry, hold 30, fade, fees 0.09% RT."""

    def test_hold_fade_long_on_long_flush(self):
        # flush dominant=long -> fade = LONG. Prices drift up after entry -> win.
        candles = _candles([(100.0, 100.0, 100.0, 100.0)] * 5
                           + [(100.0, 101.0, 100.0, 101.0)] * 40)
        # flush at minute m=0, notional above any p90 we choose
        flushes = [_flush(0, "long", 1000.0)]
        trades = simulate_hold(flushes, candles, 5, "fade", 0)
        assert len(trades) == 1
        assert trades[0]["side"] == "long"
        assert trades[0]["net_pct"] > 0
        # ~ (101/100 - 1)*100 - 0.09 = 0.91
        assert abs(trades[0]["net_pct"] - 0.91) < 1e-6

    def test_hold_fade_short_on_short_flush(self):
        # flush dominant=short -> fade = SHORT. Prices fall after entry -> win.
        candles = _candles([(100.0, 100.0, 100.0, 100.0)] * 5
                           + [(100.0, 100.0, 99.0, 99.0)] * 40)
        flushes = [_flush(0, "short", 1000.0)]
        trades = simulate_hold(flushes, candles, 5, "fade", 0)
        assert len(trades) == 1
        assert trades[0]["side"] == "short"
        assert trades[0]["net_pct"] > 0

    def test_hold_exit_at_close_of_hold_bar(self):
        # Exit = close of candle at entry index + hold. Flat market -> fees only.
        candles = _candles([(100.0, 100.0, 100.0, 100.0)] * 40)
        flushes = [_flush(0, "long", 1000.0)]
        trades = simulate_hold(flushes, candles, 10, "fade", 0)
        assert len(trades) == 1
        assert abs(trades[0]["net_pct"] + 0.09) < 1e-6  # -fees


class TestIntensity:
    def test_threshold_reduces_sample(self):
        flushes = [_flush(i * 60_000, "long", 1000.0 * (i + 1)) for i in range(5)]
        thr = 3000.0
        sel = [f for f in flushes if f["notional"] >= thr]
        assert len(sel) == 3  # 3000, 4000, 5000

    def test_percentile_matches_reference(self):
        vals = [1.0, 2.0, 3.0, 4.0]
        # p90 of 4 values: k=2.7 -> s[2] + (s[3]-s[2])*0.7 = 3.7
        assert abs(percentile(vals, 0.90) - 3.7) < 1e-9


class TestTrailing:
    def test_trail_exits_before_max_hold_when_price_reverses(self):
        # Flush minute m=0 (candle 0 flat); entry at open of candle 1 = 100.
        # Prices rise to 104 (peak, stop 102.96) without touching the trail,
        # then bar 3's low 102.5 breaks it -> exit at 102.96.
        candles = _candles(
            [(100.0, 100.0, 100.0, 100.0),               # flush minute
             (100.0, 102.0, 101.5, 101.5),               # entry open 100, low above stop
             (101.5, 104.0, 103.0, 103.5),               # peak 104 -> stop 102.96
             (103.5, 103.8, 102.5, 103.0)]               # low 102.5 <= 102.96 -> trail
        )
        flushes = [_flush(0, "long", 1000.0)]
        trades = simulate_trail(flushes, candles, 0.01, 120)
        assert len(trades) == 1
        assert trades[0]["exit_reason"] == "trail"
        # exit at 102.96 -> ret = 2.96% - 0.09% fees
        assert abs(trades[0]["net_pct"] - (2.96 - 0.09)) < 1e-6

    def test_trail_max_hold_fallback(self):
        # No reversal: trail never triggers, exit at max_hold close.
        candles = _candles([(100.0, 101.0, 100.0, 101.0)] * 40)
        flushes = [_flush(0, "long", 1000.0)]
        trades = simulate_trail(flushes, candles, 0.01, 5)
        assert len(trades) == 1
        assert trades[0]["exit_reason"] == "max_hold"

    def test_trail_short_reverses_up(self):
        # Short entry at 100. Prices fall to 97 (peak low) then rise above
        # stop 97.97 (trail 1%). Exit at 97.97 -> ret = 100/97.97 - 1 = 2.07%
        candles = _candles(
            [(100.0, 100.0, 100.0, 100.0)] * 5
            + [(100.0, 100.0, 98.0, 99.0),
               (99.0, 99.5, 97.0, 98.0),
               (98.0, 98.5, 97.5, 98.0)]
        )
        flushes = [_flush(0, "short", 1000.0)]
        trades = simulate_trail(flushes, candles, 0.01, 120)
        assert len(trades) == 1
        assert trades[0]["exit_reason"] == "trail"
        assert trades[0]["side"] == "short"
        assert trades[0]["net_pct"] > 0


class TestSummarize:
    def test_stats(self):
        trades = [{"net_pct": 1.0}, {"net_pct": -0.5}, {"net_pct": 0.2}]
        s = summarize(trades)
        assert s["n"] == 3
        assert s["win_rate"] == 66.7
        assert s["avg_net_bps"] == pytest.approx(23.3, abs=0.1)
