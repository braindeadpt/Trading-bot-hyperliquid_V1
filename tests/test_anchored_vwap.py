"""Unit tests for calculate_anchored_vwap / calculate_anchored_vwap_series.

Pure tests (no DB, no network). Run with:
    python -m pytest tests/test_anchored_vwap.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from src.strategies.indicators import (  # noqa: E402
    Candle,
    calculate_anchored_vwap,
    calculate_anchored_vwap_series,
)

pytestmark = pytest.mark.unit

# 2026-05-18 00:00 UTC
DAY0_MS = 1_747_526_400_000
# 2026-05-19 00:00 UTC
DAY1_MS = DAY0_MS + 86_400_000
HOUR_MS = 3_600_000


def _c(
    close: float,
    volume: float,
    ts: int,
    *,
    high: float | None = None,
    low: float | None = None,
) -> Candle:
    h = high if high is not None else close
    lo = low if low is not None else close
    return Candle(
        open=close, high=h, low=lo, close=close,
        volume=volume, timestamp_ms=ts,
    )


def test_empty_returns_none():
    assert calculate_anchored_vwap([]) is None
    assert calculate_anchored_vwap_series([]) == []


def test_unsupported_anchor_returns_none():
    candles = [_c(100.0, 10.0, DAY0_MS)]
    assert calculate_anchored_vwap(candles, anchor="session_open") is None
    series = calculate_anchored_vwap_series(candles, anchor="session_open")
    assert series == [None]


def test_single_candle_equals_typical_price():
    # typical = (110 + 90 + 100) / 3 = 100
    c = Candle(open=100, high=110, low=90, close=100, volume=50, timestamp_ms=DAY0_MS)
    assert calculate_anchored_vwap([c]) == pytest.approx(100.0)


def test_same_day_volume_weighted():
    """Two bars same UTC day: VWAP = Σ(tp·v)/Σv."""
    # tp1=100, v=10; tp2=200, v=30 → (1000+6000)/40 = 175
    candles = [
        _c(100.0, 10.0, DAY0_MS + HOUR_MS),
        _c(200.0, 30.0, DAY0_MS + 2 * HOUR_MS),
    ]
    assert calculate_anchored_vwap(candles) == pytest.approx(175.0)


def test_zero_volume_returns_none():
    candles = [_c(100.0, 0.0, DAY0_MS), _c(110.0, 0.0, DAY0_MS + HOUR_MS)]
    assert calculate_anchored_vwap(candles) is None


def test_resets_at_utc_midnight():
    """Day-0 volume must NOT leak into day-1 VWAP."""
    candles = [
        _c(100.0, 100.0, DAY0_MS + HOUR_MS),       # day 0, heavy volume
        _c(100.0, 100.0, DAY0_MS + 23 * HOUR_MS),  # day 0
        _c(200.0, 10.0, DAY1_MS + HOUR_MS),        # day 1 only
    ]
    # Current (last) session = day 1 → VWAP = 200
    assert calculate_anchored_vwap(candles) == pytest.approx(200.0)

    series = calculate_anchored_vwap_series(candles)
    assert len(series) == 3
    assert series[0] == pytest.approx(100.0)
    assert series[1] == pytest.approx(100.0)  # still day 0, equal prices
    assert series[2] == pytest.approx(200.0)  # reset


def test_series_accumulates_within_day():
    candles = [
        _c(100.0, 10.0, DAY0_MS),
        _c(110.0, 10.0, DAY0_MS + HOUR_MS),
        _c(120.0, 10.0, DAY0_MS + 2 * HOUR_MS),
    ]
    series = calculate_anchored_vwap_series(candles)
    assert series[0] == pytest.approx(100.0)
    assert series[1] == pytest.approx(105.0)  # (1000+1100)/20
    assert series[2] == pytest.approx(110.0)  # (1000+1100+1200)/30


def test_ignores_prior_days_in_scalar():
    """Scalar API uses only the last candle's UTC day."""
    candles = [
        _c(50.0, 1000.0, DAY0_MS),
        _c(150.0, 10.0, DAY1_MS),
        _c(250.0, 10.0, DAY1_MS + HOUR_MS),
    ]
    # day1: (150*10 + 250*10) / 20 = 200
    assert calculate_anchored_vwap(candles, anchor="utc_day") == pytest.approx(200.0)


def test_boundary_just_before_and_after_midnight():
    just_before = DAY1_MS - 1
    just_after = DAY1_MS
    candles = [
        _c(100.0, 10.0, just_before),
        _c(300.0, 10.0, just_after),
    ]
    series = calculate_anchored_vwap_series(candles)
    assert series[0] == pytest.approx(100.0)
    assert series[1] == pytest.approx(300.0)  # new day, reset
    assert calculate_anchored_vwap(candles) == pytest.approx(300.0)
