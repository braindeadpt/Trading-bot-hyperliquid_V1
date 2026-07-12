"""Walk-forward optimization with purged train/validation/test splits (Phase 06).

Protocol per window:
  1. *Train* — enumerate the param grid; pick best by in-sample Sharpe.
  2. *Validation* — score frozen train winner (parameter freeze checkpoint).
  3. *Test / holdout* — single OOS evaluation; never used for selection.

Purged gaps (embargo) between splits are >= ``max_hold`` for the target
strategy so overlapping positions cannot leak labels across boundaries.

Public API
----------
* :class:`ParamGrid` — declarative parameter grid.
* :class:`WindowConfig` — train/validation/test window layout.
* :class:`HoldoutGuard` — enforces one-shot holdout access.
* :class:`WindowResult` — per-window train/val/test metrics.
* :class:`WalkForwardReport` — aggregate OOS + multiple-testing report.
* :func:`run_walk_forward` — top-level entry point.
* :func:`render_report` — text rendering.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.backtest.experiment_manifest import build_experiment_manifest
from src.backtest.holdout_ledger import HoldoutGuard, HoldoutLedger, HoldoutViolationError
from src.backtest.metrics import normalize_metrics
from src.backtest.statistical_validation import (
    METHODOLOGY_NOTE,
    build_multiple_testing_report,
)
from src.strategies.factory import _STRATEGY_REGISTRY
from src.utils.config import compute_config_hash

logger = logging.getLogger(__name__)

_MS_PER_DAY = 86_400_000

# strategy display name → config dot-path
_STRATEGY_CONFIG_PATH: Dict[str, str] = {}
for _path, _cls in _STRATEGY_REGISTRY:
    try:
        _inst = _cls({})
        _STRATEGY_CONFIG_PATH[_inst.name] = _path
    except Exception:  # noqa: BLE001
        pass


# ── Config / dataclasses ────────────────────────────────────────────


@dataclass(frozen=True)
class ParamGrid:
    """Declarative parameter grid for a strategy."""

    axes: Mapping[str, Sequence[Any]] = field(default_factory=dict)

    def iter_points(self) -> Iterable[Dict[str, Any]]:
        keys = list(self.axes.keys())
        if not keys:
            yield {}
            return
        values_lists = [list(self.axes[k]) for k in keys]
        for combo in product(*values_lists):
            yield dict(zip(keys, combo))

    def size(self) -> int:
        n = 1
        for vals in self.axes.values():
            n *= max(1, len(vals))
        return n


@dataclass(frozen=True)
class WindowConfig:
    """Walk-forward window layout with train / validation / test splits.

    * ``train_days``       — training slice length
    * ``validation_days``  — validation slice (parameter freeze checkpoint)
    * ``test_days``        — holdout / OOS test slice
    * ``step_days``        — slide distance (defaults to ``test_days``)
    * ``n_windows``        — number of windows
    * ``embargo_days``     — explicit embargo; if None, derived from max_hold
    """

    train_days: int = 30
    validation_days: int = 7
    test_days: int = 14
    step_days: Optional[int] = None
    n_windows: int = 6
    embargo_days: Optional[float] = None

    @property
    def legacy_test_only(self) -> bool:
        """True when validation_days == 0 (train+test only, deprecated)."""
        return self.validation_days <= 0


@dataclass(frozen=True)
class SplitRanges:
    """Time ranges for train, validation, and test with purged gaps."""

    train_start_ms: int
    train_end_ms: int
    validation_start_ms: int
    validation_end_ms: int
    test_start_ms: int
    test_end_ms: int
    embargo_ms: int


@dataclass(frozen=True)
class WindowResult:
    """One window: best train params + train/val/test metrics."""

    window_index: int
    train_start_ms: int
    train_end_ms: int
    validation_start_ms: int
    validation_end_ms: int
    test_start_ms: int
    test_end_ms: int
    embargo_ms: int
    best_params: Dict[str, Any]
    train_metrics: Dict[str, float]
    validation_metrics: Dict[str, float]
    test_metrics: Dict[str, float]
    n_param_combos: int
    trial_is_scores: Tuple[float, ...] = ()
    trial_oos_scores: Tuple[float, ...] = ()

    def as_dict(self) -> Dict[str, object]:
        return {
            "window_index": int(self.window_index),
            "train_start_ms": int(self.train_start_ms),
            "train_end_ms": int(self.train_end_ms),
            "validation_start_ms": int(self.validation_start_ms),
            "validation_end_ms": int(self.validation_end_ms),
            "test_start_ms": int(self.test_start_ms),
            "test_end_ms": int(self.test_end_ms),
            "embargo_ms": int(self.embargo_ms),
            "best_params": dict(self.best_params),
            "train_metrics": dict(self.train_metrics),
            "validation_metrics": dict(self.validation_metrics),
            "test_metrics": dict(self.test_metrics),
            "n_param_combos": int(self.n_param_combos),
        }


@dataclass(frozen=True)
class WalkForwardReport:
    """Aggregate over all windows."""

    strategy_name: str
    config_path: str
    window_config: WindowConfig
    windows: List[WindowResult]
    aggregate: Dict[str, float]
    experiment_manifest: Dict[str, Any]
    multiple_testing: Dict[str, float]

    def as_dict(self) -> Dict[str, object]:
        return {
            "strategy_name": self.strategy_name,
            "config_path": self.config_path,
            "window_config": {
                "train_days": int(self.window_config.train_days),
                "validation_days": int(self.window_config.validation_days),
                "test_days": int(self.window_config.test_days),
                "step_days": int(self.window_config.step_days or self.window_config.test_days),
                "n_windows": int(self.window_config.n_windows),
                "embargo_days": self.window_config.embargo_days,
            },
            "windows": [w.as_dict() for w in self.windows],
            "aggregate": dict(self.aggregate),
            "experiment_manifest": dict(self.experiment_manifest),
            "multiple_testing": dict(self.multiple_testing),
        }


# ── Split / embargo helpers ─────────────────────────────────────────


def resolve_strategy_config_path(strategy_name: str) -> str:
    """Map strategy display name to config dot-path."""
    path = _STRATEGY_CONFIG_PATH.get(strategy_name)
    if path is None:
        raise ValueError(f"Unknown strategy for walk-forward: {strategy_name}")
    return path


def resolve_max_hold_ms(cfg: Any, strategy_name: str) -> int:
    """Max hold duration in ms for embargo sizing."""
    path = resolve_strategy_config_path(strategy_name)
    section = cfg.get(path, {}) or {}
    if "max_hold_hours" in section:
        hours = float(section["max_hold_hours"])
        return int(hours * 3_600_000)
    if "max_hold_minutes" in section:
        minutes = float(section["max_hold_minutes"])
        return int(minutes * 60_000)
    if "max_hold_seconds" in section:
        seconds = float(section["max_hold_seconds"])
        return int(seconds * 1000)
    return int(6.0 * 3_600_000)


def resolve_embargo_ms(cfg: Any, strategy_name: str, window_config: WindowConfig) -> int:
    """Embargo/purge gap >= max_hold."""
    if window_config.embargo_days is not None and window_config.embargo_days > 0:
        return int(window_config.embargo_days * _MS_PER_DAY)
    return resolve_max_hold_ms(cfg, strategy_name)


def compute_split_ranges(
    *,
    anchor_end_ms: int,
    train_days: int,
    validation_days: int,
    test_days: int,
    embargo_ms: int,
) -> SplitRanges:
    """Compute train → validation → test ranges with purged gaps."""
    test_end_ms = anchor_end_ms
    test_start_ms = test_end_ms - test_days * _MS_PER_DAY
    validation_end_ms = test_start_ms - embargo_ms
    validation_start_ms = validation_end_ms - validation_days * _MS_PER_DAY
    train_end_ms = validation_start_ms - embargo_ms
    train_start_ms = train_end_ms - train_days * _MS_PER_DAY
    return SplitRanges(
        train_start_ms=train_start_ms,
        train_end_ms=train_end_ms,
        validation_start_ms=validation_start_ms,
        validation_end_ms=validation_end_ms,
        test_start_ms=test_start_ms,
        test_end_ms=test_end_ms,
        embargo_ms=embargo_ms,
    )


def assert_embargo_respected(ranges: SplitRanges) -> None:
    """Verify gaps between splits are at least embargo_ms."""
    gap_train_val = ranges.validation_start_ms - ranges.train_end_ms
    gap_val_test = ranges.test_start_ms - ranges.validation_end_ms
    if gap_train_val < ranges.embargo_ms or gap_val_test < ranges.embargo_ms:
        raise ValueError(
            f"Embargo violated: gaps train→val={gap_train_val}ms, "
            f"val→test={gap_val_test}ms, required={ranges.embargo_ms}ms"
        )


# ── Config overrides (before strategy construction) ─────────────────


def _copy_config_with_strategy_overrides(
    cfg: Any,
    strategy_name: str,
    param_overrides: Mapping[str, Any],
) -> Any:
    """Deep-copy config and apply overrides to the target strategy section."""
    from src.utils.config import Config

    if isinstance(cfg, Config):
        raw = copy.deepcopy(cfg.raw)
        out = Config(raw)
    else:
        out = Config(copy.deepcopy(cfg))

    path = resolve_strategy_config_path(strategy_name)
    parts = path.split(".")
    node = out.raw
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    leaf = parts[-1]
    section = dict(node.get(leaf, {}) or {})
    for key, value in param_overrides.items():
        if str(key).startswith("__"):
            continue
        section[key] = value
    node[leaf] = section
    return out


def _build_engine(cfg: Any, db: Any, symbols: Sequence[str], strategy_name: str):
    """Construct BacktestEngine — overrides already baked into *cfg*."""
    from src.backtest.engine import BacktestEngine, build_backtest_config_from_yaml
    from src.strategies.factory import build_backtest_strategy

    strategy = build_backtest_strategy(cfg)
    bt_cfg = build_backtest_config_from_yaml(cfg)
    return BacktestEngine(
        database=db,
        strategy=strategy,
        config=bt_cfg,
        symbols=list(symbols),
        risk_config=cfg,
    )


def _run_backtest(
    cfg: Any,
    db: Any,
    symbols: Sequence[str],
    strategy_name: str,
    start_ms: int,
    end_ms: int,
    param_overrides: Optional[Mapping[str, Any]] = None,
) -> Dict[str, float]:
    """Run one backtest and return normalized metrics."""
    overrides = dict(param_overrides or {})
    effective_cfg = (
        _copy_config_with_strategy_overrides(cfg, strategy_name, overrides)
        if overrides
        else cfg
    )
    engine = _build_engine(effective_cfg, db, symbols, strategy_name)
    out = engine.run(start_ms=start_ms, end_ms=end_ms)
    metrics = normalize_metrics(dict(out.get("metrics", {})))
    metrics["__total_return"] = float(out.get("total_return", metrics.get("total_return", 0.0)))
    metrics["__trades"] = float(metrics.get("n_trades", 0))
    return metrics


# ── Param selection ──────────────────────────────────────────────────


def _train_score(metrics: Mapping[str, float]) -> float:
    """Scalar score for training window (Sharpe with low-trade penalty)."""
    sharpe = float(metrics.get("sharpe_ratio", 0.0))
    n_trades = float(metrics.get("__trades", metrics.get("n_trades", 0)))
    if n_trades < 3:
        return sharpe - 1.0
    return sharpe


def _aggregate(windows: Sequence[WindowResult]) -> Dict[str, float]:
    """Average OOS (test) metrics across all windows."""
    if not windows:
        return {
            "n_windows": 0.0,
            "avg_oos_sharpe": 0.0,
            "avg_oos_win_rate": 0.0,
            "avg_oos_return_pct": 0.0,
            "avg_oos_max_dd": 0.0,
            "avg_oos_expectancy_r": 0.0,
            "total_oos_trades": 0.0,
            "positive_oos_windows": 0.0,
        }
    n = len(windows)
    sharpe = sum(w.test_metrics.get("sharpe_ratio", 0.0) for w in windows) / n
    win_rate = sum(w.test_metrics.get("win_rate", 0.0) for w in windows) / n
    return_pct = sum(w.test_metrics.get("return_pct", 0.0) for w in windows) / n
    max_dd = sum(w.test_metrics.get("max_drawdown_pct", 0.0) for w in windows) / n
    expectancy_r = sum(w.test_metrics.get("expectancy_r", 0.0) for w in windows) / n
    total_trades = sum(
        w.test_metrics.get("n_trades", w.test_metrics.get("total_trades", 0.0))
        for w in windows
    )
    positive = sum(1 for w in windows if w.test_metrics.get("return_pct", 0.0) > 0.0)
    return {
        "n_windows": float(n),
        "avg_oos_sharpe": float(sharpe),
        "avg_oos_win_rate": float(win_rate),
        "avg_oos_return_pct": float(return_pct),
        "avg_oos_max_dd": float(max_dd),
        "avg_oos_expectancy_r": float(expectancy_r),
        "total_oos_trades": float(total_trades),
        "positive_oos_windows": float(positive),
    }


def _strip_internal(metrics: Mapping[str, float]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for k, v in metrics.items():
        if not str(k).startswith("__"):
            out[k] = float(v)
    return normalize_metrics(out)


def _ms(days_ago: float) -> int:
    return int((time.time() - days_ago * 86400) * 1000)


# ── Top-level entry point ──────────────────────────────────────────


def run_walk_forward(
    cfg: Any,
    db: Any,
    strategy_name: str,
    symbols: Sequence[str],
    param_grid: ParamGrid,
    window_config: Optional[WindowConfig] = None,
    *,
    end_days_ago: float = 0.0,
    base_days: float = 365.0,
    seed: Optional[int] = 42,
    holdout_guard: Optional[HoldoutGuard] = None,
    holdout_ledger_path: Optional[Path] = None,
) -> WalkForwardReport:
    """Run purged walk-forward with train → validation → test protocol."""
    wc = window_config or WindowConfig()
    if holdout_guard is not None:
        guard = holdout_guard
    elif holdout_ledger_path is not None:
        guard = HoldoutGuard(HoldoutLedger(holdout_ledger_path))
    else:
        guard = HoldoutGuard()
    config_hash = ""
    try:
        config_hash = compute_config_hash(cfg)
    except Exception:  # noqa: BLE001
        config_hash = ""
    step_days = int(wc.step_days or wc.test_days)
    embargo_ms = resolve_embargo_ms(cfg, strategy_name, wc)
    val_days = max(0, wc.validation_days)
    end_ms = _ms(end_days_ago) if end_days_ago > 0 else int(time.time() * 1000)

    total_window = wc.train_days + val_days + wc.test_days
    required_days = total_window + (wc.n_windows - 1) * step_days
    if required_days > base_days:
        base_days = float(required_days)

    windows: List[WindowResult] = []
    n_combos = param_grid.size()
    all_is_scores: List[float] = []
    all_oos_scores: List[float] = []

    logger.info(
        "Walk-forward: strategy=%s, %d windows, train=%dd, val=%dd, test=%dd, "
        "embargo=%.2fh, %d param combos",
        strategy_name,
        wc.n_windows,
        wc.train_days,
        val_days,
        wc.test_days,
        embargo_ms / 3_600_000,
        n_combos,
    )

    for w_idx in range(wc.n_windows):
        guard.begin_window()
        anchor_end = end_ms - w_idx * step_days * _MS_PER_DAY
        if val_days > 0:
            ranges = compute_split_ranges(
                anchor_end_ms=anchor_end,
                train_days=wc.train_days,
                validation_days=val_days,
                test_days=wc.test_days,
                embargo_ms=embargo_ms,
            )
        else:
            # Legacy train+test only (no validation slice)
            test_end = anchor_end
            test_start = test_end - wc.test_days * _MS_PER_DAY
            train_end = test_start - embargo_ms
            train_start = train_end - wc.train_days * _MS_PER_DAY
            ranges = SplitRanges(
                train_start_ms=train_start,
                train_end_ms=train_end,
                validation_start_ms=train_end,
                validation_end_ms=train_end,
                test_start_ms=test_start,
                test_end_ms=test_end,
                embargo_ms=embargo_ms,
            )

        assert_embargo_respected(ranges)
        earliest = _ms(base_days)
        if ranges.train_start_ms < earliest:
            logger.info(
                "Walk-forward: window %d skipped — train_start older than base_days=%.0f",
                w_idx,
                base_days,
            )
            continue

        guard.assert_selection_phase()

        # ── 1. Train: optimize ─────────────────────────────────
        best_score = -math.inf
        best_params: Dict[str, Any] = {}
        best_train_metrics: Dict[str, float] = {}
        trial_is: List[float] = []
        trial_oos_proxy: List[float] = []

        for params in param_grid.iter_points():
            try:
                m = _run_backtest(
                    cfg,
                    db,
                    symbols,
                    strategy_name,
                    ranges.train_start_ms,
                    ranges.train_end_ms,
                    params,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Walk-forward: window %d train param %s raised %s",
                    w_idx,
                    params,
                    exc,
                )
                continue
            score = _train_score(m)
            trial_is.append(score)
            if math.isfinite(score) and score > best_score:
                best_score = score
                best_params = dict(params)
                best_train_metrics = dict(m)

        if not best_params and not best_train_metrics:
            logger.info(
                "Walk-forward: window %d had no successful training runs — skipped",
                w_idx,
            )
            continue

        guard.freeze_params(best_params)

        # ── 2. Validation (freeze checkpoint, not used for re-optimization) ──
        validation_metrics: Dict[str, float] = {}
        if val_days > 0:
            try:
                validation_metrics = _run_backtest(
                    cfg,
                    db,
                    symbols,
                    strategy_name,
                    ranges.validation_start_ms,
                    ranges.validation_end_ms,
                    best_params,
                )
                trial_oos_proxy.append(
                    float(validation_metrics.get("sharpe_ratio", 0.0))
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Walk-forward: window %d validation run raised %s", w_idx, exc
                )
                validation_metrics = normalize_metrics({})

        # ── 3. Test / holdout — single consultation ────────────
        guard.set_holdout_context(
            strategy_name=strategy_name,
            test_start_ms=ranges.test_start_ms,
            test_end_ms=ranges.test_end_ms,
            config_hash=config_hash,
        )

        def _holdout_run() -> Dict[str, float]:
            return _run_backtest(
                cfg,
                db,
                symbols,
                strategy_name,
                ranges.test_start_ms,
                ranges.test_end_ms,
                best_params,
            )

        try:
            test_metrics = guard.evaluate_holdout(_holdout_run)
        except HoldoutViolationError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Walk-forward: window %d OOS run raised %s", w_idx, exc)
            test_metrics = normalize_metrics({})

        all_is_scores.extend(trial_is)
        all_oos_scores.extend(trial_oos_proxy)

        windows.append(
            WindowResult(
                window_index=w_idx,
                train_start_ms=ranges.train_start_ms,
                train_end_ms=ranges.train_end_ms,
                validation_start_ms=ranges.validation_start_ms,
                validation_end_ms=ranges.validation_end_ms,
                test_start_ms=ranges.test_start_ms,
                test_end_ms=ranges.test_end_ms,
                embargo_ms=ranges.embargo_ms,
                best_params=best_params,
                train_metrics=_strip_internal(best_train_metrics),
                validation_metrics=_strip_internal(validation_metrics),
                test_metrics=_strip_internal(test_metrics),
                n_param_combos=n_combos,
                trial_is_scores=tuple(trial_is),
                trial_oos_scores=tuple(trial_oos_proxy),
            )
        )

        logger.info(
            "Walk-forward: window %d train_sharpe=%.3f val_sharpe=%.3f "
            "test_sharpe=%.3f test_return=%.2f%% trades=%d best=%s",
            w_idx,
            best_train_metrics.get("sharpe_ratio", 0.0),
            validation_metrics.get("sharpe_ratio", 0.0),
            test_metrics.get("sharpe_ratio", 0.0),
            test_metrics.get("return_pct", 0.0) * 100.0,
            int(test_metrics.get("n_trades", 0)),
            best_params,
        )

    agg = _aggregate(windows)
    n_trials = n_combos * max(1, len(windows))
    mt_report = build_multiple_testing_report(
        n_trials=n_trials,
        n_observations=int(max(agg.get("total_oos_trades", 0), 2)),
        observed_sharpe=float(agg.get("avg_oos_sharpe", 0.0)),
        is_scores=all_is_scores,
        oos_scores=all_oos_scores,
    )
    experiment_manifest = build_experiment_manifest(
        cfg,
        strategy_name=strategy_name,
        n_trials=n_trials,
        param_grid_size=n_combos,
        protocol="train_val_test_holdout",
        window_config={
            "train_days": wc.train_days,
            "validation_days": val_days,
            "test_days": wc.test_days,
            "embargo_ms": embargo_ms,
            "n_windows": wc.n_windows,
        },
        seed=seed,
        extra={"holdout_used": guard.holdout_used},
    )

    return WalkForwardReport(
        strategy_name=strategy_name,
        config_path="<inline>",
        window_config=wc,
        windows=windows,
        aggregate=agg,
        experiment_manifest=experiment_manifest,
        multiple_testing=mt_report.as_dict(),
    )


# ── Rendering ──────────────────────────────────────────────────────


def render_report(report: WalkForwardReport) -> str:
    """Render a WalkForwardReport as a text table."""
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append(f"WALK-FORWARD REPORT — strategy: {report.strategy_name}")
    lines.append("=" * 72)
    wc = report.window_config
    lines.append(
        f"train_days={wc.train_days}  validation_days={wc.validation_days}  "
        f"test_days={wc.test_days}  step_days={wc.step_days or wc.test_days}  "
        f"n_windows={wc.n_windows}"
    )
    lines.append("-" * 72)
    lines.append(
        f"{'#':>2}  {'train_start':<12}  {'test_start':<12}  "
        f"{'OOS Sharpe':>10}  {'OOS return%':>11}  {'trades':>7}  best_params"
    )
    for w in report.windows:
        ts = datetime.fromtimestamp(
            w.test_start_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        tr = datetime.fromtimestamp(
            w.train_start_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        params = ",".join(f"{k}={v}" for k, v in w.best_params.items()) or "—"
        n_tr = int(w.test_metrics.get("n_trades", w.test_metrics.get("total_trades", 0)))
        lines.append(
            f"{w.window_index:>2}  {tr:<12}  {ts:<12}  "
            f"{w.test_metrics.get('sharpe_ratio', 0.0):>10.3f}  "
            f"{w.test_metrics.get('return_pct', 0.0) * 100:>10.2f}%  "
            f"{n_tr:>7d}  {params}"
        )
    lines.append("-" * 72)
    a = report.aggregate
    mt = report.multiple_testing
    lines.append(
        f"AGGREGATE: {int(a['n_windows'])} windows | "
        f"avg OOS Sharpe={a['avg_oos_sharpe']:.3f} | "
        f"avg OOS return={a['avg_oos_return_pct']:.2f}% | "
        f"total OOS trades={int(a['total_oos_trades'])} | "
        f"positive OOS windows={int(a['positive_oos_windows'])}"
    )
    lines.append(
        f"MULTIPLE-TESTING (internal proxies — {METHODOLOGY_NOTE}): "
        f"trials={int(mt.get('n_trials', 0))} | "
        f"DSR_proxy={mt.get('deflated_sharpe_prob_proxy', mt.get('deflated_sharpe_prob', 0.0)):.3f} | "
        f"PBO_proxy={mt.get('pbo_proxy', mt.get('pbo', 0.0)):.3f}"
    )
    lines.append("=" * 72)
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────


def _cli(argv: Optional[Iterable[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description=(
            "Walk-forward optimization for a single strategy. "
            "Reads the param grid from --grid (JSON string)."
        ),
    )
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument(
        "--strategy",
        required=True,
        help="Strategy name (e.g. CVDOrderFlow, TrendPyramid)",
    )
    p.add_argument("--windows", type=int, default=6)
    p.add_argument("--train-days", type=int, default=30)
    p.add_argument("--validation-days", type=int, default=7)
    p.add_argument("--test-days", type=int, default=14)
    p.add_argument("--step-days", type=int, default=None)
    p.add_argument("--base-days", type=float, default=365.0)
    p.add_argument(
        "--grid",
        default="{}",
        help=(
            'Param grid as JSON, e.g. {"min_adx": [20, 25, 30]}. '
            "Empty {} runs with existing config."
        ),
    )
    p.add_argument("--json", action="store_true", help="Print full report as JSON")
    args = p.parse_args(list(argv) if argv is not None else None)

    from src.data.database import Database
    from src.utils.config import load_config

    cfg = load_config(args.config)
    db_path = Path(args.config).resolve().parent.parent / cfg.get(
        "database.path",
        "data/live/bot.db",
    )
    db = Database(str(db_path))
    symbols = list(cfg.get("assets", ["BTC", "ETH", "SOL"]))
    grid_axes = json.loads(args.grid)
    grid = ParamGrid(axes=grid_axes)
    wc = WindowConfig(
        train_days=args.train_days,
        validation_days=args.validation_days,
        test_days=args.test_days,
        step_days=args.step_days,
        n_windows=args.windows,
    )
    report = run_walk_forward(
        cfg,
        db,
        args.strategy,
        symbols,
        grid,
        wc,
        base_days=args.base_days,
    )
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
