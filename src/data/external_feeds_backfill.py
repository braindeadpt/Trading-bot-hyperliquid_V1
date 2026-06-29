"""Backfill external feeds for backtest: Binance perp prices + liquidation proxy.

Binance removed public historical liquidation REST (``allForceOrders``).
We backfill:

  * **Perp mids** — USD-M ``/fapi/v1/klines`` close (1m), stored in
    ``binance_perp_prices``.
  * **Liquidation proxy** — derived from sharp 1m moves + negative OI delta
    (same heuristic as live ``_accumulate_liquidation_proxy``), stored in
    ``liquidation_events`` with ``source='proxy'``.

Live Binance ``@forceOrder`` events are persisted by the engine as
``source='binance'`` for higher-fidelity replay when available.
"""

from __future__ import annotations

import asyncio
import logging
import time
from bisect import bisect_right
from typing import List, Optional, Sequence, Tuple

import aiohttp

from src.data.database import BinancePerpPriceRecord, Candle, Database, LiquidationRecord
from src.exchanges.binance_api import to_binance_symbol

logger = logging.getLogger(__name__)

FUTURES_REST = "https://fapi.binance.com"
PER_REQUEST_TIMEOUT_SEC = 10.0
PAGE_SLEEP_SEC = 0.2
MAX_PER_REQUEST = 1000

# Match engine proxy: >5% per hour velocity + OI decreasing
_PROXY_VELOCITY_PER_HOUR = 0.05
_PROXY_LIQ_VOLUME_FRAC = 0.30
_PROXY_MIN_NOTIONAL_USD = 50_000.0


async def _fetch_futures_klines_page(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> List[list]:
    params = {
        "symbol": to_binance_symbol(symbol),
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": MAX_PER_REQUEST,
    }
    timeout = aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT_SEC)
    url = f"{FUTURES_REST}/fapi/v1/klines"
    async with session.get(url, params=params, timeout=timeout) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Binance futures klines HTTP {resp.status}: {body[:200]}")
        data = await resp.json()
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected klines response: {str(data)[:200]}")
        return data


def _futures_kline_to_perp_record(k: list, symbol: str) -> BinancePerpPriceRecord:
    """Use kline close at close_time as Binance USD-M perp mid proxy."""
    return BinancePerpPriceRecord(
        symbol=symbol.upper(),
        price=float(k[4]),
        timestamp_ms=int(k[6]),
    )


async def _download_perp_prices(
    session: aiohttp.ClientSession,
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str = "1m",
) -> List[BinancePerpPriceRecord]:
    records: List[BinancePerpPriceRecord] = []
    cursor = start_ms
    sym = symbol.upper()
    while cursor < end_ms:
        page = await _fetch_futures_klines_page(session, sym, interval, cursor, end_ms)
        if not page:
            break
        for row in page:
            ts = int(row[6])
            if start_ms <= ts <= end_ms:
                records.append(_futures_kline_to_perp_record(row, sym))
        last_open = int(page[-1][0])
        cursor = last_open + 1
        if len(page) < MAX_PER_REQUEST:
            break
        await asyncio.sleep(PAGE_SLEEP_SEC)
    return records


def _lookup_oi_delta_at(
    oi_series: List[Tuple[int, float, float]],
    ts_ms: int,
) -> Optional[float]:
    """Last OI delta with timestamp <= ts_ms."""
    if not oi_series:
        return None
    times = [row[0] for row in oi_series]
    idx = bisect_right(times, ts_ms) - 1
    if idx < 0:
        return None
    return oi_series[idx][2]


