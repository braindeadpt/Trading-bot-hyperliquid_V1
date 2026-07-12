"""Tests for the Monte Carlo bootstrap module (src/backtest/monte_carlo.py).

Rewritten against the current public API: ``MCMetrics``, ``bootstrap_metrics``,
``block_bootstrap_metrics``, ``group_trades_into_blocks``, plus a handful of
pure internal helpers with clear contracts. The previous version of this file
targeted a stale API (``MCResult``, ``PercentileCI``, ``run_monte_carlo``) that
no longer exists and was excluded from collection; this file replaces it.
"""
from __future__ import annotations

import math

import pytest

from src.backtest.monte_carlo import (
    MCMetrics,
    _empty_mc,
    _pct,
    _trade_annualization_factor,
    block_bootstrap_metrics,
    bootstrap_metrics,
    group_trades_into_blocks,
)

pytestmark = pytest.mark.unit


def _make_trades(pnls, start_ts=1_700_000_000_000, step_ms=3_600_000):
    return [
        {"pnl_usd": p, "exit_time": start_ts + i * step_ms}
        for i, p in enumerate(pnls)
    ]


# ---------------------------------------------------------------------------
# bootstrap_metrics (IID resampling)
# ---------------------------------------------------------------------------


def test_bootstrap_metrics_deterministic_with_seed():
    trades = _make_trades([10.0, -5.0, 20.0, -8.0, 15.0, -3.0, 12.0, -7.0, 18.0, -4.0])
    a = bootstrap_metrics(trades, n_iter=300, seed=123)
    b = bootstrap_metrics(trades, n_iter=300, seed=123)
    assert a.pf_median == b.pf_median
    assert a.sharpe_median == b.sharpe_median
    assert a.pnl_median == b.pnl_median
    assert a.max_dd_median == b.max_dd_median


def test_bootstrap_metrics_percentile_ordering():
    trades = _make_trades(
        [10.0, -5.0, 20.0, -8.0, 15.0, -3.0, 12.0, -7.0, 18.0, -4.0] * 5
    )
    m = bootstrap_metrics(trades, n_iter=500, seed=7)
    assert m.pf_p05 <= m.pf_median <= m.pf_p95
    assert m.sharpe_p05 <= m.sharpe_median <= m.sharpe_p95
    assert m.pnl_p05 <= m.pnl_median <= m.pnl_p95
    assert m.max_dd_median <= m.max_dd_p95


def test_bootstrap_metrics_sane_fields():
    trades = _make_trades([10.0, -5.0, 20.0, -8.0])
    m = bootstrap_metrics(trades, n_iter=200, seed=0)
    assert isinstance(m, MCMetrics)
    assert m.n_trades == 4
    assert m.n_iter == 200
    assert m.bootstrap_mode == "iid"
    assert m.block_count == 0
    assert 0.0 <= m.prob_profitable <= 1.0


def test_bootstrap_metrics_empty_trades_returns_empty_mc():
    m = bootstrap_metrics([], n_iter=500, seed=0)
    expected = _empty_mc(500, mode="iid")
    assert m == expected
    assert m.n_trades == 0
    assert m.bootstrap_mode == "iid"
    assert m.pf_median == 0.0
    assert m.prob_profitable == 0.0


def test_bootstrap_metrics_single_trade_does_not_crash():
    trades = _make_trades([10.0])
    m = bootstrap_metrics(trades, n_iter=100, seed=0)
    assert m.n_trades == 1
    # Every resample degenerates to the same single value -> zero spread.
    assert m.pf_median == m.pf_p05 == m.pf_p95
    assert m.pnl_median == 10.0
    # A single-point return series has zero variance -> sharpe is defined as 0.
    assert m.sharpe_median == 0.0


def test_bootstrap_metrics_all_losses_pf_zero_and_never_profitable():
    trades = _make_trades([-10.0] * 20)
    m = bootstrap_metrics(trades, n_iter=200, seed=0)
    assert m.pf_median == 0.0
    assert m.prob_profitable == 0.0


def test_bootstrap_metrics_all_wins_pf_sentinel_and_always_profitable():
    trades = _make_trades([10.0] * 20)
    m = bootstrap_metrics(trades, n_iter=200, seed=0)
    assert m.prob_profitable == 1.0
    # Infinite profit factor (no losses) is mapped to the 99.0 sentinel.
    assert m.pf_median == 99.0


# ---------------------------------------------------------------------------
# group_trades_into_blocks
# ---------------------------------------------------------------------------


def test_group_trades_into_blocks_day_mode_groups_by_utc_day():
    trades = [
        {"pnl_usd": 10.0, "exit_time": 1_700_000_000_000},
        {"pnl_usd": -5.0, "exit_time": 1_700_000_000_000 + 3_600_000},
        {"pnl_usd": 20.0, "exit_time": 1_700_086_400_000},
    ]
    blocks = group_trades_into_blocks(trades, mode="day")
    assert len(blocks) == 2
    assert sum(len(b) for b in blocks) == 3
    assert sorted(len(b) for b in blocks) == [1, 2]


