"""Backfill candle history from Binance public data dumps (data.binance.vision).

Bulk zipped CSVs — no rate limit, no auth, one request per month instead
of one per 1000 bars. Use for multi-month backfills; the REST path
(``backfill_candles.py``) stays better for the trailing few days.

Usage:
    python scripts/ops/backfill_candles_vision.py --days 90
    python scripts/ops/backfill_candles_vision.py --symbols BTC,ETH --start 2026-05-01 --end 2026-09-01 --timeframes 15m,1h
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.data.binance_vision import backfill_from_vision
from src.data.database import Database
from src.utils.config import load_config


def _ms(s: str, end: bool = False) -> int:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill candles from Binance public data dumps (vision)")
    parser.add_argument("--db-path", default="", help="Path to bot SQLite DB")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols")
    parser.add_argument("--start", default="", help="YYYY-MM-DD")
    parser.add_argument("--end", default="", help="YYYY-MM-DD (default: now)")
    parser.add_argument("--days", type=int, default=0,
                        help="Days back from --end/now (ignored if --start given)")
    parser.add_argument("--timeframes", default="1m,5m,15m,1h")
    args = parser.parse_args()

    cfg = load_config(ROOT / "config" / "settings.yaml")
    db_path = Path(args.db_path or cfg.get("database.path", "data/live/bot.db"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    symbols = [
        s.strip().upper()
        for s in (args.symbols or ",".join(cfg.get("assets", ["BTC", "ETH", "SOL"]))).split(",")
        if s.strip()
    ]
    end_ms = _ms(args.end, end=True) if args.end else int(time.time() * 1000)
    start_ms = _ms(args.start) if args.start else end_ms - (args.days or 90) * 86_400_000
    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    db = Database(str(db_path))
    print(f"Vision backfill {symbols} {tfs} "
          f"{datetime.fromtimestamp(start_ms/1000, tz=timezone.utc):%Y-%m-%d}.."
          f"{datetime.fromtimestamp(end_ms/1000, tz=timezone.utc):%Y-%m-%d}")
    result = asyncio.run(backfill_from_vision(
        db, symbols, start_ms, end_ms, timeframes=tfs))
    db.close()
    print(f"\nWritten: {result['written']}")
    if result["missing"]:
        print(f"Missing archives: {result['missing']}")
    print(f"Total rows: {result['total_rows']}")


if __name__ == "__main__":
    main()
