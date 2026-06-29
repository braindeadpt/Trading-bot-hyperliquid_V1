"""Backfill buy_volume/sell_volume for existing 1m candles.

Old candles (pre-v3.1.29 fix) have buy_volume=0. This re-fetches from
Binance and uses INSERT OR REPLACE to populate the taker-buy base volume
so CVDOrderFlow can run on the full historical range.

Usage:
    python scripts/_backfill_cvd_volume.py --days 36
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.candle_backfill import (
    BINANCE_INTERVALS,
    INTERVAL_MS,
    MAX_PER_REQUEST,
    PAGE_SLEEP_SEC,
    _download_range,
    _new_session,
)
from src.data.database import Database
from src.utils.config import load_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def backfill_volume(db: Database, symbols: list, days: int) -> int:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    total = 0
    async with _new_session() as session:
        for sym in symbols:
            for tf in ("1m",):
                limit = min(days * 1440, MAX_PER_REQUEST * 200)
                candles = await _download_range(
                    session, sym, BINANCE_INTERVALS[tf], start_ms, end_ms
                )
                if candles:
                    db.save_candles(candles, tf)
                    total += len(candles)
                    with_bs = sum(
                        1 for c in candles if c.buy_volume and c.buy_volume > 0
                    )
                    logger.info(
                        "%s %s: %d candles (%d with buy_volume)",
                        sym, tf, len(candles), with_bs,
                    )
    return total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=36)
    parser.add_argument("--symbols", default="BTC,ETH,SOL")
    args = parser.parse_args()

    cfg = load_config(ROOT / "config" / "settings.yaml")
    db = Database(cfg.get("database.path", "data/live/bot.db"))
    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    t0 = time.time()
    total = asyncio.run(backfill_volume(db, symbols, args.days))
    print(f"\nDone: {total} candles re-saved with buy_volume in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
