"""VWAP stats memoization — vwap/stddev depend only on the 1h candle set.

Profile 2026-09-11 (14d backtest): calculate_vwap_zscore = 5.7s tottime,
87k calls — recomputed per 1m event though candles_1h changes only on the
hourly close. The z-score also needs the live price, so the cache stores
(vwap, stddev) and on_data derives z per event.
"""
from __future__ import annotations

import pytest

import src.strategies.vwap_deviation as vd
from src.strategies.indicators import Candle
from src.strategies.vwap_deviation import VWAPDeviation

pytestmark = pytest.mark.unit

T0 = 1_700_000_000_000
H1 = 3_600_000


def _c(ts: int, close: float = 100.0) -> Candle:
    return Candle(open=close, high=close + 1, low=close - 1, close=close,
                  volume=10.0, timestamp_ms=ts)


def _hist(n: int = 30) -> list[Candle]:
    return [_c(T0 + i * H1, 100.0 + i * 0.01) for i in range(n)]


def test_vwap_stats_computed_once_per_candle_set(monkeypatch):
    s = VWAPDeviation({})
    state = s._get_state("BTC")
    calls: list[int] = []

    def fake(candles, price, lookback=24):
        calls.append(len(candles))
        return 100.0, 2.0, 0.0

    monkeypatch.setattr(vd, "calculate_vwap_zscore", fake)

    hist = _hist()
    # Same candle set, different live prices -> stats computed once.
    for price in (99.0, 100.0, 101.0, 105.0):
        assert s._vwap_stats(state, hist, price) == (100.0, 2.0)
    assert calls == [30]

    # New hourly candle -> recompute.
    hist.append(_c(T0 + 30 * H1))
    s._vwap_stats(state, hist, 100.0)
    assert calls == [30, 31]


def test_vwap_stats_insufficient_data(monkeypatch):
    s = VWAPDeviation({})
    state = s._get_state("BTC")
    monkeypatch.setattr(
        vd, "calculate_vwap_zscore",
        lambda candles, price, lookback=24: (None, None, None),
    )
    hist = _hist(30)
    assert s._vwap_stats(state, hist, 100.0) == (None, None)
    # Cached Nones must not mask a recomputation on a changed set.
    hist.append(_c(T0 + 30 * H1))
    s._vwap_stats(state, hist, 100.0)


def test_zscore_matches_batch_for_same_inputs():
    """Cached stats + per-event z must equal the batch result exactly."""
    s = VWAPDeviation({})
    state = s._get_state("BTC")
    hist = _hist(30)
    vwap, stddev = s._vwap_stats(state, hist, 105.0)
    batch_v, batch_s, batch_z = vd.calculate_vwap_zscore(hist, 105.0, lookback=24)
    assert vwap == batch_v and stddev == batch_s
    z = (105.0 - vwap) / stddev
    assert z == pytest.approx(batch_z)
