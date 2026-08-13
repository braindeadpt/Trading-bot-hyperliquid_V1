"""Pre-start feed-delivery check.

Before starting the bot, verify that every feed contracted by THIS
deployment has recent delivery evidence — fail early instead of letting a
blocked feed sit silent until the 6h/1h/12h watchdog threshold trips (the
2026-06-29 fstream outage lesson).

Sources of evidence (persisted artifacts, so the check works with the bot
stopped):

  * liquidation_okx / liquidation_bybit / liquidation_binance
      -> liquidation_events (max timestamp_ms per source)
  * funding_hl / funding_cex
      -> funding_history (max timestamp)
  * taker_split
      -> candles_1m (max timestamp_ms where buy/sell volume > 0)
  * binance_perp
      -> binance_perp_prices (max timestamp_ms)
  * l2_book_recording
      -> newest file mtime under data/research/l2_books/
  * liquidation_coinalyze_check
      -> verify-only venue: no persisted evidence; reported but never gated.

Exit codes:
  0  all contracted feeds have fresh evidence
  1  at least one feed aged past its silence threshold (or missing entirely)
  2  at least one feed aged past --warn-fraction of its threshold
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.engine import feed_silence_contracts  # noqa: E402
from src.utils.config import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "live" / "bot.db"
L2_BOOKS_DIR = ROOT / "data" / "research" / "l2_books"


def _db_latest(db: sqlite3.Connection, table: str, col: str,
               where: str = "") -> int:
    q = f"SELECT MAX({col}) FROM {table}"
    if where:
        q += f" WHERE {where}"
    row = db.execute(q).fetchone()
    return int(row[0]) if row and row[0] else 0


def _l2_books_mtime(l2_dir: Path) -> int:
    newest = 0
    if l2_dir.exists():
        for p in l2_dir.rglob("*"):
            if p.is_file():
                newest = max(newest, int(p.stat().st_mtime * 1000))
    return newest


def collect_evidence(db: sqlite3.Connection, *, l2_dir: Path = L2_BOOKS_DIR) -> dict:
    """Latest delivery timestamp per feed key (ms). Absent keys = no evidence."""
    ev: dict = {}
    ev["liquidation_okx"] = _db_latest(db, "liquidation_events", "timestamp_ms",
                                       "source='okx'")
    ev["liquidation_bybit"] = _db_latest(db, "liquidation_events", "timestamp_ms",
                                         "source='bybit'")
    ev["liquidation_binance"] = _db_latest(db, "liquidation_events", "timestamp_ms",
                                           "source='binance'")
    ev["funding_hl"] = _db_latest(db, "funding_history", "timestamp")
    ev["funding_cex"] = _db_latest(db, "funding_history", "timestamp")
    ev["taker_split"] = _db_latest(
        db, "candles_1m", "timestamp_ms",
        "(buy_volume > 0 OR sell_volume > 0)",
    )
    ev["binance_perp"] = _db_latest(db, "binance_perp_prices", "timestamp_ms")
    ev["l2_book_recording"] = _l2_books_mtime(l2_dir)
    # coinalyze_check: verify-only, no persisted evidence -> key absent
    return ev


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="bot.db path (default: data/live/bot.db)")
    parser.add_argument("--config", default=str(ROOT / "config" / "settings.yaml"),
                        help="settings.yaml path")
    parser.add_argument(
        "--l2-dir",
        default=str(L2_BOOKS_DIR),
        help="L2 book recording directory (default: data/research/l2_books)",
    )
    parser.add_argument("--warn-fraction", type=float, default=0.5,
                        help="warn when age exceeds this fraction of threshold")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument("--gate-coinalyze", action="store_true",
                        help="fail if coinalyze has no evidence (default: skipped, verify-only)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    contracts = feed_silence_contracts(cfg)  # same decision the engine makes

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: bot DB not found: {db_path}", file=sys.stderr)
        return 1
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    now_ms = int(time.time() * 1000)
    l2_dir = Path(args.l2_dir)
    evidence = collect_evidence(db, l2_dir=l2_dir)
    db.close()

    report: dict = {"now_ms": now_ms, "feeds": {}}
    failures = 0
    warnings = 0

    for feed, max_sec in sorted(contracts.items()):
        latest = evidence.get(feed, 0)
        age_sec = None
        status = "ok"
        if latest == 0:
            if feed == "liquidation_coinalyze_check" and not args.gate_coinalyze:
                # Verify-only venue: never persisted, never blocks. Reported
                # so operators still see it in the panel.
                status = "skipped"
            else:
                status = "fail"
                failures += 1
        else:
            age_sec = max(0.0, (now_ms - latest) / 1000.0)
            if age_sec >= max_sec:
                status = "fail"
                failures += 1
            elif age_sec >= max_sec * args.warn_fraction:
                status = "warn"
                warnings += 1
        report["feeds"][feed] = {
            "max_silence_sec": max_sec,
            "age_sec": None if age_sec is None else round(age_sec, 1),
            "latest_ms": latest or None,
            "pct_of_threshold": (
                None if age_sec is None else round(age_sec / max_sec * 100, 1)
            ),
            "status": status,
        }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for feed, st in report["feeds"].items():
            age = st["age_sec"]
            age_txt = (f"{age:.0f}s" if age is not None and age < 60 else
                       f"{age / 60:.1f}m" if age is not None and age < 3600 else
                       f"{age / 3600:.1f}h" if age is not None else "no evidence")
            th = st["max_silence_sec"]
            th_txt = (f"{th:.0f}s" if th < 60 else
                      f"{th / 60:.0f}m" if th < 3600 else f"{th / 3600:.1f}h")
            print(f"{feed:32s} age={age_txt:>10s} "
                  f"threshold={th_txt:>6s} "
                  f"pct={st['pct_of_threshold']}%  {st['status'].upper()}")

    if failures:
        print(f"\n[FAIL] {failures} contracted feed(s) not delivering — check "
              f"before starting the bot (silence would only degrade later).",
              file=sys.stderr if not args.json else sys.stdout)
        return 1
    if warnings:
        print(f"\n[WARN] {warnings} feed(s) past {args.warn_fraction * 100:.0f}% "
              "of threshold — delivery slowing?", file=sys.stderr)
        return 2
    if not args.json:
        print("\n[PASS] all contracted feeds have fresh delivery evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
