"""
Coinalyze-like aggregator: fetches funding rates + open interest
from multiple free exchanges (Binance, Bybit, OKX) and aggregates.

No API key required. Uses public REST endpoints.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp

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


# ── Exchange clients ──

class CoinalyzeClient:
    """Coinalyze API — aggregated funding + OI from 15+ exchanges."""
    BASE = "https://api.coinalyze.net/v1"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch(self, session: aiohttp.ClientSession, symbol: str) -> Optional[FundingOI]:
        """symbol: BTCUSDT_PERP"""
        try:
            async with session.get(
                f"{self.BASE}/futures/funding/latest",
                params={"symbols": symbol},
                headers={"api-key": self.api_key},
            ) as resp:
                if resp.status != 200:
                    logger.warning("Coinalyze HTTP %s for %s", resp.status, symbol)
                    return None
                data = await resp.json()
                if not data or not data.get("success"):
                    return None
                result = data.get("result", [])
                if not result:
                    return None
                latest = result[0]
                funding = float(latest.get("fundingRate", 0))
                predicted = float(latest.get("predictedFundingRate", 0)) if latest.get("predictedFundingRate") else None
                oi_value = float(latest.get("oiValue", 0))  # OI in USD
                oi_coin = float(latest.get("openInterest", 0))  # OI in coins
                mark = float(latest.get("markPrice", 0))
                funding_time = int(latest.get("fundingTime", 0)) * 1000  # Convert to ms

                return FundingOI(
                    symbol=symbol.replace("USDT_PERP", "").replace("-PERP", ""),
                    exchange="coinalyze",
                    funding_rate=funding,
                    predicted_funding=predicted,
                    open_interest=oi_value,  # Already in USD
                    mark_price=mark,
                    timestamp_ms=funding_time,
                )
        except Exception as e:
            logger.warning("Coinalyze fetch failed for %s: %s", symbol, e)
            return None


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
                result = data.get("result", {}).get("list", [])
                if not result:
                    return None
                latest = result[0]
                funding = float(latest.get("fundingRate", 0))
                funding_time = int(latest.get("fundingRateTimestamp", 0))

            # Open interest
            async with session.get(
                f"{self.BASE}/recent-open-interest",
                params={"category": "linear", "symbol": symbol, "limit": 1}
            ) as resp:
                oi_data = await resp.json()
                oi_list = oi_data.get("result", {}).get("list", [])
                oi = float(oi_list[0].get("openInterest", 0)) if oi_list else None

            # Mark price
            async with session.get(
                f"{self.BASE}/tickers",
                params={"category": "linear", "symbol": symbol}
            ) as resp:
                ticker_data = await resp.json()
                ticker_list = ticker_data.get("result", {}).get("list", [])
                mark = float(ticker_list[0].get("markPrice", 0)) if ticker_list else None

            return FundingOI(
                symbol=symbol.replace("USDT", ""),
                exchange="bybit",
                funding_rate=funding,
                open_interest=oi * mark if (oi and mark) else oi,
                mark_price=mark,
                timestamp_ms=funding_time,
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
            "coinalyze": "BTCUSDT_PERP",
            "binance": "BTCUSDT",
            "bybit": "BTCUSDT",
            "okx": "BTC-USDT-SWAP",
        },
        "ETH": {
            "coinalyze": "ETHUSDT_PERP",
            "binance": "ETHUSDT",
            "bybit": "ETHUSDT",
            "okx": "ETH-USDT-SWAP",
        },
        "SOL": {
            "coinalyze": "SOLUSDT_PERP",
            "binance": "SOLUSDT",
            "bybit": "SOLUSDT",
            "okx": "SOL-USDT-SWAP",
        },
    }

    def __init__(self, coinalyze_key: Optional[str] = None):
        self.clients: Dict[str, Any] = {}
        if coinalyze_key:
            self.clients["coinalyze"] = CoinalyzeClient(coinalyze_key)
        self.clients["binance"] = BinanceFundingClient()
        self.clients["bybit"] = BybitFundingClient()
        self.clients["okx"] = OKXFundingClient()
        self._cache: Dict[str, AggregatedFundingOI] = {}
        self._last_poll_ms: int = 0

    async def poll(self, symbols: List[str]) -> Dict[str, AggregatedFundingOI]:
        """Fetch and aggregate funding/OI for given symbols."""
        results: Dict[str, AggregatedFundingOI] = {}

        async with aiohttp.ClientSession() as session:
            for sym in symbols:
                mappings = self.SYMBOL_MAP.get(sym)
                if not mappings:
                    continue

                by_exchange: Dict[str, FundingOI] = {}
                tasks = []
                
                # Try Coinalyze first (aggregated 15+ exchanges)
                if "coinalyze" in mappings and "coinalyze" in self.clients:
                    client = self.clients["coinalyze"]
                    mapped = mappings["coinalyze"]
                    coinalyze_result = await client.fetch(session, mapped)
                    if coinalyze_result:
                        by_exchange["coinalyze"] = coinalyze_result
                        logger.info("Coinalyze %s: funding=%.6f, predicted=%s, OI=$%.0f",
                                    sym, coinalyze_result.funding_rate or 0,
                                    f"{coinalyze_result.predicted_funding:.6f}" if coinalyze_result.predicted_funding else "N/A",
                                    coinalyze_result.open_interest or 0)

                # Fallback to individual exchanges if Coinalyze failed
                if not by_exchange:
                    for ex, mapped in mappings.items():
                        if ex == "coinalyze":
                            continue
                        client = self.clients.get(ex)
                        if client:
                            tasks.append(client.fetch(session, mapped))

                    responses = await asyncio.gather(*tasks, return_exceptions=True)
                    for resp in responses:
                        if isinstance(resp, FundingOI):
                            by_exchange[resp.exchange] = resp

                # Aggregate
                if not by_exchange:
                    continue

                fundings = [f.funding_rate for f in by_exchange.values() if f.funding_rate is not None]
                predicted = [f.predicted_funding for f in by_exchange.values() if f.predicted_funding is not None]
                ois = [f.open_interest for f in by_exchange.values() if f.open_interest is not None]

                funding_avg = sum(fundings) / len(fundings) if fundings else None
                predicted_avg = sum(predicted) / len(predicted) if predicted else None
                oi_total = sum(ois) if ois else None

                # Weighted funding by OI
                funding_weighted = None
                if fundings and ois and len(fundings) == len(ois):
                    total_oi = sum(ois)
                    if total_oi > 0:
                        funding_weighted = sum(f * oi for f, oi in zip(fundings, ois)) / total_oi

                # If Coinalyze is present, use its values as primary (already aggregated)
                coinalyze_data = by_exchange.get("coinalyze")
                if coinalyze_data:
                    funding_avg = coinalyze_data.funding_rate
                    funding_weighted = coinalyze_data.funding_rate
                    predicted_avg = coinalyze_data.predicted_funding
                    oi_total = coinalyze_data.open_interest

                results[sym] = AggregatedFundingOI(
                    symbol=sym,
                    funding_avg=funding_avg,
                    funding_weighted=funding_weighted,
                    predicted_funding_avg=predicted_avg,
                    oi_total=oi_total,
                    exchange_count=len(by_exchange),
                    by_exchange=by_exchange,
                )

        self._cache = results
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
