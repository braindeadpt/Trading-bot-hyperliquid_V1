"""Walk-forward optimization framework (v3.1.21).

Slides a (train, test) window across the full backtest history. For
each window:

  1.  The first 70% of the window is the *training* set: the param grid
      for the chosen strategy is enumerated, the backtest is run for
      each parameter combination, and the best configuration is
      chosen by **out-of-sample Sharpe on the training slice itself**
      (in-sample fit — the user is responsible for sanity-checking
      against overfitting).
  2.  The remaining 30% of the window is the *test* set: the
      backtest is run with the *frozen* best parameters, and the
      resulting OOS metrics are reported.

The output is a list of :class:`WindowResult` records plus a
:class:`WalkForwardReport` aggregate that the caller can render as
JSON, CSV, or a printed table.

Public API
----------
* :class:`ParamGrid` — declarative parameter grid (one dimension per
  axis; the cartesian product is enumerated).
* :class:`WindowConfig` — train/test window length + step.
* :class:`WindowResult` — per-window result (best params + train
  + test metrics).
* :class:`WalkForwardReport` — aggregate over all windows.
* :func:`run_walk_forward` — top-level entry point.
* :func:`render_report` — text rendering.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ── Config / dataclasses ────────────────────────────────────────────


@dataclass(frozen=True)
class ParamGrid:
    """Declarative parameter grid for a strategy.

    Each axis maps a strategy-key (e.g. ``"min_adx"``) to a list of
    candidate values. The cartesian product of all axes is
    enumerated at optimization time.

    Empty axes produce exactly one evaluation with the strategy's
    existing config (no optimization).
    """

    axes: Mapping[str, Sequence[Any]] = field(default_factory=dict)

    def iter_points(self) -> Iterable[Dict[str, Any]]:
        """Yield each parameter combination as a dict."""
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
    """Walk-forward window layout.

    * ``train_days``  — number of days in the training slice
    * ``test_days``   — number of days in the OOS test slice
    * ``step_days``   — slide distance (defaults to ``test_days``,
                        so windows are non-overlapping)
    * ``n_windows``   — number of windows to run (default 6)
    """

    train_days: int = 30
    test_days: int = 14
    step_days: Optional[int] = None
    n_windows: int = 6


@dataclass(frozen=True)
class WindowResult:
    """One window of the walk-forward: best train params + test metrics."""

    window_index: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int
    best_params: Dict[str, Any]
    train_metrics: Dict[str, float]
    test_metrics: Dict[str, float]
    n_param_combos: int

    def as_dict(self) -> Dict[str, object]:
        return {
            "window_index": int(self.window_index),
            "train_start_ms": int(self.train_start_ms),
            "train_end_ms": int(self.train_end_ms),
            "test_start_ms": int(self.test_start_ms),
            "test_end_ms": int(self.test_end_ms),
            "best_params": dict(self.best_params),
            "train_metrics": dict(self.train_metrics),
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

    def as_dict(self) -> Dict[str, object]:
        return {
            "strategy_name": self.strategy_name,
            "config_path": self.config_path,
            "window_config": {
                "train_days": int(self.window_config.train_days),
                "test_days": int(self.window_config.test_days),
                "step_days": int(self.window_config.step_days or self.window_config.test_days),
                "n_windows": int(self.window_config.n_windows),
            },
            "windows": [w.as_dict() for w in self.windows],
            "aggregate": dict(self.aggregate),
        }


# ── Backtest invocation helpers ────────────────────────────────────


def _ms(days_ago: float) -> int:
    return int((time.time() - days_ago * 86400) * 1000)


def _build_engine(cfg: Any, db: Any, symbols: Sequence[str], param_overrides: Mapping[str, Any]):
    """Construct a BacktestEngine with a param override applied to the
    strategy config block.  Avoids mutating the user's config object.
    """
    from src.backtest.engine import BacktestConfig, BacktestEngine
    from src.strategies.factory import build_ensemble

    ensemble = build_ensemble(cfg)

    # Apply param overrides to the *target* strategy in-place if we
    # can find it.  Otherwise we patch the cfg dict (shallow) and
    # rebuild the ensemble.  We do the latter for simplicity.
    if param_overrides:
        try:
            for strat in ensemble._strategies.values():
                if hasattr(strat, "_cfg") and isinstance(strat._cfg, dict):
                    strat._cfg.update(dict(param_overrides))
        except Exception:  # noqa: BLE001
            # Fall back to a fresh build via param override on the cfg
            target = param_overrides.get("__target_strategy__")
            if target:
                section = f"strategy.{target}"
                for k, v in param_overrides.items():
                    if k != "__target_strategy__":
                        try:
                            cfg[section][k] = v
                        except Exception:  # noqa: BLE001
                            pass
            ensemble = build_ensemble(cfg)

    bt_cfg = BacktestConfig(
        initial_capital=float(cfg.get("backtest.initial_capital", 10_000.0)),
        commission_pct=float(cfg.get("backtest.commission_pct", 0.04)),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 5)),
        tca_enabled=bool(cfg.get("execution.tca_enabled", True)),
        min_edge_buffer_pct=float(cfg.get("execution.min_edge_buffer_pct", 0.05)),
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.05)),
        use_regime_weights=bool(cfg.get("backtest.use_regime_weights", True)),
        use_cooldown=bool(cfg.get("backtest.use_cooldown", True)),
        use_kelly=bool(cfg.get("backtest.use_kelly", True)),
        use_microstructure_proxy=bool(cfg.get("backtest.use_microstructure_proxy", True)),
        regime_weights=cfg.get("strategy.regime_weights", {}),
        adx_trend_threshold=float(cfg.get("strategy.adx_trend_threshold", 25.0)),
        adx_range_threshold=float(cfg.get("strategy.adx_range_threshold", 20.0)),
        cooldown_base_ms=int(cfg.get("strategy.cooldown.base_minutes", 60) * 60_000),
        max_daily_trades=int(cfg.get("risk.max_daily_trades", 5)),
    )
    return BacktestEngine(database=db, strategy=ensemble, config=bt_cfg, symbols=list(symbols))


def _run_backtest(
    cfg: Any,
    db: Any,
    symbols: Sequence[str],
    start_ms: int,
    end_ms: int,
    param_overrides: Optional[Mapping[str, Any]] = None,
) -> Dict[str, float]:
    """Run one backtest and return the metrics dict."""
    overrides = dict(param_overrides or {})
    engine = _build_engine(cfg, db, symbols, overrides)
    out = engine.run(start_ms=start_ms, end_ms=end_ms)
    metrics = dict(out.get("metrics", {}))
    metrics["__total_return"] = float(out.get("total_return", 0.0))
    metrics["__trades"] = float(metrics.get("total_trades", 0))
    return metrics


# ── Param selection by training Sharpe ─────────────────────────────


def _train_score(metrics: Mapping[str, float]) -> float:
    """Return a scalar score for the training window.

    Default: in-sample Sharpe (with a small penalty for very few
    trades so degenerate fits don't get picked).
    """
    sharpe = float(metrics.get("sharpe_ratio", 0.0))
    n_trades = float(metrics.get("__trades", 0))
    # Penalize if fewer than 3 trades (curve-fit on a single example)
    if n_trades < 3:
        return sharpe - 1.0
    return sharpe


def _aggregate(windows: Sequence[WindowResult]) -> Dict[str, float]:
    """Average / aggregate the OOS metrics across all windows."""
    if not windows:
        return {
            "n_windows": 0,
            "avg_oos_sharpe": 0.0,
            "avg_oos_win_rate": 0.0,
            "avg_oos_return_pct": 0.0,
            "avg_oos_max_dd": 0.0,
            "total_oos_trades": 0.0,
            "positive_oos_windows": 0,
        }
    n = len(windows)
    sharpe = sum(w.test_metrics.get("sharpe_ratio", 0.0) for w in windows) / n
    win_rate = sum(w.test_metrics.get("win_rate", 0.0) for w in windows) / n
    return_pct = sum(w.test_metrics.get("return_pct", 0.0) for w in windows) / n
    max_dd = sum(w.test_metrics.get("max_drawdown_pct", 0.0) for w in windows) / n
    total_trades = sum(w.test_metrics.get("__trades", 0.0) for w in windows)
    positive = sum(
        1 for w in windows
        if w.test_metrics.get("return_pct", 0.0) > 0.0
    )
    return {
        "n_windows": float(n),
        "avg_oos_sharpe": float(sharpe),
        "avg_oos_win_rate": float(win_rate),
        "avg_oos_return_pct": float(return_pct),
        "avg_oos_max_dd": float(max_dd),
        "total_oos_trades": float(total_trades),
        "positive_oos_windows": float(positive),
    }


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
) -> WalkForwardReport:
    """Run the walk-forward optimization over the configured windows.

    Parameters
    ----------
    cfg :
        The application config object (e.g. from
        :func:`src.utils.config.load_config`).
    db :
        The :class:`src.data.database.Database` instance.
    strategy_name :
        The strategy to optimize (must be present in the config).
    symbols :
        Symbols to backtest across.
    param_grid :
        The parameter grid to enumerate per window.
    window_config :
        :class:`WindowConfig`; defaults to ``(train=30d, test=14d, n=6)``.
    end_days_ago :
        End of the latest window in *days ago* (default 0 — end at
        "now"). Useful to back off when the most-recent candles
        are not yet fully populated.
    base_days :
        How far back (in days) the earliest window starts (default
        365 — one year of history). The total coverage is
        ``train_days + test_days + (n_windows - 1) * step_days``;
        ``base_days`` is a hard floor that ensures the loop doesn't
        request data older than the database holds.
    """
    wc = window_config or WindowConfig()
    step_days = int(wc.step_days or wc.test_days)
    end_ms = _ms(end_days_ago) if end_days_ago > 0 else int(time.time() * 1000)
    total_window = wc.train_days + wc.test_days
    required_days = total_window + (wc.n_windows - 1) * step_days
    if required_days > base_days:
        base_days = float(required_days)

    windows: List[WindowResult] = []
    n_combos = param_grid.size()

    logger.info(
        "Walk-forward: strategy=%s, %d windows, train=%dd, test=%dd, step=%dd, "
        "%d param combos",
        strategy_name, wc.n_windows, wc.train_days, wc.test_days, step_days, n_combos,
    )

    for w_idx in range(wc.n_windows):
        # Most recent window ends at end_ms. Each earlier window
        # is step_days earlier.
        test_end = end_ms - w_idx * step_days * 86_400_000
        test_start = test_end - wc.test_days * 86_400_000
        train_start = test_start - wc.train_days * 86_400_000
        train_end = test_start
        # Floor: don't go past the base_days lookback
        earliest = _ms(base_days)
        if train_start < earliest:
            logger.info(
                "Walk-forward: window %d skipped — train_start would be older than "
                "base_days=%.0f", w_idx, base_days,
            )
            continue

        # ── 1. Optimize on training slice ──────────────────────
        best_score = -math.inf
        best_params: Dict[str, Any] = {}
        best_train_metrics: Dict[str, float] = {}
        for params in param_grid.iter_points():
            # Inject target strategy so the engine knows which one
            # to optimize. The exact dispatch happens inside
            # _build_engine via the ``__target_strategy__`` key.
            tagged = dict(params)
            tagged["__target_strategy__"] = strategy_name
            try:
                m = _run_backtest(
                    cfg, db, symbols, train_start, train_end, tagged,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Walk-forward: window %d train param %s raised %s",
                    w_idx, params, exc,
                )
                continue
            score = _train_score(m)
            if math.isfinite(score) and score > best_score:
                best_score = score
                best_params = dict(params)
                best_train_metrics = dict(m)

        if not best_params and not best_train_metrics:
            # No successful training run at all — skip the window.
            logger.info(
                "Walk-forward: window %d had no successful training runs — skipped",
                w_idx,
            )
            continue

        # ── 2. Test on the OOS slice ───────────────────────────
        try:
            test_metrics = _run_backtest(
                cfg, db, symbols, test_start, test_end, best_params,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Walk-forward: window %d OOS run raised %s", w_idx, exc,
            )
            test_metrics = {"__trades": 0.0, "sharpe_ratio": 0.0, "win_rate": 0.0,
                            "max_drawdown_pct": 0.0, "return_pct": 0.0}

        windows.append(WindowResult(
            window_index=w_idx,
            train_start_ms=train_start,
            train_end_ms=train_end,
            test_start_ms=test_start,
            test_end_ms=test_end,
            best_params=best_params,
            train_metrics=_strip_internal(best_train_metrics),
            test_metrics=_strip_internal(test_metrics),
            n_param_combos=n_combos,
        ))

        logger.info(
            "Walk-forward: window %d train_sharpe=%.3f test_sharpe=%.3f test_return=%.2f%% "
            "best=%s",
            w_idx,
            best_train_metrics.get("sharpe_ratio", 0.0),
            test_metrics.get("sharpe_ratio", 0.0),
            test_metrics.get("return_pct", 0.0) * 100.0,
            best_params,
        )

    agg = _aggregate(windows)
    report = WalkForwardReport(
        strategy_name=strategy_name,
        config_path="<inline>",
        window_config=wc,
        windows=windows,
        aggregate=agg,
    )
    return report


def _strip_internal(metrics: Mapping[str, float]) -> Dict[str, float]:
    """Drop the ``__trades`` private key for the public report."""
    out: Dict[str, float] = {}
    for k, v in metrics.items():
        if not k.startswith("__"):
            out[k] = float(v)
    return out


# ── Rendering ──────────────────────────────────────────────────────


def render_report(report: WalkForwardReport) -> str:
    """Render a WalkForwardReport as a text table."""
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append(f"WALK-FORWARD REPORT — strategy: {report.strategy_name}")
    lines.append("=" * 72)
    wc = report.window_config
    lines.append(
        f"train_days={wc.train_days}  test_days={wc.test_days}  "
        f"step_days={wc.step_days or wc.test_days}  n_windows={wc.n_windows}"
    )
    lines.append("-" * 72)
    lines.append(
        f"{'#':>2}  {'train_start':<12}  {'test_start':<12}  "
        f"{'OOS Sharpe':>10}  {'OOS return%':>11}  {'trades':>7}  best_params"
    )
    for w in report.windows:
        ts = datetime.fromtimestamp(w.test_start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        tr = datetime.fromtimestamp(w.train_start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        params = ",".join(f"{k}={v}" for k, v in w.best_params.items()) or "—"
        lines.append(
            f"{w.window_index:>2}  {tr:<12}  {ts:<12}  "
            f"{w.test_metrics.get('sharpe_ratio', 0.0):>10.3f}  "
            f"{w.test_metrics.get('return_pct', 0.0) * 100:>10.2f}%  "
            f"{int(w.test_metrics.get('__trades', 0.0)):>7d}  {params}"
        )
    lines.append("-" * 72)
    a = report.aggregate
    lines.append(
        f"AGGREGATE: {int(a['n_windows'])} windows | "
        f"avg OOS Sharpe={a['avg_oos_sharpe']:.3f} | "
        f"avg OOS return={a['avg_oos_return_pct']:.2f}% | "
        f"positive OOS windows={int(a['positive_oos_windows'])}"
    )
    lines.append("=" * 72)
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────


def _cli(argv: Optional[Iterable[str]] = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(
        description=("Walk-forward optimization for a single strategy. "
                     "Reads the param grid from --grid (JSON string)."),
    )
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--strategy", required=True,
                   help="Strategy name (e.g. CVDOrderFlow, TrendPyramid)")
    p.add_argument("--windows", type=int, default=6)
    p.add_argument("--train-days", type=int, default=30)
    p.add_argument("--test-days", type=int, default=14)
    p.add_argument("--step-days", type=int, default=None)
    p.add_argument("--base-days", type=float, default=365.0)
    p.add_argument(
        "--grid", default="{}",
        help=('Param grid as a JSON object mapping param-name → list of values. '
              'Example: {"min_adx": [20, 25, 30], "max_adx": [35, 40]}. '
              'Empty {} runs the strategy with its existing config (no optimization).'),
    )
    p.add_argument("--json", action="store_true", help="Print the full report as JSON")
    args = p.parse_args(list(argv) if argv is not None else None)

    from src.data.database import Database
    from src.utils.config import load_config

    cfg = load_config(args.config)
    db_path = Path(args.config).resolve().parent.parent / cfg.get(
        "database.path", "data/live/bot.db",
    )
    db = Database(str(db_path))
    symbols = list(cfg.get("assets", ["BTC", "ETH", "SOL"]))
    grid_axes = json.loads(args.grid)
    grid = ParamGrid(axes=grid_axes)
    wc = WindowConfig(
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
        n_windows=args.windows,
    )
    report = run_walk_forward(
        cfg, db, args.strategy, symbols, grid, wc, base_days=args.base_days,
    )
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
