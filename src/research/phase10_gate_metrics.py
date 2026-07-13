"""Fase 10 frozen-window gate metrics — read-only against ``data/live/bot.db``.

Computes the four go/no-go criteria for the Fase 10 paper-trading validation
window against the manifest frozen by ``src.research.phase10_preregister``:

  * trade count (>= ``min_trades``)
  * Profit Factor (>= ``min_profit_factor``)
  * expectancy_R (> ``expectancy_r_gt``)
  * max drawdown % (<= ``max_drawdown_pct``)

plus an informational cost/slippage summary (not a gate criterion).

R-multiple methodology
-----------------------
The trades table has no first-class "initial risk" column. Both
``VolatilityBreakout`` and ``VWAPDeviation`` compute an ATR-based stop and,
as of the current code, persist it into the trade's ``signal_metadata`` JSON
blob as ``stop_loss_pct`` (fraction of entry price) — see
``Database.enrich_trade_stop_metadata`` and each strategy's signal builder.
For a trade with ``stop_loss_pct`` available, the risk basis is:

    risk_usd = entry_price * stop_loss_pct * size

Older trades (present in the live DB from before ``stop_loss_pct`` was added
to the metadata blob) fall back to ``atr_pct * stop_loss_atr_multiplier``
using the *live* config's per-strategy ``stop_loss_atr_multiplier`` — this is
an approximation (the multiplier at trade time may have differed from the
multiplier in the live/frozen config) and is only applied when
``atr_pct`` is present in ``signal_metadata``. If neither ``stop_loss_pct``
nor ``atr_pct`` can be found, the trade is excluded from the expectancy_R
average entirely (and this is surfaced explicitly in the report as
``r_multiple_trades_used`` vs ``trade_count`` — never silently dropped).

Profit Factor edge case
------------------------
``src/backtest/metrics.py::calculate_metrics`` computes
``profit_factor = gross_profit / gross_loss if gross_loss != 0 else np.inf``
and then, only for its own rounded display key, collapses a non-finite PF to
``0.0``. That collapse is wrong for gate purposes (an all-winners sample
would spuriously read as PF=0.0 == FAIL). ``src/backtest/monte_carlo.py``
instead uses a sentinel: ``inf -> 99.0`` when there is gross profit and no
gross loss. This module reuses the *monte_carlo* convention: zero gross
loss with positive gross profit is treated as PF=99.0 (a clear, obvious
PASS-if-thresholded value), and zero gross profit AND zero gross loss
(e.g. no trades) is PF=0.0.

Max drawdown methodology
-------------------------
There is no isolated capital allocation for these two strategies alone —
they share the overall portfolio. To express drawdown as a percentage (as
requested) rather than a raw dollar figure, this module builds a synthetic
equity curve as ``risk.initial_capital`` (from config, default $10,000) plus
the cumulative sum of ``pnl_usd`` in chronological order (by ``exit_time``),
then reuses ``src.backtest.metrics.max_drawdown`` unmodified. This is an
approximation: it assumes the strategy-subset PnL is additive on top of a
flat capital base, not a fully isolated sub-account equity curve. Documented
here rather than silently assumed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from src.backtest.metrics import max_drawdown as _max_drawdown_frac
from src.research.phase10_preregister import (
    Phase10PreregisterError,
    load_preregister_manifest,
    verify_preregister_integrity,
)
from src.utils.config import Config, get_strategy_section
from src.utils.helpers import safe_float, safe_json_loads

DEFAULT_DB_PATH = Path("data") / "live" / "bot.db"

# Maps a strategy name (as stored in trades.strategy) to the settings.yaml
# strategy sub-section holding its stop_loss_atr_multiplier, used only as a
# fallback risk basis when signal_metadata lacks stop_loss_pct.
_STRATEGY_CONFIG_SECTION = {
    "VolatilityBreakout": "volatility_breakout",
    "VWAPDeviation": "vwap_deviation",
}

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Phase10GateMetricsError(RuntimeError):
    """Raised when the gate report cannot be computed (missing manifest, bad DB path, ...)."""


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise Phase10GateMetricsError(f"Trades DB not found: {db_path}")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_qualifying_trades(
    db_path: Path,
    *,
    window_start_ms: int,
    execution_strategies: List[str],
) -> List[Dict[str, Any]]:
    """Read closed trades for the frozen execution-strategy set since window_start_ms.

    Opens the DB strictly read-only (``mode=ro`` URI) — never writes.
    """
    if not execution_strategies:
        return []
    placeholders = ",".join("?" for _ in execution_strategies)
    sql = f"""
        SELECT *
        FROM trades
        WHERE status = 'closed'
          AND exit_time IS NOT NULL
          AND entry_time >= ?
          AND strategy IN ({placeholders})
        ORDER BY exit_time ASC
    """
    conn = _open_readonly(db_path)
    try:
        cur = conn.execute(sql, (int(window_start_ms), *execution_strategies))
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


def _trade_risk_usd(trade: Dict[str, Any], config: Optional[Config]) -> Optional[float]:
    """Best-available initial-risk basis for a single trade, or None if undeterminable."""
    meta = safe_json_loads(trade.get("signal_metadata") or "", default={}) or {}
    entry_price = safe_float(trade.get("entry_price"))
    size = safe_float(trade.get("size"))
    if entry_price <= 0 or size <= 0:
        return None

    stop_loss_pct = meta.get("stop_loss_pct")
    if stop_loss_pct is not None:
        stop_loss_pct = safe_float(stop_loss_pct)
        if stop_loss_pct > 0:
            return entry_price * stop_loss_pct * size

    atr_pct = meta.get("atr_pct")
    if atr_pct is not None and config is not None:
        section_name = _STRATEGY_CONFIG_SECTION.get(str(trade.get("strategy")))
        if section_name:
            multiplier = safe_float(
                get_strategy_section(config, section_name).get("stop_loss_atr_multiplier"),
                default=0.0,
            )
            atr_pct_f = safe_float(atr_pct)
            if multiplier > 0 and atr_pct_f > 0:
                return entry_price * (atr_pct_f * multiplier) * size

    return None


def compute_profit_factor(trades: List[Dict[str, Any]]) -> float:
    """PF with the monte_carlo.py sentinel convention — see module docstring."""
    pnls = [safe_float(t.get("pnl_usd")) for t in trades]
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss > 0:
        return gross_profit / gross_loss
    return 99.0 if gross_profit > 0 else 0.0


def compute_expectancy_r(
    trades: List[Dict[str, Any]],
    config: Optional[Config],
) -> Dict[str, Any]:
    """Mean R-multiple across trades with a derivable risk basis.

    Returns ``{"expectancy_r": float, "trades_used": int, "trades_total": int}``.
    """
    r_values: List[float] = []
    for t in trades:
        risk_usd = _trade_risk_usd(t, config)
        if risk_usd is None or risk_usd <= 0:
            continue
        r_values.append(safe_float(t.get("pnl_usd")) / risk_usd)
    expectancy = sum(r_values) / len(r_values) if r_values else 0.0
    return {
        "expectancy_r": expectancy,
        "trades_used": len(r_values),
        "trades_total": len(trades),
    }


def compute_max_drawdown_pct(
    trades: List[Dict[str, Any]],
    *,
    initial_capital: float,
) -> float:
    """Max drawdown % from a synthetic equity curve (initial_capital + cumulative pnl_usd).

    Trades must already be in chronological order (by exit_time). See module
    docstring for the "shared portfolio capital base" caveat.
    """
    if not trades:
        return 0.0
    pnls = pd.Series([safe_float(t.get("pnl_usd")) for t in trades], dtype=float)
    equity = float(initial_capital) + pnls.cumsum()
    equity = pd.concat([pd.Series([float(initial_capital)]), equity], ignore_index=True)
    return _max_drawdown_frac(equity) * 100.0


def compute_cost_summary(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Actuals-only cost/slippage sanity summary (informational, not a gate criterion)."""
    entry_fees = sum(safe_float(t.get("entry_fee")) for t in trades)
    order_fees = sum(safe_float(t.get("cumulative_order_fee")) for t in trades)
    funding_paid = sum(safe_float(t.get("funding_paid")) for t in trades)
    total_costs = entry_fees + order_fees + funding_paid
    gross_profit = sum(p for p in (safe_float(t.get("pnl_usd")) for t in trades) if p > 0)
    cost_pct_of_gross_profit = (
        (total_costs / gross_profit) * 100.0 if gross_profit > 0 else None
    )
    return {
        "total_entry_fee_usd": entry_fees,
        "total_order_fee_usd": order_fees,
        "total_funding_paid_usd": funding_paid,
        "total_costs_usd": total_costs,
        "gross_profit_usd": gross_profit,
        "cost_pct_of_gross_profit": cost_pct_of_gross_profit,
    }


