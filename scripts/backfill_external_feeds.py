"""Backfill Binance perp prices + liquidation proxy for backtest replay.

Usage:
    python scripts/backfill_external_feeds.py
    python scripts/backfill_external_feeds.py --days 14 --force
    python scripts/backfill_external_feeds.py --perp-only
    python scripts/backfill_external_feeds.py --liquidations-only
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.database import Database
from src.data.external_feeds_backfill import (
    backfill_binance_perp_prices,
    backfill_liquidation_proxy_from_db,
    needs_liquidation_backfill,
    needs_perp_backfill,
)
from src.utils.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill Binance perp mids + liquidation proxy for backtest",
    )
    parser.add_argument("--db-path", default="", help="SQLite DB path")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols")
    parser.add_argument("--days", type=int, default=0, help="Days of perp history")
    parser.add_argument("--force", action="store_true", help="Re-download even if data exists")
    parser.add_argument("--perp-only", action="store_true", help="Skip liquidation proxy")
    parser.add_argument("--liquidations-only", action="store_true", help="Skip perp prices")
    parser.add_argument(
        "--proxy-min-notional",
        type=float,
        default=50_000.0,
        help="Min USD notional for proxy liquidation events",
    )
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
    days = args.days or int(cfg.get("database.backfill_perp_days", 7))

    db = Database(str(db_path))
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000

    perp_n = 0
    liq_n = 0

    if not args.liquidations_only:
        perp_targets = symbols if args.force else [s for s in symbols if needs_perp_backfill(db, s)]
        if perp_targets:
            print(f"Backfilling Binance perp prices: {', '.join(perp_targets)} ({days}d)...")
            perp_n = backfill_binance_perp_prices(db, perp_targets, days=days)
        else:
            print("Perp prices already populated (use --force to refresh).")

    if not args.perp_only:
        liq_targets = (
            symbols
            if args.force
            else [s for s in symbols if needs_liquidation_backfill(db, s)]
        )
        if liq_targets:
            print(
                f"Deriving liquidation proxy from candles+OI: {', '.join(liq_targets)}..."
            )
            liq_n = backfill_liquidation_proxy_from_db(
                db,
                liq_targets,
                start_ms=start_ms,
                end_ms=end_ms,
                min_notional_usd=args.proxy_min_notional,
                replace_existing=args.force,
            )
        else:
            print("Liquidation events already present (use --force to regenerate).")

    print(f"Done: {perp_n} perp rows, {liq_n} liquidation proxy rows -> {db_path}")
    db.close()


if __name__ == "__main__":
    main()
