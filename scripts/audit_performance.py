"""Performance audit — trades DB + hold times + price direction."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "live" / "bot.db"


def main() -> int:
    if not DB.exists():
        print("No database at", DB)
        return 1
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute(
        "SELECT COUNT(*), ROUND(SUM(pnl_usd),2), "
        "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) "
        "FROM trades WHERE status='closed'"
    )
    total = c.fetchone()
    print("=== ALL CLOSED ===")
    print(f"  trades={total[0]}  pnl=${total[1]}  wins={total[2]}")

    c.execute(
        "SELECT COUNT(*), ROUND(SUM(pnl_usd),2), "
        "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) "
        "FROM trades WHERE status='closed' "
        "AND exit_time > (strftime('%s','now') - 7*86400) * 1000"
    )
    w7 = c.fetchone()
    print("\n=== LAST 7 DAYS ===")
    print(f"  trades={w7[0]}  pnl=${w7[1]}  wins={w7[2]}")

    print("\n=== BY SIDE (7d) ===")
    for row in c.execute(
        "SELECT side, COUNT(*) n, ROUND(SUM(pnl_usd),2) t, "
        "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) w "
        "FROM trades WHERE status='closed' "
        "AND exit_time > (strftime('%s','now') - 7*86400) * 1000 "
        "GROUP BY side"
    ):
        print(f"  {row['side']}: n={row['n']} pnl=${row['t']} wins={row['w']}")

    print("\n=== BY STRATEGY (7d) ===")
    for row in c.execute(
        "SELECT COALESCE(sub_strategy,'?') s, COUNT(*) n, "
        "ROUND(SUM(pnl_usd),2) t, "
        "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) w "
        "FROM trades WHERE status='closed' "
        "AND exit_time > (strftime('%s','now') - 7*86400) * 1000 "
        "GROUP BY sub_strategy ORDER BY t"
    ):
        print(f"  {row['s']}: n={row['n']} pnl=${row['t']} wins={row['w']}")

    print("\n=== BY EXIT REASON (7d) ===")
    for row in c.execute(
        "SELECT exit_reason, COUNT(*) n, ROUND(AVG(pnl_pct)*100,3) avg_pct "
        "FROM trades WHERE status='closed' "
        "AND exit_time > (strftime('%s','now') - 7*86400) * 1000 "
        "GROUP BY exit_reason ORDER BY n DESC"
    ):
        print(f"  {row['exit_reason']}: n={row['n']} avg%={row['avg_pct']}")

    print("\n=== LAST 20 TRADES (price move vs side) ===")
    for row in c.execute(
        "SELECT id, symbol, side, COALESCE(sub_strategy,'?') strat, "
        "entry_price ep, exit_price xp, "
        "ROUND((exit_time-entry_time)/60000.0,1) hold_min, "
        "ROUND(pnl_usd,2) pnl, ROUND(pnl_pct*100,3) pct, exit_reason, "
        "datetime(entry_time/1000,'unixepoch') et "
        "FROM trades WHERE status='closed' ORDER BY id DESC LIMIT 20"
    ):
        ep, xp = row["ep"], row["xp"]
        move = (xp - ep) / ep * 100 if ep else 0
        favorable = (row["side"] == "long" and move > 0) or (
            row["side"] == "short" and move < 0
        )
        print(
            f"  #{row['id']} {row['symbol']} {row['side']} {row['strat']} "
            f"move={move:+.3f}% favorable={favorable} pnl=${row['pnl']} "
            f"({row['pct']}%) {row['exit_reason']} hold={row['hold_min']}m "
            f"{row['et']}"
        )

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
