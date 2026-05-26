"""Backfill candle history from Binance into the bot's SQLite database.

Usage:
    python scripts/backfill_candles.py
    python scripts/backfill_candles.py --symbols BTC,ETH,SOL --days 7
    python scripts/backfill_candles.py --db-path data/live/bot.db

On normal bot start, backfill runs automatically when history is missing
(see database.auto_backfill_on_start in config/settings.yaml).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.candle_backfill import backfill_symbols, needs_backfill
from src.data.database import Database
from src.utils.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill candle history from Binance")
    parser.add_argument("--db-path", default="", help="Path to bot SQLite DB")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols")
    parser.add_argument("--days", type=int, default=0, help="Days of history to fetch")
    parser.add_argument("--force", action="store_true", help="Backfill even if DB looks warm")
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
    days = args.days or int(cfg.get("database.backfill_days", 7))
    min_15m = int(cfg.get("database.backfill_min_candles_15m", 80))
    timeframes = cfg.get("database.backfill_timeframes", ["1m", "5m", "15m", "1h"])

    if not db_path.exists():
        print(f"DB not found at {db_path}. It will be created.")
    db = Database(str(db_path))

    targets = symbols if args.force else needs_backfill(db, symbols, min_15m)
    if not targets:
        print(f"All symbols already have >= {min_15m} x 15m candles. Use --force to refresh.")
        db.close()
        return

    print(f"Backfilling {', '.join(targets)} ({days} days)...")
    total = backfill_symbols(db, targets, days=days, timeframes=timeframes)
    db.close()
    print(f"\nDone! {total} candles saved to {db_path}")


if __name__ == "__main__":
    main()
