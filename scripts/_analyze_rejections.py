"""Decision audit deep dive."""
from __future__ import annotations
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "live" / "bot.db"


def main() -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    print("=== Top rejection reasons (truncated to 60 chars) ===")
    cnt = Counter()
    for r in db.execute(
        "SELECT decision_type, reason FROM decision_audit WHERE result='rejected'"
    ):
        key = (r["decision_type"], (r["reason"] or "")[:60])
        cnt[key] += 1
    for (dt, reason), n in cnt.most_common(25):
        print(f"  {n:5d}  [{dt}] {reason}")

    print("\n=== By symbol (rejected) ===")
    for r in db.execute(
        "SELECT symbol, COUNT(*) n FROM decision_audit WHERE result='rejected' GROUP BY symbol ORDER BY n DESC LIMIT 10"
    ):
        print(f"  {r['symbol']}: {r['n']}")

    print("\n=== Accepted vs rejected by type ===")
    for r in db.execute(
        "SELECT decision_type, result, COUNT(*) n FROM decision_audit GROUP BY decision_type, result ORDER BY decision_type, result"
    ):
        print(f"  {r['decision_type']:15s} {r['result']:10s} {r['n']}")

    print("\n=== Last 10 rejected correlation ===")
    for r in db.execute(
        "SELECT timestamp, symbol, strategy, reason FROM decision_audit WHERE decision_type='correlation' AND result='rejected' ORDER BY timestamp DESC LIMIT 10"
    ):
        print(f"  {r['timestamp']} {r['symbol']} {r['strategy']} {(r['reason'] or '')[:80]}")

    print("\n=== Last 10 rejected risk ===")
    for r in db.execute(
        "SELECT timestamp, symbol, strategy, reason FROM decision_audit WHERE decision_type='risk' AND result='rejected' ORDER BY timestamp DESC LIMIT 10"
    ):
        print(f"  {r['timestamp']} {r['symbol']} {r['strategy']} {(r['reason'] or '')[:80]}")

    db.close()


if __name__ == "__main__":
    main()
