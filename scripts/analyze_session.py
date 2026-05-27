"""Quick session analysis from SQLite."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "live" / "bot.db"

if not DB.exists():
    print(f"DB missing: {DB}")
    raise SystemExit(1)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("Tables:", tables)

if "trades" in tables:
    total = cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    closed = cur.execute("SELECT COUNT(*) FROM trades WHERE status='closed'").fetchone()[0]
    open_n = cur.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
    agg = cur.execute(
        "SELECT SUM(pnl_usd), AVG(pnl_pct), SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) "
        "FROM trades WHERE status='closed'"
    ).fetchone()
    print(f"\nTrades: total={total} closed={closed} open={open_n}")
    print(f"Closed PnL USD: {agg[0]} | avg pnl_pct: {agg[1]} | wins: {agg[2]}")

    print("\n--- Last 25 closed trades ---")
    for r in cur.execute(
        """
        SELECT id, symbol, side, sub_strategy, entry_price, exit_price,
               pnl_usd, pnl_pct, exit_reason, status,
               datetime(entry_time_ms/1000,'unixepoch') AS entry,
               datetime(exit_time_ms/1000,'unixepoch') AS exit
        FROM trades WHERE status='closed' ORDER BY id DESC LIMIT 25
        """
    ):
        print(dict(r))

    print("\n--- Open positions ---")
    for r in cur.execute("SELECT * FROM trades WHERE status='open'"):
        print(dict(r))

    print("\n--- By sub_strategy ---")
    for r in cur.execute(
        """
        SELECT sub_strategy, COUNT(*) AS n,
               SUM(pnl_usd) AS pnl, AVG(pnl_pct) AS avg_pct
        FROM trades WHERE status='closed'
        GROUP BY sub_strategy ORDER BY pnl ASC
        """
    ):
        print(dict(r))

if "portfolio_snapshots" in tables:
    snap = cur.execute(
        "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if snap:
        print("\n--- Latest portfolio snapshot ---")
        print(dict(snap))

conn.close()
