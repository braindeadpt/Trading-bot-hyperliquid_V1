"""CVDOrderFlow isolated backtest sweep — find best params after buy/sell volume backfill.

Tests CVD across (window x threshold-set) and writes results to CSV.
"""
from __future__ import annotations

import csv
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.database import Database
from src.strategies.cvd_orderflow import CVDOrderFlow
from src.utils.config import load_config

logging.basicConfig(level=logging.ERROR)
for n in ("src.core.volatility_circuit", "src.backtest.engine", "src.strategies", "src.core.risk_manager"):
    logging.getLogger(n).setLevel(logging.ERROR)


WINDOWS = [
    ("D_full", "2026-05-24", "2026-06-29"),  # 1m candles start May 24
    ("B_2weeks", "2026-06-11", "2026-06-25"),
]


@dataclass
class ParamSet:
    name: str
    overrides: Dict[str, Any]


PARAM_SETS = [
    ParamSet("current", {}),
    ParamSet("relaxed", {
        "min_divergence_strength": 0.25,
        "min_volume_usd": 30_000.0,
        "require_oir_confirm": False,
        "min_confidence": 0.40,
        "min_price_move_pct": 0.0015,
    }),
    ParamSet("relaxed_no_oir_loose_adx", {
        "min_divergence_strength": 0.25,
        "min_volume_usd": 30_000.0,
        "require_oir_confirm": False,
        "min_confidence": 0.40,
        "min_adx": 8.0,
        "max_adx": 35.0,
    }),
    ParamSet("tight", {
        "min_divergence_strength": 0.45,
        "min_volume_usd": 80_000.0,
        "require_oir_confirm": True,
        "min_confidence": 0.55,
        "take_profit_r_multiple": 2.5,
    }),
]


def ms_from_date(s: str, end: bool = False) -> int:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def run_one(
    cfg: Any,
    db: Database,
    base_section: Dict[str, Any],
    overrides: Dict[str, Any],
    window: str,
    start_ms: int,
    end_ms: int,
) -> Dict[str, Any]:
    section = dict(base_section)
    section.update(overrides)
    section["enabled"] = True

    strategy = CVDOrderFlow(section)

    risk_cfg = dict(cfg.get("risk", {}) or {})
    pg = dict(risk_cfg.get("portfolio_governance", {}) or {})
    pg["max_correlation"] = 0.98
    risk_cfg["portfolio_governance"] = pg

    bt_cfg = BacktestConfig(
        initial_capital=float(cfg.get("backtest.initial_capital", 10_000.0)),
        commission_pct=float(cfg.get("backtest.commission_pct", 0.035)),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 5)),
        tca_enabled=bool(cfg.get("execution.tca_enabled", True)),
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.02)),
        use_regime_weights=False,
        use_cooldown=True,
        use_kelly=True,
        use_microstructure_proxy=True,
        use_risk_manager=True,
        use_volatility_circuit=False,
        use_funding_blackout=False,
        use_external_feeds_replay=True,
        max_daily_trades=0,
    )

    engine = BacktestEngine(
        database=db,
        strategy=strategy,
        config=bt_cfg,
        symbols=["BTC", "ETH", "SOL"],
        risk_config=risk_cfg,
    )

    try:
        result = engine.run(start_ms=start_ms, end_ms=end_ms)
    except Exception as exc:
        return {"error": str(exc), "n_trades": 0}

    metrics = result.get("metrics", {})
    trades = result.get("trades", [])
    n = int(metrics.get("n_trades", 0))
    wins = sum(1 for t in trades if float(t.get("pnl_usd", 0)) > 0)
    losses = n - wins
    total_pnl = sum(float(t.get("pnl_usd", 0)) for t in trades)

    return {
        "n_trades": n,
        "win_rate": round(float(metrics.get("win_rate", 0)) * 100, 1),
        "avg_win_usd": round(float(metrics.get("avg_win", 0)), 2),
        "avg_loss_usd": round(float(metrics.get("avg_loss", 0)), 2),
        "profit_factor": round(float(metrics.get("profit_factor", 0)), 3),
        "sharpe": round(float(metrics.get("sharpe_ratio", 0)), 3),
        "max_dd_pct": round(float(metrics.get("max_drawdown", 0)) * 100, 2),
        "total_return_pct": round(float(metrics.get("total_return", 0)) * 100, 2),
        "total_pnl_usd": round(total_pnl, 2),
        "wins": wins,
        "losses": losses,
    }


def main() -> None:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    db = Database(cfg.get("database.path", "data/live/bot.db"))
    base = dict(cfg.get("strategy.cvd_orderflow", {}) or {})

    rows: List[Dict[str, Any]] = []
    total = len(WINDOWS) * len(PARAM_SETS)
    done = 0
    t0 = time.time()
    print(f"CVD sweep: {total} runs\n")

    for ps in PARAM_SETS:
        for w_label, w_start, w_end in WINDOWS:
            done += 1
            print(
                f"  [{done}/{total}] {ps.name:30s} {w_label:10s} ...",
                end=" ",
                flush=True,
            )
            t1 = time.time()
            res = run_one(
                cfg, db, base, ps.overrides,
                w_label, ms_from_date(w_start), ms_from_date(w_end, end=True),
            )
            res["param_set"] = ps.name
            res["window"] = w_label
            res["elapsed_s"] = round(time.time() - t1, 1)
            rows.append(res)
            if "error" in res:
                print(f"ERR {res['error'][:40]} ({res['elapsed_s']}s)")
            else:
                print(
                    f"n={res['n_trades']:3d} PF={res['profit_factor']:>5} "
                    f"Sharpe={res['sharpe']:>6} Exp=${res.get('total_pnl_usd', 0):>7.2f} "
                    f"({res['elapsed_s']}s)"
                )

    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"cvd_sweep_{ts}.csv"
    cols = ["param_set", "window", "n_trades", "wins", "losses", "win_rate",
            "avg_win_usd", "avg_loss_usd", "profit_factor", "sharpe",
            "max_dd_pct", "total_return_pct", "total_pnl_usd", "elapsed_s"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"\nCSV: {csv_path}")
    print(f"Total time: {(time.time()-t0)/60:.1f} min")

    print("\n=== SUMMARY ===")
    print(f"{'ParamSet':32s} {'Window':10s} {'n':>4s} {'PF':>6s} {'Sharpe':>7s} {'PnL$':>8s} {'WR%':>5s}")
    for r in sorted(rows, key=lambda x: -x.get("profit_factor", 0)):
        if "error" in r:
            continue
        print(
            f"{r['param_set']:32s} {r['window']:10s} {r['n_trades']:>4d} "
            f"{r['profit_factor']:>6.3f} {r['sharpe']:>7.3f} "
            f"{r['total_pnl_usd']:>8.2f} {r['win_rate']:>5.1f}"
        )

    db.close()


if __name__ == "__main__":
    main()
