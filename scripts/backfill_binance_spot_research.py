#!/usr/bin/env python3
"""Backfill Binance SPOT klines into a dedicated research DB (ATR long study).

NEVER writes to ``data/live/bot.db``. Stores under
``data/research/binance_spot_proxy.db`` with provenance
``source=binance_spot_klines``.

Usage:
  python scripts/backfill_binance_spot_research.py --months 24
  python scripts/backfill_binance_spot_research.py --months 18 --symbols BTC,ETH,SOL
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.candle_backfill import (  # noqa: E402
    BINANCE_INTERVALS,
    INTERVAL_MS,
    PAGE_SLEEP_SEC,
    _download_range,
    _new_session,
)
from src.data.research_database import ResearchDatabase  # noqa: E402
from src.data.series_metadata import (  # noqa: E402
    VENUE_BINANCE,
    SeriesMetadata,
)

logger = logging.getLogger("backfill_binance_spot_research")

DEFAULT_DB = ROOT / "data" / "research" / "binance_spot_proxy.db"
SOURCE = "binance_spot_klines"
API_VERSION = "spot-api-v3-klines"


async def _backfill(
    db: ResearchDatabase,
    symbols: Sequence[str],
    timeframes: Sequence[str],
    start_ms: int,
    end_ms: int,
) -> dict:
    meta = SeriesMetadata(
        source=SOURCE,
        venue=VENUE_BINANCE,
        api_version=API_VERSION,
        ingested_at_ms=int(time.time() * 1000),
        quality_flags={"purpose": "atr_percentile_long_revalidation", "market": "spot"},
        volume_unit="base",
    )
    stats: dict = {"by_symbol_tf": {}, "total": 0, "errors": []}
    async with _new_session() as session:
        for sym in symbols:
            for tf in timeframes:
                if tf not in BINANCE_INTERVALS:
                    continue
                key = f"{sym}/{tf}"
                try:
                    candles = await _download_range(
                        session, sym, BINANCE_INTERVALS[tf], start_ms, end_ms
                    )
                    # Filter to window (close_time convention)
                    candles = [
                        c for c in candles if start_ms <= c.timestamp_ms <= end_ms
                    ]
                    if candles:
                        db.save_research_candles(candles, tf, meta)
                    stats["by_symbol_tf"][key] = len(candles)
                    stats["total"] += len(candles)
                    if candles:
                        t0 = datetime.fromtimestamp(
                            candles[0].timestamp_ms / 1000, tz=timezone.utc
                        )
                        t1 = datetime.fromtimestamp(
                            candles[-1].timestamp_ms / 1000, tz=timezone.utc
                        )
                        logger.info(
                            "%s: %d bars %s → %s", key, len(candles), t0.date(), t1.date()
                        )
                    else:
                        logger.warning("%s: 0 bars (symbol may be missing on spot)", key)
                    await asyncio.sleep(PAGE_SLEEP_SEC)
                except Exception as exc:  # noqa: BLE001
                    msg = f"{key}: {exc}"
                    logger.exception(msg)
                    stats["errors"].append(msg)
    return stats


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--months", type=int, default=24, help="Lookback months (default 24)")
    ap.add_argument("--symbols", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--timeframes", default="15m,1h")
    ap.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="Dedicated research DB (never live bot.db)",
    )
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    if "live" in db_path.parts and db_path.name == "bot.db":
        print("REFUSING to write Binance spot proxy into live bot.db")
        return 2

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    end_ms = int(time.time() * 1000)
    # approx months → days
    start_ms = end_ms - int(args.months * 30.4375 * 86400_000)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = ResearchDatabase(db_path)
    print(
        f"Backfilling Binance SPOT → {db_path}\n"
        f"  symbols={symbols} tfs={timeframes} months≈{args.months}\n"
        f"  from {datetime.fromtimestamp(start_ms/1000, tz=timezone.utc).date()} "
        f"to {datetime.fromtimestamp(end_ms/1000, tz=timezone.utc).date()}"
    )
    t0 = time.time()
    stats = asyncio.run(_backfill(db, symbols, timeframes, start_ms, end_ms))
    db.close()
    print(f"\nDone in {time.time()-t0:.1f}s — total rows={stats['total']}")
    for k, n in sorted(stats["by_symbol_tf"].items()):
        print(f"  {k}: {n}")
    if stats["errors"]:
        print("ERRORS:")
        for e in stats["errors"]:
            print(f"  {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
