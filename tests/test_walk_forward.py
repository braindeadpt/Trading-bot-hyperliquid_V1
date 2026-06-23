"""Tests for the walk-forward optimization framework (v3.1.21)."""

from __future__ import annotations

import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.backtest.walk_forward import (
    ParamGrid,
    WalkForwardReport,
    WindowConfig,
    WindowResult,
    _aggregate,
    _ms,
    _train_score,
    render_report,
)


FAILED = 0


def _pass(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILED += 1


# ── ParamGrid ───────────────────────────────────────────────────────


def test_param_grid_empty_iterates_once() -> None:
    grid = ParamGrid()
    points = list(grid.iter_points())
    _pass("param_grid_empty_iterates_once",
          len(points) == 1 and points[0] == {})


def test_param_grid_single_axis() -> None:
    grid = ParamGrid(axes={"min_adx": [20, 25, 30]})
    points = list(grid.iter_points())
    _pass("param_grid_single_axis",
          len(points) == 3
          and points[0]["min_adx"] == 20
          and points[2]["min_adx"] == 30)


def test_param_grid_multi_axis_cartesian() -> None:
    grid = ParamGrid(axes={"min_adx": [20, 30], "max_adx": [35, 45]})
    _pass("param_grid_multi_axis_cartesian", grid.size() == 4)


def test_param_grid_size_empty() -> None:
    _pass("param_grid_size_empty", ParamGrid().size() == 1)


def test_param_grid_size_multi() -> None:
    grid = ParamGrid(axes={"a": [1, 2, 3], "b": [4, 5]})
    _pass("param_grid_size_multi", grid.size() == 6)


# ── WindowConfig ────────────────────────────────────────────────────


def test_window_config_default_step() -> None:
    wc = WindowConfig(train_days=30, test_days=14, n_windows=6)
    _pass("window_config_default_step", wc.step_days is None)


def test_window_config_step_overrides() -> None:
    wc = WindowConfig(train_days=30, test_days=14, step_days=7, n_windows=4)
    _pass("window_config_step_overrides", wc.step_days == 7)


# ── _train_score ────────────────────────────────────────────────────


def test_train_score_normal() -> None:
    metrics = {"sharpe_ratio": 1.5, "__trades": 10.0}
    _pass("train_score_normal", _train_score(metrics) == 1.5)


def test_train_score_penalizes_few_trades() -> None:
    metrics = {"sharpe_ratio": 1.0, "__trades": 1.0}
    score = _train_score(metrics)
    _pass("train_score_penalizes_few_trades", score < 1.0)


def test_train_score_handles_zero_trades() -> None:
    metrics = {"sharpe_ratio": 0.0, "__trades": 0.0}
    _pass("train_score_handles_zero_trades", math.isfinite(_train_score(metrics)))


# ── _aggregate ─────────────────────────────────────────────────────


def test_aggregate_empty() -> None:
    agg = _aggregate([])
    _pass("aggregate_empty", agg["n_windows"] == 0.0 and agg["avg_oos_sharpe"] == 0.0)


def test_aggregate_single_window() -> None:
    w = WindowResult(
        window_index=0,
        train_start_ms=0, train_end_ms=0, test_start_ms=0, test_end_ms=0,
        best_params={}, train_metrics={},
        test_metrics={"sharpe_ratio": 0.5, "win_rate": 0.6, "return_pct": 0.1,
                      "max_drawdown_pct": 0.05, "__trades": 5.0},
        n_param_combos=1,
    )
    agg = _aggregate([w])
    _pass("aggregate_single_window",
          agg["n_windows"] == 1.0
          and abs(agg["avg_oos_sharpe"] - 0.5) < 1e-9
          and agg["positive_oos_windows"] == 1.0
          and agg["total_oos_trades"] == 5.0)


def test_aggregate_counts_positive_windows() -> None:
    w_pos = WindowResult(
        window_index=0, train_start_ms=0, train_end_ms=0,
        test_start_ms=0, test_end_ms=0, best_params={}, train_metrics={},
        test_metrics={"return_pct": 0.05, "sharpe_ratio": 0.1, "win_rate": 0.5,
                      "max_drawdown_pct": 0.05, "__trades": 5.0},
        n_param_combos=1,
    )
    w_neg = WindowResult(
        window_index=1, train_start_ms=0, train_end_ms=0,
        test_start_ms=0, test_end_ms=0, best_params={}, train_metrics={},
        test_metrics={"return_pct": -0.05, "sharpe_ratio": -0.1, "win_rate": 0.4,
                      "max_drawdown_pct": 0.1, "__trades": 5.0},
        n_param_combos=1,
    )
    agg = _aggregate([w_pos, w_neg])
    _pass("aggregate_counts_positive_windows", agg["positive_oos_windows"] == 1.0)


# ── _ms ──────────────────────────────────────────────────────────────


def test_ms_returns_positive_int() -> None:
    _pass("ms_returns_positive_int", _ms(0) > 0 and isinstance(_ms(0), int))


def test_ms_days_ago() -> None:
    now = _ms(0)
    past = _ms(7)
    _pass("ms_days_ago", now - past >= 7 * 86_400_000 - 1000)


# ── render_report ──────────────────────────────────────────────────


def test_render_report_contains_header() -> None:
    wc = WindowConfig()
    w = WindowResult(
        window_index=0, train_start_ms=1_700_000_000_000, train_end_ms=1_700_500_000_000,
        test_start_ms=1_700_500_000_000, test_end_ms=1_701_000_000_000,
        best_params={"min_adx": 25}, train_metrics={},
        test_metrics={"sharpe_ratio": 0.5, "win_rate": 0.6, "return_pct": 0.1,
                      "max_drawdown_pct": 0.05, "__trades": 5.0},
        n_param_combos=3,
    )
    report = WalkForwardReport(
        strategy_name="CVDOrderFlow",
        config_path="<inline>",
        window_config=wc,
        windows=[w],
        aggregate=_aggregate([w]),
    )
    text = render_report(report)
    _pass("render_report_contains_header",
          "WALK-FORWARD REPORT" in text
          and "CVDOrderFlow" in text
          and "OOS Sharpe" in text
          and "min_adx=25" in text)


def test_render_report_handles_empty() -> None:
    wc = WindowConfig()
    report = WalkForwardReport(
        strategy_name="X", config_path="<inline>", window_config=wc,
        windows=[], aggregate=_aggregate([]),
    )
    text = render_report(report)
    _pass("render_report_handles_empty", "0 windows" in text)


# ── WindowResult.as_dict ───────────────────────────────────────────


def test_window_result_as_dict_keys() -> None:
    w = WindowResult(
        window_index=0, train_start_ms=1, train_end_ms=2,
        test_start_ms=3, test_end_ms=4, best_params={"x": 1},
        train_metrics={"a": 1.0}, test_metrics={"b": 2.0},
        n_param_combos=4,
    )
    d = w.as_dict()
    _pass("window_result_as_dict_keys",
          set(d.keys()) >= {
              "window_index", "train_start_ms", "train_end_ms",
              "test_start_ms", "test_end_ms", "best_params",
              "train_metrics", "test_metrics", "n_param_combos",
          }
          and d["best_params"] == {"x": 1}
          and d["n_param_combos"] == 4)


# ── Report.as_dict ─────────────────────────────────────────────────


def test_report_as_dict_keys() -> None:
    wc = WindowConfig(n_windows=2)
    report = WalkForwardReport(
        strategy_name="S", config_path="<inline>", window_config=wc,
        windows=[], aggregate=_aggregate([]),
    )
    d = report.as_dict()
    _pass("report_as_dict_keys",
          "strategy_name" in d
          and "config_path" in d
          and "window_config" in d
          and "windows" in d
          and "aggregate" in d
          and d["window_config"]["n_windows"] == 2)


def main() -> int:
    print("=" * 70)
    print("Walk-forward optimization tests")
    print("=" * 70)
    tests = [
        test_param_grid_empty_iterates_once,
        test_param_grid_single_axis,
        test_param_grid_multi_axis_cartesian,
        test_param_grid_size_empty,
        test_param_grid_size_multi,
        test_window_config_default_step,
        test_window_config_step_overrides,
        test_train_score_normal,
        test_train_score_penalizes_few_trades,
        test_train_score_handles_zero_trades,
        test_aggregate_empty,
        test_aggregate_single_window,
        test_aggregate_counts_positive_windows,
        test_ms_returns_positive_int,
        test_ms_days_ago,
        test_render_report_contains_header,
        test_render_report_handles_empty,
        test_window_result_as_dict_keys,
        test_report_as_dict_keys,
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
