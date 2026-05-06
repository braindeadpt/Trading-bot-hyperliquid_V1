"""Historical data fetcher using Binance free API.

Downloads 1-minute klines for major perp symbols and backfills the
SQLite candles_1m table.  Uses batch inserts and a sliding time-window
to avoid Binance rate limits.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

import requests

# Local import – Candle lives in database.py
from src.data.database import Candle, Database

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
DEFAULT_SYMBOLS: List[str] = ["BTC", "ETH", "SOL"]
INTERVAL = "1m"
LIMIT_PER_REQUEST = 1000           # Binance hard cap
RATE_LIMIT_SLEEP = 0.35            # ~3 req/s to stay safely under the 1200 weight/min limit


@dataclass(frozen=True)
class FetchResult:
    """Summary of a fetch run."""
    symbol: str
    candles_inserted: int
    start_ms: int
    end_ms: int
    error: Optional[str] = None


class HistoricalFetcher:
    """Pulls 1m candles from Binance and writes them to the local DB."""

    def __init__(self, database: Database, symbols: Optional[Sequence[str]] = None) -> None:
        self.db = database
        self.symbols = list(symbols) if symbols else list(DEFAULT_SYMBOLS)
        self.session = requests.Session()
        # 60-second read/connect timeout – Binance can be slow under load
        self.session.timeout = (10, 60)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def backfill_all(
        self,
        days: int = 30,
        symbols: Optional[Sequence[str]] = None,
    ) -> List[FetchResult]:
        """Backfill *days* of 1m data for every configured symbol.

        Returns one FetchResult per symbol so callers can log or retry.
        """
        targets = list(symbols) if symbols else self.symbols
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (days * 86400 * 1000)

        results: List[FetchResult] = []
        for sym in targets:
            res = self._fetch_symbol(sym, start_ms, end_ms)
            results.append(res)
            time.sleep(RATE_LIMIT_SLEEP)
        return results

    def backfill_symbol(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> FetchResult:
        """Backfill a single symbol between two millisecond timestamps."""
        return self._fetch_symbol(symbol, start_ms, end_ms)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fetch_symbol(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> FetchResult:
        """Paginate Binance klines and batch-insert into DB."""
        binance_symbol = f"{symbol}USDT"
        all_candles: List[Candle] = []
        cursor_ms = start_ms
        inserted = 0
        error: Optional[str] = None

        print(f"[Fetcher] Starting backfill for {symbol} ({binance_symbol}) "
              f"from {start_ms} to {end_ms}")

        try:
            while cursor_ms < end_ms:
                batch = self._request_page(binance_symbol, cursor_ms, end_ms)
                if not batch:
                    break

                candles = [self._binance_to_candle(symbol, row) for row in batch]
                all_candles.extend(candles)

                # Slide cursor to just after the last candle in this page
                last_ts = batch[-1][0]
                cursor_ms = last_ts + 60_000  # +1 minute

                # Progress
                print(f"[Fetcher] {symbol}: fetched {len(candles)} candles, "
                      f"cursor at {cursor_ms} ({len(all_candles)} total)")

                time.sleep(RATE_LIMIT_SLEEP)

            # Batch insert in chunks of 500 to keep SQLite bindings reasonable
            if all_candles:
                chunk_size = 500
                for i in range(0, len(all_candles), chunk_size):
                    chunk = all_candles[i:i + chunk_size]
                    self.db.save_candles(chunk, timeframe="1m")
                    inserted += len(chunk)
                print(f"[Fetcher] {symbol}: inserted {inserted} candles into DB")

        except requests.RequestException as exc:
            error = str(exc)
            print(f"[Fetcher] ERROR {symbol}: {error}")
        except Exception as exc:
            error = str(exc)
            print(f"[Fetcher] ERROR {symbol}: {error}")

        return FetchResult(
            symbol=symbol,
            candles_inserted=inserted,
            start_ms=start_ms,
            end_ms=end_ms,
            error=error,
        )

    def _request_page(
        self,
        binance_symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> List[List]:
        """GET a single page of klines from Binance."""
        params = {
            "symbol": binance_symbol,
            "interval": INTERVAL,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": LIMIT_PER_REQUEST,
        }
        resp = self.session.get(BINANCE_KLINES_URL, params=params)
        resp.raise_for_status()
        data: List[List] = resp.json()
        return data

    @staticmethod
    def _binance_to_candle(symbol: str, row: List) -> Candle:
        """Convert a Binance kline array into our Candle dataclass.

        Binance kline layout:
        0  open_time
        1  open
        2  high
        3  low
        4  close
        5  volume
        6  close_time   (ignored)
        7  quote_volume (ignored)
        8  trades       (ignored)
        9  taker_buy_volume (ignored)
        10 taker_buy_quote_volume (ignored)
        11 ignore       (ignored)
        """
        return Candle(
            symbol=symbol,
            timestamp_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            funding_rate=None,
            oi_total=None,
            oi_delta=None,
        )

    def close(self) -> None:
        """Close the HTTP session."""
        self.session.close()
