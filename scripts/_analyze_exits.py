"""Analyze exit behavior: winners cut early vs losers held long, by strategy."""
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "live" / "bot.db"
con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row

# Last 60 trades (more sample)
rows = con.execute("""
    SELECT symbol, side, strategy, entry_price, exit_price, entry_time, exit_time,
           pnl_usd, pnl_pct, exit_reason, size, signal_metadata
    FROM trades
    WHERE exit_time IS NOT NULL
    ORDER BY exit_time DESC
    LIMIT 60
""").fetchall()

print(f"=== Last {len(rows)} closed trades ===\n")
print(f"{'exit_dt':<17s} {'sym':<5s} {'side':<5s} {'strategy':<18s} {'entry':>11s} {'exit':>11s} {'pnl$':>8s} {'pnl%':>7s} {'hold_h':>6s} {'exit_reason'}")
print("-" * 130)

by_strategy = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "sum_win": 0.0, "sum_loss": 0.0, "avg_win_hold_h": [], "avg_loss_hold_h": []})
by_reason = defaultdict(lambda: {"n": 0, "sum_pnl": 0.0, "wins": 0, "losses": 0})

for r in rows:
    try:
        et = datetime.fromtimestamp(r["entry_time"] / 1000.0, tz=timezone.utc)
        xt = datetime.fromtimestamp(r["exit_time"] / 1000.0, tz=timezone.utc)
        hold_h = (xt - et).total_seconds() / 3600.0
        exit_dt = xt.strftime("%m-%d %H:%M")
    except Exception:
        hold_h = 0.0
        exit_dt = "?"
    pnl = r["pnl_usd"] or 0.0
    pnl_pct = (r["pnl_pct"] or 0.0) * 100
    side = r["side"] or "?"
    sym = r["symbol"] or "?"
    strat = (r["strategy"] or "?")[:18]
    reason = r["exit_reason"] or "?"
    entry = r["entry_price"] or 0.0
    exit_p = r["exit_price"] or 0.0
    print(f"{exit_dt:<17s} {sym:<5s} {side:<5s} {strat:<18s} {entry:>11.4f} {exit_p:>11.4f} {pnl:>8.3f} {pnl_pct:>6.3f}% {hold_h:>6.2f} {reason}")

    s = by_strategy[r["strategy"] or "?"]
    s["n"] += 1
    if pnl > 0:
        s["wins"] += 1
        s["sum_win"] += pnl
        s["avg_win_hold_h"].append(hold_h)
    elif pnl < 0:
        s["losses"] += 1
        s["sum_loss"] += pnl
        s["avg_loss_hold_h"].append(hold_h)
    r2 = by_reason[reason]
    r2["n"] += 1
    r2["sum_pnl"] += pnl
    if pnl > 0:
        r2["wins"] += 1
    elif pnl < 0:
        r2["losses"] += 1

print("\n=== Per-strategy summary ===\n")
print(f"{'strategy':<22s} {'n':>3s} {'W':>3s} {'L':>3s} {'WR%':>5s} {'avg_win$':>9s} {'avg_loss$':>10s} {'avgW_hold_h':>12s} {'avgL_hold_h':>12s} {'PF':>6s}")
print("-" * 100)
for name, s in sorted(by_strategy.items()):
    wr = 100.0 * s["wins"] / s["n"] if s["n"] else 0.0
    avg_w = s["sum_win"] / s["wins"] if s["wins"] else 0.0
    avg_l = s["sum_loss"] / s["losses"] if s["losses"] else 0.0
    avg_wh = sum(s["avg_win_hold_h"]) / len(s["avg_win_hold_h"]) if s["avg_win_hold_h"] else 0.0
    avg_lh = sum(s["avg_loss_hold_h"]) / len(s["avg_loss_hold_h"]) if s["avg_loss_hold_h"] else 0.0
    pf = s["sum_win"] / abs(s["sum_loss"]) if s["sum_loss"] != 0 else 0.0
    print(f"{name:<22s} {s['n']:>3d} {s['wins']:>3d} {s['losses']:>3d} {wr:>5.1f} {avg_w:>9.3f} {avg_l:>10.3f} {avg_wh:>12.2f} {avg_lh:>12.2f} {pf:>6.2f}")

print("\n=== Per-exit-reason summary ===\n")
print(f"{'exit_reason':<32s} {'n':>3s} {'W':>3s} {'L':>3s} {'sum_pnl$':>10s} {'avg_pnl$':>9s}")
print("-" * 70)
for reason, r2 in sorted(by_reason.items(), key=lambda x: -x[1]["n"]):
    avg = r2["sum_pnl"] / r2["n"] if r2["n"] else 0.0
    print(f"{reason:<32s} {r2['n']:>3d} {r2['wins']:>3d} {r2['losses']:>3d} {r2['sum_pnl']:>10.3f} {avg:>9.3f}")

con.close()
