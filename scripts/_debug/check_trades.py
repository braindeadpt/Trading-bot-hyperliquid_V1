import sqlite3
from pathlib import Path

c = sqlite3.connect(Path("data/live/bot.db"))
print("trades total", c.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
print("closed", c.execute("SELECT COUNT(*) FROM trades WHERE status='closed'").fetchone()[0])
print("open", c.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0])
print("pnl sum", c.execute("SELECT SUM(pnl_usd) FROM trades WHERE status='closed'").fetchone())
print("recent", c.execute("SELECT symbol, side, pnl_usd, status FROM trades ORDER BY entry_time DESC LIMIT 10").fetchall())
c.close()
