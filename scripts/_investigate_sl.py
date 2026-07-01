"""Investigate recent short positions closed at SL after being in profit."""
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

DB = Path(__file__).resolve().parents[1] / "data" / "live" / "bot.db"
con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row

rows = con.execute("""
    SELECT id, symbol, side, entry_price, exit_price, entry_time, exit_time,
           pnl_usd, pnl_pct, strategy, exit_reason, signal_metadata, size
    FROM trades
    WHERE exit_time IS NOT NULL
    ORDER BY exit_time DESC
    LIMIT 20
""").fetchall()

print("=== Last 20 closed trades ===\n")
for r in rows:
    et = datetime.fromtimestamp(r["entry_time"]/1000, tz=timezone.utc)
    xt = datetime.fromtimestamp(r["exit_time"]/1000, tz=timezone.utc)
    hold_h = (xt - et).total_seconds() / 3600
    meta = r["signal_metadata"] or ""
    print(f"#{r['id']} {xt.strftime('%m-%d %H:%M')} {r['symbol']:5s} {r['side']:5s} "
          f"strat={r['strategy'][:18]:18s} entry={r['entry_price']:.2f} exit={r['exit_price']:.2f} "
          f"pnl=${r['pnl_usd']:.3f} ({r['pnl_pct']*100:.2f}%) hold={hold_h:.1f}h "
          f"reason={r['exit_reason']}")
    if meta:
        try:
            m = json.loads(meta) if isinstance(meta, str) else meta
            if isinstance(m, dict) and len(str(m)) < 200:
                print(f"    meta: {m}")
        except Exception:
            pass

# Find shorts around BTC 57700
print("\n=== BTC shorts (recent) ===\n")
btc = con.execute("""
    SELECT * FROM trades
    WHERE symbol='BTC' AND side='short' AND exit_time IS NOT NULL
    ORDER BY exit_time DESC LIMIT 10
""").fetchall()
for r in btc:
    xt = datetime.fromtimestamp(r["exit_time"]/1000, tz=timezone.utc)
    print(f"  {xt.strftime('%Y-%m-%d %H:%M')} entry={r['entry_price']:.1f} exit={r['exit_price']:.1f} "
          f"pnl=${r['pnl_usd']:.3f} reason={r['exit_reason']} strat={r['strategy']}")

con.close()
