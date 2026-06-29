"""Backfill funding + open-interest history from Binance into the bot DB.

Usage:
    python scripts/backfill_funding.py
    python scripts/backfill_funding.py --symbols BTC,ETH,SOL --days 30
    python scripts/backfill_funding.py --db-path data/live/bot.db --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.database import Database
from src.data.funding_backfill import backfill_funding_oi, needs_funding_backfill
from src.utils.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill funding + OI history from Binance USD-M futures",
    )
    parser.add_argument("--db-path", default="", help="Path to bot SQLite DB")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols")
    parser.add_argument("--days", type=int, default=0, help="Days of history")
    parser.add_argument(
        "--oi-period",
        default="1h",
        choices=["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"],
        help="Binance openInterestHist period (default: 1h)",
    )
    parser.add_argument("--force", action="store_true", help="Backfill even if rows exist")
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
    days = args.days or int(cfg.get("database.backfill_funding_days", 30))

    db = Database(str(db_path))

    if not args.force:
        missing = [s for s in symbols if needs_funding_backfill(db, s)]
        if not missing:
            print(f"All symbols already have funding history. Use --force to refresh.")
            db.close()
            return
        symbols = missing

    print(f"Backfilling funding + OI for {', '.join(symbols)} ({days} days)...")
    n_funding, n_oi = backfill_funding_oi(db, symbols, days=days, oi_period=args.oi_period)
    print(f"Done: {n_funding} funding rows, {n_oi} OI rows -> {db_path}")
    db.close()


if __name__ == "__main__":
    main()
