"""Seed SQLite DB with Binance candle history for a date range.

Idempotent -- uses INSERT OR REPLACE, safe to re-run.

Usage:
    python scripts/seed_db.py --from-date 2025-01-01 --to-date 2025-03-01
    python scripts/seed_db.py --from-date 2025-01-01 --to-date 2025-01-07 --symbols BTC,ETH --timeframes 1m,5m
    python scripts/seed_db.py --from-date 2025-01-01 --to-date 2025-01-07 --db-path data/live/bot.db
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.database import Database, Candle

BINANCE_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h"}
BINANCE_BASE = "https://api.binance.com/api/v3/klines"
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL"]
DEFAULT_TIMEFRAMES = ["1m", "5m", "15m", "1h"]
MAX_PER_REQUEST = 1000

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def _ms(dt: str) -> int:
    return int(datetime.fromisoformat(dt).replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[list]:
    """Fetch one page (max 1000) of Binance klines."""
    url = (
        f"{BINANCE_BASE}"
        f"?symbol={symbol}USDT&interval={interval}"
        f"&startTime={start_ms}&endTime={end_ms}&limit={MAX_PER_REQUEST}"
    )
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def kline_to_candle(k: list, symbol: str) -> Candle:
    return Candle(
        symbol=symbol,
        timestamp_ms=int(k[0]),
        open=float(k[1]),
        high=float(k[2]),
        low=float(k[3]),
        close=float(k[4]),
        volume=float(k[5]),
    )


def download_range(
    symbol: str, interval: str, start_ms: int, end_ms: int
) -> List[Candle]:
    """Download candles for a symbol/interval in paginated chunks."""
    all_candles: List[Candle] = []
    cursor = start_ms
    page = 0

    while cursor < end_ms:
        raw = fetch_klines(symbol, interval, cursor, end_ms)
        if not raw:
            break

        candles = [kline_to_candle(k, symbol) for k in raw]
        all_candles.extend(candles)

        last_ts = raw[-1][0]
        cursor = int(last_ts) + 1
        page += 1

        if len(raw) < MAX_PER_REQUEST:
            break

        time.sleep(0.25)

    return all_candles


def print_summary(db: Database, symbols: Sequence[str], timeframes: Sequence[str]) -> None:
    """Log per-symbol, per-timeframe candle count."""
    print()
    print("=" * 60)
    print(f"{'Symbol':<8} {'TF':<6} {'Candles':<10}")
    print(f"{'-'*8} {'-'*6} {'-'*10}")
    grand = 0
    for sym in symbols:
        for tf in timeframes:
            count = db.count_candles(sym, tf)
            print(f"{sym:<8} {tf:<6} {count:<10}")
            grand += count
    print(f"{'-'*8} {'-'*6} {'-'*10}")
    print(f"{'TOTAL':<8} {'':<6} {grand:<10}")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed DB with Binance candles")
    parser.add_argument("--from-date", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to-date", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="Comma-separated symbols")
    parser.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES), help="Comma-separated timeframes")
    parser.add_argument("--db-path", default="data/live/bot.db", help="Path to SQLite DB")
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
    print()

    db = Database(str(db_path))
    total_rows = 0

    for symbol in symbols:
        for tf in timeframes:
            interval = BINANCE_INTERVALS[tf]
            gap_ms = INTERVAL_MS[tf]
            aligned_start = (from_ms // gap_ms) * gap_ms
            aligned_end = (to_ms // gap_ms) * gap_ms

            print(f"[{symbol}][{tf}] Downloading...", end="", flush=True)
            candles = download_range(symbol, interval, aligned_start, aligned_end)
            if not candles:
                print(" no data")
                continue

            before = db.count_candles(symbol, tf)
            db.save_candles(candles, tf)
            after = db.count_candles(symbol, tf)
            new_count = after - before
            total_rows += len(candles)

            print(f" {len(candles)} downloaded ({new_count} new, {before} existed)")

    print_summary(db, symbols, timeframes)
    db.close()
    print(f"\nDone -- {total_rows} total candles saved to {db_path}")


if __name__ == "__main__":
    main()
