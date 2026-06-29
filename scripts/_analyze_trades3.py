"""Exit reason and size analysis."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "live" / "bot.db"
cut = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)


def main() -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    print("=== Jun 2026 exit reasons (ex-FE) ===")
    for row in db.execute(
        """
        SELECT exit_reason, COUNT(*) n, SUM(pnl_usd) pnl
        FROM trades
        WHERE exit_time >= ? AND COALESCE(sub_strategy,strategy)!='FundingExtreme'
        GROUP BY exit_reason ORDER BY pnl
        """,
        (cut,),
    ):
        print(f"  {row['exit_reason']}: {row['n']} trades PnL ${row['pnl']:.2f}")

    print("\n=== Avg loss vs avg win (ex-FE, all time) ===")
    r = db.execute(
        """
        SELECT AVG(pnl_usd) FROM trades
        WHERE exit_time IS NOT NULL AND pnl_usd > 0
          AND COALESCE(sub_strategy,strategy)!='FundingExtreme'
        """
    ).fetchone()[0]
    l = db.execute(
        """
        SELECT AVG(pnl_usd) FROM trades
        WHERE exit_time IS NOT NULL AND pnl_usd < 0
          AND COALESCE(sub_strategy,strategy)!='FundingExtreme'
        """
    ).fetchone()[0]
    print(f"  Avg win: ${r:.2f} | Avg loss: ${l:.2f} | R:R ~ {abs(r/l):.2f}")

    print("\n=== Symbol concentration Jun ===")
    for row in db.execute(
        """
        SELECT symbol, COUNT(*) n, SUM(pnl_usd) pnl
        FROM trades WHERE exit_time >= ? AND COALESCE(sub_strategy,strategy)!='FundingExtreme'
        GROUP BY symbol ORDER BY pnl
        """,
        (cut,),
    ):
        print(f"  {row['symbol']}: {row['n']} PnL ${row['pnl']:.2f}")

    print("\n=== Strategies with 0 trades ===")
    enabled = [
        "LeadLag", "LiquidationCatcher", "OrderBookScalper", "FundingMomentum",
        "RangeGrid", "DonchianBreakout", "FundingArbitrage", "SpotPerpCarry",
    ]
    for s in enabled:
        n = db.execute(
            "SELECT COUNT(*) FROM trades WHERE COALESCE(sub_strategy,strategy)=?",
            (s,),
        ).fetchone()[0]
        if n == 0:
            print(f"  {s}: 0 trades")

    print("\n=== Largest wins / losses (ex-FE) ===")
    for row in db.execute(
        """
        SELECT id, symbol, COALESCE(sub_strategy,strategy) s, pnl_usd, exit_reason
        FROM trades WHERE exit_time IS NOT NULL AND COALESCE(sub_strategy,strategy)!='FundingExtreme'
        ORDER BY pnl_usd DESC LIMIT 5
        """
    ):
        print(f"  WIN #{row['id']} {row['symbol']} {row['s']} ${row['pnl_usd']:.2f} {row['exit_reason']}")
    for row in db.execute(
        """
        SELECT id, symbol, COALESCE(sub_strategy,strategy) s, pnl_usd, exit_reason
        FROM trades WHERE exit_time IS NOT NULL AND COALESCE(sub_strategy,strategy)!='FundingExtreme'
        ORDER BY pnl_usd ASC LIMIT 5
        """
    ):
        print(f"  LOSS #{row['id']} {row['symbol']} {row['s']} ${row['pnl_usd']:.2f} {row['exit_reason']}")

    db.close()


if __name__ == "__main__":
    main()
