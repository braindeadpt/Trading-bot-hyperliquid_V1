"""Binance candle backfill for fast strategy warm-up on bot start."""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, List, Optional, Sequence

from src.data.database import Candle, Database

logger = logging.getLogger(__name__)

BINANCE_INTERVALS = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
}

DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "1h")


def _limit_for_days(tf: str, days: int) -> int:
    per_day = {"1m": 24 * 60, "5m": 24 * 12, "15m": 24 * 4, "1h": 24}
    return min(days * per_day.get(tf, 24 * 4), 1000)


def fetch_binance_klines(symbol: str, interval: str, limit: int = 1000) -> list:
    """Fetch klines from Binance REST API."""
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}USDT&interval={interval}&limit={limit}"
    )
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def kline_to_candle(k: list, symbol: str) -> Candle:
    return Candle(
        symbol=symbol,
        timestamp_ms=int(k[0]),
        open=float(k[1]),
        high=float(k[2]),
        low=float(k[3]),
        close=float(k[4]),
        volume=float(k[5]),
        funding_rate=None,
        oi_total=None,
        oi_delta=None,
    )


def needs_backfill(
    db: Database,
    symbols: Sequence[str],
    min_candles_15m: int,
) -> List[str]:
    """Return symbols with fewer than min_candles_15m bars in DB."""
    missing: List[str] = []
    for symbol in symbols:
        count = db.count_candles(symbol, "15m")
        if count < min_candles_15m:
            missing.append(symbol)
    return missing


def backfill_symbols(
    db: Database,
    symbols: Sequence[str],
    days: int = 7,
    timeframes: Sequence[str] = DEFAULT_TIMEFRAMES,
) -> int:
    """Download and store candles from Binance. Returns total rows saved."""
    total = 0
    for symbol in symbols:
        sym = symbol.strip().upper()
        for tf in timeframes:
            if tf not in BINANCE_INTERVALS:
                continue
            limit = _limit_for_days(tf, days)
            try:
                raw = fetch_binance_klines(sym, BINANCE_INTERVALS[tf], limit)
                candles = [kline_to_candle(k, sym) for k in raw]
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
            time.sleep(0.3)
    return total


def ensure_candle_history(
    db: Database,
    config: Any,
    log: Optional[logging.Logger] = None,
) -> int:
    """Backfill from Binance when DB lacks enough history for warm-up."""
    active_log = log or logger
    if not config.get("database.auto_backfill_on_start", True):
        return 0

    symbols = list(config.get("assets", config.get("symbols", ["BTC", "ETH", "SOL"])))
    min_15m = int(config.get("database.backfill_min_candles_15m", 80))
    days = int(config.get("database.backfill_days", 7))
    timeframes = config.get("database.backfill_timeframes", list(DEFAULT_TIMEFRAMES))

    need = needs_backfill(db, symbols, min_15m)
    if not need:
        active_log.info(
            "Candle warm-up OK — all symbols have >= %d x 15m candles in DB",
            min_15m,
        )
        return 0

    active_log.info(
        "Backfilling Binance history (%d days) for %s — faster strategy warm-up",
        days,
        ", ".join(need),
    )
    return backfill_symbols(db, symbols, days=days, timeframes=timeframes)
