"""Walk-forward optimisation sweep for VolatilityBreakout + VWAPDeviation.

For each strategy x param_set, runs the backtest in 3 non-overlapping
2-week windows, then applies Monte Carlo bootstrap (1000 resamples) on
the trade list to get confidence intervals on PF / Sharpe / MaxDD.

A param set is "robust" only if:
  - n_trades >= 8 in ALL 3 windows (statistical signal)
  - pf_p05 > 0.9 in ALL 3 windows (lower 5% bootstrap PF still near breakeven)
  - sharpe_p05 > -0.5 in ALL 3 windows
  - median across windows of pf_median >= 1.25

Outputs:
  data/backtests/vb_vwap_walkforward_<ts>.csv  (per run metrics)
  data/backtests/vb_vwap_walkforward_<ts>.json (full MC distribution)
  Console: robust ranking + final recommendation per strategy
"""
from __future__ import annotations

import csv
import json
import logging
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.backtest.monte_carlo import bootstrap_metrics
from src.data.database import Database
from src.strategies.volatility_breakout import VolatilityBreakout
from src.strategies.vwap_deviation import VWAPDeviation
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


# Three non-overlapping 2-week windows covering the full 1h/15m history.
WINDOWS = [
    ("W1_0518_0531", "2026-05-18", "2026-05-31"),
    ("W2_0601_0614", "2026-06-01", "2026-06-14"),
    ("W3_0615_0628", "2026-06-15", "2026-06-28"),
]


@dataclass
class ParamSet:
    name: str
    overrides: Dict[str, Any]


# --- VolatilityBreakout param sets ---
VB_SETS: List[ParamSet] = [
    ParamSet("baseline", {}),
    ParamSet("trailing_ema9", {
        "use_trailing_stop": True, "trailing_method": "ema9",
        "trailing_start_r": 1.0,
    }),
    ParamSet("trailing_swing", {
        "use_trailing_stop": True, "trailing_method": "swing",
        "trailing_start_r": 1.0, "trailing_swing_lookback": 5,
    }),
    ParamSet("trailing_atr", {
        "use_trailing_stop": True, "trailing_method": "atr",
        "trailing_start_r": 1.0, "trailing_atr_mult": 1.5,
    }),
    ParamSet("time_tp", {
        "use_time_scaled_tp": True, "time_tp_first_hours": 3.0, "time_tp_after_r": 2.5,
    }),
    ParamSet("trailing_ema9_plus_time_tp", {
        "use_trailing_stop": True, "trailing_method": "ema9", "trailing_start_r": 1.0,
        "use_time_scaled_tp": True, "time_tp_first_hours": 3.0, "time_tp_after_r": 2.5,
    }),
    ParamSet("vol15_plus_trailing_ema9", {
        "volume_surge": 1.5, "use_trailing_stop": True, "trailing_method": "ema9",
    }),
    ParamSet("vol15_plus_oi_plus_trailing_ema9", {
        "volume_surge": 1.5, "require_oi_confirm": True,
        "use_trailing_stop": True, "trailing_method": "ema9",
    }),
]

