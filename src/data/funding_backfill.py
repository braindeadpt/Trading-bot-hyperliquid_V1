"""Historical funding + open-interest backfill from Binance USD-M futures.

Populates ``funding_history`` and ``oi_history`` for backtests of
SpotPerpCarry, FundingMomentum, MeanReversion, and related strategies.

Data sources (public REST, no API key):
  * ``GET /fapi/v1/fundingRate`` — 8h funding settlements
  * ``GET /futures/data/openInterestHist`` — hourly OI (USD notional)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Sequence, Tuple

import aiohttp

from src.data.database import Database, FundingRecord, OIRecord
from src.exchanges.binance_api import to_binance_symbol

logger = logging.getLogger(__name__)

FUTURES_REST = "https://fapi.binance.com"
PER_REQUEST_TIMEOUT_SEC = 10.0
PAGE_SLEEP_SEC = 0.2
MAX_FUNDING_PAGE = 1000
MAX_OI_PAGE = 500


async def _fetch_funding_page(
    session: aiohttp.ClientSession,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> List[dict]:
    """One page of Binance funding rate history."""
    params = {
        "symbol": to_binance_symbol(symbol),
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": MAX_FUNDING_PAGE,
    }
    timeout = aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT_SEC)
    url = f"{FUTURES_REST}/fapi/v1/fundingRate"
    async with session.get(url, params=params, timeout=timeout) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Binance funding HTTP {resp.status}: {body[:200]}")
        data = await resp.json()
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected funding response: {str(data)[:200]}")
        return data


async def _download_funding(
    session: aiohttp.ClientSession,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> List[FundingRecord]:
    """Paginated funding history for *symbol* in ``[start_ms, end_ms]``."""
    records: List[FundingRecord] = []
    cursor = start_ms
    while cursor < end_ms:
        page = await _fetch_funding_page(session, symbol, cursor, end_ms)
        if not page:
            break
        for row in page:
            ts = int(row["fundingTime"])
            if ts < start_ms or ts > end_ms:
                continue
            rate = float(row["fundingRate"])
            records.append(
                FundingRecord(
                    symbol=symbol.upper(),
                    current=rate,
                    predicted=rate,
                    timestamp=ts,
                )
            )
        last_ts = int(page[-1]["fundingTime"])
        cursor = last_ts + 1
        if len(page) < MAX_FUNDING_PAGE:
            break
        await asyncio.sleep(PAGE_SLEEP_SEC)
    return records


async def _fetch_oi_page(
    session: aiohttp.ClientSession,
    symbol: str,
    period: str,
    start_ms: int,
    end_ms: int,
) -> List[dict]:
    params = {
        "symbol": to_binance_symbol(symbol),
        "period": period,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": MAX_OI_PAGE,
    }
    timeout = aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT_SEC)
    url = f"{FUTURES_REST}/futures/data/openInterestHist"
    async with session.get(url, params=params, timeout=timeout) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Binance OI HTTP {resp.status}: {body[:200]}")
        data = await resp.json()
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected OI response: {str(data)[:200]}")
        return data


async def _download_oi(
    session: aiohttp.ClientSession,
    symbol: str,
    start_ms: int,
    end_ms: int,
    period: str = "1h",
) -> List[OIRecord]:
    """Paginated open-interest history; ``oi_delta`` = change vs prior bar."""
    raw_rows: List[Tuple[int, float]] = []
    cursor = start_ms
    while cursor < end_ms:
        page = await _fetch_oi_page(session, symbol, period, cursor, end_ms)
        if not page:
            break
        for row in page:
            ts = int(row["timestamp"])
            if ts < start_ms or ts > end_ms:
                continue
            # Prefer USD notional; fall back to contracts * mark if needed.
            oi_val = row.get("sumOpenInterestValue") or row.get("sumOpenInterest")
            if oi_val is None:
                continue
            raw_rows.append((ts, float(oi_val)))
        last_ts = int(page[-1]["timestamp"])
        cursor = last_ts + 1
        if len(page) < MAX_OI_PAGE:
            break
        await asyncio.sleep(PAGE_SLEEP_SEC)

    raw_rows.sort(key=lambda x: x[0])
    records: List[OIRecord] = []
    prev_oi: Optional[float] = None
    sym = symbol.upper()
    for ts, oi_total in raw_rows:
        delta = (oi_total - prev_oi) if prev_oi is not None else 0.0
        prev_oi = oi_total
        records.append(
            OIRecord(
                symbol=sym,
                oi_total=oi_total,
                oi_delta=delta,
                timestamp=ts,
            )
        )
    return records


async def _async_backfill(
    db: Database,
    symbols: Sequence[str],
    days: int,
    oi_period: str = "1h",
) -> Tuple[int, int]:
    """Download and persist funding + OI for all *symbols*."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    funding_total = 0
    oi_total = 0

    timeout = aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for symbol in symbols:
            sym = symbol.upper()
            try:
                funding_rows = await _download_funding(session, sym, start_ms, end_ms)
                if funding_rows:
                    db.save_funding_batch(funding_rows)
                    funding_total += len(funding_rows)
                    logger.info(
                        "Funding backfill %s: %d rows (%dd)",
                        sym,
                        len(funding_rows),
                        days,
                    )
            except Exception as exc:
                logger.warning("Funding backfill failed for %s: %s", sym, exc)

            try:
                oi_rows = await _download_oi(session, sym, start_ms, end_ms, period=oi_period)
                if oi_rows:
                    db.save_oi_batch(oi_rows)
                    oi_total += len(oi_rows)
                    logger.info(
                        "OI backfill %s: %d rows (%s, %dd)",
                        sym,
                        len(oi_rows),
                        oi_period,
                        days,
                    )
            except Exception as exc:
                logger.warning("OI backfill failed for %s: %s", sym, exc)

    return funding_total, oi_total


def backfill_funding_oi(
    db: Database,
    symbols: Sequence[str],
    days: int = 30,
    oi_period: str = "1h",
) -> Tuple[int, int]:
    """Sync facade for CLI and startup hooks."""
    return asyncio.run(_async_backfill(db, symbols, days, oi_period=oi_period))


def needs_funding_backfill(db: Database, symbol: str, min_rows: int = 10) -> bool:
    """True when ``funding_history`` has fewer than *min_rows* for *symbol*."""
    rows = db.get_funding_history(symbol, limit=min_rows + 1)
    return len(rows) < min_rows
