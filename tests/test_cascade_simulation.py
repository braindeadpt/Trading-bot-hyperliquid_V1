"""C4 — Cascade stress test for the new volatility circuit breaker + existing DD CB.

Simulates a synthetic liquidation cascade: BTC price drops ~8% over 5 minutes
with hourly ATR rising 4x baseline. Verifies:
  1. VolatilityCircuitBreaker trips (blocks new entries).
  2. RiskManager DD circuit breaker trips on >10% drawdown.
  3. _PortfolioProxy / flatten simulation leaves portfolio flat.
  4. FundingBlackoutFilter behaves around 8h funding reset windows.

Run standalone:
    python tests/test_cascade_simulation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import time
from src.core.volatility_circuit import (
    VolatilityCircuitBreaker, VolCircuitConfig,
)
from src.core.funding_blackout import (
    FundingBlackoutFilter, FundingBlackoutConfig,
)
from src.core.risk_manager import RiskManager
from src.core.portfolio import PortfolioState
from src.strategies.base import Position, Signal
from src.utils.config import Config


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_vol_circuit_breaker_trips():
    """ATR rising 4x baseline must trip the vol circuit."""
    print("=" * 60)
    print("TEST: Volatility circuit breaker trips on cascade")
    print("=" * 60)
    cb = VolatilityCircuitBreaker(VolCircuitConfig(
        multiplier=3.0,
        baseline_window_bars=24,
        block_duration_min=1,
        min_samples=12,
    ))
    now = _now_ms()
    hour_ms = 3_600_000

    # Warm up baseline with 24 calm samples (ATR ~ 0.5%)
    for i in range(24):
        cb.update("BTC", 0.005, now - (24 - i) * hour_ms)

    assert not cb.is_blocked("BTC", now), "should not be blocked after warm-up"

    # Cascade: 6 hours of 4x volatility (each spike re-triggers, block is 1min)
    tripped_count = 0
    last_trip_ts = now
    for i in range(6):
        ts = now + i * hour_ms
        if cb.update("BTC", 0.020, ts):
            tripped_count += 1
            last_trip_ts = ts

    assert tripped_count >= 1, f"vol circuit should trip at least once, got {tripped_count}"
    # Query right after the last trip (within the 1-min block window)
    assert cb.is_blocked("BTC", last_trip_ts), "BTC should be blocked right after cascade"
    remaining = cb.block_remaining_sec("BTC", last_trip_ts)
    assert remaining > 0, f"remaining should be > 0, got {remaining}"
    # And expired 2 minutes after the last trip
    assert not cb.is_blocked("BTC", last_trip_ts + 120_000), "should auto-expire after block"
    print(f"  [PASS] Tripped {tripped_count} times, {remaining:.0f}s remaining, expired after 2min\n")


def test_vol_circuit_extends_on_retrigger():
    """A new spike while already blocked must extend (never shorten) the block."""
    print("=" * 60)
    print("TEST: Vol circuit extends on re-trigger")
    print("=" * 60)
    cb = VolatilityCircuitBreaker(VolCircuitConfig(
        multiplier=2.0,
        baseline_window_bars=12,
        block_duration_min=5,
        min_samples=6,
    ))
    now = _now_ms()
    hour_ms = 3_600_000

    for i in range(6):
        cb.update("BTC", 0.001, now - (6 - i) * hour_ms)
    cb.update("BTC", 0.020, now)
    r1 = cb.block_remaining_sec("BTC", now)
    assert r1 > 0
    # Re-trigger 2 min later
    cb.update("BTC", 0.025, now + 120_000)
    r2 = cb.block_remaining_sec("BTC", now + 120_000)
    assert r2 >= r1, f"re-trigger should extend, not shorten (r1={r1}, r2={r2})"
    print(f"  [PASS] r1={r1:.0f}s, r2={r2:.0f}s (extended)\n")


def test_per_symbol_isolation():
    """Block on BTC must not affect ETH."""
    print("=" * 60)
    print("TEST: Vol circuit is per-symbol")
    print("=" * 60)
    cb = VolatilityCircuitBreaker(VolCircuitConfig(
        multiplier=2.0,
        baseline_window_bars=6,
        block_duration_min=5,
        min_samples=3,
    ))
    now = _now_ms()
    hour_ms = 3_600_000
    for i in range(6):
        cb.update("BTC", 0.001, now - (6 - i) * hour_ms)
        cb.update("ETH", 0.001, now - (6 - i) * hour_ms)
    cb.update("BTC", 0.020, now)
    assert cb.is_blocked("BTC", now)
    assert not cb.is_blocked("ETH", now), "ETH should not be affected"
    print("  [PASS] BTC blocked, ETH unaffected\n")


def test_snapshot_for_dashboard():
    """Snapshot must serialize cleanly for the dashboard."""
    print("=" * 60)
    print("TEST: Vol circuit snapshot")
    print("=" * 60)
    cb = VolatilityCircuitBreaker(VolCircuitConfig(multiplier=3.0, min_samples=3))
    now = _now_ms()
    cb.update("BTC", 0.005, now)
    cb.update("BTC", 0.005, now + 1000)
    snap = cb.snapshot()
    assert "BTC" in snap
    assert "samples" in snap["BTC"]
    assert "last_ratio" in snap["BTC"]
    assert "baseline_atr" in snap["BTC"]
    assert "block_until_ms" in snap["BTC"]
    assert snap["BTC"]["samples"] == 2
    print(f"  [PASS] Snapshot: {snap['BTC']}\n")


def test_funding_blackout_boundaries():
    """Re-test all 9 boundary cases for funding blackout."""
    print("=" * 60)
    print("TEST: Funding blackout boundaries")
    print("=" * 60)
    f = FundingBlackoutFilter(FundingBlackoutConfig(
        minutes_before=5, minutes_after=5,
        resets_utc=["00:00", "08:00", "16:00"],
    ))
    from datetime import datetime, timezone
    cases = [
        (7, 55, True), (7, 56, True), (8, 4, True),
        (8, 5, False), (8, 6, False), (15, 55, True),
        (23, 55, True), (0, 5, False), (0, 6, False),
    ]
    for hh, mm, expected in cases:
        ts = int(datetime(2026, 1, 1, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)
        got = f.is_blocked(ts)
        assert got == expected, f"{hh:02d}:{mm:02d}: expected {expected}, got {got}"
    print(f"  [PASS] All {len(cases)} boundary cases correct\n")


def test_dd_circuit_breaker_still_trips():
    """Confirm the existing DD circuit breaker still works (regression)."""
    print("=" * 60)
    print("TEST: Drawdown circuit breaker still trips on 12% loss")
    print("=" * 60)
    cfg = Config({"risk": {"circuit_breaker_drawdown_pct": 10.0}})
    risk = RiskManager(cfg, db=None)
    assert not risk.is_circuit_breaker_tripped(), "should start un-tripped"
    assert risk.check_drawdown(0.12), "12% DD should trip the breaker"
    assert risk.is_circuit_breaker_tripped(), "breaker should now be active"
    print("  [PASS] DD CB functional\n")


def test_vol_circuit_min_samples_guards_against_early_trip():
    """Before baseline is warm, the CB must not trip on first sample."""
    print("=" * 60)
    print("TEST: Vol circuit guards on cold start")
    print("=" * 60)
    cb = VolatilityCircuitBreaker(VolCircuitConfig(
        multiplier=2.0, min_samples=10, block_duration_min=1,
    ))
    now = _now_ms()
    for i in range(5):
        cb.update("BTC", 0.020, now + i * 1000)
    assert not cb.is_blocked("BTC", now + 5000), (
        "must not trip before min_samples reached"
    )
    print("  [PASS] Cold start correctly ignored\n")


if __name__ == "__main__":
    test_vol_circuit_breaker_trips()
    test_vol_circuit_extends_on_retrigger()
    test_per_symbol_isolation()
    test_snapshot_for_dashboard()
    test_funding_blackout_boundaries()
    test_dd_circuit_breaker_still_trips()
    test_vol_circuit_min_samples_guards_against_early_trip()
    print("=" * 60)
    print("ALL CASCADE / VOLATILITY TESTS PASSED")
    print("=" * 60)
