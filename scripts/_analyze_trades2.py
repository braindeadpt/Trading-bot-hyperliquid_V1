"""Quick supplemental trade stats."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "live" / "bot.db"


def ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    cols = [r[1] for r in db.execute("PRAGMA table_info(trades)")]
    print("trades columns:", ", ".join(cols))

    r = db.execute(
        """
        SELECT COUNT(*), SUM(pnl_usd), SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)
        FROM trades WHERE exit_time IS NOT NULL
        """
    ).fetchone()
    print(f"\nALL TIME: {r[0]} trades | PnL ${r[1] or 0:.2f} | WR {100*r[2]/r[0]:.0f}%")

    fe = db.execute(
        """
        SELECT COUNT(*), SUM(pnl_usd) FROM trades
        WHERE exit_time IS NOT NULL
          AND (sub_strategy = 'FundingExtreme' OR strategy = 'FundingExtreme')
        """
    ).fetchone()
    ex = db.execute(
        """
        SELECT COUNT(*), SUM(pnl_usd), SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)
        FROM trades WHERE exit_time IS NOT NULL
          AND COALESCE(sub_strategy, strategy) != 'FundingExtreme'
        """
    ).fetchone()
    print(f"FundingExtreme: {fe[0]} trades PnL ${fe[1] or 0:.2f}")
    print(
        f"Sem FundingExtreme: {ex[0]} trades PnL ${ex[1] or 0:.2f} "
        f"WR {100*ex[2]/ex[0]:.0f}%"
    )

    cut = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    r2 = db.execute(
        """
        SELECT COUNT(*), SUM(pnl_usd), SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)
        FROM trades WHERE exit_time >= ?
        """,
        (cut,),
    ).fetchone()
    print(
        f"\nDesde Jun 2026: {r2[0]} trades PnL ${r2[1] or 0:.2f} "
        f"WR {100*r2[2]/r2[0]:.0f}%"
    )

    print("\nPnL por dia (ultimos 14 dias com trades):")
    for row in db.execute(
        """
        SELECT date(exit_time/1000, 'unixepoch') AS d,
               COUNT(*) AS n, SUM(pnl_usd) AS pnl,
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS w
        FROM trades WHERE exit_time IS NOT NULL
        GROUP BY d ORDER BY d DESC LIMIT 14
        """
    ):
        wr = 100 * row["w"] / row["n"] if row["n"] else 0
        print(f"  {row['d']}: {row['n']} trades PnL ${row['pnl']:.2f} WR {wr:.0f}%")

    print("\nFundingExtreme por dia:")
    for row in db.execute(
        """
        SELECT date(exit_time/1000, 'unixepoch') AS d, COUNT(*) AS n, SUM(pnl_usd) AS pnl
        FROM trades
        WHERE exit_time IS NOT NULL
          AND (sub_strategy = 'FundingExtreme' OR strategy = 'FundingExtreme')
        GROUP BY d ORDER BY d
        """
    ):
        print(f"  {row['d']}: {row['n']} trades PnL ${row['pnl']:.2f}")

    print("\nUltimos 30 trades (sem FE):")
    for row in db.execute(
        """
        SELECT id, symbol, side, COALESCE(sub_strategy, strategy) AS strat,
               pnl_usd, exit_time, exit_reason
        FROM trades
        WHERE exit_time IS NOT NULL
          AND COALESCE(sub_strategy, strategy) != 'FundingExtreme'
        ORDER BY exit_time DESC LIMIT 30
        """
    ):
        hold = ""
        print(
            f"  #{row['id']} {ts(row['exit_time'])} {row['symbol']} {row['side']} "
            f"{row['strat']} ${row['pnl_usd']:.2f} {row['exit_reason']}"
        )

    db.close()


if __name__ == "__main__":
    main()
