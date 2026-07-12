"""Tests for TrendPyramid strategy."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategies.base import MarketEvent, Position, ExitSignal
from src.strategies.indicators import Candle
from src.strategies.trend_pyramid import TrendPyramid
import pytest

pytestmark = pytest.mark.unit


FAILED = 0


def _pass(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILED += 1


def _make_candle(
    close: float,
    timestamp_ms: int,
    high: Optional[float] = None,
    low: Optional[float] = None,
    open_: Optional[float] = None,
    volume: float = 1000.0,
) -> Candle:
    o = open_ if open_ is not None else close
    h = high if high is not None else close * 1.005
    lo = low if low is not None else close * 0.995
    return Candle(
        open=o, high=h, low=lo, close=close, volume=volume,
        timestamp_ms=timestamp_ms,
    )


def _uptrend_15m(n: int = 80, base: float = 100.0, base_ms: int = 1_700_000_000_000) -> List[Candle]:
    """Construct an uptrend that ends with a *quiet* pullback at i=n-1.

    The first 60 bars ramp 100 → 106 (trend); bars 60..n-1 hold the
    level around 106 so the running EMA20/EMA50 settle near 105-106.
    The final bar dips a few bps below the running EMA20 with half
    the normal volume — this is the pullback the strategy is looking
    for.
    """
    candles: List[Candle] = []
    ramp_close = []
    for i in range(n):
        if i < 60:
            price = 100.0 + i * 0.1
        else:
            # Hold the level near 106 with mild noise.
            price = 106.0 + (i - 60) * 0.01
        ramp_close.append(price)

    # Final bar: dip 0.4% below the running EMA20 with 0.5x volume.
    # Compute EMA20 over the first 60 bars' closes for an approximate
    # target, then tune it so the final bar's distance to EMA20 is
    # within 0.5%.
    last_emafast = ramp_close[-1] * 0.997  # 0.3% below the level

    for i in range(n):
        if i < 60:
            candles.append(_make_candle(ramp_close[i], base_ms + i * 900_000, volume=1000.0))
        elif i < n - 1:
            candles.append(_make_candle(ramp_close[i], base_ms + i * 900_000, volume=1000.0))
        else:
            # Last bar: quiet pullback.
            candles.append(_make_candle(last_emafast, base_ms + i * 900_000, volume=500.0))
    return candles


def _uptrend_1h(n: int = 60, base: float = 100.0, base_ms: int = 1_699_000_000_000) -> List[Candle]:
    candles: List[Candle] = []
    for i in range(n):
        price = base + i * 0.1
        candles.append(_make_candle(price, base_ms + i * 3_600_000, volume=10_000.0))
    return candles


def _event(
    symbol: str,
    price: float,
    candles_15m: List[Candle],
    candles_1h: List[Candle],
    adx: Optional[float] = 30.0,
    timestamp_ms: int = 1_700_000_000_000 + 60 * 900_000,
) -> MarketEvent:
    # The strategy needs the *current* 1h candle as MarketEvent.candle_1h.
    # We pass the last one if available, but on a 1m/15m update the 1h
    # candle timestamp is the *boundary* and the strategy only appends
    # it to state if the timestamp differs from the last. To build a
    # real history across many seeds, the caller must pass multiple
    # 1h candles in chronological order (e.g. ``list(candles_1h)``).
    return MarketEvent(
        symbol=symbol,
        price=price,
        timestamp_ms=timestamp_ms,
        candle_15m=candles_15m[-1] if candles_15m else None,
        candle_1h=candles_1h[-1] if candles_1h else None,
        adx_14=adx,
    )


def _seed_1h_history(strategy: TrendPyramid, candles_1h: List[Candle]) -> None:
    """Push the full 1h series into the state so the dedup lets each
    unique timestamp through on successive calls.
    """
    for c in candles_1h:
        ev = MarketEvent(
            symbol="BTC", price=c.close, timestamp_ms=c.timestamp_ms,
            candle_1h=c, adx_14=30.0,
        )
        strategy.on_data(ev)


def test_instantiation_defaults() -> None:
    s = TrendPyramid()
    _pass("instantiation_defaults", s.name == "TrendPyramid")
    _pass("instantiation_defaults_ema_fast", s.EMA_FAST == 20)
    _pass("instantiation_defaults_min_adx", s.MIN_ADX == 25.0)
    _pass("instantiation_defaults_max_pyramids", s.MAX_PYRAMIDS == 2)


def test_no_signal_when_disabled() -> None:
    s = TrendPyramid({"enabled": False})
    candles_15m = _uptrend_15m()
    candles_1h = _uptrend_1h()
    sig = s.on_data(_event("BTC", 100.0, candles_15m, candles_1h))
    _pass("no_signal_when_disabled", sig is None)


def test_no_signal_in_downtrend() -> None:
    """EMA20 < EMA50 → no long signal."""
    s = TrendPyramid({"min_confidence": 0.40})
    candles_15m: List[Candle] = []
    for i in range(80):
        price = 200.0 - i * 0.1  # downtrend
        candles_15m.append(_make_candle(price, 1_700_000_000_000 + i * 900_000))
    candles_1h = _uptrend_1h()
    ev = _event("BTC", 195.0, candles_15m, candles_1h)
    sig = s.on_data(ev)
    _pass("no_signal_in_downtrend", sig is None)


def test_no_signal_without_pullback() -> None:
    """In a strong uptrend with price far above EMA20, no pullback entry."""
    s = TrendPyramid({"min_confidence": 0.40})
    candles_15m = _uptrend_15m()  # uptrend
    candles_1h = _uptrend_1h()
    # Price way above EMA20 (which is at ~104 for an 80-bar uptrend from 100)
    ev = _event("BTC", 120.0, candles_15m, candles_1h)
    sig = s.on_data(ev)
    _pass("no_signal_without_pullback", sig is None)


def test_no_signal_low_adx() -> None:
    """ADX below min_adx → no signal even if pullback + trend present."""
    s = TrendPyramid({"min_confidence": 0.40})
    candles_15m = _uptrend_15m()
    candles_1h = _uptrend_1h()
    ev = _event("BTC", candles_15m[40].close, candles_15m, candles_1h, adx=15.0)
    sig = s.on_data(ev)
    _pass("no_signal_low_adx", sig is None)


def test_no_signal_high_volume_pullback() -> None:
    """A pullback with high volume is a reversal — no signal."""
    s = TrendPyramid({"min_confidence": 0.40})
    candles_15m = _uptrend_15m()
    # Make bar 40 a HIGH volume bar (reversal, not quiet pullback)
    candles_15m[40] = _make_candle(candles_15m[40].close, candles_15m[40].timestamp_ms, volume=5000.0)
    candles_1h = _uptrend_1h()
    ev = _event("BTC", candles_15m[40].close, candles_15m, candles_1h)
    sig = s.on_data(ev)
    _pass("no_signal_high_volume_pullback", sig is None)


def test_long_signal_on_quiet_pullback() -> None:
    """Quiet pullback to EMA20 in uptrend with high ADX → long signal."""
    s = TrendPyramid({"min_confidence": 0.40})
    candles_15m = _uptrend_15m()
    candles_1h = _uptrend_1h()
    pullback = candles_15m[-1]
    # First seed the full 1h history into state.
    _seed_1h_history(s, candles_1h)
    # Then seed 15m bars except the last.
    for c in candles_15m[:-1]:
        s.on_data(_event("BTC", c.close, [c], list(candles_1h), adx=30.0,
                          timestamp_ms=c.timestamp_ms))
    # Build a *fresh* pullback candle with a +1ms timestamp so the
    # state dedup allows it.
    fresh = _make_candle(pullback.close, pullback.timestamp_ms + 1,
                          volume=pullback.volume)
    ev = _event("BTC", pullback.close, [fresh], candles_1h, adx=30.0,
                 timestamp_ms=pullback.timestamp_ms + 1)
    sig = s.on_data(ev)
    _pass(
        "long_signal_on_quiet_pullback",
        sig is not None and sig.side == "long",
        f"got {sig!r}",
    )
    if sig is not None:
        _pass(
            "long_signal_metadata_leg_initial",
            sig.metadata.get("leg") == "initial",
        )
        _pass(
            "long_signal_metadata_pyramid_count",
            sig.metadata.get("pyramid_count") == 0,
        )


def test_throttle_blocks_rapid_signals() -> None:
    s = TrendPyramid({"min_confidence": 0.40, "signal_throttle_ms": 600_000})
    candles_15m = _uptrend_15m()
    candles_1h = _uptrend_1h()
    pullback = candles_15m[-1]
    _seed_1h_history(s, candles_1h)
    for c in candles_15m[:-1]:
        s.on_data(_event("BTC", c.close, [c], list(candles_1h), adx=30.0,
                          timestamp_ms=c.timestamp_ms))
    fresh1 = _make_candle(pullback.close, pullback.timestamp_ms + 1,
                           volume=pullback.volume)
    fresh2 = _make_candle(pullback.close, pullback.timestamp_ms + 2,
                           volume=pullback.volume)
    sig1 = s.on_data(_event("BTC", pullback.close, [fresh1], candles_1h,
                              adx=30.0, timestamp_ms=2_000))
    sig2 = s.on_data(_event("BTC", pullback.close, [fresh2], candles_1h,
                              adx=30.0, timestamp_ms=3_000))
    _pass(
        "throttle_blocks_rapid_signals",
        sig1 is not None and sig2 is None,
        f"sig1={sig1.side if sig1 else None} sig2={sig2.side if sig2 else None}",
    )


def test_trend_reversal_exit_long() -> None:
    """EMA20 < EMA50 while long → exit."""
    s = TrendPyramid()
    pos = Position(
        symbol="BTC", side="long", entry_price=104.0, size=0.1,
        entry_time_ms=1_700_000_000_000,
        metadata={"strategy": "TrendPyramid"},
    )
    # 80-bar downtrend so EMA20 < EMA50
    candles_15m: List[Candle] = []
    for i in range(80):
        price = 200.0 - i * 0.1
        candles_15m.append(_make_candle(price, 1_700_000_000_000 + i * 900_000))
    # Seed state
    for c in candles_15m[:-1]:
        s.on_data(_event("BTC", c.close, [c], [candles_15m[0]], adx=30.0,
                          timestamp_ms=c.timestamp_ms))
    ev = MarketEvent(
        symbol="BTC", price=195.0,
        timestamp_ms=1_700_000_000_000 + 80 * 900_000,
        candle_15m=candles_15m[-1],
    )
    exit_sig = s.on_position(pos, ev)
    _pass(
        "trend_reversal_exit_long",
        exit_sig is not None and "ema_trend_reversal" in exit_sig.reason,
        f"got {exit_sig!r}",
    )


def test_trend_reversal_exit_short() -> None:
    s = TrendPyramid()
    pos = Position(
        symbol="BTC", side="short", entry_price=104.0, size=0.1,
        entry_time_ms=1_700_000_000_000,
        metadata={"strategy": "TrendPyramid"},
    )
    candles_15m: List[Candle] = []
    for i in range(80):
        price = 100.0 + i * 0.1  # uptrend
        candles_15m.append(_make_candle(price, 1_700_000_000_000 + i * 900_000))
    for c in candles_15m[:-1]:
        s.on_data(_event("BTC", c.close, [c], [candles_15m[0]], adx=30.0,
                          timestamp_ms=c.timestamp_ms))
    ev = MarketEvent(
        symbol="BTC", price=110.0,
        timestamp_ms=1_700_000_000_000 + 80 * 900_000,
        candle_15m=candles_15m[-1],
    )
    exit_sig = s.on_position(pos, ev)
    _pass(
        "trend_reversal_exit_short",
        exit_sig is not None and "ema_trend_reversal" in exit_sig.reason,
        f"got {exit_sig!r}",
    )


def main() -> int:
    print("=" * 70)
    print("TrendPyramid strategy tests")
    print("=" * 70)
    tests = [
        test_instantiation_defaults,
        test_no_signal_when_disabled,
        test_no_signal_in_downtrend,
        test_no_signal_without_pullback,
        test_no_signal_low_adx,
        test_no_signal_high_volume_pullback,
        test_long_signal_on_quiet_pullback,
        test_throttle_blocks_rapid_signals,
        test_trend_reversal_exit_long,
        test_trend_reversal_exit_short,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            _pass(t.__name__, False, f"AssertionError: {e}")
        except Exception as e:  # noqa: BLE001
            _pass(t.__name__, False, f"{type(e).__name__}: {e}")
    print("=" * 70)
    if FAILED == 0:
        print(f"ALL TESTS PASSED ({len(tests)}/{len(tests)})")
        return 0
    print(f"FAILED: {FAILED}/{len(tests)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
