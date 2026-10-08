"""Tests for the executed-trade section of scripts/research/jev_eval.py.

The bracket simulator + random baseline power the preregistered JevJudge
kill read (docs/PREREGISTER_JEV_OOS_KILL_2026-10-08.md) — correctness here
is evidence correctness.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.research.jev_eval import (  # noqa: E402
    GEOMETRY_BOUNDARY_MS,
    JEV_FEE_RT,
    _bracket_from_metadata,
    _independent_trades,
    _percentile,
    random_baseline,
    simulate_bracket,
)

pytestmark = [pytest.mark.unit]


def _tr(symbol="BTC", entry=100, exit_=200, entry_px=100.0, side="long",
        pnl=0.0, meta=None):
    return {
        "symbol": symbol, "side": side, "entry_price": entry_px,
        "entry_time": entry, "exit_time": exit_, "pnl_pct": pnl,
        "signal_metadata": meta,
    }


# ── independence (same rule as the shadow evaluator) ─────────────────────


def test_independent_trades_overlap_collapses() -> None:
    trades = [
        _tr(entry=0, exit_=100),
        _tr(entry=50, exit_=150),   # overlaps first — drops
        _tr(entry=200, exit_=300),  # counts
    ]
    kept = _independent_trades(trades)
    assert len(kept) == 2
    assert kept[0]["entry_time"] == 0 and kept[1]["entry_time"] == 200


def test_independent_trades_per_symbol() -> None:
    trades = [
        _tr(symbol="BTC", entry=0, exit_=100),
        _tr(symbol="ETH", entry=50, exit_=150),   # different symbol — counts
    ]
    assert len(_independent_trades(trades)) == 2


def test_independent_trades_boundary_touch_counts() -> None:
    trades = [_tr(entry=0, exit_=100), _tr(entry=100, exit_=200)]
    assert len(_independent_trades(trades)) == 2


# ── bracket recovery from signal_metadata ────────────────────────────────


def test_bracket_recovery_uses_engine_atr_fallback() -> None:
    # engine stores atr_pct = stop_loss_pct / 2 → sl = 2*atr = 0.68%
    sl, tp = _bracket_from_metadata('{"atr_pct": 0.0034}')
    assert sl == pytest.approx(0.0068)
    assert tp == pytest.approx(0.0068)          # tp_r_mult = 1.0


def test_bracket_recovery_floors_at_half_pct() -> None:
    sl, tp = _bracket_from_metadata('{"atr_pct": 0.001}')
    assert sl == pytest.approx(0.005)
    assert tp == pytest.approx(0.005)


def test_bracket_recovery_missing_metadata() -> None:
    sl, tp = _bracket_from_metadata(None)
    assert sl == tp == pytest.approx(0.005)


# ── bracket simulator ────────────────────────────────────────────────────


def test_simulate_bracket_long_tp() -> None:
    candles = [(60_000, 101.0, 99.9, 100.8)]
    r = simulate_bracket(candles, 0, 100.0, "long", 0.005, 0.005)
    assert r == pytest.approx(0.005 - JEV_FEE_RT)


def test_simulate_bracket_long_sl() -> None:
    candles = [(60_000, 100.2, 99.0, 99.5)]
    r = simulate_bracket(candles, 0, 100.0, "long", 0.005, 0.005)
    assert r == pytest.approx(-0.005 - JEV_FEE_RT)


def test_simulate_bracket_short_sl() -> None:
    candles = [(60_000, 101.0, 99.9, 100.8)]
    r = simulate_bracket(candles, 0, 100.0, "short", 0.005, 0.005)
    assert r == pytest.approx(-0.005 - JEV_FEE_RT)


def test_simulate_bracket_ambiguous_candle_resolves_sl() -> None:
    candles = [(60_000, 101.0, 99.0, 100.0)]  # both barriers touched
    r = simulate_bracket(candles, 0, 100.0, "long", 0.005, 0.005)
    assert r == pytest.approx(-0.005 - JEV_FEE_RT)


def test_simulate_bracket_timeout_exits_at_last_close() -> None:
    candles = [(60_000, 100.2, 99.9, 100.2), (120_000, 100.3, 100.1, 100.3)]
    r = simulate_bracket(candles, 0, 100.0, "long", 0.005, 0.005)
    assert r == pytest.approx(0.003 - JEV_FEE_RT)


def test_simulate_bracket_no_candles_returns_none() -> None:
    assert simulate_bracket([], 0, 100.0, "long", 0.005, 0.005) is None


# ── random baseline ──────────────────────────────────────────────────────


def test_random_baseline_deterministic_same_seed() -> None:
    candles = {
        "BTC": [(i * 60_000 + 60_000, 100.5, 99.5, 100.1) for i in range(10)]
    }
    trades = [_tr(entry=0, exit_=600_000, meta='{"atr_pct": 0.0025}',
                  pnl=0.0)]
    for t in trades:
        t["sl_pct"], t["tp_pct"] = _bracket_from_metadata(t["signal_metadata"])
    a = random_baseline(trades, candles, n_runs=10, seed=42)
    b = random_baseline(trades, candles, n_runs=10, seed=42)
    assert a == b
    assert len(a) == 10


def test_random_baseline_coin_flip_varies_outcome() -> None:
    # Only-up candles: long wins, short loses — run totals must split into
    # distinct values, proving the side is actually randomized.
    candles = {
        "BTC": [(i * 60_000 + 60_000, 101.0, 99.8, 100.5) for i in range(10)]
    }
    trades = [_tr(entry=0, exit_=600_000, meta='{"atr_pct": 0.0025}')]
    for t in trades:
        t["sl_pct"], t["tp_pct"] = _bracket_from_metadata(t["signal_metadata"])
    runs = random_baseline(trades, candles, n_runs=20, seed=1)
    assert len(set(runs)) == 2
    assert max(runs) > 0 > min(runs)


def test_percentile_basic() -> None:
    assert _percentile([1.0, 2.0, 3.0, 4.0], 2.5) == pytest.approx(50.0)
    assert _percentile([1.0, 2.0], 99.0) == pytest.approx(100.0)
    assert _percentile([], 1.0) != _percentile([], 1.0)  # NaN


def test_geometry_boundary_is_2026_10_04_commit() -> None:
    # 96f1b15 committed 2026-10-04 14:39:40 UTC
    assert GEOMETRY_BOUNDARY_MS == 1791124780000
