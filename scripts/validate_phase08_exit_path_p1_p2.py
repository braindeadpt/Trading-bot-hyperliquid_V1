"""Run Phase08 12w validation for exit_path_policy P1 and P2."""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_phase08_ruleset_12w import (  # noqa: E402
    FOLDS,
    _summarize,
    ms_from_date,
)
from src.backtest.engine import BacktestEngine, build_backtest_config_from_yaml
from src.data.database import Database
from src.strategies.factory import build_backtest_strategy
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

POLICIES = {
    "P1_favorable_first": "favorable_first",
    "P2_adverse_first": "adverse_first",
}


def run_one(cfg: Any, db: Database, symbols: list, policy: str) -> Dict[str, Any]:
    folds: Dict[str, Any] = {}
    for label, start, end in FOLDS:
        print(f"  [{policy}] {label}", flush=True)
        bt = build_backtest_config_from_yaml(cfg)
        bt.use_volatility_circuit = False
        bt.use_funding_blackout = False
        bt.max_daily_trades = 0
        bt.use_microstructure_proxy = True
        bt.exit_path_policy = policy
        eng = BacktestEngine(
            database=db,
            strategy=build_backtest_strategy(cfg),
            config=bt,
            symbols=symbols,
            risk_config=cfg,
        )
        t0 = time.time()
        result = eng.run(start_ms=ms_from_date(start), end_ms=ms_from_date(end, end=True))
        summary = _summarize(result)
        summary["elapsed_s"] = round(time.time() - t0, 1)
        # exit mix
        reasons: Dict[str, int] = {}
        for t in result.get("trades") or []:
            r = str(t.get("exit_reason") or "")
            key = "sl_to_be*" if r.startswith("sl_to_be") else (r or "?")
            reasons[key] = reasons.get(key, 0) + 1
        summary["exit_reasons"] = reasons
        folds[label] = summary
        print(json.dumps(summary, indent=2), flush=True)
    n = sum(int(v["n_trades"]) for v in folds.values())
    times = [float(v["elapsed_s"]) for v in folds.values()]
    mean_t = sum(times) / len(times) if times else 0.0
    max_dev = max(abs(t - mean_t) / mean_t for t in times) if mean_t else 0.0
    return {
        "folds": folds,
        "aggregate": {
            "n_trades": n,
            "total_pnl": round(sum(float(v["total_pnl"]) for v in folds.values()), 2),
            "trade_weighted_wr": round(
                sum(float(v["win_rate"]) * int(v["n_trades"]) for v in folds.values()) / n, 4
            )
            if n
            else 0.0,
            "trade_weighted_pf": round(
                sum(float(v["profit_factor"]) * int(v["n_trades"]) for v in folds.values()) / n, 4
            )
            if n
            else 0.0,
            "elapsed_s": round(sum(times), 1),
            "fold_times": times,
            "max_rel_time_dev": round(max_dev, 4),
            "times_linear": max_dev <= 0.30,
        },
    }


def main() -> int:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    snap = ROOT / "data" / "live" / "bot_ruleset_validate.db"
    db = Database(str(snap if snap.exists() else ROOT / "data" / "live" / "bot.db"))
    symbols = list(cfg.get("assets") or ["BTC", "ETH", "SOL", "HYPE"])
    out: Dict[str, Any] = {"policies": {}}
    for name, policy in POLICIES.items():
        print(f"\n######## {name} ########", flush=True)
        out["policies"][name] = run_one(cfg, db, symbols, policy)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = ROOT / "data" / "backtests" / f"phase08_exit_path_p1_p2_12w_{ts}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
