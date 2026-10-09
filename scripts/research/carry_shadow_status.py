#!/usr/bin/env python3
"""Read-only status dump for the carry-shadow daemon ledger.

Answers the startup checks without touching the daemon or the bot:
  heartbeat age, restart counter, active subs, per-coin last-book/trade
  counts, book_missing breakdown, open episodes, kill flag.

Usage: python -m scripts.research.carry_shadow_status [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research.carry_shadow import spec  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=spec.DB_PATH)
    args = ap.parse_args()
    db = Path(args.db)
    if not db.exists():
        print(f"no ledger at {db} — daemon has not booted")
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3.0)
    meta = dict(con.execute("SELECT key, value FROM meta"))
    now_ms = int(time.time() * 1000)

    hb = int(float(meta.get("heartbeat_ms") or 0))
    age = (now_ms - hb) / 1000.0 if hb else None
    print(f"heartbeat_ms : {hb} ({f'{age:.0f}s ago' if age is not None else 'never'})"
          f"  [{'RED' if (age is None or age > spec.HEARTBEAT_STALE_S) else 'green'}]")
    print(f"restart_count: {meta.get('restart_count', '0')}   "
          f"subs_active  : {meta.get('subs_active', '0')}   "
          f"dead         : {meta.get('dead') or '-'}")
    print(f"book_missing total: {meta.get('book_missing_count', '0')}")

    cands = json.loads(meta.get("candidates") or "[]")
    print(f"\ncandidates ({len(cands)}): {', '.join(cands)}")

    book = {k[10:]: int(v) for k, v in meta.items() if k.startswith("book_seen:")}
    trd = {k[7:]: int(v) for k, v in meta.items() if k.startswith("trades:")}
    miss = Counter()
    for (blob,) in con.execute(
            "SELECT data FROM events WHERE kind='book_missing'"):
        miss[json.loads(blob)["pair"]] += 1
    print("\nper-coin liveness:")
    for coin in sorted(set(book) | set(trd) | set(miss)):
        b_age = (now_ms - book[coin]) / 1000.0 if coin in book else None
        print(f"  {coin:14s} book:{('%6.0fs' % b_age) if b_age is not None else '   none':>8s}"
              f"  trades:{trd.get(coin, 0):>7d}  book_missing:{miss.get(coin, 0)}")

    eps = con.execute(
        "SELECT pair, state, close_reason, gap_unverified, cost_bps"
        " FROM episodes ORDER BY entry_decision_ms").fetchall()
    print(f"\nepisodes ({len(eps)}):")
    for p, st, cr, gu, cb in eps:
        print(f"  {p:14s} {st:14s} {cr or '-':18s} gap_unverified={gu} cost={cb:.1f}bps")
    n, rate = (0, 0.0)
    row = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(filled),0) FROM fill_stats").fetchone()
    if row and row[0]:
        n, rate = row[0], row[1] / row[0]
    prow = con.execute(
        "SELECT COALESCE(SUM(proxy_filled),0), COUNT(*) FROM fill_stats").fetchone()
    print(f"\nfill strict: {int(row[1])}/{n} ({rate:.0%})   "
          f"proxy (context): {int(prow[0])}/{prow[1]}")
    con.close()


if __name__ == "__main__":
    main()
