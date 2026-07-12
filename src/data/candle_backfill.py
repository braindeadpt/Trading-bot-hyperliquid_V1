"""Unified Binance candle backfill (async).

Single source of truth for historical candle backfill. Used by:

  * ``main.py`` at startup (``ensure_candle_history``) — fast warm-up
  * ``scripts/seed_db.py`` — explicit date-range CLI

All public entry points are coroutines that respect a per-run deadline
(``BOT_BACKFILL_TIMEOUT_SEC`` env var, default 15s) and a per-request
timeout (5s). Uses ``aiohttp`` so the event loop is never blocked.

Idempotent: candles are inserted with ``INSERT OR REPLACE`` keyed on
``(symbol, timestamp_ms)``. Re-running with overlapping ranges is safe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence, Tuple

import aiohttp

from src.data.database import Candle, Database
from src.data.hl_research_backfill import split_taker_volume_from_kline
from src.utils.config import get_trading_symbols

logger = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com/api/v3/klines"
BINANCE_FUTURES_BASE = "https://fapi.binance.com/fapi/v1/klines"

BINANCE_INTERVALS = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
}

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}

DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "1h")

DEFAULT_BACKFILL_TIMEOUT_SEC = 15.0
PER_REQUEST_TIMEOUT_SEC = 5.0
MAX_PER_REQUEST = 1000
PAGE_SLEEP_SEC = 0.25


# ────────────────────────────────────────────────────────────────────
# Pure helpers (no I/O)
# ────────────────────────────────────────────────────────────────────

def _limit_for_days(tf: str, days: int) -> int:
    per_day = {"1m": 24 * 60, "5m": 24 * 12, "15m": 24 * 4, "1h": 24}
    return min(days * per_day.get(tf, 24 * 4), MAX_PER_REQUEST)


def kline_to_candle(k: list, symbol: str) -> Candle:
    """Convert a Binance kline row to a :class:`Candle`.

    v3.1.16 C11: Use close_time (k[6]) for ``timestamp_ms`` to match live
    candle_builder convention. Previously we used open_time (k[0]), so
    backfilled rows had a different PK value than the live rows that
    eventually replaced them, producing duplicates and look-ahead bias
    in backtests.

    v3.1.29: Populate ``buy_volume`` / ``sell_volume`` from taker-buy base
    volume (k[9]) so CVDOrderFlow can run on backfilled 1m history.
    """
    volume = float(k[5])
    buy, sell, has_taker = split_taker_volume_from_kline(k, volume)
    trade_count = int(k[8]) if len(k) > 8 else 0
    return Candle(
        symbol=symbol,
        timestamp_ms=int(k[6]),
        open=float(k[1]),
        high=float(k[2]),
        low=float(k[3]),
        close=float(k[4]),
        volume=volume,
        funding_rate=None,
        oi_total=None,
        oi_delta=None,
        buy_volume=buy if has_taker else None,
        sell_volume=sell if has_taker else None,
        trade_count=trade_count,
    )


def _ms_from_date(date_str: str) -> int:
    return int(datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _aligned_range(tf: str, start_ms: int, end_ms: int) -> Tuple[int, int]:
    """Snap a [start, end] ms range to the bar boundaries for the timeframe."""
    gap = INTERVAL_MS.get(tf, 900_000)
    aligned_start = (start_ms // gap) * gap
    aligned_end = (end_ms // gap) * gap
    return aligned_start, aligned_end


# ────────────────────────────────────────────────────────────────────
# Async I/O
# ────────────────────────────────────────────────────────────────────

async def _fetch_klines_page(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    use_futures: bool = False,
) -> List[list]:
    """Fetch one page (max 1000) of Binance klines (spot or USD-M futures)."""
    params = {
        "symbol": f"{symbol}USDT",
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": MAX_PER_REQUEST,
    }
    timeout = aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT_SEC)
    url = BINANCE_FUTURES_BASE if use_futures else BINANCE_BASE
    async with session.get(url, params=params, timeout=timeout) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Binance HTTP {resp.status}: {body[:200]}")
        return await resp.json()


async def _download_range(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> List[Candle]:
    """Paginated download across ``[start_ms, end_ms]``.

    Tries Binance spot first; symbols listed only on USD-M (e.g. HYPE)
    fall back to ``/fapi/v1/klines``.
    """
    for use_futures in (False, True):
        candles: List[Candle] = []
        cursor = start_ms
        try:
            while cursor < end_ms:
                page = await _fetch_klines_page(
                    session, symbol, interval, cursor, end_ms, use_futures=use_futures,
                )
                if not page:
                    break
                candles.extend(kline_to_candle(k, symbol) for k in page)
                last_ts = int(page[-1][0])
                cursor = last_ts + 1
                if len(page) < MAX_PER_REQUEST:
                    break
                await asyncio.sleep(PAGE_SLEEP_SEC)
            if use_futures and candles:
                logger.info(
                    "Candle backfill %s %s: using Binance USD-M futures klines",
                    symbol,
                    interval,
                )
            return candles
        except RuntimeError as exc:
            if not use_futures and "Invalid symbol" in str(exc):
                logger.info(
                    "Spot klines unavailable for %s — retrying via Binance futures",
                    symbol,
                )
                continue
            raise
    return []


# ────────────────────────────────────────────────────────────────────
# Public sync facade (used by main.py startup)
# ────────────────────────────────────────────────────────────────────

def needs_backfill(
    db: Database,
    symbols: Sequence[str],
    min_candles_15m: int,
) -> List[str]:
    """Return symbols with fewer than ``min_candles_15m`` 15m bars in DB."""
    missing: List[str] = []
    for symbol in symbols:
        if db.count_candles(symbol, "15m") < min_candles_15m:
            missing.append(symbol)
    return missing


def _new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT_SEC)
    )


def backfill_symbols(
    db: Database,
    symbols: Sequence[str],
    days: int = 7,
    timeframes: Sequence[str] = DEFAULT_TIMEFRAMES,
    timeout_sec: float = DEFAULT_BACKFILL_TIMEOUT_SEC,
) -> int:
    """Sync facade — runs the async backfill in a one-shot loop.

    Used at startup where there is no live event loop yet. Returns the
    number of candle rows actually written.
    """
    return asyncio.run(
        _async_backfill_symbols(
            db=db,
            symbols=symbols,
            days=days,
            timeframes=timeframes,
            timeout_sec=timeout_sec,
        )
    )


async def _async_backfill_symbols(
    db: Database,
    symbols: Sequence[str],
    days: int,
    timeframes: Sequence[str],
    timeout_sec: float,
) -> int:
    total = 0
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    async with _new_session() as session:
        for symbol in symbols:
            sym = symbol.strip().upper()
            for tf in timeframes:
                if tf not in BINANCE_INTERVALS:
                    continue
                if time.monotonic() >= deadline:
                    logger.warning(
                        "Backfill timeout reached (%.1fs) — aborting after %d rows",
                        timeout_sec,
                        total,
                    )
                    return total
                limit = _limit_for_days(tf, days)
                end_ms = int(time.time() * 1000)
                start_ms = end_ms - limit * INTERVAL_MS.get(tf, 900_000)
                try:
                    candles = await _download_range(
                        session, sym, BINANCE_INTERVALS[tf], start_ms, end_ms
                    )
                    if candles:
                        db.save_candles(candles, tf)
                        total += len(candles)
                        logger.info(
                            "Backfill saved %d %s candles for %s",
                            len(candles),
                            tf,
                            sym,
                        )
                except Exception as exc:
                    logger.warning("Backfill failed %s %s: %s", sym, tf, exc)
                await asyncio.sleep(0.3)
    return total


def ensure_candle_history(
    db: Database,
    config: Any,
    log: Optional[logging.Logger] = None,
) -> int:
    """Startup backfill when DB lacks enough history for warm-up.

    Reads configuration keys (no I/O outside Binance):
      * ``database.auto_backfill_on_start`` — toggle
      * ``assets`` / ``symbols`` — list
      * ``database.backfill_min_candles_15m`` — minimum bars per symbol
      * ``database.backfill_days`` — days of history (capped to 1000 bars)
      * ``database.backfill_timeframes`` — TFs to fetch
    Honors ``BOT_BACKFILL_TIMEOUT_SEC`` env var (default 15s).
    """
    active_log = log or logger
    if not config.get("database.auto_backfill_on_start", True):
        return 0

    symbols = get_trading_symbols(config)
    min_15m = int(config.get("database.backfill_min_candles_15m", 80))
    days = int(config.get("database.backfill_days", 7))
    timeframes = config.get("database.backfill_timeframes", list(DEFAULT_TIMEFRAMES))
    timeout_sec = float(
        os.environ.get("BOT_BACKFILL_TIMEOUT_SEC", DEFAULT_BACKFILL_TIMEOUT_SEC)
    )

    need = needs_backfill(db, symbols, min_15m)
    if not need:
        active_log.info(
            "Candle warm-up OK — all symbols have >= %d x 15m candles in DB",
            min_15m,
        )
        return 0

    active_log.info(
        "Backfilling Binance history (%d days, cap=%.1fs) for %s — faster strategy warm-up",
        days,
        timeout_sec,
        ", ".join(need),
    )
    return backfill_symbols(
        db, symbols, days=days, timeframes=timeframes, timeout_sec=timeout_sec
    )


# ────────────────────────────────────────────────────────────────────
# Public async API (used by scripts/seed_db.py)
# ────────────────────────────────────────────────────────────────────

async def run_range_backfill(
    db: Database,
    symbols: Sequence[str],
    start_ms: int,
    end_ms: int,
    timeframes: Sequence[str] = DEFAULT_TIMEFRAMES,
    timeout_sec: float = 60.0,
    progress_cb: Optional[Any] = None,
) -> int:
    """Download a [start_ms, end_ms] window for ``symbols`` × ``timeframes``.

    ``timeout_sec`` bounds the entire run. ``progress_cb`` (optional) is
    called with ``(symbol, tf, rows_so_far)`` after each TF completes.
    Returns total rows written.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    total = 0
    async with _new_session() as session:
        for sym in symbols:
            sym_u = sym.strip().upper()
            for tf in timeframes:
                if tf not in BINANCE_INTERVALS:
                    continue
                if time.monotonic() >= deadline:
                    logger.warning(
                        "Range backfill deadline reached (%.1fs) — %d rows saved",
                        timeout_sec,
                        total,
                    )
                    return total
                a_start, a_end = _aligned_range(tf, start_ms, end_ms)
                if a_end <= a_start:
                    continue
                try:
                    candles = await _download_range(
                        session, sym_u, BINANCE_INTERVALS[tf], a_start, a_end
                    )
                    if candles:
                        before = db.count_candles(sym_u, tf)
                        db.save_candles(candles, tf)
                        after = db.count_candles(sym_u, tf)
                        total += len(candles)
                        new_count = max(0, after - before)
                        logger.info(
                            "[%s][%s] %d downloaded (%d new, %d existed)",
                            sym_u, tf, len(candles), new_count, before,
                        )
                        if progress_cb is not None:
                            try:
                                progress_cb(sym_u, tf, total)
                            except Exception:
                                pass
                except Exception as exc:
                    logger.warning("Range backfill failed %s %s: %s", sym_u, tf, exc)
                await asyncio.sleep(PAGE_SLEEP_SEC)
    return total