# --- VWAPDeviation param sets ---
VWAP_SETS: List[ParamSet] = [
    ParamSet("baseline", {}),
    ParamSet("sl_to_be", {
        "use_sl_to_be_after_1r": True, "sl_to_be_r_trigger": 1.0,
    }),
    ParamSet("session_filter", {
        "use_session_filter": True, "session_start_utc_h": 7, "session_end_utc_h": 22,
    }),
    ParamSet("oir_disable_extreme", {
        "oir_disable_extreme_z": True, "oir_extreme_z": 4.0,
    }),
    ParamSet("dynamic_z", {
        "use_dynamic_z": True, "dynamic_z_min": 2.2, "dynamic_z_max": 3.5,
    }),
    ParamSet("sl_to_be_plus_session", {
        "use_sl_to_be_after_1r": True, "use_session_filter": True,
        "session_start_utc_h": 7, "session_end_utc_h": 22,
    }),
    ParamSet("all_optimisations", {
        "use_sl_to_be_after_1r": True,
        "use_session_filter": True, "session_start_utc_h": 7, "session_end_utc_h": 22,
        "oir_disable_extreme_z": True, "oir_extreme_z": 4.0,
        "use_dynamic_z": True,
        "exit_pre_funding_min": 5,
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
    strategy_cls: type,
    base_section: Dict[str, Any],
    overrides: Dict[str, Any],
    window: str,
    start_ms: int,
    end_ms: int,
    initial_capital: float,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    section = dict(base_section)
    section.update(overrides)
    section["enabled"] = True

    strategy = strategy_cls(section)

    risk_cfg = dict(cfg.get("risk", {}) or {})
    pg = dict(risk_cfg.get("portfolio_governance", {}) or {})
    pg["max_correlation"] = 0.98
    risk_cfg["portfolio_governance"] = pg

    bt_cfg = BacktestConfig(
        initial_capital=initial_capital,
        commission_pct=float(cfg.get("backtest.commission_pct", 0.035)),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 5)),
        tca_enabled=bool(cfg.get("execution.tca_enabled", True)),
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.02)),
        use_regime_weights=False,
        use_cooldown=True,
        use_kelly=False,  # isolate strategy edge; size with fixed % later
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
        return {"error": str(exc)[:200], "n_trades": 0}, []

    metrics = result.get("metrics", {})
    trades = result.get("trades", [])
    n = int(metrics.get("n_trades", 0))
    wins = sum(1 for t in trades if float(t.get("pnl_usd", 0)) > 0)
    total_pnl = sum(float(t.get("pnl_usd", 0)) for t in trades)

    res = {
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
        "losses": n - wins,
    }
    return res, trades


