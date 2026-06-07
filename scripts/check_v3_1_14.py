"""Daily v3.1.14 health check.

Usage:  python scripts/check_v3_1_14.py

Reports:
  - Bot uptime / process state
  - Recent CVDOrderFlow signals (vol_usd + div)
  - decision_audit count (should grow if ensemble fires)
  - signals table (last 24h)
  - last 5 trades
  - governor disabled set
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path("data/live/bot.db")
NOW_MS = int(time.time() * 1000)
DAY_MS = 24 * 3600 * 1000


def header(text: str) -> None:
    print()
    print("=" * 70)
    print(f"  {text}")
    print("=" * 70)


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return 1

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    header("v3.1.14 health check @ " + time.strftime("%Y-%m-%d %H:%M:%S"))

    # 1. decision_audit
    print("\n[1] decision_audit (QW1)")
    total = db.execute("SELECT COUNT(*) AS n FROM decision_audit").fetchone()["n"]
    last_24 = db.execute(
        "SELECT COUNT(*) AS n FROM decision_audit WHERE timestamp > ?",
        (NOW_MS - DAY_MS,),
    ).fetchone()["n"]
    print(f"  total rows: {total}")
    print(f"  last 24h:   {last_24}")
    if last_24 == 0 and total == 0:
        print("  [WARN] No decisions recorded since restart. Ensemble is returning None for every event.")
    elif last_24 == 0:
        print(f"  [WARN] No decisions in last 24h. {total} historical rows exist.")

    # 2. signals table
    print("\n[2] signals table")
    sig_24 = db.execute(
        "SELECT COUNT(*) AS n FROM signals WHERE timestamp > ?",
        (NOW_MS - DAY_MS,),
    ).fetchone()["n"]
    sig_total = db.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
    last_sig = db.execute(
        "SELECT MAX(timestamp) AS ts FROM signals"
    ).fetchone()["ts"]
    last_sig_str = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(last_sig / 1000))
        if last_sig
        else "n/a"
    )
    print(f"  total: {sig_total}")
    print(f"  last 24h: {sig_24}")
    print(f"  last signal: {last_sig_str}")

    # 3. trades
    print("\n[3] trades")
    tr_24 = db.execute(
        "SELECT COUNT(*) AS n, SUM(pnl_usd) AS pnl FROM trades WHERE entry_time > ?",
        (NOW_MS - DAY_MS,),
    ).fetchone()
    tr_total = db.execute("SELECT COUNT(*) AS n, SUM(pnl_usd) AS pnl FROM trades").fetchone()
    print(f"  last 24h: n={tr_24['n']} pnl=${tr_24['pnl'] or 0:.2f}")
    print(f"  all-time: n={tr_total['n']} pnl=${tr_total['pnl'] or 0:.2f}")

    # 4. per-sub-strategy PnL (last 30 days)
    print("\n[4] sub_strategy PnL (last 30d)")
    since30 = NOW_MS - 30 * DAY_MS
    rows = db.execute(
        """
        SELECT sub_strategy,
               COUNT(*) AS n,
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(SUM(pnl_usd), 2) AS pnl,
               ROUND(AVG(pnl_pct) * 100, 3) AS avg_pct
        FROM trades
        WHERE entry_time > ? AND sub_strategy IS NOT NULL
        GROUP BY sub_strategy
        ORDER BY n DESC
        """,
        (since30,),
    ).fetchall()
    if not rows:
        print("  no sub_strategy data (trades table is empty or all NULL)")
    for r in rows:
        wr = (r["wins"] / r["n"] * 100) if r["n"] else 0
        print(
            f"  {str(r['sub_strategy']):25s} n={r['n']:3d} wins={r['wins']:2d} "
            f"wr={wr:5.1f}% pnl=${r['pnl']:8.2f} avg_pct={r['avg_pct']:7.3f}%"
        )

    # 5. recent bot log lines for CVDOrderFlow (with vol_usd)
    print("\n[5] last 5 CVDOrderFlow log lines (vol_usd=)")
    log_path = Path("logs/bot.log")
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            cvd_lines = [l for l in lines if "CVDOrderFlow" in l and "vol_usd" in l]
            for line in cvd_lines[-5:]:
                print(f"  {line[:140]}")
        except Exception as exc:
            print(f"  [ERR] reading log: {exc}")

    # 6. governor disabled set
    print("\n[6] governor state (heuristic — last log lines)")
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            gov_lines = [
                l for l in lines[-500:]
                if "Governor" in l and ("DISABLED" in l or "RE-ENABLED" in l)
            ]
            if gov_lines:
                for line in gov_lines[-5:]:
                    print(f"  {line[:140]}")
            else:
                print("  no governor activity in last 500 log lines (all strategies enabled)")
        except Exception:
            pass

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
