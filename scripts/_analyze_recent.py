"""Analyze trades since yesterday — entries, exits, PnL, SL-to-BE behavior."""
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

DB = Path(__file__).resolve().parents[1] / "data" / "live" / "bot.db"
con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row

# Since yesterday 00:00 UTC (Jul 1 2026 if today is Jul 2)
now = datetime.now(timezone.utc)
yesterday = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
since_ms = int(yesterday.timestamp() * 1000)

rows = con.execute("""
    SELECT id, symbol, side, entry_price, exit_price, entry_time, exit_time,
           pnl_usd, pnl_pct, strategy, exit_reason, size, signal_metadata
    FROM trades
    WHERE exit_time IS NOT NULL AND exit_time >= ?
    ORDER BY exit_time ASC
""", (since_ms,)).fetchall()

print(f"=== Trades since {yesterday.strftime('%Y-%m-%d %H:%M')} UTC ({len(rows)} closed) ===\n")

by_strat = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "wins": [], "losses": [], "reasons": defaultdict(int)})
by_reason = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0, "l": 0})
total_pnl = 0.0

print(f"{'exit_dt':<17s} {'sym':<5s} {'side':<5s} {'strategy':<18s} {'entry':>10s} {'exit':>10s} "
      f"{'pnl$':>8s} {'pnl%':>7s} {'hold_h':>6s} {'exit_reason'}")
print("-" * 120)

for r in rows:
    et = datetime.fromtimestamp(r["entry_time"]/1000, tz=timezone.utc)
    xt = datetime.fromtimestamp(r["exit_time"]/1000, tz=timezone.utc)
    hold_h = (xt - et).total_seconds() / 3600
    pnl = r["pnl_usd"] or 0.0
    pnl_pct = (r["pnl_pct"] or 0) * 100
    strat = (r["strategy"] or "?")[:18]
    reason = r["exit_reason"] or "?"
    total_pnl += pnl

    print(f"{xt.strftime('%m-%d %H:%M'):<17s} {r['symbol']:<5s} {r['side']:<5s} {strat:<18s} "
          f"{r['entry_price']:>10.2f} {r['exit_price']:>10.2f} {pnl:>8.3f} {pnl_pct:>6.2f}% "
          f"{hold_h:>6.1f} {reason}")

    s = by_strat[r["strategy"] or "?"]
    s["n"] += 1
    s["pnl"] += pnl
    s["reasons"][reason] += 1
    if pnl > 0:
        s["w"] += 1
        s["wins"].append(pnl)
    elif pnl < 0:
        s["l"] += 1
        s["losses"].append(pnl)

    br = by_reason[reason]
    br["n"] += 1
    br["pnl"] += pnl
    if pnl > 0: br["w"] += 1
    elif pnl < 0: br["l"] += 1

print(f"\n=== TOTAL PnL since yesterday: ${total_pnl:.3f} ===\n")

print("=== Per strategy ===")
print(f"{'strategy':<22s} {'n':>3s} {'W':>3s} {'L':>3s} {'WR%':>5s} {'sum_pnl':>9s} {'avg_win':>8s} {'avg_loss':>9s} {'PF':>6s}")
print("-" * 80)
for name, s in sorted(by_strat.items(), key=lambda x: -x[1]["pnl"]):
    wr = 100 * s["w"] / s["n"] if s["n"] else 0
    avg_w = sum(s["wins"]) / len(s["wins"]) if s["wins"] else 0
    avg_l = sum(s["losses"]) / len(s["losses"]) if s["losses"] else 0
    pf = sum(s["wins"]) / abs(sum(s["losses"])) if s["losses"] else float("inf")
    print(f"{name:<22s} {s['n']:>3d} {s['w']:>3d} {s['l']:>3d} {wr:>5.1f} {s['pnl']:>9.3f} "
          f"{avg_w:>8.3f} {avg_l:>9.3f} {pf:>6.2f}")
    for reason, cnt in sorted(s["reasons"].items(), key=lambda x: -x[1]):
        print(f"    {reason}: {cnt}")

print("\n=== Per exit reason ===")
print(f"{'reason':<35s} {'n':>3s} {'W':>3s} {'L':>3s} {'sum_pnl':>9s}")
print("-" * 60)
for reason, br in sorted(by_reason.items(), key=lambda x: -x[1]["n"]):
    print(f"{reason:<35s} {br['n']:>3d} {br['w']:>3d} {br['l']:>3d} {br['pnl']:>9.3f}")

# Open positions
open_pos = con.execute("""
    SELECT symbol, side, entry_price, entry_time, strategy, size
    FROM trades WHERE status='open' OR exit_time IS NULL
    ORDER BY entry_time DESC
""").fetchall()
# might not have open in trades table - check positions table
try:
    open_pos2 = con.execute("SELECT * FROM positions").fetchall()
    if open_pos2:
        print("\n=== Open positions ===")
        for p in open_pos2:
            print(dict(p))
except Exception:
    pass

# Compare pre/post v3.1.41 (approx Jul 1 02:50 UTC when user applied changes - use Jul 1 03:00 as cutoff)
cutoff = int(datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc).timestamp() * 1000)
post = [r for r in rows if r["exit_time"] >= cutoff]
pre = [r for r in rows if r["exit_time"] < cutoff]

def stats(trade_list):
    if not trade_list:
        return {"n": 0, "pnl": 0, "w": 0, "l": 0}
    pnl = sum(r["pnl_usd"] or 0 for r in trade_list)
    w = sum(1 for r in trade_list if (r["pnl_usd"] or 0) > 0)
    l = sum(1 for r in trade_list if (r["pnl_usd"] or 0) < 0)
    return {"n": len(trade_list), "pnl": pnl, "w": w, "l": l}

pre_s = stats(pre)
post_s = stats(post)
print(f"\n=== Pre v3.1.41 (~before 03:00 UTC Jul 1): n={pre_s['n']} W={pre_s['w']} L={pre_s['l']} pnl=${pre_s['pnl']:.3f}")
print(f"=== Post v3.1.41 (~after 03:00 UTC Jul 1):  n={post_s['n']} W={post_s['w']} L={post_s['l']} pnl=${post_s['pnl']:.3f}")

con.close()
