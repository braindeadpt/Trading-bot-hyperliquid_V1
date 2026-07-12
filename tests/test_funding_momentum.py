"""Tests for FundingMomentum strategy."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategies.base import MarketEvent, Position, ExitSignal
from src.strategies.indicators import Candle
from src.strategies.funding_momentum import FundingMomentum
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
) -> Candle:
    h = high if high is not None else close * 1.005
    lo = low if low is not None else close * 0.995
    return Candle(
        open=close, high=h, low=lo, close=close, volume=1000.0,
        timestamp_ms=timestamp_ms,
    )


def _uptrend_1h(n: int = 60, base: float = 100.0, base_ms: int = 1_699_000_000_000) -> List[Candle]:
    candles: List[Candle] = []
    for i in range(n):
        price = base + i * 0.1
        candles.append(_make_candle(price, base_ms + i * 3_600_000))
    return candles


def _event(
    symbol: str,
    price: float,
    candles_1h: List[Candle],
    *,
    funding: Optional[float] = None,
    predicted_funding: Optional[float] = None,
    oi_delta: Optional[float] = None,
    adx: Optional[float] = 25.0,
    timestamp_ms: int = 1_700_000_000_000,
) -> MarketEvent:
    return MarketEvent(
        symbol=symbol,
        price=price,
        timestamp_ms=timestamp_ms,
        candle_1h=candles_1h[-1] if candles_1h else None,
        funding=funding,
        predicted_funding=predicted_funding,
        oi_delta=oi_delta,
        adx_14=adx,
    )


def _seed_1h(strategy: FundingMomentum, candles_1h: List[Candle]) -> None:
    for c in candles_1h:
        ev = MarketEvent(
            symbol="BTC", price=c.close, timestamp_ms=c.timestamp_ms,
            candle_1h=c, adx_14=25.0,
        )
        strategy.on_data(ev)


def test_instantiation_defaults() -> None:
    s = FundingMomentum()
    _pass("instantiation_defaults", s.name == "FundingMomentum")
    _pass("instantiation_defaults_threshold", s.FUNDING_FLIP_THRESHOLD == 0.0001)
    _pass("instantiation_defaults_min_adx", s.MIN_ADX == 20.0)


def test_no_signal_when_disabled() -> None:
    s = FundingMomentum({"enabled": False})
    candles_1h = _uptrend_1h()
    ev = _event("BTC", 110.0, candles_1h, predicted_funding=0.001, oi_delta=10.0)
    _pass("no_signal_when_disabled", s.on_data(ev) is None)


def test_no_signal_without_flip() -> None:
    """Two consecutive positive funding samples → no flip detected."""
    s = FundingMomentum({"min_confidence": 0.40})
    candles_1h = _uptrend_1h()
    _seed_1h(s, candles_1h)
    # First funding: positive
    s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=0.0005, oi_delta=10.0, timestamp_ms=1_700_000_000_000))
    # Second funding: still positive (no flip)
    sig = s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=0.0005, oi_delta=10.0, timestamp_ms=1_700_000_001_000))
    _pass("no_signal_without_flip", sig is None)


def test_long_signal_on_funding_flip_with_oi_up() -> None:
    s = FundingMomentum({"min_confidence": 0.40})
    candles_1h = _uptrend_1h()
    _seed_1h(s, candles_1h)
    # Previous funding negative, OI down
    s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=-0.0005,
                       oi_delta=-10.0, timestamp_ms=1_700_000_000_000))
    # Now flip: positive funding + OI up + price > EMA50 + ADX > 20
    sig = s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=0.0005,
                            oi_delta=10.0, adx=25.0, timestamp_ms=1_700_000_001_000))
    _pass(
        "long_signal_on_funding_flip_with_oi_up",
        sig is not None and sig.side == "long",
        f"got {sig!r}",
    )
    if sig is not None:
        _pass("long_signal_r_above_1", sig.take_profit_pct / sig.stop_loss_pct >= 1.0)


def test_short_signal_on_funding_flip_with_oi_down() -> None:
    s = FundingMomentum({"min_confidence": 0.40})
    # Downtrend 1h
    candles_1h: List[Candle] = []
    for i in range(60):
        price = 200.0 - i * 0.1
        candles_1h.append(_make_candle(price, 1_699_000_000_000 + i * 3_600_000))
    _seed_1h(s, candles_1h)
    # Previous: positive funding, OI up
    s.on_data(_event("BTC", 190.0, candles_1h, predicted_funding=0.0005,
                       oi_delta=10.0, timestamp_ms=1_700_000_000_000))
    # Flip: negative funding + OI down + price < EMA50
    sig = s.on_data(_event("BTC", 190.0, candles_1h, predicted_funding=-0.0005,
                            oi_delta=-10.0, adx=25.0, timestamp_ms=1_700_000_001_000))
    _pass(
        "short_signal_on_funding_flip_with_oi_down",
        sig is not None and sig.side == "short",
        f"got {sig!r}",
    )


def test_no_signal_when_oi_direction_wrong() -> None:
    """Flip detected but OI direction is wrong → no signal."""
    s = FundingMomentum({"min_confidence": 0.40})
    candles_1h = _uptrend_1h()
    _seed_1h(s, candles_1h)
    # Previous: negative funding, OI up
    s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=-0.0005,
                       oi_delta=10.0, timestamp_ms=1_700_000_000_000))
    # Flip to positive funding BUT OI is down (contradicts the new-long narrative)
    sig = s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=0.0005,
                            oi_delta=-10.0, adx=25.0, timestamp_ms=1_700_000_001_000))
    _pass("no_signal_when_oi_direction_wrong", sig is None)


def test_no_signal_below_trend_alignment() -> None:
    """Long signal requires price > EMA50."""
    s = FundingMomentum({"min_confidence": 0.40})
    candles_1h = _uptrend_1h()
    _seed_1h(s, candles_1h)
    # Previous: negative funding
    s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=-0.0005,
                       oi_delta=-10.0, timestamp_ms=1_700_000_000_000))
    # Flip to positive but price far below EMA50 (use 50 — way below recent 100+)
    sig = s.on_data(_event("BTC", 50.0, candles_1h, predicted_funding=0.0005,
                            oi_delta=10.0, adx=25.0, timestamp_ms=1_700_000_001_000))
    _pass("no_signal_below_trend_alignment", sig is None)


def test_no_signal_low_adx() -> None:
    s = FundingMomentum({"min_confidence": 0.40})
    candles_1h = _uptrend_1h()
    _seed_1h(s, candles_1h)
    s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=-0.0005,
                       oi_delta=-10.0, timestamp_ms=1_700_000_000_000))
    sig = s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=0.0005,
                            oi_delta=10.0, adx=10.0, timestamp_ms=1_700_000_001_000))
    _pass("no_signal_low_adx", sig is None)


def test_throttle_blocks_rapid_signals() -> None:
    s = FundingMomentum({"min_confidence": 0.40, "signal_throttle_ms": 600_000})
    candles_1h = _uptrend_1h()
    _seed_1h(s, candles_1h)
    s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=-0.0005,
                       oi_delta=-10.0, timestamp_ms=1_700_000_000_000))
    sig1 = s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=0.0005,
                            oi_delta=10.0, adx=25.0, timestamp_ms=1_700_000_001_000))
    s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=-0.0005,
                       oi_delta=-10.0, timestamp_ms=1_700_000_002_000))
    sig2 = s.on_data(_event("BTC", 110.0, candles_1h, predicted_funding=0.0005,
                            oi_delta=10.0, adx=25.0, timestamp_ms=1_700_000_003_000))
    _pass(
        "throttle_blocks_rapid_signals",
        sig1 is not None and sig2 is None,
        f"sig1={sig1.side if sig1 else None} sig2={sig2.side if sig2 else None}",
    )


def test_funding_reversal_exit_long() -> None:
    s = FundingMomentum()
    pos = Position(
        symbol="BTC", side="long", entry_price=110.0, size=0.1,
        entry_time_ms=1_700_000_000_000,
        metadata={"strategy": "FundingMomentum"},
    )
    # Funding flips back to negative
    exit_sig = s.on_position(
        pos,
        _event("BTC", 110.0, [], predicted_funding=-0.0005,
                 timestamp_ms=1_700_000_001_000),
    )
    _pass(
        "funding_reversal_exit_long",
        exit_sig is not None and "funding_reversed" in exit_sig.reason,
        f"got {exit_sig!r}",
    )


def test_max_hold_exit() -> None:
    s = FundingMomentum({"max_hold_hours": 1.0})
    pos = Position(
        symbol="BTC", side="long", entry_price=110.0, size=0.1,
        entry_time_ms=1_700_000_000_000,
        metadata={"strategy": "FundingMomentum"},
    )
    exit_sig = s.on_position(
        pos,
        _event("BTC", 110.0, [], predicted_funding=0.0001,
                 timestamp_ms=1_700_000_000_000 + 2 * 3_600_000),
    )
    _pass(
        "max_hold_exit",
        exit_sig is not None and "max_hold" in exit_sig.reason,
        f"got {exit_sig!r}",
    )


def main() -> int:
    print("=" * 70)
    print("FundingMomentum strategy tests")
    print("=" * 70)
    tests = [
        test_instantiation_defaults,
        test_no_signal_when_disabled,
        test_no_signal_without_flip,
        test_long_signal_on_funding_flip_with_oi_up,
        test_short_signal_on_funding_flip_with_oi_down,
        test_no_signal_when_oi_direction_wrong,
        test_no_signal_below_trend_alignment,
        test_no_signal_low_adx,
        test_throttle_blocks_rapid_signals,
        test_funding_reversal_exit_long,
        test_max_hold_exit,
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
