"""Unit tests for the v3.1.15 volume indicators.

Covers:
  - calculate_obv: classic up/down/flat, insufficient data
  - calculate_obv_slope: divergence detection across lookback
  - calculate_mfi: bullish/bearish range, all-rising, all-falling,
    no-flow, insufficient data
  - calculate_vwap_multi_tf: empty, single-tf, multi-tf, zero-volume

All tests are pure (no DB, no network, no I/O). Run with:
    python tests/test_volume_indicators.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.strategies.indicators import (  # noqa: E402
    Candle,
    calculate_mfi,
    calculate_obv,
    calculate_obv_slope,
    calculate_vwap_multi_tf,
)


def make_candle(open_: float, high: float, low: float, close: float,
                volume: float, ts: int = 0) -> Candle:
    return Candle(open=open_, high=high, low=low, close=close,
                  volume=volume, timestamp_ms=ts)


# ════════════════════════════════════════════════════════════════
# OBV
# ════════════════════════════════════════════════════════════════
def test_obv_classic_trend():
    """3 up candles -> OBV = sum of volumes from 2nd candle (1st is seed)."""
    candles = [
        make_candle(100, 101, 99, 100, 10, ts=1),
        make_candle(100, 102, 100, 101, 20, ts=2),
        make_candle(101, 103, 101, 102, 30, ts=3),
        make_candle(102, 104, 102, 103, 40, ts=4),
    ]
    obv = calculate_obv(candles)
    # First candle is the seed (no comparison possible). Subsequent up
    # bars add their volume: 20 + 30 + 40 = 90.
    assert obv == 20 + 30 + 40, f"expected 90, got {obv}"


def test_obv_mixed_direction():
    """Up, down, up, down -> signed sum."""
    candles = [
        make_candle(100, 101, 99, 100, 10, ts=1),
        make_candle(100, 102, 99, 101, 20, ts=2),    # up   +20
        make_candle(101, 101, 99, 99, 30, ts=3),     # down -30
        make_candle(99, 101, 99, 100, 40, ts=4),     # up   +40
        make_candle(100, 100, 98, 98, 50, ts=5),     # down -50
    ]
    obv = calculate_obv(candles)
    assert obv == 20 - 30 + 40 - 50, f"expected -20, got {obv}"


def test_obv_flat_candles_ignored():
    """Close == prev close -> OBV unchanged for that bar."""
    candles = [
        make_candle(100, 101, 99, 100, 10, ts=1),
        make_candle(100, 101, 99, 100, 20, ts=2),    # flat
        make_candle(100, 102, 99, 101, 30, ts=3),    # up
    ]
    obv = calculate_obv(candles)
    assert obv == 0 + 30, f"expected 30, got {obv}"


def test_obv_insufficient_data():
    """0 or 1 candle -> None."""
    assert calculate_obv([]) is None
    candles = [make_candle(100, 101, 99, 100, 10, ts=1)]
    assert calculate_obv(candles) is None


# ════════════════════════════════════════════════════════════════
# OBV SLOPE (divergence helper)
# ════════════════════════════════════════════════════════════════
def test_obv_slope_positive_bullish():
    """Price rising + rising volume -> positive slope."""
    candles = []
    for i in range(20):
        vol = 100 + i * 10  # increasing volume
        candles.append(make_candle(100 + i, 100 + i + 0.5, 100 + i - 0.5,
                                   100 + i + 0.3, vol, ts=i))
    slope = calculate_obv_slope(candles, lookback=14)
    assert slope is not None and slope > 0, f"expected positive, got {slope}"


def test_obv_slope_bearish_divergence():
    """Price up but volume declining -> slope should be <= 0."""
    candles = []
    for i in range(20):
        vol = 100 - i * 5  # declining volume
        candles.append(make_candle(100 + i, 100 + i + 0.5, 100 + i - 0.5,
                                   100 + i + 0.3, vol, ts=i))
    slope = calculate_obv_slope(candles, lookback=14)
    assert slope is not None and slope <= 0, f"expected <=0, got {slope}"


def test_obv_slope_insufficient_data():
    """Need lookback + 1 candles."""
    candles = [make_candle(100, 101, 99, 100, 10, ts=i) for i in range(5)]
    assert calculate_obv_slope(candles, lookback=14) is None


# ════════════════════════════════════════════════════════════════
# MFI
# ════════════════════════════════════════════════════════════════
def test_mfi_all_rising_bullish():
    """TP strictly rising every bar -> MFI = 100 (no negative flow)."""
    candles = []
    for i in range(20):
        c = 100 + i
        candles.append(make_candle(c, c + 0.5, c - 0.5, c + 0.1, 50, ts=i))
    mfi = calculate_mfi(candles, period=14)
    assert mfi == 100.0, f"expected 100, got {mfi}"


def test_mfi_all_falling_bearish():
    """TP strictly falling every bar -> MFI = 0 (no positive flow)."""
    candles = []
    for i in range(20):
        c = 100 - i
        candles.append(make_candle(c, c + 0.5, c - 0.5, c - 0.1, 50, ts=i))
    mfi = calculate_mfi(candles, period=14)
    assert mfi == 0.0, f"expected 0, got {mfi}"


def test_mfi_mid_range():
    """Equal positive and negative flow -> MFI = 50."""
    candles = []
    for i in range(20):
        # alternate up/down with equal TP shift and equal volume
        if i % 2 == 0:
            c = 100 + i
        else:
            c = 100 + i - 2  # net -2 vs even bar
        candles.append(make_candle(c, c + 1, c - 1, c + 0.5, 100, ts=i))
    mfi = calculate_mfi(candles, period=14)
    assert mfi is not None and 40 <= mfi <= 60, f"expected ~50, got {mfi}"


def test_mfi_insufficient_data():
    """Need period + 1 candles."""
    candles = [make_candle(100, 101, 99, 100, 10, ts=i) for i in range(10)]
    assert calculate_mfi(candles, period=14) is None


# ════════════════════════════════════════════════════════════════
# VWAP MULTI-TF
# ════════════════════════════════════════════════════════════════
def test_vwap_multi_tf_empty():
    """Empty dict -> empty dict."""
    out = calculate_vwap_multi_tf({})
    assert out == {}, f"expected empty, got {out}"


def test_vwap_multi_tf_single():
    """Single TF with mixed prices -> weighted average."""
    candles = [
        make_candle(100, 102, 98, 100, 10, ts=1),  # TP = 100
        make_candle(100, 104, 98, 102, 30, ts=2),  # TP = 101.33
    ]
    out = calculate_vwap_multi_tf({"1m": candles})
    expected = ((100 * 10) + (101.3333333 * 30)) / 40
    assert out["1m"] is not None
    assert abs(out["1m"] - expected) < 0.01, \
        f"expected ~{expected:.2f}, got {out['1m']:.2f}"


def test_vwap_multi_tf_multiple():
    """Multiple TFs each compute independently."""
    candles_1m = [make_candle(100, 101, 99, 100, 50, ts=1)]
    candles_5m = [
        make_candle(99, 102, 98, 101, 200, ts=1),
        make_candle(101, 103, 100, 102, 300, ts=2),
    ]
    out = calculate_vwap_multi_tf({"1m": candles_1m, "5m": candles_5m})
    assert out["1m"] == 100.0
    # 5m: TP1=(102+98+101)/3=100.33, TP2=(103+100+102)/3=101.67
    # weighted: (100.33*200 + 101.67*300) / 500 = (20066.67 + 30500) / 500 = 101.13
    assert out["5m"] is not None
    assert 100.9 < out["5m"] < 101.3, f"5m VWAP out of range: {out['5m']}"


def test_vwap_multi_tf_zero_volume():
    """All zero volume -> None (avoid div by zero)."""
    candles = [make_candle(100, 101, 99, 100, 0, ts=1) for _ in range(5)]
    out = calculate_vwap_multi_tf({"1m": candles})
    assert out["1m"] is None


def test_vwap_multi_tf_missing_tf():
    """Unknown TF in dict -> None for that key."""
    out = calculate_vwap_multi_tf({"1m": [], "5m": [
        make_candle(100, 101, 99, 100, 10, ts=1)
    ]})
    assert out["1m"] is None
    assert out["5m"] == 100.0


# ════════════════════════════════════════════════════════════════
# Runner
# ════════════════════════════════════════════════════════════════
def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    passed = 0
    failed = 0
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\n{passed}/{passed + failed} tests passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
