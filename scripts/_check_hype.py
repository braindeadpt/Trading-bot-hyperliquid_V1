"""HYPE live diagnostics."""
from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "live" / "bot.db"


def main() -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    print("=== HYPE trades ===")
    print("total:", db.execute("SELECT COUNT(*) FROM trades WHERE symbol='HYPE'").fetchone()[0])

    print("\n=== HYPE decision_audit ===")
    for r in db.execute(
        """
        SELECT result, decision_type, COUNT(*) n FROM decision_audit
        WHERE symbol='HYPE' GROUP BY result, decision_type ORDER BY n DESC
        """
    ):
        print(dict(r))

    print("\n=== Top HYPE rejections ===")
    c = Counter()
    for r in db.execute(
        "SELECT decision_type, reason FROM decision_audit WHERE symbol='HYPE' AND result='rejected'"
    ):
        c[(r["decision_type"], (r["reason"] or "")[:70])] += 1
    for (dt, reason), n in c.most_common(12):
        print(f"  {n:4d} [{dt}] {reason}")

    print("\n=== HYPE candles ===")
    for tf in ["candles_1m", "candles_15m", "candles_1h"]:
        r = db.execute(
            f"SELECT COUNT(*), MIN(timestamp_ms), MAX(timestamp_ms) FROM {tf} WHERE symbol='HYPE'"
        ).fetchone()
        print(f"  {tf}: {r[0]} rows")

    db.close()


if __name__ == "__main__":
    main()
