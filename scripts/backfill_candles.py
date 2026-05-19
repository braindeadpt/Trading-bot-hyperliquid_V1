"""Backfill candle history from Binance into the bot's SQLite database.

Usage:
    python scripts/backfill_candles.py
    python scripts/backfill_candles.py --symbols BTC,ETH,SOL --days 7
    python scripts/backfill_candles.py --db-path data/live/bot.db

This populates candles_1m, candles_5m, candles_15m, candles_1h tables
so strategies (especially TrendFollow/SmartMoneyFlow) can warm up
instantly on next bot restart.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.database import Database, Candle as DBCandle


def fetch_binance_klines(symbol: str, interval: str, limit: int = 1000):
    """Fetch klines from Binance REST API (no external deps needed)."""
    import urllib.request
    import json
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data


def kline_to_candle(k, symbol: str) -> DBCandle:
    """Convert a Binance kline row to our Candle dataclass."""
    return DBCandle(
        symbol=symbol,
        timestamp_ms=k[0],
        open=float(k[1]),
        high=float(k[2]),
        low=float(k[3]),
        close=float(k[4]),
        volume=float(k[5]),
        funding_rate=None,
        oi_total=None,
        oi_delta=None,
    )


TIMEFRAMES = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
}

BINANCE_INTERVALS = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill candle history from Binance")
    parser.add_argument("--db-path", default="data/live/bot.db", help="Path to bot SQLite DB")
    parser.add_argument("--symbols", default="BTC,ETH,SOL", help="Comma-separated symbols")
    parser.add_argument("--days", type=int, default=7, help="Days of history to fetch")
    parser.add_argument("--timeframes", default="1m,5m,15m,1h", help="Timeframes to backfill")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    timeframes = [t.strip() for t in args.timeframes.split(",")]
    limit_per_tf = {
        "1m": min(args.days * 24 * 60, 1000),
        "5m": min(args.days * 24 * 12, 1000),
        "15m": min(args.days * 24 * 4, 1000),
        "1h": min(args.days * 24, 1000),
    }

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"DB not found at {db_path}. Starting fresh — it will be created.")
    db = Database(str(db_path))

    total = 0
    for symbol in symbols:
        binance_symbol = symbol.replace("SOL", "SOL")
        for tf in timeframes:
            if tf not in limit_per_tf:
                continue
            limit = limit_per_tf[tf]
            print(f"Fetching {limit} {tf} candles for {symbol}... ", end="", flush=True)
            try:
                raw = fetch_binance_klines(binance_symbol, BINANCE_INTERVALS[tf], limit)
                candles = [kline_to_candle(k, symbol) for k in raw]
                db.save_candles(candles, tf)
                print(f"{len(candles)} saved")
                total += len(candles)
            except Exception as e:
                print(f"FAILED: {e}")
            time.sleep(0.3)  # rate limit

    print(f"\nDone! {total} total candles saved to {db_path}")


if __name__ == "__main__":
    main()
