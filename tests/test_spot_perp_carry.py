"""Tests for SpotPerpCarry strategy."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategies.base import MarketEvent, Position, Signal, ExitSignal
from src.strategies.spot_perp_carry import SpotPerpCarry
import pytest

pytestmark = pytest.mark.unit


FAILED = 0


def _pass(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILED += 1


def _event(
    symbol: str,
    price: float,
    funding: Optional[float] = None,
    predicted: Optional[float] = None,
    binance_mid: Optional[float] = None,
    timestamp_ms: int = 1_700_000_000_000,
) -> MarketEvent:
    return MarketEvent(
        symbol=symbol,
        price=price,
        timestamp_ms=timestamp_ms,
        funding=funding,
        predicted_funding=predicted,
        binance_mid=binance_mid,
        orderbook_spread_pct=0.0005,
    )


def test_instantiation_defaults() -> None:
    s = SpotPerpCarry()
    _pass("instantiation_defaults", s.name == "SpotPerpCarry")
    _pass("instantiation_defaults_min_funding", s.MIN_FUNDING_HOURLY == 0.0005)
    _pass("instantiation_defaults_max_hold_h", s.MAX_HOLD_HOURS == 24.0)


def test_no_signal_when_disabled() -> None:
    s = SpotPerpCarry({"enabled": False})
    sig = s.on_data(_event("BTC", 80000.0, predicted=0.001))
    _pass("no_signal_when_disabled", sig is None)


def test_no_signal_without_funding() -> None:
    s = SpotPerpCarry()
    sig = s.on_data(_event("BTC", 80000.0, funding=None, predicted=None))
    _pass("no_signal_without_funding", sig is None)


def test_no_signal_when_funding_below_threshold() -> None:
    s = SpotPerpCarry()
    sig = s.on_data(_event("BTC", 80000.0, predicted=0.0002))
    _pass("no_signal_when_funding_below_threshold", sig is None)


def test_signal_on_extreme_funding() -> None:
    s = SpotPerpCarry()
    sig = s.on_data(_event("BTC", 80000.0, predicted=0.0010))  # 0.1% / h
    _pass(
        "signal_on_extreme_funding",
        sig is not None and sig.side == "short",
        f"got {sig!r}",
    )
    if sig is not None:
        _pass(
            "signal_on_extreme_funding_metadata",
            sig.metadata.get("leg_setup", "").startswith("perp_short"),
        )


def test_throttle_blocks_rapid_signals() -> None:
    s = SpotPerpCarry()
    sig1 = s.on_data(_event("BTC", 80000.0, predicted=0.001, timestamp_ms=1000))
    sig2 = s.on_data(_event("BTC", 80100.0, predicted=0.001, timestamp_ms=2000))
    _pass(
        "throttle_blocks_rapid_signals",
        sig1 is not None and sig2 is None,
        f"sig1={sig1.side if sig1 else None} sig2={sig2.side if sig2 else None}",
    )


def test_basis_stop_exit() -> None:
    s = SpotPerpCarry({"max_hold_hours": 999})  # disable time exit
    pos = Position(
        symbol="BTC", side="short", entry_price=80000.0, size=0.1,
        entry_time_ms=1_700_000_000_000,
        metadata={"strategy": "SpotPerpCarry"},
    )
    # HL 3% above spot → basis blowout
    ev = _event(
        "BTC", price=82400.0,  # 3% above 80000
        funding=0.001,
        binance_mid=80000.0,
        timestamp_ms=1_700_000_000_000 + 60_000,
    )
    exit_sig = s.on_position(pos, ev)
    _pass(
        "basis_stop_exit",
        exit_sig is not None and "basis_stop" in exit_sig.reason,
        f"got {exit_sig!r}",
    )


def test_funding_reversion_exit() -> None:
    s = SpotPerpCarry({"max_hold_hours": 999})
    pos = Position(
        symbol="BTC", side="short", entry_price=80000.0, size=0.1,
        entry_time_ms=1_700_000_000_000,
        metadata={"strategy": "SpotPerpCarry"},
    )
    ev = _event(
        "BTC", price=80050.0,
        funding=0.00005,  # below 0.0001 exit threshold
        binance_mid=80000.0,
        timestamp_ms=1_700_000_000_000 + 60_000,
    )
    exit_sig = s.on_position(pos, ev)
    _pass(
        "funding_reversion_exit",
        exit_sig is not None and "funding_reverted" in exit_sig.reason,
        f"got {exit_sig!r}",
    )


def test_max_hold_exit() -> None:
    s = SpotPerpCarry({"max_hold_hours": 1.0})
    pos = Position(
        symbol="BTC", side="short", entry_price=80000.0, size=0.1,
        entry_time_ms=1_700_000_000_000,
        metadata={"strategy": "SpotPerpCarry"},
    )
    ev = _event(
        "BTC", price=80050.0,
        funding=0.001,  # still high — basis_stop not triggered (no binance_mid)
        binance_mid=None,
        timestamp_ms=1_700_000_000_000 + 2 * 3_600_000,  # 2h later
    )
    exit_sig = s.on_position(pos, ev)
    _pass(
        "max_hold_exit",
        exit_sig is not None and "max_hold" in exit_sig.reason,
        f"got {exit_sig!r}",
    )


def test_low_rr_blocks_signal() -> None:
    s = SpotPerpCarry({"min_funding_hourly": 0.0001, "basis_stop_pct": 0.05})
    # funding 0.0002/h over 24h = 0.0048, basis stop 0.05 → R:R 0.096
    sig = s.on_data(_event("BTC", 80000.0, predicted=0.0002))
    _pass("low_rr_blocks_signal", sig is None)


def main() -> int:
    print("=" * 70)
    print("SpotPerpCarry strategy tests")
    print("=" * 70)
    tests = [
        test_instantiation_defaults,
        test_no_signal_when_disabled,
        test_no_signal_without_funding,
        test_no_signal_when_funding_below_threshold,
        test_signal_on_extreme_funding,
        test_throttle_blocks_rapid_signals,
        test_basis_stop_exit,
        test_funding_reversion_exit,
        test_max_hold_exit,
        test_low_rr_blocks_signal,
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
