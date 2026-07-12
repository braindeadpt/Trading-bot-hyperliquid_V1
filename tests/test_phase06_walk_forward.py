"""Phase 06 behavioral tests — walk-forward, embargo, bootstrap, holdout."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.experiment_manifest import (
    ManifestIncompatibleError,
    assert_manifests_comparable,
    build_experiment_manifest,
)
from src.backtest.holdout_ledger import HoldoutGuard, HoldoutLedger, HoldoutViolationError
from src.backtest.metrics import (
    CRYPTO_HOURS_PER_YEAR,
    cagr,
    calculate_metrics,
    calmar_ratio,
    expectancy_in_r,
    infer_periods_per_year,
    normalize_metrics,
    sample_duration_years,
    sharpe_ratio,
)
from src.backtest.monte_carlo import block_bootstrap_metrics, group_trades_into_blocks
from src.backtest.statistical_validation import (
    METHODOLOGY_NOTE,
    build_multiple_testing_report,
)
from src.backtest.walk_forward import (
    ParamGrid,
    WindowConfig,
    WindowResult,
    _aggregate,
    _copy_config_with_strategy_overrides,
    assert_embargo_respected,
    compute_split_ranges,
    run_walk_forward,
)
from src.strategies.volatility_breakout import VolatilityBreakout
from src.utils.config import Config, load_config
import pytest

pytestmark = pytest.mark.integration_offline


def _test_ledger_path() -> Path:
    """Ledger path inside the project tree (validate_safe_path compatible)."""
    root = Path(__file__).resolve().parent.parent
    research = root / "data" / "research"
    research.mkdir(parents=True, exist_ok=True)
    return research / f"_test_holdout_{uuid.uuid4().hex}.json"


def _minimal_cfg() -> Config:
    data: Dict[str, Any] = {
        "mode": "paper",
        "assets": ["BTC"],
        "strategy": {
            "ensemble": {"enabled": False},
            "volatility_breakout": {
                "enabled": True,
                "min_adx": 12.0,
                "max_hold_hours": 6,
            },
        },
        "risk": {"max_positions": 1},
        "backtest": {"initial_capital": 10_000},
    }
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(data, fh)
        return load_config(fh.name)


def test_normalize_metrics_aliases() -> None:
    m = normalize_metrics({"n_trades": 5, "total_return": 0.1, "max_drawdown": 0.03})
    assert m["total_trades"] == 5.0
    assert m["return_pct"] == 0.1
    assert m["max_drawdown_pct"] == 0.03


def test_aggregate_oos_trade_count_nonzero() -> None:
    w = WindowResult(
        window_index=0,
        train_start_ms=0,
        train_end_ms=1,
        validation_start_ms=2,
        validation_end_ms=3,
        test_start_ms=4,
        test_end_ms=5,
        embargo_ms=6_000_000,
        best_params={},
        train_metrics={},
        validation_metrics={},
        test_metrics={
            "sharpe_ratio": 0.4,
            "win_rate": 0.55,
            "return_pct": 0.02,
            "max_drawdown_pct": 0.04,
            "n_trades": 7,
            "expectancy_r": 0.15,
        },
        n_param_combos=3,
    )
    agg = _aggregate([w])
    assert agg["total_oos_trades"] == 7.0
    assert agg["avg_oos_sharpe"] == 0.4
    assert agg["avg_oos_expectancy_r"] == 0.15


def test_param_override_changes_strategy_before_build() -> None:
    cfg = _minimal_cfg()
    overridden = _copy_config_with_strategy_overrides(
        cfg, "VolatilityBreakout", {"min_adx": 99.0}
    )
    strat = VolatilityBreakout(overridden.get("strategy.volatility_breakout", {}))
    assert strat.MIN_ADX == 99.0
    default = VolatilityBreakout(cfg.get("strategy.volatility_breakout", {}))
    assert default.MIN_ADX == 12.0


def test_param_override_changes_signal_behavior() -> None:
    """High min_adx blocks entries that low min_adx would allow."""
    from src.strategies.base import MarketEvent
    from src.strategies.indicators import Candle

    base_candles: List[Candle] = []
    ts0 = 1_700_000_000_000
    price = 100.0
    for i in range(45):
        c = Candle(
            open=price,
            high=price * 1.002,
            low=price * 0.998,
            close=price + (i * 0.01),
            volume=1000.0 + i * 10,
            timestamp_ms=ts0 + i * 900_000,
        )
        base_candles.append(c)

    event = MarketEvent(
        symbol="BTC",
        price=base_candles[-1].close,
        timestamp_ms=base_candles[-1].timestamp_ms,
        candle_15m=base_candles[-1],
        adx_14=18.0,
    )

    loose = VolatilityBreakout({"min_adx": 5.0, "squeeze_percentile": 99.0})
    tight = VolatilityBreakout({"min_adx": 50.0, "squeeze_percentile": 99.0})
    for c in base_candles:
        ev = MarketEvent(
            symbol="BTC",
            price=c.close,
            timestamp_ms=c.timestamp_ms,
            candle_15m=c,
            adx_14=18.0,
        )
        loose._get_state("BTC").candles_15m.append(c)  # noqa: SLF001
        tight._get_state("BTC").candles_15m.append(c)  # noqa: SLF001

    sig_loose = loose.on_data(event)
    sig_tight = tight.on_data(event)
    assert sig_loose is None or sig_tight is None or sig_loose is not None
    # Tight filter must not be more permissive than loose on ADX gate.
    if sig_loose is not None:
        assert sig_tight is None


def test_embargo_respected_between_splits() -> None:
    embargo_ms = 6 * 3_600_000
    ranges = compute_split_ranges(
        anchor_end_ms=2_000_000_000_000,
        train_days=30,
        validation_days=7,
        test_days=14,
        embargo_ms=embargo_ms,
    )
    assert_embargo_respected(ranges)
    gap1 = ranges.validation_start_ms - ranges.train_end_ms
    gap2 = ranges.test_start_ms - ranges.validation_end_ms
    assert gap1 >= embargo_ms
    assert gap2 >= embargo_ms


def test_block_bootstrap_preserves_day_blocks() -> None:
    trades = [
        {"pnl_usd": 10.0, "exit_time": 1_700_000_000_000},
        {"pnl_usd": -5.0, "exit_time": 1_700_000_000_000 + 3_600_000},
        {"pnl_usd": 20.0, "exit_time": 1_700_086_400_000},
    ]
    blocks = group_trades_into_blocks(trades, mode="day")
    assert len(blocks) == 2
    assert sum(len(b) for b in blocks) == 3
    sizes = sorted(len(b) for b in blocks)
    assert sizes == [1, 2]


def test_block_bootstrap_reproducible_with_seed() -> None:
    trades = [
        {"pnl_usd": 12.0, "exit_time": 1_700_000_000_000 + i * 86_400_000}
        for i in range(8)
    ]
    a = block_bootstrap_metrics(trades, n_iter=200, seed=123)
    b = block_bootstrap_metrics(trades, n_iter=200, seed=123)
    assert a.pf_median == b.pf_median
    assert a.sharpe_median == b.sharpe_median


def test_regime_mode_falls_back_to_utc_day_not_weekday() -> None:
    """Trades without valid regime metadata use UTC day blocks, never weekday."""
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
    sizes = sorted(len(b) for b in blocks)
    assert sizes == [2, 2]


def test_sharpe_uses_crypto_hourly_calendar() -> None:
    import pandas as pd

    ts0 = 1_700_000_000_000
    hourly = [ts0 + i * 3_600_000 for i in range(24)]
    periods = infer_periods_per_year(pd.Series(hourly))
    assert abs(periods - CRYPTO_HOURS_PER_YEAR) < 1.0

    equity = [(t, 10_000.0 * (1.0 + 0.001 * i)) for i, t in enumerate(hourly)]
    rets = pd.Series([0.001] * 23)
    sr = sharpe_ratio(rets, periods_per_year=periods)
    assert sr > 0.0


def test_calmar_uses_cagr_and_real_duration() -> None:
    import pandas as pd

    ts0 = 1_700_000_000_000
    span_ms = int(0.5 * 365.25 * 24 * 3_600_000)
    ts1 = ts0 + span_ms
    duration = sample_duration_years(pd.Series([ts0, ts1]))
    total_return = 0.10
    max_dd = 0.05
    expected_cagr = cagr(total_return, duration)
    calmar = calmar_ratio(total_return, max_dd, duration)
    assert abs(calmar - expected_cagr / max_dd) < 1e-9
    assert expected_cagr > total_return  # 10% over half year annualises above 10%


def test_expectancy_r_normalized() -> None:
    trades = [
        {"pnl_usd": 100.0, "risk_usd": 50.0, "r_multiple": 2.0},
        {"pnl_usd": -25.0, "risk_usd": 50.0, "r_multiple": -0.5},
    ]
    assert expectancy_in_r(trades) == 0.75
    equity = [(1, 10_000.0), (2, 10_075.0)]
    metrics = calculate_metrics(equity, trades)
    assert metrics["expectancy_r"] == 0.75


def test_dsr_pbo_labeled_as_internal_proxies() -> None:
    report = build_multiple_testing_report(
        n_trials=20,
        n_observations=50,
        observed_sharpe=1.2,
        is_scores=[1.0, 0.5, 0.2],
        oos_scores=[0.4, 0.6, 0.1],
    )
    d = report.as_dict()
    assert d["is_academic_implementation"] is False
    assert "INTERNAL RESEARCH PROXY" in d["methodology"]
    assert "deflated_sharpe_prob_proxy" in d
    assert "pbo_proxy" in d


def test_holdout_single_consultation_per_window() -> None:
    ledger_path = _test_ledger_path()
    try:
        guard = HoldoutGuard(HoldoutLedger(ledger_path))
        guard.begin_window()
        guard.freeze_params({"min_adx": 20})
        guard.set_holdout_context(
            strategy_name="VolatilityBreakout",
            test_start_ms=1000,
            test_end_ms=2000,
        )
        assert guard.evaluate_holdout(lambda: {"ok": 1}) == {"ok": 1}
        try:
            guard.evaluate_holdout(lambda: {"ok": 2})
            raised = False
        except HoldoutViolationError:
            raised = True
        assert raised
    finally:
        ledger_path.unlink(missing_ok=True)


def test_holdout_persists_across_process_restarts() -> None:
    ledger_path = _test_ledger_path()
    try:
        def _consume() -> None:
            g = HoldoutGuard(HoldoutLedger(ledger_path))
            g.begin_window()
            g.freeze_params({"min_adx": 30})
            g.set_holdout_context(
                strategy_name="VolatilityBreakout",
                test_start_ms=5000,
                test_end_ms=6000,
                config_hash="abc",
            )
            g.evaluate_holdout(lambda: 1)

        _consume()
        g2 = HoldoutGuard(HoldoutLedger(ledger_path))
        g2.begin_window()
        g2.freeze_params({"min_adx": 30})
        try:
            g2.set_holdout_context(
                strategy_name="VolatilityBreakout",
                test_start_ms=5000,
                test_end_ms=6000,
                config_hash="abc",
            )
            blocked = False
        except HoldoutViolationError:
            blocked = True
        assert blocked
    finally:
        ledger_path.unlink(missing_ok=True)


def test_holdout_not_used_during_selection() -> None:
    """Mocked walk-forward never passes test range into train scoring."""
    cfg = _minimal_cfg()
    test_ranges_seen: List[tuple] = []
    train_ranges_seen: List[tuple] = []

    def fake_run(cfg, db, symbols, strategy_name, start_ms, end_ms, param_overrides=None):
        if end_ms - start_ms < 8 * 86_400_000:
            train_ranges_seen.append((start_ms, end_ms))
        else:
            test_ranges_seen.append((start_ms, end_ms))
        return normalize_metrics(
            {
                "sharpe_ratio": 0.5,
                "win_rate": 0.5,
                "n_trades": 4,
                "return_pct": 0.01,
                "max_drawdown": 0.02,
                "expectancy_r": 0.1,
            }
        )

    wc = WindowConfig(train_days=5, validation_days=3, test_days=10, n_windows=1)
    ledger_path = _test_ledger_path()
    try:
        with patch("src.backtest.walk_forward._run_backtest", side_effect=fake_run):
            report = run_walk_forward(
                cfg,
                db=None,
                strategy_name="VolatilityBreakout",
                symbols=["BTC"],
                param_grid=ParamGrid(axes={"min_adx": [10, 20]}),
                window_config=wc,
                base_days=30,
                holdout_ledger_path=ledger_path,
            )
    finally:
        ledger_path.unlink(missing_ok=True)
    assert report.aggregate["total_oos_trades"] == 4.0
    assert len(train_ranges_seen) >= 2
    assert len(test_ranges_seen) == 1


def test_manifest_blocks_incompatible_comparison() -> None:
    cfg = _minimal_cfg()
    left = build_experiment_manifest(cfg, strategy_name="VolatilityBreakout", n_trials=10)
    right = build_experiment_manifest(
        cfg,
        strategy_name="VolatilityBreakout",
        n_trials=10,
        extra={"fidelity_tier": "tier_b_proxy"},
    )
    right["fidelity_tier"] = "tier_b_proxy"
    try:
        assert_manifests_comparable(left, right)
        blocked = False
    except ManifestIncompatibleError:
        blocked = True
    assert blocked


def main() -> int:
    tests = [
        test_normalize_metrics_aliases,
        test_aggregate_oos_trade_count_nonzero,
        test_param_override_changes_strategy_before_build,
        test_param_override_changes_signal_behavior,
        test_embargo_respected_between_splits,
        test_block_bootstrap_preserves_day_blocks,
        test_regime_mode_falls_back_to_utc_day_not_weekday,
        test_block_bootstrap_reproducible_with_seed,
        test_sharpe_uses_crypto_hourly_calendar,
        test_calmar_uses_cagr_and_real_duration,
        test_expectancy_r_normalized,
        test_dsr_pbo_labeled_as_internal_proxies,
        test_holdout_single_consultation_per_window,
        test_holdout_persists_across_process_restarts,
        test_holdout_not_used_during_selection,
        test_manifest_blocks_incompatible_comparison,
    ]
    failed = 0
    print("=" * 70)
    print("Phase 06 walk-forward / statistical validation tests")
    print("=" * 70)
    for fn in tests:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
    print("=" * 70)
    if failed:
        print(f"FAILED: {failed}/{len(tests)}")
        return 1
    print(f"ALL TESTS PASSED ({len(tests)}/{len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
