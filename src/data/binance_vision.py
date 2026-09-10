"""Bulk historical backfill from Binance public data dumps.

``data.binance.vision`` serves zipped CSV archives of official klines with
no rate limit and no auth — far better than paginated ``/api/v3/klines``
for multi-month backfills (one HTTP request per symbol × interval × month
instead of one per 1000 bars).

Layouts:
    futures/um/{monthly|daily}/klines/{SYMBOL}/{iv}/{SYMBOL}-{iv}-{YYYY-MM[ -DD]}.zip
    spot/{monthly|daily}/klines/{SYMBOL}/{iv}/{SYMBOL}-{iv}-{YYYY-MM[-DD]}.zip

CSV rows are the same 12-field kline shape the REST API returns, so
``candle_backfill.kline_to_candle`` converts them unchanged (including the
taker-buy split at index 9). Monthly archives only exist for completed
months — the current partial month falls back to daily archives.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import aiohttp

from src.data.candle_backfill import BINANCE_INTERVALS, kline_to_candle
from src.data.database import Candle, Database
from src.data.hl_research_backfill import INTERVAL_MS
from src.utils.http import make_client_session

logger = logging.getLogger(__name__)

VISION_BASE = "https://data.binance.vision/data"
REQUEST_TIMEOUT_SEC = 120.0
SLEEP_BETWEEN_FILES_SEC = 0.15
# Zip bombs are not a thing here (official domain) but keep a sane cap.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024


def _month_keys(start_ms: int, end_ms: int) -> List[str]:
    """Full calendar months inside [start_ms, end_ms] as ``YYYY-MM``."""
    out: List[str] = []
    cur = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    y, m = cur.year, cur.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _day_keys(start_ms: int, end_ms: int) -> List[str]:
    out: List[str] = []
    t = start_ms
    while t <= end_ms:
        out.append(datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))
        t += 86_400_000
    return out


def _zip_url(kind: str, cadence: str, symbol: str, interval: str, key: str) -> str:
    sym = f"{symbol.upper()}USDT"
    return (
        f"{VISION_BASE}/{kind}/{cadence}/klines/{sym}/{interval}/{sym}-{interval}-{key}.zip"
    )


def iter_kline_rows(blob: bytes) -> Iterator[List[str]]:
    """Yield kline field lists from a zipped vision CSV (header-tolerant)."""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if not names:
            return
        info = zf.getinfo(names[0])
        if info.file_size > MAX_UNCOMPRESSED_BYTES:
            raise RuntimeError(f"vision CSV too large: {info.file_size} bytes")
        with zf.open(names[0]) as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8"))
            for row in reader:
                if not row:
                    continue
                # monthly archives have a header row; daily may not
                if not row[0].lstrip("-").isdigit():
                    continue
                yield row


async def _fetch_zip(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    try:
        async with session.get(url) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                body = (await resp.text())[:200]
                raise RuntimeError(f"vision HTTP {resp.status}: {body}")
            return await resp.read()
    except aiohttp.ClientError as exc:
        logger.warning("vision fetch failed %s: %s", url, exc)
        return None


async def _candles_for_month(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    month_key: str,
    kinds: Sequence[str],
    start_ms: int,
    end_ms: int,
) -> List[Candle]:
    """Monthly archive first; daily files only for the current month."""
    now_key = datetime.fromtimestamp(
        int(time.time() * 1000) / 1000, tz=timezone.utc
    ).strftime("%Y-%m")
    if month_key < now_key:
        # completed month -> one archive per kind, first kind wins
        groups = [[_zip_url(k, "monthly", symbol, interval, month_key)]
                  for k in kinds]
    else:
        # partial month -> per-day archives; for each day try kinds in order
        days = _day_keys(
            max(start_ms, int(datetime.strptime(month_key, "%Y-%m")
                              .replace(tzinfo=timezone.utc).timestamp() * 1000)),
            end_ms,
        )
        groups = [[_zip_url(k, "daily", symbol, interval, d) for k in kinds]
                  for d in days]

    candles: List[Candle] = []
    for urls in groups:
        for url in urls:
            blob = await _fetch_zip(session, url)
            if blob is None:
                continue
            for row in iter_kline_rows(blob):
                open_ms = int(row[0])
                if open_ms < start_ms or open_ms > end_ms:
                    continue
                candles.append(kline_to_candle(row, symbol))
            break  # first kind that produced data wins for this group
        await asyncio.sleep(SLEEP_BETWEEN_FILES_SEC)
    candles.sort(key=lambda c: c.timestamp_ms)
    return candles


async def backfill_from_vision(
    db: Database,
    symbols: Sequence[str],
    start_ms: int,
    end_ms: int,
    timeframes: Sequence[str] = ("1m", "5m", "15m", "1h"),
    kinds: Sequence[str] = ("futures/um", "spot"),
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """Download vision archives for [start_ms, end_ms] and persist candles.

    ``kinds`` is tried in order per month — USD-M futures first (perp
    semantics, taker-buy field), spot as fallback for spot-only listings.
    Returns a per-(symbol, tf) summary; missing archives are skipped, not
    fatal.
    """
    summary: Dict[str, Any] = {"written": {}, "missing": [], "total_rows": 0}
    async with make_client_session(
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)
    ) as session:
        for sym in symbols:
            sym_u = sym.strip().upper()
            for tf in timeframes:
                interval = BINANCE_INTERVALS.get(tf)
                if interval is None:
                    continue
                got = 0
                months = _month_keys(start_ms, end_ms)
                for mk in months:
                    candles = await _candles_for_month(
                        session, sym_u, interval, mk, kinds, start_ms, end_ms,
                    )
                    if candles:
                        db.save_candles(candles, tf)
                        got += len(candles)
                    else:
                        summary["missing"].append(f"{sym_u}:{tf}:{mk}")
                summary["written"][f"{sym_u}:{tf}"] = got
                summary["total_rows"] += got
                logger.info(
                    "vision backfill %s %s: %d candles (%d months)",
                    sym_u, tf, got, len(months),
                )
                if progress_cb is not None:
                    try:
                        progress_cb(sym_u, tf, got)
                    except Exception:
                        pass
    return summary
