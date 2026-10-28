#!/usr/bin/env python3
"""Read-only status + read-gate report for resmom-shadow.

Prints heartbeat, rebalance/position counts, the descriptive KILL check
(<= -35% net dd — reports only, never stops anything), and once
READ_REBALANCES closed weeks exist the PASS/C verdict with its checks.
Never unseals early: before 26 closed weeks the verdict is IN_PROGRESS
and only counts/state are shown.

Usage: python -m scripts.research.resmom_status [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.resmom_shadow import spec  # noqa: E402
from src.resmom_shadow.read import evaluate, open_ro  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=spec.DB_PATH)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    db = Path(args.db)
    res = evaluate(str(db))
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True, default=str))
        return
    if res.get("verdict") == "NO_LEDGER":
        print(f"no ledger at {db} — daemon has not booted")
        return

    hb = res.get("heartbeat_age_s")
    print(f"heartbeat : {f'{hb:.0f}s ago' if hb is not None else 'never'}"
          f"  [{'RED' if (hb is None or hb > spec.HEARTBEAT_STALE_S) else 'green'}]")
    print(f"rebalances A: {res['rebalances_A']}  closed weeks: "
          f"{res['closed_weeks']}/{res['read_due_at']}")

    k = res["kill"]
    print(f"kill check: dd_min={k['dd_min']:.2%}  "
          f"{'BREACHED (<=-35%) — REPORT ONLY' if k['breached'] else 'ok'}")
    if k.get("flag_meta"):
        print(f"            meta flag: {k['flag_meta']}")

    if res["verdict"] == "IN_PROGRESS":
        print("\nread: SEALED until 26 closed rebalances "
              "(counts/state only)")
    else:
        print(f"\nverdict: {res['verdict']}")
        print(f"  cum_return     : {res['cum_return']:.2%}  (>0 req)")
        sh = res.get("sharpe_ann")
        print(f"  sharpe_ann     : {f'{sh:.3f}' if sh is not None else 'n/a'}"
              f"  (>{spec.PASS_SHARPE} req)")
        p75 = res.get("null_sharpe_p75")
        print(f"  null p75 sharpe: {f'{p75:.3f}' if p75 is not None else 'n/a'}"
              f"  (sharpe must beat it; n={res['n_nulls_scored']})")
        print(f"  pnl ex-top3    : ${res['pnl_ex_top3']:,.2f}  (>0 req)")
        if res.get("checks"):
            print("  checks         : "
                  + ", ".join(f"{k}={'OK' if v else 'FAIL'}"
                              for k, v in res["checks"].items()))
    print("\nweekly returns (closed): "
          + ", ".join(f"{r:.2%}" for r in res.get("weekly_returns", [])))


if __name__ == "__main__":
    main()
