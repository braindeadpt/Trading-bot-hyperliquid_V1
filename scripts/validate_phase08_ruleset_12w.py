"""Frozen Phase08 ruleset validation — 12-week backtest + walk-forward (no tuning).

Validates the v3.1.48 execution set (ChecklistMeta + VWAPDeviation) with the
live YAML params. Intentionally does NOT sweep parameters — this is OOS-style
measurement of the already-selected ruleset (ChecklistMeta promotion was
partly in-sample; this is the required walk-forward check).

Usage:
  python scripts/validate_phase08_ruleset_12w.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine, build_backtest_config_from_yaml
from src.backtest.monte_carlo import bootstrap_metrics
from src.data.database import Database
from src.strategies.factory import build_backtest_strategy, build_phase08_strategies
from src.utils.config import load_config

logging.basicConfig(level=logging.ERROR)
for n in (
    "src.core.volatility_circuit",
    "src.backtest.engine",
    "src.strategies",
    "src.core.risk_manager",
    "src.core.funding_blackout",
):
    logging.getLogger(n).setLevel(logging.ERROR)

# Contiguous 4-week folds covering ~12 weeks ending 2026-08-07 (no param sweep).
FOLDS = [
    ("W1_0516_0612", "2026-05-16", "2026-06-12"),
    ("W2_0613_0710", "2026-06-13", "2026-07-10"),
    ("W3_0711_0807", "2026-07-11", "2026-08-07"),
]


def ms_from_date(s: str, end: bool = False) -> int:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def _trade_pnl(t: Dict[str, Any]) -> float:
    return float(t.get("pnl_usd") or t.get("net_pnl") or t.get("pnl") or 0.0)


def _trade_fees(t: Dict[str, Any]) -> float:
    return float(t.get("fees") or t.get("fee_usd") or t.get("total_fees") or 0.0)


def _summarize(result: Dict[str, Any]) -> Dict[str, Any]:
    """Build fold summary from trades + metrics (canonical key mapping).

    ``calculate_metrics`` exposes ``avg_trade`` / ``avg_win`` / ``avg_loss`` /
    ``expectancy_r`` but not ``total_pnl`` / dollar ``expectancy``. Derive
    dollar aggregates from the trade list so JSON never silently reports 0.
    """
    metrics = result.get("metrics") or {}
    trades = result.get("trades") or []
    by_strat: Dict[str, int] = {}
    pnls: List[float] = []
    fees: List[float] = []
    for t in trades:
        name = str(t.get("strategy") or t.get("sub_strategy") or "?")
        by_strat[name] = by_strat.get(name, 0) + 1
        pnls.append(_trade_pnl(t))
        fees.append(_trade_fees(t))

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net_pnl = float(sum(pnls))
    fees_total = float(sum(fees))
    n = len(pnls)
    # Dollar expectancy = mean trade PnL (matches metrics.avg_trade when present).
    expectancy = (
        float(metrics.get("avg_trade"))
        if metrics.get("avg_trade") is not None and n
        else (net_pnl / n if n else 0.0)
    )
    avg_win = float(metrics.get("avg_win") or (sum(wins) / len(wins) if wins else 0.0))
    avg_loss = float(
        metrics.get("avg_loss") or (sum(losses) / len(losses) if losses else 0.0)
    )

    out = {
        "n_trades": int(metrics.get("n_trades") or metrics.get("total_trades") or n),
        "win_rate": float(metrics.get("win_rate") or 0.0),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "profit_factor": float(metrics.get("profit_factor") or 0.0),
        "sharpe": float(metrics.get("sharpe_ratio") or metrics.get("sharpe") or 0.0),
        "max_dd_pct": float(metrics.get("max_drawdown_pct") or metrics.get("max_drawdown") or 0.0),
        "fees_total": round(fees_total, 4),
        "gross_pnl": round(net_pnl + fees_total, 4),
        "total_pnl": round(net_pnl, 4),
        "net_pnl": round(net_pnl, 4),
        "by_strategy": by_strat,
    }
    if trades:
        try:
            mc = bootstrap_metrics(trades, n_iter=1000, seed=42)
            out["mc_pf_p05"] = float(getattr(mc, "pf_p05", 0.0) or 0.0)
            out["mc_sharpe_p05"] = float(getattr(mc, "sharpe_p05", 0.0) or 0.0)
        except Exception as exc:  # noqa: BLE001
            out["mc_error"] = str(exc)
    return out


def run_window(
    cfg: Any,
    db: Database,
    symbols: List[str],
    start: str,
    end: str,
) -> Dict[str, Any]:
    strategy = build_backtest_strategy(cfg)
    bt = build_backtest_config_from_yaml(cfg)
    # Validation run: keep risk gates; disable soft vol CB / funding blackout noise.
    if hasattr(bt, "use_volatility_circuit"):
        bt.use_volatility_circuit = False
    if hasattr(bt, "use_funding_blackout"):
        bt.use_funding_blackout = False
    if hasattr(bt, "max_daily_trades"):
        bt.max_daily_trades = 0
    # Proxy TCA for candle-only replay (strict live TCA needs L2).
    bt.tca_enabled = True
    bt.use_microstructure_proxy = True
    engine = BacktestEngine(
        database=db,
        strategy=strategy,
        config=bt,
        symbols=symbols,
        risk_config=cfg,
    )
    t0 = time.time()
    result = engine.run(start_ms=ms_from_date(start), end_ms=ms_from_date(end, end=True))
    summary = _summarize(result)
    summary["elapsed_s"] = round(time.time() - t0, 1)
    return summary


def main() -> int:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    # Prefer a snapshot DB so the live paper bot can keep the primary file locked.
    snap = ROOT / "data" / "live" / "bot_ruleset_validate.db"
    db_path = snap if snap.exists() else Path(cfg.get("database.path", "data/live/bot.db"))
    db = Database(str(db_path))
    symbols = list(cfg.get("assets") or cfg.get("symbols") or ["BTC", "ETH", "SOL", "HYPE"])
    exec_s, _ = build_phase08_strategies(cfg)
    print("=" * 72)
    print("PHASE08 RULESET VALIDATION (frozen — no param sweep)")
    print(f"execution: {[s.name for s in exec_s]}")
    print(f"db:        {db_path}")
    print(f"symbols:   {symbols}")
    print(f"sizing:    max_position_size_pct={cfg.get('risk.max_position_size_pct')}")
    print("=" * 72)

    rows: Dict[str, Any] = {"folds": {}, "aggregate": {}}
    all_trades_n = 0
    weighted_pf_num = 0.0
    weighted_pf_den = 0.0

    for label, start, end in FOLDS:
        print(f"\n[{label}] {start} .. {end}", flush=True)
        summary = run_window(cfg, db, symbols, start, end)
        rows["folds"][label] = summary
        print(json.dumps(summary, indent=2), flush=True)
        n = int(summary.get("n_trades") or 0)
        all_trades_n += n
        # Approximate aggregate PF from fold PFs weighted by trade count.
        pf = float(summary.get("profit_factor") or 0.0)
        if n > 0 and pf > 0:
            # Reconstruct rough win/loss mass is unavailable; keep list for report.
            weighted_pf_num += pf * n
            weighted_pf_den += n

    rows["aggregate"] = {
        "n_trades": all_trades_n,
        "trade_weighted_mean_pf": (
            round(weighted_pf_num / weighted_pf_den, 4) if weighted_pf_den else 0.0
        ),
        "total_pnl": round(
            sum(float(v.get("total_pnl") or 0.0) for v in rows["folds"].values()), 4
        ),
        "elapsed_s_total": round(
            sum(float(v.get("elapsed_s") or 0.0) for v in rows["folds"].values()), 1
        ),
        "window": "2026-05-16 .. 2026-08-07 (3x4w folds)",
    }

    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"phase08_ruleset_12w_{ts}.json"
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}", flush=True)

    fold_pfs = [v.get("profit_factor", 0.0) for v in rows["folds"].values()]
    fold_n = [v.get("n_trades", 0) for v in rows["folds"].values()]
    print("\n--- SUMMARY ---", flush=True)
    print(
        f"aggregate trades={all_trades_n} "
        f"trade_weighted_mean_PF={rows['aggregate']['trade_weighted_mean_pf']}",
        flush=True,
    )
    print(f"fold trades={fold_n} fold PFs={['%.3f' % x for x in fold_pfs]}", flush=True)
    if any(n < 20 for n in fold_n):
        print("NOTE: at least one fold has <20 trades — treat as underpowered.", flush=True)
    if all_trades_n < 100:
        print("NOTE: aggregate <100 trades — below Fase 10 min_trades gate.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
