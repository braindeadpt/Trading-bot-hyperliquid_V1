"""
Coinalyze-like aggregator: fetches funding rates + open interest
from multiple free exchanges (Binance, Bybit, OKX) and aggregates.

No API key required. Uses public REST endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from src.exchanges.funding_normalize import (
    EXCHANGE_FUNDING_INTERVAL_HOURS,
    normalize_funding_to_8h,
)

logger = logging.getLogger(__name__)


# ── Data models ──

@dataclass
class FundingOI:
    """Aggregated funding + OI snapshot for a symbol."""
    symbol: str                          # e.g. "BTC"
    exchange: str                      # e.g. "binance", "bybit", "okx"
    funding_rate: Optional[float] = None     # 8h funding rate
    predicted_funding: Optional[float] = None  # Next 8h predicted (if available)
    open_interest: Optional[float] = None     # In USD
    oi_change_24h_pct: Optional[float] = None
    mark_price: Optional[float] = None
    timestamp_ms: int = 0
    # v3.1.21: the exchange-side timestamp of the data (e.g. Binance's
    # ``fundingTime`` field) is *not* the same as our local wall-time
    # at which we cached the row. We now track both so the staleness
    # check can flag a row whose underlying exchange data is hours
    # old, even if the local cache was just refreshed.
    exchange_timestamp_ms: int = 0
    funding_interval_hours: float = 8.0


def _normalize_funding_oi(foi: FundingOI) -> None:
    """In-place: scale funding fields to 8h equivalent for cross-venue comparison."""
    hours = EXCHANGE_FUNDING_INTERVAL_HOURS.get(foi.exchange, foi.funding_interval_hours)
    foi.funding_interval_hours = hours
    if foi.funding_rate is not None:
        foi.funding_rate = normalize_funding_to_8h(foi.funding_rate, hours)
    if foi.predicted_funding is not None:
        foi.predicted_funding = normalize_funding_to_8h(foi.predicted_funding, hours)


@dataclass
class AggregatedFundingOI:
    """Cross-exchange aggregated funding + OI for a symbol."""
    symbol: str
    funding_avg: Optional[float] = None       # Simple average across exchanges
    funding_weighted: Optional[float] = None  # Weighted by OI
    predicted_funding_avg: Optional[float] = None
    oi_total: Optional[float] = None          # Sum of OI across exchanges
    oi_total_change_24h_pct: Optional[float] = None
    exchange_count: int = 0
    by_exchange: Dict[str, FundingOI] = field(default_factory=dict)
    timestamp_ms: int = 0
    # v3.1.21: separate the wall-time at which we cached this row
    # (``cache_insertion_ms``) from the *exchange-side* timestamp
    # (``exchange_timestamp_ms``). The staleness check considers
    # both ages.
    cache_insertion_ms: int = 0
    exchange_timestamp_ms: int = 0
    stale: bool = False
    age_sec: float = 0.0
    exchange_age_sec: float = 0.0
    failed_exchanges: List[str] = field(default_factory=list)


# ── Exchange clients ──

class CoinalyzeClient:
    """Coinalyze API — aggregated funding + OI from 15+ exchanges.

    Free tier: 40 requests/min.  We issue 3 parallel calls per symbol
    (funding-rate, predicted-funding-rate, open-interest) and merge.
    """
    BASE = "https://api.coinalyze.net/v1"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def _get(
        self,
        session: aiohttp.ClientSession,
        path: str,
        symbol: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            async with session.get(
                f"{self.BASE}{path}",
                params={"symbols": symbol},
                headers={"api-key": self.api_key},
            ) as resp:
                if resp.status != 200:
                    logger.warning("Coinalyze HTTP %s for %s %s", resp.status, path, symbol)
                    return None
                data = await resp.json()
                if isinstance(data, list) and data:
                    return data[0]
                return None
        except Exception as exc:
            logger.warning("Coinalyze %s %s error: %s", path, symbol, exc)
            return None

    async def fetch(self, session: aiohttp.ClientSession, symbol: str) -> Optional[FundingOI]:
        """symbol: BTCUSDT_PERP.A (include exchange code, e.g. .A = Binance)."""
        funding_row, predicted_row, oi_row = await asyncio.gather(
            self._get(session, "/funding-rate", symbol),
            self._get(session, "/predicted-funding-rate", symbol),
            self._get(session, "/open-interest", symbol),
        )
        if funding_row is None and predicted_row is None and oi_row is None:
            return None
        funding = float(funding_row["value"]) if funding_row and "value" in funding_row else None
        predicted = float(predicted_row["value"]) if predicted_row and "value" in predicted_row else None
        oi_value = float(oi_row["value"]) if oi_row and "value" in oi_row else None
        ts_ms = int((funding_row or predicted_row or oi_row or {}).get("update", 0))
        return FundingOI(
            symbol=symbol.replace("USDT_PERP", "").replace("-PERP", "").split(".")[0],
            exchange="coinalyze",
            funding_rate=funding,
            predicted_funding=predicted,
            open_interest=oi_value,
            timestamp_ms=ts_ms,
        )


class BinanceFundingClient:
    """Binance public API for funding rates."""
    BASE = "https://fapi.binance.com/fapi/v1"

    async def fetch(self, session: aiohttp.ClientSession, symbol: str) -> Optional[FundingOI]:
        """symbol: BTCUSDT"""
        try:
            # Funding rate
            async with session.get(
                f"{self.BASE}/fundingRate",
                params={"symbol": symbol, "limit": 1}
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not data:
                    return None
                latest = data[0]
                funding = float(latest.get("fundingRate", 0))
                funding_time = latest.get("fundingTime", 0)

            # Open interest
            async with session.get(
                f"{self.BASE}/openInterest",
                params={"symbol": symbol}
            ) as resp:
                oi_data = await resp.json()
                oi = float(oi_data.get("openInterest", 0)) if oi_data else None

            # Mark price (for OI valuation)
            async with session.get(
                f"{self.BASE}/premiumIndex",
                params={"symbol": symbol}
            ) as resp:
                mp_data = await resp.json()
                mark = float(mp_data.get("markPrice", 0)) if mp_data else None

            # Predicted funding (from premium index)
            predicted = None
            if mp_data:
                predicted = float(mp_data.get("lastFundingRate", 0))

            return FundingOI(
                symbol=symbol.replace("USDT", ""),
                exchange="binance",
                funding_rate=funding,
                predicted_funding=predicted,
                open_interest=oi * mark if (oi and mark) else oi,
                mark_price=mark,
                timestamp_ms=funding_time,
                exchange_timestamp_ms=funding_time,
            )
        except Exception as e:
            logger.warning("Binance funding fetch failed for %s: %s", symbol, e)
            return None


class BybitFundingClient:
    """Bybit public API v5 for funding rates."""
    BASE = "https://api.bybit.com/v5/market"

    async def fetch(self, session: aiohttp.ClientSession, symbol: str) -> Optional[FundingOI]:
        """symbol: BTCUSDT"""
        try:
            # Funding rate history (latest)
            async with session.get(
                f"{self.BASE}/funding/history",
                params={"category": "linear", "symbol": symbol, "limit": 1}
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data is None:
                    return None
                result = data.get("result", {}).get("list", [])
                if not result:
                    return None
                latest = result[0]
                funding = float(latest.get("fundingRate", 0))
                funding_time = int(latest.get("fundingRateTimestamp", 0))

            mark: Optional[float] = None
            async with session.get(
                f"{self.BASE}/tickers",
                params={"category": "linear", "symbol": symbol},
            ) as resp:
                ticker_data = await resp.json()
                if ticker_data and ticker_data.get("retCode") == 0:
                    ticker_list = ticker_data.get("result", {}).get("list", [])
                    if ticker_list:
                        mark = float(ticker_list[0].get("markPrice", 0))

            oi_usd: Optional[float] = None
            async with session.get(
                f"{self.BASE}/open-interest",
                params={
                    "category": "linear",
                    "symbol": symbol,
                    "intervalTime": "5min",
                    "limit": 1,
                },
            ) as resp:
                oi_data = await resp.json()
                if oi_data and oi_data.get("retCode") == 0:
                    oi_list = oi_data.get("result", {}).get("list", [])
                    if oi_list:
                        row = oi_list[0]
                        oi_val = row.get("openInterestValue") or row.get(
                            "singleOpenInterestValue"
                        )
                        if oi_val is not None:
                            oi_usd = float(oi_val)
                        else:
                            oi_coin = float(row.get("openInterest", 0))
                            oi_usd = oi_coin * mark if (oi_coin and mark) else oi_coin

            return FundingOI(
                symbol=symbol.replace("USDT", ""),
                exchange="bybit",
                funding_rate=funding,
                open_interest=oi_usd,
                mark_price=mark,
                timestamp_ms=funding_time,
                exchange_timestamp_ms=funding_time,
            )
        except Exception as e:
            logger.warning("Bybit funding fetch failed for %s: %s", symbol, e)
            return None


class OKXFundingClient:
    """OKX public API for funding rates."""
    BASE = "https://www.okx.com/api/v5/public"

    async def fetch(self, session: aiohttp.ClientSession, symbol: str) -> Optional[FundingOI]:
        """symbol: BTC-USDT-SWAP"""
        try:
            # Funding rate
            async with session.get(
                f"{self.BASE}/funding-rate",
                params={"instId": symbol}
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                result = data.get("data", [])
                if not result:
                    return None
                latest = result[0]
                funding = float(latest.get("fundingRate", 0))
                funding_time = int(latest.get("fundingTime", "").replace("-", "")) if latest.get("fundingTime") else 0

            # Open interest
            async with session.get(
                f"{self.BASE}/open-interest",
                params={"instType": "SWAP", "instId": symbol}
            ) as resp:
                oi_data = await resp.json()
                oi_list = oi_data.get("data", [])
                oi = float(oi_list[0].get("oi", 0)) if oi_list else None
                oi_ccy = oi_list[0].get("oiCcy", "") if oi_list else ""

            # Mark price
            async with session.get(
                f"{self.BASE}/mark-price",
                params={"instType": "SWAP", "instId": symbol}
            ) as resp:
                mp_data = await resp.json()
                mp_list = mp_data.get("data", [])
                mark = float(mp_list[0].get("markPx", 0)) if mp_list else None

            # OI is in coin units or USD — OKX returns both
            oi_usd = oi
            if oi and oi_ccy == "coin":
                # Convert to USD using mark price
                oi_usd = oi * mark if mark else oi

            return FundingOI(
                symbol=symbol.replace("-USDT-SWAP", ""),
                exchange="okx",
                funding_rate=funding,
                open_interest=oi_usd,
                mark_price=mark,
                timestamp_ms=funding_time,
                exchange_timestamp_ms=funding_time,
            )
        except Exception as e:
            logger.warning("OKX funding fetch failed for %s: %s", symbol, e)
            return None


# ── Aggregator ──

class FundingOIAggregator:
    """
    Aggregates funding + OI from multiple exchanges.
    Primary: Coinalyze (15+ exchanges aggregated)
    Fallback: Binance, Bybit, OKX (individual exchanges)
    Polls every 8 hours (funding rate interval).
    """

    SYMBOL_MAP = {
        "BTC": {
            "coinalyze": "BTCUSDT_PERP.A",
            "binance": "BTCUSDT",
            "bybit": "BTCUSDT",
            "okx": "BTC-USDT-SWAP",
        },
        "ETH": {
            "coinalyze": "ETHUSDT_PERP.A",
            "binance": "ETHUSDT",
            "bybit": "ETHUSDT",
            "okx": "ETH-USDT-SWAP",
        },
        "SOL": {
            "coinalyze": "SOLUSDT_PERP.A",
            "binance": "SOLUSDT",
            "bybit": "SOLUSDT",
            "okx": "SOL-USDT-SWAP",
        },
        "HYPE": {
            "coinalyze": "HYPEUSDT_PERP.A",
            "binance": "HYPEUSDT",
            "bybit": "HYPEUSDT",
            "okx": "HYPE-USDT-SWAP",
        },
    }

    CEX_EXCHANGES = ("binance", "bybit", "okx")

    def __init__(
        self,
        coinalyze_key: Optional[str] = None,
        *,
        stale_max_sec: float = 300.0,
        connect_timeout: float = 10.0,
        total_timeout: float = 25.0,
        max_retries: int = 3,
    ) -> None:
        self.clients: Dict[str, Any] = {}
        if coinalyze_key:
            self.clients["coinalyze"] = CoinalyzeClient(coinalyze_key)
        self.clients["binance"] = BinanceFundingClient()
        self.clients["bybit"] = BybitFundingClient()
        self.clients["okx"] = OKXFundingClient()
        self._cache: Dict[str, AggregatedFundingOI] = {}
        self._last_poll_ms: int = 0
        self._stale_max_sec = float(stale_max_sec)
        self._connect_timeout = float(connect_timeout)
        self._total_timeout = float(total_timeout)
        self._max_retries = int(max_retries)

    async def _fetch_with_retry(
        self,
        client: Any,
        session: aiohttp.ClientSession,
        mapped: str,
        max_retries: Optional[int] = None,
        base_delay: float = 1.0,
    ) -> Optional[FundingOI]:
        """Fetch with exponential backoff retry for transient errors."""
        retries = max_retries if max_retries is not None else self._max_retries
        for attempt in range(retries):
            try:
                result = await client.fetch(session, mapped)
                if result is not None:
                    return result
                # Result is None (soft failure) — retry once quickly
                if attempt < retries - 1:
                    await asyncio.sleep(base_delay * (2 ** attempt))
            except (aiohttp.ClientConnectorError, aiohttp.ServerTimeoutError, asyncio.TimeoutError) as exc:
                if attempt < retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "%s fetch %s transient error (attempt %d/%d): %s — retrying in %.1fs",
                        getattr(client, "__class__", "").__name__, mapped,
                        attempt + 1, retries, exc, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning(
                        "%s fetch %s failed after %d attempts: %s",
                        getattr(client, "__class__", "").__name__, mapped,
                        retries, exc,
                    )
            except Exception as exc:
                logger.warning(
                    "%s fetch %s unexpected error: %s",
                    getattr(client, "__class__", "").__name__, mapped, exc,
                )
                break
        return None

    def _build_aggregate(
        self,
        sym: str,
        by_exchange: Dict[str, FundingOI],
        failed: List[str],
        timestamp_ms: int,
        cache_insertion_ms: int = 0,
    ) -> AggregatedFundingOI:
        fundings = [f.funding_rate for f in by_exchange.values() if f.funding_rate is not None]
        predicted = [
            f.predicted_funding for f in by_exchange.values() if f.predicted_funding is not None
        ]
        ois = [f.open_interest for f in by_exchange.values() if f.open_interest is not None]

        funding_avg = sum(fundings) / len(fundings) if fundings else None
        predicted_avg = sum(predicted) / len(predicted) if predicted else None
        oi_total = sum(ois) if ois else None

        funding_weighted = None
        if fundings and ois:
            pairs = [
                (f.funding_rate, f.open_interest)
                for f in by_exchange.values()
                if f.funding_rate is not None and f.open_interest is not None
            ]
            total_oi = sum(oi for _, oi in pairs)
            if total_oi > 0:
                funding_weighted = sum(f * oi for f, oi in pairs) / total_oi

        coinalyze_data = by_exchange.get("coinalyze")
        if coinalyze_data:
            funding_avg = coinalyze_data.funding_rate
            funding_weighted = coinalyze_data.funding_rate
            predicted_avg = coinalyze_data.predicted_funding
            if coinalyze_data.open_interest is not None:
                oi_total = coinalyze_data.open_interest

        # v3.1.21: pick the most recent exchange-side timestamp so the
        # caller can age-check the underlying data (independent of
        # the local cache hit).
        ex_ts_ms = max(
            (f.exchange_timestamp_ms for f in by_exchange.values()
             if f.exchange_timestamp_ms > 0),
            default=0,
        )

        return AggregatedFundingOI(
            symbol=sym,
            funding_avg=funding_avg,
            funding_weighted=funding_weighted,
            predicted_funding_avg=predicted_avg,
            oi_total=oi_total,
            exchange_count=len(by_exchange),
            by_exchange=by_exchange,
            timestamp_ms=timestamp_ms,
            cache_insertion_ms=cache_insertion_ms or timestamp_ms,
            exchange_timestamp_ms=ex_ts_ms,
            stale=False,
            age_sec=0.0,
            exchange_age_sec=0.0,
            failed_exchanges=failed,
        )

    async def _poll_symbol(
        self,
        session: aiohttp.ClientSession,
        sym: str,
    ) -> Tuple[Optional[AggregatedFundingOI], List[str]]:
        """Fetch one symbol from Coinalyze (optional) + CEX in parallel."""
        mappings = self.SYMBOL_MAP.get(sym)
        if not mappings:
            return None, []

        by_exchange: Dict[str, FundingOI] = {}
        failed: List[str] = []
        fetch_tasks: List[Tuple[str, asyncio.Task]] = []

        if "coinalyze" in mappings and "coinalyze" in self.clients:
            client = self.clients["coinalyze"]
            mapped = mappings["coinalyze"]
            fetch_tasks.append(
                (
                    "coinalyze",
                    asyncio.create_task(self._fetch_with_retry(client, session, mapped)),
                )
            )

        for ex in self.CEX_EXCHANGES:
            if ex not in mappings:
                continue
            client = self.clients.get(ex)
            if client is None:
                continue
            mapped = mappings[ex]
            fetch_tasks.append(
                (ex, asyncio.create_task(self._fetch_with_retry(client, session, mapped))),
            )

        if fetch_tasks:
            names, tasks = zip(*fetch_tasks)
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            for ex_name, resp in zip(names, responses):
                if isinstance(resp, FundingOI):
                    _normalize_funding_oi(resp)
                    by_exchange[resp.exchange] = resp
                else:
                    failed.append(ex_name)

        if not by_exchange:
            return None, failed

        now_ms = int(time.time() * 1000)
        agg = self._build_aggregate(
            sym,
            by_exchange,
            failed,
            now_ms,
            cache_insertion_ms=now_ms,
        )
        return agg, failed

    async def poll(self, symbols: List[str]) -> Dict[str, AggregatedFundingOI]:
        """Fetch and aggregate funding/OI; serve stale cache on transient failures."""
        results: Dict[str, AggregatedFundingOI] = {}
        now_ms = int(time.time() * 1000)

        connector = aiohttp.TCPConnector(
            ttl_dns_cache=300,
            use_dns_cache=True,
            limit=30,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(
            total=self._total_timeout,
            connect=self._connect_timeout,
        )
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            sym_list = [s for s in symbols if s in self.SYMBOL_MAP]
            polled = await asyncio.gather(
                *[self._poll_symbol(session, sym) for sym in sym_list],
                return_exceptions=True,
            )

            for sym, item in zip(sym_list, polled):
                if isinstance(item, Exception):
                    logger.warning("Funding poll error for %s: %s", sym, item)
                    fresh = None
                    failed = ["poll_exception"]
                else:
                    fresh, failed = item

                if fresh is not None:
                    # v3.1.21: even on a "fresh" poll, the underlying
                    # exchange data may already be hours old (e.g. we
                    # just polled but the venue hasn't published a new
                    # funding tick since the last settlement). Flag
                    # that case here so downstream consumers know.
                    exchange_age_sec = 0.0
                    if fresh.exchange_timestamp_ms > 0:
                        exchange_age_sec = max(
                            0.0,
                            (now_ms - fresh.exchange_timestamp_ms) / 1000.0,
                        )
                    if exchange_age_sec > self._stale_max_sec:
                        logger.warning(
                            "Funding %s: exchange data is stale "
                            "(exchange_age=%.0fs > %.0fs, venues=%d)",
                            sym,
                            exchange_age_sec,
                            self._stale_max_sec,
                            fresh.exchange_count,
                        )
                        fresh = replace(
                            fresh,
                            stale=True,
                            exchange_age_sec=exchange_age_sec,
                        )
                    self._cache[sym] = fresh
                    results[sym] = fresh
                    if failed:
                        logger.info(
                            "Funding %s: OK (%d exchanges, partial fail: %s)",
                            sym,
                            fresh.exchange_count,
                            ",".join(failed),
                        )
                    continue

                cached = self._cache.get(sym)
                if cached is None:
                    logger.warning("Funding %s: no data and no cache", sym)
                    continue

                # v3.1.21: staleness = max(cache age, exchange age).
                # The previous code only looked at cache age, so a
                # row we cached 30s ago whose underlying exchange
                # tick is 4h old would have been reported as fresh.
                cache_age_sec = (
                    (now_ms - (cached.cache_insertion_ms or cached.timestamp_ms)) / 1000.0
                    if (cached.cache_insertion_ms or cached.timestamp_ms)
                    else 0.0
                )
                exchange_age_sec = 0.0
                if cached.exchange_timestamp_ms > 0:
                    exchange_age_sec = max(
                        0.0,
                        (now_ms - cached.exchange_timestamp_ms) / 1000.0,
                    )
                worst_age_sec = max(cache_age_sec, exchange_age_sec)

                if worst_age_sec > self._stale_max_sec:
                    logger.warning(
                        "Funding %s: poll failed and cache expired "
                        "(cache_age=%.0fs, exchange_age=%.0fs > %.0fs)",
                        sym,
                        cache_age_sec,
                        exchange_age_sec,
                        self._stale_max_sec,
                    )
                    continue

                stale_row = replace(
                    cached,
                    stale=True,
                    age_sec=cache_age_sec,
                    exchange_age_sec=exchange_age_sec,
                    failed_exchanges=failed,
                )
                self._cache[sym] = stale_row
                results[sym] = stale_row
                logger.warning(
                    "Funding %s: using STALE cache "
                    "(cache_age=%.0fs, exchange_age=%.0fs, last_exchanges=%d)",
                    sym,
                    cache_age_sec,
                    exchange_age_sec,
                    stale_row.exchange_count,
                )

        self._last_poll_ms = now_ms
        return results

    def get(self, symbol: str) -> Optional[AggregatedFundingOI]:
        return self._cache.get(symbol)


# ── Standalone test ──

async def main():
    logging.basicConfig(level=logging.INFO)
    agg = FundingOIAggregator()
    results = await agg.poll(["BTC", "ETH", "SOL"])
    for sym, data in results.items():
        print(f"\n{sym}:")
        print(f"  Funding avg: {data.funding_avg}")
        print(f"  Funding weighted (by OI): {data.funding_weighted}")
        print(f"  Predicted funding avg: {data.predicted_funding_avg}")
        print(f"  OI total: ${data.oi_total:,.0f}" if data.oi_total else "  OI total: N/A")
        print(f"  Exchanges: {data.exchange_count}")
        for ex, f in data.by_exchange.items():
            print(f"    {ex}: funding={f.funding_rate}, OI=${f.open_interest:,.0f}" if f.open_interest else f"    {ex}: funding={f.funding_rate}")


if __name__ == "__main__":
    asyncio.run(main())
