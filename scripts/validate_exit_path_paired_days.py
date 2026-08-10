"""Paired-day live vs replay exit parity for P1/P2 OHLC paths.

Only days where ChecklistMeta traded live are compared (not full 12w aggregate).
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestEngine, build_backtest_config_from_yaml
from src.data.database import Database
from src.strategies.factory import build_backtest_strategy
from src.utils.config import load_config

PATHS = {
    "P1": "favorable_first",
    "P2": "adverse_first",
}


def ms_day(day: str, end: bool = False) -> int:
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def _stats(trades: List[Dict[str, Any]], reason_key: str = "exit_reason") -> Dict[str, Any]:
    pnls = [float(t.get("pnl_usd") or 0.0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    by = Counter()
    be_pnls = []
    for t in trades:
        r = str(t.get(reason_key) or "")
        if r.startswith("sl_to_be"):
            by["sl_to_be*"] += 1
            be_pnls.append(float(t.get("pnl_usd") or 0.0))
        elif r in ("take_profit", "checklist_tp_hit"):
            by["tp*"] += 1
        elif r == "stop_loss":
            by["stop_loss"] += 1
        else:
            by[r or "?"] += 1
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    rr = abs(avg_w / avg_l) if avg_l < 0 else 0.0
    return {
        "n": len(trades),
        "wr": round(len(wins) / len(trades), 4) if trades else 0.0,
        "avg_win": round(avg_w, 2),
        "avg_loss": round(avg_l, 2),
        "rr": round(rr, 3),
        "be_n": by.get("sl_to_be*", 0),
        "be_avg_pnl": round(sum(be_pnls) / len(be_pnls), 2) if be_pnls else None,
        "stop_n": by.get("stop_loss", 0),
        "tp_n": by.get("tp*", 0),
        "by_reason": dict(by),
        "sum_pnl": round(sum(pnls), 2),
    }


def live_cm_days(db: Database) -> Dict[str, List[Dict[str, Any]]]:
    rows = db._conn().execute(
        """
        SELECT symbol, side, exit_reason, pnl_usd, entry_time, exit_time,
               sub_strategy, strategy
        FROM trades
        WHERE status IS NULL OR status != 'open'
        """
    ).fetchall()
    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        d = dict(r)
        if str(d.get("sub_strategy") or d.get("strategy") or "") != "ChecklistMeta":
            continue
        exit_ms = int(d["exit_time"])
        day = datetime.fromtimestamp(exit_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day].append(d)
    return dict(by_day)


def replay_day(
    cfg: Any,
    db: Database,
    symbols: List[str],
    day: str,
    path_policy: str,
) -> List[Dict[str, Any]]:
    bt = build_backtest_config_from_yaml(cfg)
    bt.use_volatility_circuit = False
    bt.use_funding_blackout = False
    bt.max_daily_trades = 0
    bt.use_microstructure_proxy = True
    bt.exit_path_policy = path_policy
    eng = BacktestEngine(
        database=db,
        strategy=build_backtest_strategy(cfg),
        config=bt,
        symbols=symbols,
        risk_config=cfg,
    )
    result = eng.run(start_ms=ms_day(day), end_ms=ms_day(day, end=True))
    return [
        t
        for t in (result.get("trades") or [])
        if str(t.get("strategy") or t.get("sub_strategy") or "") == "ChecklistMeta"
    ]


def main() -> int:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    snap = ROOT / "data" / "live" / "bot_ruleset_validate.db"
    db = Database(str(snap if snap.exists() else ROOT / "data" / "live" / "bot.db"))
    symbols = list(cfg.get("assets") or ["BTC", "ETH", "SOL", "HYPE"])
    live_days = live_cm_days(db)
    # Focus on parity window with known density
    days = sorted(d for d in live_days if "2026-06-30" <= d <= "2026-07-12")
    print(f"paired days ({len(days)}): {days}", flush=True)

    out: Dict[str, Any] = {"days": days, "paths": {}}
    for label, policy in PATHS.items():
        print(f"\n=== {label} ({policy}) ===", flush=True)
        per_day = {}
        live_all: List[Dict[str, Any]] = []
        replay_all: List[Dict[str, Any]] = []
        t0 = time.time()
        for day in days:
            live = live_days[day]
            # skip pure shutdown noise days optional — keep all
            t1 = time.time()
            replay = replay_day(cfg, db, symbols, day, policy)
            live_s = _stats(live)
            rep_s = _stats(replay)
            per_day[day] = {
                "live": live_s,
                "replay": rep_s,
                "elapsed_s": round(time.time() - t1, 1),
            }
            live_all.extend(live)
            replay_all.extend(replay)
            print(
                f"  {day}: live n={live_s['n']} be={live_s['be_n']} wr={live_s['wr']} "
                f"| replay n={rep_s['n']} be={rep_s['be_n']} wr={rep_s['wr']} "
                f"be_avg={rep_s['be_avg_pnl']} rr={rep_s['rr']}",
                flush=True,
            )
        out["paths"][label] = {
            "policy": policy,
            "per_day": per_day,
            "paired_aggregate": {
                "live": _stats(live_all),
                "replay": _stats(replay_all),
                "elapsed_s": round(time.time() - t0, 1),
            },
        }

    # Bracket summary
    p1 = out["paths"]["P1"]["paired_aggregate"]["replay"]
    p2 = out["paths"]["P2"]["paired_aggregate"]["replay"]
    live = out["paths"]["P1"]["paired_aggregate"]["live"]
    out["bracket"] = {
        "live": live,
        "P1_favorable": p1,
        "P2_adverse": p2,
        "be_count_close": abs(p1["be_n"] - p2["be_n"]) <= max(2, abs(live["be_n"]) // 5),
        "rr_span": [min(p1["rr"], p2["rr"]), max(p1["rr"], p2["rr"])],
        "note": (
            "Success = BE counts comparable to live and R:R moves toward live (~0.73); "
            "WR may fall as large winners become BE≈0."
        ),
    }

    path = ROOT / "data" / "backtests" / "parity_diag" / "exit_path_paired_p1_p2.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out["bracket"], indent=2), flush=True)
    print(f"Wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