def test_group_trades_into_blocks_regime_mode_groups_by_regime():
    day_a = 1_700_000_000_000
    day_b = day_a + 86_400_000
    trades = [
        {"pnl_usd": 1.0, "exit_time": day_a, "metadata": {}},
        {"pnl_usd": 2.0, "exit_time": day_a + 3_600_000, "metadata": {}},
        {"pnl_usd": 3.0, "exit_time": day_b, "metadata": {"regime": "trend"}},
        {"pnl_usd": 4.0, "exit_time": day_b + 3_600_000, "metadata": {"regime": "trend"}},
    ]
    blocks = group_trades_into_blocks(trades, mode="regime")
    assert len(blocks) == 2
    assert sorted(len(b) for b in blocks) == [2, 2]


def test_group_trades_into_blocks_regime_invalid_labels_fallback_to_utc_day():
    """Trades with no/invalid regime label fall back to a UTC-day bucket."""
    ts = 1_700_000_000_000
    trades = [
        {"pnl_usd": 1.0, "exit_time": ts, "metadata": {"regime": "none"}},
        {"pnl_usd": 2.0, "exit_time": ts + 3_600_000, "metadata": {"regime": None}},
    ]
    blocks = group_trades_into_blocks(trades, mode="regime")
    assert len(blocks) == 1
    assert sorted(blocks[0]) == [1.0, 2.0]


def test_group_trades_into_blocks_empty():
    assert group_trades_into_blocks([], mode="day") == []


# ---------------------------------------------------------------------------
# block_bootstrap_metrics
# ---------------------------------------------------------------------------


def test_block_bootstrap_metrics_valid_mcmetrics_and_block_count():
    trades = [
        {"pnl_usd": 12.0, "exit_time": 1_700_000_000_000 + i * 86_400_000}
        for i in range(8)
    ]
    m = block_bootstrap_metrics(trades, n_iter=200, seed=123, block_mode="day")
    assert isinstance(m, MCMetrics)
    assert m.bootstrap_mode == "block_day"
    assert m.block_count == 8  # each trade lands on its own UTC day
    assert m.n_trades == 8


def test_block_bootstrap_metrics_reproducible_with_seed():
    trades = [
        {"pnl_usd": 12.0, "exit_time": 1_700_000_000_000 + i * 86_400_000}
        for i in range(8)
    ]
    a = block_bootstrap_metrics(trades, n_iter=200, seed=123)
    b = block_bootstrap_metrics(trades, n_iter=200, seed=123)
    assert a.pf_median == b.pf_median
    assert a.sharpe_median == b.sharpe_median
    assert a.pnl_median == b.pnl_median


def test_block_bootstrap_metrics_regime_mode():
    day_a = 1_700_000_000_000
    day_b = day_a + 86_400_000
    trades = [
        {"pnl_usd": 1.0, "exit_time": day_a, "metadata": {}},
        {"pnl_usd": 2.0, "exit_time": day_a + 3_600_000, "metadata": {}},
        {"pnl_usd": 3.0, "exit_time": day_b, "metadata": {"regime": "trend"}},
        {"pnl_usd": 4.0, "exit_time": day_b + 3_600_000, "metadata": {"regime": "trend"}},
    ]
    m = block_bootstrap_metrics(trades, n_iter=100, seed=1, block_mode="regime")
    assert m.bootstrap_mode == "block_regime"
    assert m.block_count == 2


def test_block_bootstrap_metrics_empty_trades_returns_empty_mc():
    m = block_bootstrap_metrics([], n_iter=100, seed=0, block_mode="day")
    assert m.n_trades == 0
    assert m.bootstrap_mode == "block_day"
    assert m.block_count == 0


# ---------------------------------------------------------------------------
# Pure helpers with clear contracts
# ---------------------------------------------------------------------------


def test_pct_helper_bounds_and_ordering():
    values = sorted([1.0, 2.0, 3.0, 4.0, 5.0])
    assert _pct(values, 0.0) == 1.0
    assert _pct(values, 1.0) == 5.0
    assert _pct(values, 0.5) <= _pct(values, 0.95)


def test_pct_helper_empty_list_returns_zero():
    assert _pct([], 0.5) == 0.0


def test_trade_annualization_factor_defaults_for_too_few_trades():
    assert _trade_annualization_factor([], 0) == math.sqrt(365.25)
    assert _trade_annualization_factor([{"exit_time": 1}], 1) == math.sqrt(365.25)


def test_trade_annualization_factor_scales_with_trade_frequency():
    trades = [
        {"exit_time": 1_700_000_000_000},
        {"exit_time": 1_700_000_000_000 + 86_400_000},
    ]
    factor = _trade_annualization_factor(trades, 2)
    assert factor >= 1.0
    assert math.isfinite(factor)