def derive_liquidation_proxy_events(
    symbol: str,
    candles_1m: Sequence[Candle],
    oi_series: Sequence[Tuple[int, float, float]],
    min_notional_usd: float = _PROXY_MIN_NOTIONAL_USD,
) -> List[LiquidationRecord]:
    """Scan 1m candles and emit proxy liquidation events for backtest."""
    if len(candles_1m) < 2:
        return []

    oi_list = sorted(oi_series, key=lambda x: x[0])
    sym = symbol.upper()
    out: List[LiquidationRecord] = []

    for i in range(1, len(candles_1m)):
        prev_c = candles_1m[i - 1]
        curr_c = candles_1m[i]
        if prev_c.close <= 0 or curr_c.close <= 0:
            continue
        dt_ms = curr_c.timestamp_ms - prev_c.timestamp_ms
        if dt_ms <= 0:
            continue

        price_change = (curr_c.close - prev_c.close) / prev_c.close
        velocity = abs(price_change) * (3_600_000 / dt_ms)
        if velocity <= _PROXY_VELOCITY_PER_HOUR:
            continue

        oi_delta = _lookup_oi_delta_at(oi_list, curr_c.timestamp_ms)
        if oi_delta is None or oi_delta >= 0:
            continue

        est_notional = curr_c.volume * curr_c.close * _PROXY_LIQ_VOLUME_FRAC
        if est_notional < min_notional_usd:
            continue

        liq_side = "long" if price_change < 0 else "short"
        out.append(
            LiquidationRecord(
                symbol=sym,
                timestamp_ms=curr_c.timestamp_ms,
                notional_usd=est_notional,
                side=liq_side,
                source="proxy",
            )
        )
    return out


def backfill_liquidation_proxy_from_db(
    db: Database,
    symbols: Sequence[str],
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    min_notional_usd: float = _PROXY_MIN_NOTIONAL_USD,
    replace_existing: bool = False,
) -> int:
    """Derive proxy liquidation rows from existing 1m candles + OI in DB."""
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    total = 0
    for symbol in symbols:
        sym = symbol.upper()
        if replace_existing:
            db.delete_liquidation_proxy(sym)
        candles = db.get_candles(sym, "1m", limit=500_000, start_ms=start_ms, end_ms=end_ms)
        if not candles:
            logger.warning("Liquidation proxy %s: no 1m candles", sym)
            continue
        oi_rows = db.get_oi_history(sym, limit=500_000)
        oi_series: List[Tuple[int, float, float]] = []
        for r in oi_rows:
            ts = int(r["timestamp"])
            if start_ms is not None and ts < start_ms:
                continue
            if ts > end_ms:
                continue
            oi_series.append((ts, float(r["oi_total"]), float(r["oi_delta"])))

        events = derive_liquidation_proxy_events(sym, candles, oi_series, min_notional_usd)
        if events:
            db.save_liquidation_batch(events)
            total += len(events)
            logger.info("Liquidation proxy %s: %d events", sym, len(events))
    return total


async def _async_backfill_perp(
    db: Database,
    symbols: Sequence[str],
    days: int,
    interval: str = "1m",
) -> int:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    total = 0
    timeout = aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for symbol in symbols:
            sym = symbol.upper()
            try:
                rows = await _download_perp_prices(session, sym, start_ms, end_ms, interval)
                if rows:
                    db.save_binance_perp_batch(rows)
                    total += len(rows)
                    logger.info(
                        "Binance perp backfill %s: %d rows (%s, %dd)",
                        sym,
                        len(rows),
                        interval,
                        days,
                    )
            except Exception as exc:
                logger.warning("Binance perp backfill failed for %s: %s", sym, exc)
    return total


def backfill_binance_perp_prices(
    db: Database,
    symbols: Sequence[str],
    days: int = 7,
    interval: str = "1m",
) -> int:
    """Sync facade — download USD-M perp klines into ``binance_perp_prices``."""
    return asyncio.run(_async_backfill_perp(db, symbols, days, interval=interval))


def needs_perp_backfill(db: Database, symbol: str, min_rows: int = 100) -> bool:
    return db.count_binance_perp_prices(symbol) < min_rows


def needs_liquidation_backfill(db: Database, symbol: str, min_rows: int = 1) -> bool:
    return db.count_liquidation_events(symbol) < min_rows
