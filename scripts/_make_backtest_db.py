"""One-off: create a snapshot copy of bot.db for backtest sweeps.

Copies the live DB to bot_backtest.db so sweeps don't contend with the
running paper bot for DB writes.
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
src = ROOT / "data" / "live" / "bot.db"
dst = ROOT / "data" / "live" / "bot_backtest.db"

# Remove old backup
for suffix in ("", "-wal", "-shm"):
    p = Path(str(dst) + suffix)
    if p.exists():
        p.unlink()

# WAL checkpoint + backup
con = sqlite3.connect(str(src))
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
bcon = sqlite3.connect(str(dst))
con.backup(bcon)
bcon.close()
con.close()

print(f"Backup OK: {dst} ({dst.stat().st_size} bytes)")
