"""Deep dive May 30 FundingExtreme cluster."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "live" / "bot.db"


def ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """
        SELECT * FROM trades
        WHERE sub_strategy = 'FundingExtreme' OR strategy = 'FundingExtreme'
        ORDER BY entry_time
        """
    ).fetchall()
    print(f"FundingExtreme trades: {len(rows)}")
    holds = []
    for r in rows:
        hold = (r["exit_time"] - r["entry_time"]) / 60000.0
        holds.append(hold)
    print(f"Avg hold: {sum(holds)/len(holds):.1f} min | min {min(holds):.1f} max {max(holds):.1f}")
    print(f"Symbols: {set(r['symbol'] for r in rows)}")
    print(f"Sides: long={sum(1 for r in rows if r['side']=='long')} short={sum(1 for r in rows if r['side']=='short')}")
    print(f"Total fees: ${sum(r['entry_fee'] or 0 for r in rows):.2f}")
    print(f"Total funding_paid: ${sum(r['funding_paid'] or 0 for r in rows):.2f}")
    print("\nFirst 8 trades:")
    for r in rows[:8]:
        hold = (r["exit_time"] - r["entry_time"]) / 60000.0
        print(
            f"  #{r['id']} {ts(r['entry_time'])}->{ts(r['exit_time'])} "
            f"{r['symbol']} {r['side']} entry={r['entry_price']:.2f} "
            f"pnl=${r['pnl_usd']:.2f} hold={hold:.1f}m "
            f"funding={r['entry_funding']} pred={r['entry_predicted_funding']}"
        )

    # Non-FE since June breakdown
    print("\n=== Jun 2026 ex-FE by strategy ===")
    cut = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    for row in db.execute(
        """
        SELECT COALESCE(sub_strategy, strategy) AS s, COUNT(*) n, SUM(pnl_usd) pnl,
               SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) w
        FROM trades WHERE exit_time >= ? AND COALESCE(sub_strategy,strategy)!='FundingExtreme'
        GROUP BY s ORDER BY pnl
        """,
        (cut,),
    ):
        print(f"  {row['s']}: n={row['n']} PnL=${row['pnl']:.2f} WR={100*row['w']/row['n']:.0f}%")

    print("\n=== Fees all trades ===")
    r = db.execute(
        "SELECT SUM(entry_fee), SUM(funding_paid) FROM trades WHERE exit_time IS NOT NULL"
    ).fetchone()
    print(f"  entry_fees: ${r[0] or 0:.2f} | funding_paid: ${r[1] or 0:.2f}")

    # Ensemble vs individual
    print("\n=== StrategyEnsemble detail ===")
    for row in db.execute(
        """
        SELECT COUNT(*), SUM(pnl_usd), SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END)
        FROM trades WHERE COALESCE(sub_strategy,strategy)='StrategyEnsemble' AND exit_time IS NOT NULL
        """
    ):
        pass
    r = db.execute(
        """
        SELECT COUNT(*), SUM(pnl_usd), SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END)
        FROM trades WHERE COALESCE(sub_strategy,strategy)='StrategyEnsemble' AND exit_time IS NOT NULL
        """
    ).fetchone()
    print(f"  All-time: {r[0]} trades PnL ${r[1] or 0:.2f} WR {100*r[2]/r[0]:.0f}%")

    db.close()


if __name__ == "__main__":
    main()
