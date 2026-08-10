"""Backfill ``binance_perp_prices`` from Binance USD-M REST klines.

Fills the gap after the fstream outage (data stops ~2026-06-29). Uses
``/fapi/v1/klines`` (HTTP 200 on networks where fstream is blocked).

Usage:
  python scripts/backfill_binance_perp_prices.py
  python scripts/backfill_binance_perp_prices.py --from-gap
  python scripts/backfill_binance_perp_prices.py --start 2026-06-29 --days 50
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.database import Database  # noqa: E402
from src.data.external_feeds_backfill import (  # noqa: E402
    PAGE_SLEEP_SEC,
    PER_REQUEST_TIMEOUT_SEC,
    _download_perp_prices,
)
from src.utils.config import load_config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_bn_perp")


def _parse_day(s: str) -> int:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _gap_start_ms(db_path: Path) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT MAX(timestamp_ms) FROM binance_perp_prices"
        ).fetchone()
        mx = int(row[0]) if row and row[0] else None
    finally:
        con.close()
    if mx is None:
        # default: 45d lookback
        return int(time.time() * 1000) - 45 * 86_400_000
    return mx + 1


async def _run(
    db: Database,
    symbols: Sequence[str],
    start_ms: int,
    end_ms: int,
    interval: str,
) -> int:
    total = 0
    timeout = aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for symbol in symbols:
            sym = symbol.upper()
            logger.info(
                "Fetching %s %s → %s …",
                sym,
                datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc),
                datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc),
            )
            rows = await _download_perp_prices(
                session, sym, start_ms, end_ms, interval=interval
            )
            if rows:
                db.save_binance_perp_batch(rows)
                total += len(rows)
                logger.info("  saved %d rows for %s", len(rows), sym)
            else:
                logger.warning("  no rows for %s", sym)
            await asyncio.sleep(PAGE_SLEEP_SEC)
    return total


def _density_report(db_path: Path, symbols: Sequence[str]) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        for sym in symbols:
            rows = con.execute(
                """
                SELECT date(timestamp_ms/1000,'unixepoch') d, COUNT(*)
                FROM binance_perp_prices WHERE symbol=?
                GROUP BY 1 ORDER BY 1
                """,
                (sym,),
            ).fetchall()
            if not rows:
                logger.info("%s: EMPTY", sym)
                continue
            gaps = []
            for i in range(1, len(rows)):
                # expect ~1440 1m bars/day
                if rows[i][1] < 1000:
                    gaps.append((rows[i][0], rows[i][1]))
            logger.info(
                "%s: days=%d first=%s last=%s thin_days=%d %s",
                sym,
                len(rows),
                rows[0][0],
                rows[-1][0],
                len(gaps),
                gaps[:5],
            )
        mx = con.execute(
            "SELECT MIN(timestamp_ms), MAX(timestamp_ms), COUNT(*) FROM binance_perp_prices"
        ).fetchone()
        logger.info(
            "GLOBAL n=%s min=%s max=%s",
            mx[2],
            datetime.fromtimestamp(mx[0] / 1000, tz=timezone.utc) if mx[0] else None,
            datetime.fromtimestamp(mx[1] / 1000, tz=timezone.utc) if mx[1] else None,
        )
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "live" / "bot.db")
    ap.add_argument("--from-gap", action="store_true", help="Start after max(timestamp)")
    ap.add_argument("--start", type=str, default="", help="YYYY-MM-DD UTC")
    ap.add_argument("--days", type=int, default=0, help="Lookback days from now")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--symbols", default="")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config" / "settings.yaml")
    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else list(cfg.get("assets") or ["BTC", "ETH", "SOL", "HYPE"])
    )
    end_ms = int(time.time() * 1000)
    if args.from_gap:
        start_ms = _gap_start_ms(args.db)
    elif args.start:
        start_ms = _parse_day(args.start)
    elif args.days > 0:
        start_ms = end_ms - args.days * 86_400_000
    else:
        start_ms = _gap_start_ms(args.db)

    if start_ms >= end_ms:
        logger.info("Nothing to backfill (start>=end). Density report only.")
        _density_report(args.db, symbols)
        return 0

    db = Database(str(args.db))
    n = asyncio.run(_run(db, symbols, start_ms, end_ms, args.interval))
    logger.info("Backfill complete: %d rows", n)
    _density_report(args.db, symbols)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
