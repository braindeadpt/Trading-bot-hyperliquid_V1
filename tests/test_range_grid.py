"""Tests for RangeGrid strategy."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategies.base import MarketEvent, Position
from src.strategies.indicators import Candle
from src.strategies.range_grid import RangeGrid
import pytest

pytestmark = pytest.mark.unit


FAILED = 0


def _pass(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILED += 1


def _make_candles(prices: List[float], base_ms: int = 1_700_000_000_000) -> List[Candle]:
    out: List[Candle] = []
    for i, p in enumerate(prices):
        out.append(Candle(
            open=p, high=p * 1.005, low=p * 0.995, close=p,
            volume=1000.0, timestamp_ms=base_ms + i * 900_000,
        ))
    return out


def _event(
    symbol: str,
    price: float,
    candles_15m: List[Candle],
    adx: Optional[float] = None,
    timestamp_ms: int = 1_700_000_000_000 + 60 * 900_000,
) -> MarketEvent:
    return MarketEvent(
        symbol=symbol,
        price=price,
        timestamp_ms=timestamp_ms,
        candle_15m=candles_15m[-1] if candles_15m else None,
        adx_14=adx,
    )


def test_instantiation_defaults() -> None:
    s = RangeGrid()
    _pass("instantiation_defaults", s.name == "RangeGrid")
    _pass("instantiation_defaults_max_adx", s.MAX_ADX == 18.0)
    _pass("instantiation_defaults_min_conf", s.MIN_CONFIDENCE == 0.50)


def test_no_signal_when_disabled() -> None:
    s = RangeGrid({"enabled": False})
    candles = _make_candles([100.0] * 60)
    sig = s.on_data(_event("BTC", 95.0, candles, adx=10.0))
    _pass("no_signal_when_disabled", sig is None)


def test_no_signal_when_trending() -> None:
    s = RangeGrid()
    candles = _make_candles([100.0] * 60)
    sig = s.on_data(_event("BTC", 95.0, candles, adx=40.0))
    _pass("no_signal_when_trending", sig is None)


def test_no_signal_insufficient_history() -> None:
    s = RangeGrid()
    candles = _make_candles([100.0] * 5)
    sig = s.on_data(_event("BTC", 95.0, candles, adx=10.0))
    _pass("no_signal_insufficient_history", sig is None)


def test_long_signal_at_support() -> None:
    """In a tight range with ADX < max, the strategy should signal long near support."""
    s = RangeGrid({"min_confidence": 0.40, "max_band_width_pct": 0.20})
    # Tight range: support ~99, resistance ~101
    prices = [100.0] * 60
    prices[10] = 99.0
    prices[11] = 99.0
    prices[12] = 99.0
    prices[50] = 101.0
    prices[51] = 101.0
    prices[52] = 101.0
    candles = _make_candles(prices)
    # Seed the state with each candle so S/R + BB history build up.
    for c in candles[:-1]:
        s.on_data(_event("BTC", c.close, [c], adx=10.0,
                          timestamp_ms=c.timestamp_ms))
    ev = _event("BTC", 98.0, candles, adx=10.0)
    sig = s.on_data(ev)
    _pass(
        "long_signal_at_support",
        sig is not None and sig.side == "long",
        f"got {sig!r}",
    )
    if sig is not None:
        _pass(
            "long_signal_metadata_uses_limit_maker",
            sig.metadata.get("order_type") == "limit_maker",
        )


def test_short_signal_at_resistance() -> None:
    s = RangeGrid({"min_confidence": 0.40, "max_band_width_pct": 0.20})
    prices = [100.0] * 60
    for i in (10, 11, 12):
        prices[i] = 99.0
    for i in (50, 51, 52):
        prices[i] = 101.0
    candles = _make_candles(prices)
    for c in candles[:-1]:
        s.on_data(_event("BTC", c.close, [c], adx=10.0,
                          timestamp_ms=c.timestamp_ms))
    ev = _event("BTC", 102.0, candles, adx=10.0)
    sig = s.on_data(ev)
    _pass(
        "short_signal_at_resistance",
        sig is not None and sig.side == "short",
        f"got {sig!r}",
    )


def test_no_signal_in_band_middle() -> None:
    """Price in the middle of the band → no signal (no edge)."""
    s = RangeGrid({"min_confidence": 0.40, "max_band_width_pct": 0.20})
    prices = [100.0] * 60
    for i in (10, 11, 12):
        prices[i] = 99.0
    for i in (50, 51, 52):
        prices[i] = 101.0
    candles = _make_candles(prices)
    for c in candles[:-1]:
        s.on_data(_event("BTC", c.close, [c], adx=10.0,
                          timestamp_ms=c.timestamp_ms))
    ev = _event("BTC", 100.0, candles, adx=10.0)
    sig = s.on_data(ev)
    _pass("no_signal_in_band_middle", sig is None)


def test_throttle_blocks_rapid_signals() -> None:
    s = RangeGrid({"min_confidence": 0.40, "signal_throttle_ms": 600_000, "max_band_width_pct": 0.20})
    prices = [100.0] * 60
    for i in (10, 11, 12):
        prices[i] = 99.0
    for i in (50, 51, 52):
        prices[i] = 101.0
    candles = _make_candles(prices)
    for c in candles[:-1]:
        s.on_data(_event("BTC", c.close, [c], adx=10.0,
                          timestamp_ms=c.timestamp_ms))
    sig1 = s.on_data(_event("BTC", 98.0, candles, adx=10.0, timestamp_ms=2_000))
    sig2 = s.on_data(_event("BTC", 98.0, candles, adx=10.0, timestamp_ms=3_000))
    _pass(
        "throttle_blocks_rapid_signals",
        sig1 is not None and sig2 is None,
        f"sig1={sig1.side if sig1 else None} sig2={sig2.side if sig2 else None}",
    )


def test_max_hold_exit() -> None:
    s = RangeGrid({"max_hold_hours": 1.0})
    pos = Position(
        symbol="BTC", side="long", entry_price=99.7, size=0.1,
        entry_time_ms=1_700_000_000_000,
        metadata={"strategy": "RangeGrid"},
    )
    ev = _event("BTC", 99.7, [], adx=10.0,
                timestamp_ms=1_700_000_000_000 + 2 * 3_600_000)
    exit_sig = s.on_position(pos, ev)
    _pass(
        "max_hold_exit",
        exit_sig is not None and "max_hold" in exit_sig.reason,
        f"got {exit_sig!r}",
    )


def test_rr_below_2_blocks_signal() -> None:
    """If stop is too close to TP, R:R < 2 and signal is rejected."""
    s = RangeGrid({"stop_offset_pct": 0.05, "band_offset_pct": 0.001, "min_confidence": 0.40})
    prices = [100.0] * 60
    prices[20] = 95.0
    prices[40] = 105.0
    candles = _make_candles(prices)
    ev = _event("BTC", 99.7, candles, adx=10.0)
    sig = s.on_data(ev)
    _pass("rr_below_2_blocks_signal", sig is None)


def main() -> int:
    print("=" * 70)
    print("RangeGrid strategy tests")
    print("=" * 70)
    tests = [
        test_instantiation_defaults,
        test_no_signal_when_disabled,
        test_no_signal_when_trending,
        test_no_signal_insufficient_history,
        test_long_signal_at_support,
        test_short_signal_at_resistance,
        test_no_signal_in_band_middle,
        test_throttle_blocks_rapid_signals,
        test_max_hold_exit,
        test_rr_below_2_blocks_signal,
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