def robust_score(mc_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute robust verdict across 3 windows for one param set."""
    n_windows = len(mc_list)
    min_n = min((m.get("n_trades", 0) for m in mc_list), default=0)
    min_pf_p05 = min((m.get("pf_p05", 0) for m in mc_list), default=0)
    min_sharpe_p05 = min((m.get("sharpe_p05", 0) for m in mc_list), default=0)
    median_pf = sorted([m.get("pf_median", 0) for m in mc_list])[n_windows // 2]
    median_pnl = sorted([m.get("pnl_median", 0) for m in mc_list])[n_windows // 2]
    median_prob_profit = sorted([m.get("prob_profitable", 0) for m in mc_list])[n_windows // 2]

    robust = (
        min_n >= 8
        and min_pf_p05 > 0.9
        and min_sharpe_p05 > -0.5
        and median_pf >= 1.25
    )
    return {
        "min_n_trades": min_n,
        "min_pf_p05": round(min_pf_p05, 3),
        "min_sharpe_p05": round(min_sharpe_p05, 3),
        "median_pf_median": round(median_pf, 3),
        "median_pnl": round(median_pnl, 2),
        "median_prob_profitable": round(median_prob_profit * 100, 1),
        "robust": robust,
    }


def main() -> None:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    db = Database(cfg.get("database.path", "data/live/bot.db"))
    initial_capital = float(cfg.get("backtest.initial_capital", 10_000.0))

    vb_base = dict(cfg.get("strategy.volatility_breakout", {}) or {})
    vwap_base = dict(cfg.get("strategy.vwap_deviation", {}) or {})

    all_rows: List[Dict[str, Any]] = []
    all_mc: Dict[str, Dict[str, Any]] = {}  # {strategy|param_set: {window: mc_dict}}

    plans = [
        ("VB", VolatilityBreakout, vb_base, VB_SETS),
        ("VWAP", VWAPDeviation, vwap_base, VWAP_SETS),
    ]

    total = sum(len(ps) for _, _, _, ps in plans) * len(WINDOWS)
    done = 0
    t0 = time.time()
    print(f"VB+VWAP walk-forward sweep: {total} runs\n")

    for strat_name, strat_cls, base, param_sets in plans:
        print(f"\n=== {strat_name} ===")
        for ps in param_sets:
            mc_per_window: List[Dict[str, Any]] = []
            for w_label, w_start, w_end in WINDOWS:
                done += 1
                print(
                    f"  [{done}/{total}] {strat_name:4s} {ps.name:32s} {w_label:18s} ...",
                    end=" ",
                    flush=True,
                )
                t1 = time.time()
                res, trades = run_one(
                    cfg, db, strat_cls, base, ps.overrides,
                    w_label, ms_from_date(w_start), ms_from_date(w_end, end=True),
                    initial_capital,
                )
                if "error" in res:
                    print(f"ERR {res['error'][:60]}")
                    all_rows.append({
                        "strategy": strat_name, "param_set": ps.name, "window": w_label,
                        "error": res["error"], "n_trades": 0, "elapsed_s": round(time.time() - t1, 1),
                    })
                    mc_per_window.append({
                        "n_trades": 0, "pf_p05": 0, "pf_median": 0,
                        "sharpe_p05": 0, "pnl_median": 0, "prob_profitable": 0,
                        "max_dd_p95": 0,
                    })
                    continue

                mc = bootstrap_metrics(trades, initial_capital, n_iter=1000)
                mc_d = mc.as_dict()
                mc_per_window.append(mc_d)

                row = {
                    "strategy": strat_name,
                    "param_set": ps.name,
                    "window": w_label,
                    "elapsed_s": round(time.time() - t1, 1),
                    **res,
                    "mc_pf_median": mc.pf_median,
                    "mc_pf_p05": mc.pf_p05,
                    "mc_pf_p95": mc.pf_p95,
                    "mc_sharpe_median": mc.sharpe_median,
                    "mc_sharpe_p05": mc.sharpe_p05,
                    "mc_max_dd_p95": round(mc.max_dd_p95 * 100, 2),
                    "mc_pnl_median": mc.pnl_median,
                    "mc_pnl_p05": mc.pnl_p05,
                    "mc_prob_profit_pct": round(mc.prob_profitable * 100, 1),
                }
                all_rows.append(row)
                all_mc.setdefault(f"{strat_name}|{ps.name}", {})[w_label] = mc_d

                print(
                    f"n={res['n_trades']:3d} PF={res['profit_factor']:>5} "
                    f"mcPF_p05={mc.pf_p05:>5.2f} mcSharpe_p05={mc.sharpe_p05:>6.2f} "
                    f"ProbP={mc.prob_profitable*100:>3.0f}% ({row['elapsed_s']}s)"
                )

    # --- Save CSV ---
    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"vb_vwap_walkforward_{ts}.csv"
    json_path = out_dir / f"vb_vwap_walkforward_{ts}.json"

    cols = [
        "strategy", "param_set", "window", "elapsed_s",
        "n_trades", "wins", "losses", "win_rate",
        "avg_win_usd", "avg_loss_usd", "profit_factor", "sharpe",
        "max_dd_pct", "total_return_pct", "total_pnl_usd",
        "mc_pf_median", "mc_pf_p05", "mc_pf_p95",
        "mc_sharpe_median", "mc_sharpe_p05",
        "mc_max_dd_p95", "mc_pnl_median", "mc_pnl_p05",
        "mc_prob_profit_pct",
    ]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)

    with open(json_path, "w") as f:
        json.dump({"runs": all_rows, "mc_per_param_set": all_mc}, f, indent=2, default=str)

    print(f"\nCSV: {csv_path}")
    print(f"JSON: {json_path}")
    print(f"Total time: {(time.time()-t0)/60:.1f} min")

    # --- Robust ranking per strategy ---
    print("\n=== ROBUST RANKING (walk-forward + Monte Carlo) ===")
    for strat_name, _, _, param_sets in plans:
        print(f"\n--- {strat_name} ---")
        scored = []
        for ps in param_sets:
            key = f"{strat_name}|{ps.name}"
            mc_list = list(all_mc.get(key, {}).values())
            if not mc_list:
                continue
            score = robust_score(mc_list)
            scored.append((ps.name, score))
        # Sort: robust first, then by median_pf_median desc
        scored.sort(key=lambda x: (not x[1]["robust"], -x[1]["median_pf_median"]))
        print(f"{'ParamSet':36s} {'minN':>4s} {'PF_p05':>6s} {'Shr_p05':>7s} "
              f"{'medPF':>6s} {'ProbP%':>6s} {'Robust':>6s}")
        for name, s in scored:
            flag = "YES" if s["robust"] else "no"
            print(f"{name:36s} {s['min_n_trades']:>4d} {s['min_pf_p05']:>6.2f} "
                  f"{s['min_sharpe_p05']:>7.2f} {s['median_pf_median']:>6.2f} "
                  f"{s['median_prob_profitable']:>6.1f} {flag:>6s}")

    db.close()


if __name__ == "__main__":
    main()
