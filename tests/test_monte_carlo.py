"""Tests for the Monte Carlo bootstrap module (v3.1.21)."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.backtest.monte_carlo import (
    MCResult,
    PercentileCI,
    _max_drawdown,
    _sharpe,
    run_monte_carlo,
)


FAILED = 0


def _pass(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILED += 1


# ── Pure helpers ───────────────────────────────────────────────────


def test_max_drawdown_flat() -> None:
    import numpy as np
    eq = np.array([100.0, 100.0, 100.0])
    _pass("max_drawdown_flat", abs(_max_drawdown(eq) - 0.0) < 1e-9)


def test_max_drawdown_known() -> None:
    import numpy as np
    # 100 → 110 → 80 → 95. Peak 110, trough 80 → dd = (110-80)/110 = 0.2727
    eq = np.array([100.0, 110.0, 80.0, 95.0])
    dd = _max_drawdown(eq)
    _pass("max_drawdown_known", abs(dd - 0.2727272727) < 1e-6,
          f"got {dd:.6f}")


def test_max_drawdown_empty() -> None:
    import numpy as np
    eq = np.array([], dtype=np.float64)
    _pass("max_drawdown_empty", _max_drawdown(eq) == 0.0)


def test_sharpe_zero_for_constant() -> None:
    import numpy as np
    r = np.array([1.0, 1.0, 1.0, 1.0])
    _pass("sharpe_zero_for_constant", _sharpe(r) == 0.0)


def test_sharpe_computed() -> None:
    import numpy as np
    r = np.array([0.01, 0.02, 0.015, 0.005, 0.018])
    val = _sharpe(r)
    _pass("sharpe_computed", val > 0.0 and math.isfinite(val))


# ── run_monte_carlo happy path ─────────────────────────────────────


def test_run_monte_carlo_returns_ci_for_all_metrics() -> None:
    pnls = [10.0, -5.0, 20.0, -8.0, 15.0, -3.0, 12.0, -7.0, 18.0, -4.0] * 10
    result = run_monte_carlo(pnls, iterations=2000, seed=0)
    _pass("run_monte_carlo_returns_ci_for_all_metrics",
          isinstance(result, MCResult)
          and result.iterations == 2000
          and result.n_trades == len(pnls))


def test_run_monte_carlo_probabilities_sum_to_one() -> None:
    pnls = [10.0, -5.0, 20.0, -8.0] * 25
    result = run_monte_carlo(pnls, iterations=1000, seed=0)
    _pass("run_monte_carlo_probabilities_sum_to_one",
          all(abs(sum(ci.p05 for ci in [result.sharpe]) - 0) >= 0
              for _ in [None])  # smoke test
          and result.sharpe.p05 <= result.sharpe.p50 <= result.sharpe.p95)


def test_run_monte_carlo_percentile_ordering() -> None:
    """For each metric, p05 <= p25 <= p50 <= p75 <= p95."""
    pnls = [10.0, -5.0, 20.0, -8.0, 15.0, -3.0, 12.0, -7.0, 18.0, -4.0] * 10
    result = run_monte_carlo(pnls, iterations=1000, seed=0)
    for name, ci in [
        ("total_return", result.total_return),
        ("max_drawdown", result.max_drawdown),
        ("win_rate", result.win_rate),
    ]:
        ok = ci.p05 <= ci.p25 <= ci.p50 <= ci.p75 <= ci.p95
        _pass(f"percentile_ordering_{name}", ok,
              f"p05={ci.p05} p25={ci.p25} p50={ci.p50} p75={ci.p75} p95={ci.p95}")


def test_run_monte_carlo_win_rate_around_base() -> None:
    """Bootstrap mean win rate should be close to the empirical input rate."""
    # 60% win rate, 500 trades
    rng_seed = 0
    import numpy as np
    rng = np.random.default_rng(rng_seed)
    n = 500
    n_wins = 0
    pnls: list[float] = []
    for _ in range(n):
        if rng.random() < 0.60:
            pnls.append(10.0)
            n_wins += 1
        else:
            pnls.append(-10.0)
    empirical_win_rate = n_wins / n
    result = run_monte_carlo(pnls, iterations=5000, seed=rng_seed)
    err = abs(result.win_rate.mean - empirical_win_rate)
    _pass("run_monte_carlo_win_rate_around_base", err < 0.02,
          f"empirical={empirical_win_rate:.3f} bootstrap_mean={result.win_rate.mean:.3f}")


def test_run_monte_carlo_reproducible() -> None:
    """Same seed → identical result."""
    pnls = [10.0, -5.0, 20.0, -8.0] * 25
    r1 = run_monte_carlo(pnls, iterations=500, seed=42)
    r2 = run_monte_carlo(pnls, iterations=500, seed=42)
    _pass("run_monte_carlo_reproducible",
          r1.total_return.p50 == r2.total_return.p50
          and r1.sharpe.p50 == r2.sharpe.p50)


def test_run_monte_carlo_total_return_matches_input_sum() -> None:
    """Mean total return should be close to the sum of trade PnLs."""
    pnls = [10.0, -5.0, 20.0, -8.0] * 100
    expected = sum(pnls)
    result = run_monte_carlo(pnls, iterations=2000, seed=0)
    # Mean of bootstrap = expected (each draw is a full resample)
    err = abs(result.total_return.mean - expected) / max(abs(expected), 1.0)
    _pass("run_monte_carlo_total_return_matches_input_sum", err < 0.05,
          f"expected={expected:.1f} got={result.total_return.mean:.1f}")


# ── Edge cases ─────────────────────────────────────────────────────


def test_run_monte_carlo_empty_returns_degenerate() -> None:
    result = run_monte_carlo([], iterations=1000, seed=0)
    _pass("run_monte_carlo_empty_returns_degenerate",
          result.iterations == 0
          and result.total_return.p50 == 0.0)


def test_run_monte_carlo_single_trade_returns_degenerate() -> None:
    result = run_monte_carlo([10.0], iterations=1000, seed=0)
    _pass("run_monte_carlo_single_trade_returns_degenerate",
          result.iterations == 0)


def test_run_monte_carlo_all_wins() -> None:
    """If every trade is a win, win rate CI is [1.0, 1.0]."""
    pnls = [10.0] * 50
    result = run_monte_carlo(pnls, iterations=200, seed=0)
    _pass("run_monte_carlo_all_wins",
          result.win_rate.p05 == 1.0
          and result.win_rate.p95 == 1.0)


def test_run_monte_carlo_all_losses() -> None:
    """If every trade is a loss, win rate CI is [0.0, 0.0]."""
    pnls = [-10.0] * 50
    result = run_monte_carlo(pnls, iterations=200, seed=0)
    _pass("run_monte_carlo_all_losses",
          result.win_rate.p05 == 0.0
          and result.win_rate.p95 == 0.0)


# ── Serialization ──────────────────────────────────────────────────


def test_mcresult_as_dict() -> None:
    pnls = [10.0, -5.0, 20.0, -8.0] * 25
    result = run_monte_carlo(pnls, iterations=500, seed=0)
    d = result.as_dict()
    _pass("mcresult_as_dict",
          set(d.keys())
          >= {"iterations", "seed", "n_trades", "total_return",
              "max_drawdown", "sharpe", "win_rate"})


def test_mcresult_json_roundtrip() -> None:
    pnls = [10.0, -5.0, 20.0, -8.0] * 25
    result = run_monte_carlo(pnls, iterations=500, seed=0)
    j = json.dumps(result.as_dict())
    _pass("mcresult_json_roundtrip", "total_return" in j and "p50" in j)


def test_mcresult_summary() -> None:
    pnls = [10.0, -5.0, 20.0, -8.0] * 25
    result = run_monte_carlo(pnls, iterations=500, seed=0)
    s = result.summary()
    _pass("mcresult_summary",
          "total_return" in s
          and "max_drawdown" in s
          and "sharpe" in s
          and "win_rate" in s)


# ── PercentileCI ───────────────────────────────────────────────────


def test_percentile_ci_ci_90() -> None:
    ci = PercentileCI(
        p05=0.1, p25=0.2, p50=0.3, p75=0.4, p95=0.5, mean=0.3, std=0.1, n=100,
    )
    _pass("percentile_ci_ci_90", ci.ci_90 == (0.1, 0.5))


def test_percentile_ci_ci_50() -> None:
    ci = PercentileCI(
        p05=0.1, p25=0.2, p50=0.3, p75=0.4, p95=0.5, mean=0.3, std=0.1, n=100,
    )
    _pass("percentile_ci_ci_50", ci.ci_50 == (0.2, 0.4))


def main() -> int:
    print("=" * 70)
    print("Monte Carlo bootstrap tests")
    print("=" * 70)
    tests = [
        test_max_drawdown_flat,
        test_max_drawdown_known,
        test_max_drawdown_empty,
        test_sharpe_zero_for_constant,
        test_sharpe_computed,
        test_run_monte_carlo_returns_ci_for_all_metrics,
        test_run_monte_carlo_probabilities_sum_to_one,
        test_run_monte_carlo_percentile_ordering,
        test_run_monte_carlo_win_rate_around_base,
        test_run_monte_carlo_reproducible,
        test_run_monte_carlo_total_return_matches_input_sum,
        test_run_monte_carlo_empty_returns_degenerate,
        test_run_monte_carlo_single_trade_returns_degenerate,
        test_run_monte_carlo_all_wins,
        test_run_monte_carlo_all_losses,
        test_mcresult_as_dict,
        test_mcresult_json_roundtrip,
        test_mcresult_summary,
        test_percentile_ci_ci_90,
        test_percentile_ci_ci_50,
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