def _criterion(
    name: str,
    value: float,
    threshold: float,
    *,
    comparison: str,
    n_trades: int,
    min_trades: int,
) -> Dict[str, Any]:
    """comparison: 'gte' (value >= threshold) or 'gt' (value > threshold) or 'lte'."""
    if n_trades < min_trades:
        status = STATUS_INSUFFICIENT_DATA
    elif comparison == "gte":
        status = STATUS_PASS if value >= threshold else STATUS_FAIL
    elif comparison == "gt":
        status = STATUS_PASS if value > threshold else STATUS_FAIL
    elif comparison == "lte":
        status = STATUS_PASS if value <= threshold else STATUS_FAIL
    else:  # pragma: no cover - programming error guard
        raise ValueError(f"unknown comparison: {comparison}")
    return {"name": name, "value": value, "threshold": threshold, "status": status}


def build_gate_report(
    *,
    db_path: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
    config: Optional[Config] = None,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the full Fase 10 gate report by comparing live trades to the frozen manifest.

    Raises ``Phase10GateMetricsError`` if the manifest doesn't exist yet
    (Task 1 must run first) or if its integrity hash fails to verify.
    """
    import time as _time

    manifest = load_preregister_manifest(manifest_path)
    if manifest is None:
        raise Phase10GateMetricsError(
            "Fase 10 preregister manifest not found — run "
            "src.research.phase10_preregister.persist_preregister_manifest() "
            "before checking the gate."
        )
    try:
        verify_preregister_integrity(manifest)
    except Phase10PreregisterError as exc:
        raise Phase10GateMetricsError(str(exc)) from exc

    window_start_ms = int(manifest["window_start_ms"])
    execution_strategies = list(manifest.get("execution_strategies", []))
    thresholds = manifest.get("gate_thresholds", {})
    min_trades = int(thresholds.get("min_trades", 100))
    min_pf = float(thresholds.get("min_profit_factor", 1.20))
    expectancy_r_gt = float(thresholds.get("expectancy_r_gt", 0.0))
    max_dd_pct = float(thresholds.get("max_drawdown_pct", 5.0))

    db = db_path or DEFAULT_DB_PATH
    trades = load_qualifying_trades(
        db, window_start_ms=window_start_ms, execution_strategies=execution_strategies
    )
    n_trades = len(trades)

    pf = compute_profit_factor(trades)
    expectancy = compute_expectancy_r(trades, config)
    initial_capital = (
        safe_float(config.get("risk.initial_capital"), default=10_000.0)
        if config is not None
        else 10_000.0
    )
    max_dd = compute_max_drawdown_pct(trades, initial_capital=initial_capital)
    cost_summary = compute_cost_summary(trades)

    criteria = {
        "min_trades": {
            "name": "min_trades",
            "value": n_trades,
            "threshold": min_trades,
            "status": STATUS_PASS if n_trades >= min_trades else STATUS_FAIL,
        },
        "profit_factor": _criterion(
            "profit_factor", pf, min_pf, comparison="gte",
            n_trades=n_trades, min_trades=min_trades,
        ),
        "expectancy_r": _criterion(
            "expectancy_r", expectancy["expectancy_r"], expectancy_r_gt, comparison="gt",
            n_trades=n_trades, min_trades=min_trades,
        ),
        "max_drawdown_pct": _criterion(
            "max_drawdown_pct", max_dd, max_dd_pct, comparison="lte",
            n_trades=n_trades, min_trades=min_trades,
        ),
    }
    all_pass = all(c["status"] == STATUS_PASS for c in criteria.values())

    return {
        "generated_at_ms": int(now_ms if now_ms is not None else _time.time() * 1000),
        "experiment_id": manifest.get("experiment_id"),
        "window_start_ms": window_start_ms,
        "execution_strategies": execution_strategies,
        "trade_count": n_trades,
        "expectancy_r_trades_used": expectancy["trades_used"],
        "expectancy_r_trades_total": expectancy["trades_total"],
        "criteria": criteria,
        "cost_summary": cost_summary,
        "gate_met": all_pass,
    }


def format_report_text(report: Dict[str, Any]) -> str:
    lines = [
        "=== Fase 10 Frozen-Window Gate Report ===",
        f"experiment_id: {report['experiment_id']}",
        f"window_start_ms: {report['window_start_ms']}",
        f"execution_strategies: {', '.join(report['execution_strategies'])}",
        f"trade_count: {report['trade_count']}",
        (
            f"expectancy_r basis: {report['expectancy_r_trades_used']}/"
            f"{report['expectancy_r_trades_total']} trades had a derivable risk basis"
        ),
        "",
        "Criteria:",
    ]
    for c in report["criteria"].values():
        lines.append(
            f"  [{c['status']:<20}] {c['name']:<18} value={c['value']!r} threshold={c['threshold']!r}"
        )
    lines.append("")
    cs = report["cost_summary"]
    lines.append("Cost summary (informational):")
    lines.append(f"  total_costs_usd={cs['total_costs_usd']:.4f} gross_profit_usd={cs['gross_profit_usd']:.4f}")
    cost_pct = cs["cost_pct_of_gross_profit"]
    lines.append(
        "  cost_pct_of_gross_profit="
        + (f"{cost_pct:.2f}%" if cost_pct is not None else "N/A (no gross profit)")
    )
    lines.append("")
    lines.append(f"GATE MET: {report['gate_met']}")
    return "\n".join(lines)
