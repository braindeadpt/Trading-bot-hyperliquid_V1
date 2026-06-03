"""Seed SQLite DB with Binance candle history for a date range.

Thin CLI wrapper around :mod:`src.data.candle_backfill`. Idempotent —
re-running with overlapping ranges is safe (``INSERT OR REPLACE``).

Usage:
    python scripts/seed_db.py --from-date 2025-01-01 --to-date 2025-03-01
    python scripts/seed_db.py --from-date 2025-01-01 --to-date 2025-01-07 --symbols BTC,ETH
    python scripts/seed_db.py --from-date 2025-01-01 --to-date 2025-01-07 --db-path data/live/bot.db
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.candle_backfill import (
    BINANCE_INTERVALS,
    DEFAULT_TIMEFRAMES,
    run_range_backfill,
)
from src.data.database import Database

DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL"]


def _ms(dt: str) -> int:
    return int(datetime.fromisoformat(dt).replace(tzinfo=timezone.utc).timestamp() * 1000)


def print_summary(db: Database, symbols: Sequence[str], timeframes: Sequence[str]) -> None:
    print()
    print("=" * 60)
    print(f"{'Symbol':<8} {'TF':<6} {'Candles':<10}")
    print(f"{'-' * 8} {'-' * 6} {'-' * 10}")
    grand = 0
    for sym in symbols:
        for tf in timeframes:
            count = db.count_candles(sym, tf)
            print(f"{sym:<8} {tf:<6} {count:<10}")
            grand += count
    print(f"{'-' * 8} {'-' * 6} {'-' * 10}")
    print(f"{'TOTAL':<8} {'':<6} {grand:<10}")
    print("=" * 60)


async def _run(
    db: Database,
    symbols: List[str],
    timeframes: List[str],
    from_ms: int,
    to_ms: int,
    timeout_sec: float,
) -> int:
    total = 0

    def _progress(sym: str, tf: str, rows: int) -> None:
        print(f"  [{sym}][{tf}] cumulative={rows}")

    total = await run_range_backfill(
        db=db,
        symbols=symbols,
        start_ms=from_ms,
        end_ms=to_ms,
        timeframes=timeframes,
        timeout_sec=timeout_sec,
        progress_cb=_progress,
    )
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed DB with Binance candles")
    parser.add_argument("--from-date", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to-date", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="Comma-separated symbols")
    parser.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES), help="Comma-separated timeframes")
    parser.add_argument("--db-path", default="data/live/bot.db", help="Path to SQLite DB")
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=120.0,
        help="Total wall-time cap (default 120s)",
    )
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip() in BINANCE_INTERVALS]
    from_ms = _ms(args.from_date)
    to_ms = _ms(args.to_date)

    if not symbols:
        print("No symbols specified.")
        sys.exit(1)
    if not timeframes:
        print("No valid timeframes specified.")
        sys.exit(1)
    if from_ms >= to_ms:
        print("--from-date must be before --to-date.")
        sys.exit(1)

    db_path = Path(args.db_path)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"DB path : {db_path}")
    print(f"Symbols : {', '.join(symbols)}")
    print(f"Range   : {args.from_date} -> {args.to_date}")
    print(f"TFs     : {', '.join(timeframes)}")
    print(f"Cap     : {args.timeout_sec:.0f}s")
    print()

    db = Database(str(db_path))
    try:
        total_rows = asyncio.run(
            _run(db, symbols, timeframes, from_ms, to_ms, args.timeout_sec)
        )
    finally:
        db.close()

    print_summary(db, symbols, timeframes)
    print(f"\nDone -- {total_rows} candles downloaded in range")


if __name__ == "__main__":
    main()
