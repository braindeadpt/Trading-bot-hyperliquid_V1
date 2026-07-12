"""Hyperliquid REST API client with rate limiting and retries."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

from src.exchanges.hl_predicted_funding import (
    HlSymbolFundingSnapshot,
    HyperliquidPredictedFundingClient,
    parse_predicted_fundings_response,
)

logger = logging.getLogger(__name__)

# Endpoints
INFO_URL = "https://api.hyperliquid.xyz/info"
EXCHANGE_URL = "https://api.hyperliquid.xyz/exchange"
TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"
TESTNET_EXCHANGE_URL = "https://api.hyperliquid-testnet.xyz/exchange"

# Limits
MAX_REQUESTS_PER_SECOND = 5.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0


@dataclass(frozen=True, slots=True)
class HlFundingRate:
    """Per-asset funding rate snapshot."""

    symbol: str
    funding_rate: float
    timestamp_ms: int


class HyperliquidRESTClient:
    """Async REST client for Hyperliquid info + order placement.

    A token-bucket style rate limiter enforces **max 5 req/s**.  All
    public ``info`` calls return parsed dataclasses; order placement
    returns the raw server response for the caller to interpret.
    """

    def __init__(
        self,
        use_testnet: bool = False,
        max_requests_per_second: float = MAX_REQUESTS_PER_SECOND,
    ) -> None:
        self._info_url = TESTNET_INFO_URL if use_testnet else INFO_URL
        self._exchange_url = TESTNET_EXCHANGE_URL if use_testnet else EXCHANGE_URL

        self._session: Optional[aiohttp.ClientSession] = None
        self._tokens = max_requests_per_second
        self._max_tokens = max_requests_per_second
        self._last_update = time.monotonic()
        self._rate_lock = asyncio.Lock()

    # ── Lifecycle ──

    async def open(self) -> None:
        """Create the aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            )

    async def close(self) -> None:
        """Close the underlying session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "HyperliquidRESTClient":
        await self.open()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ── Rate limiter (token bucket) ──

    async def _acquire_token(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_update
            self._last_update = now
            self._tokens = min(self._max_tokens, self._tokens + elapsed * self._max_tokens)
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._max_tokens
                logger.debug("Rate limit: sleeping %.3fs", wait)
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0

    # ── Low-level request ──

    async def _post(
        self,
        url: str,
        payload: Dict[str, Any],
        retries: int = MAX_RETRIES,
    ) -> Any:
        """POST *payload* to *url* with rate limiting + exponential back-off."""
        if self._session is None:
            raise RuntimeError("Session not open — call .open() first")

        last_exception: Optional[Exception] = None
        for attempt in range(retries + 1):
            await self._acquire_token()
            try:
                async with self._session.post(url, json=payload) as resp:
                    text = await resp.text()
                    if resp.status == 429:
                        logger.warning("Rate limited (429) — backing off")
                        raise aiohttp.ClientError("Rate limited")
                    resp.raise_for_status()
                    return json.loads(text)
            except aiohttp.ClientError as exc:
                last_exception = exc
                if attempt < retries:
                    backoff = RETRY_BACKOFF_BASE * (2 ** attempt)
                    logger.warning("Request failed (%s), retry %d/%d in %.1fs", exc, attempt + 1, retries, backoff)
                    await asyncio.sleep(backoff)
                else:
                    break
            except Exception as exc:  # noqa: BLE001
                last_exception = exc
                if attempt < retries:
                    await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                else:
                    break

        raise HyperliquidAPIError(
            f"POST failed after {retries + 1} attempts: {last_exception}"
        ) from last_exception

    # ── Public info endpoints ──

    async def all_mids(self) -> Dict[str, float]:
        """Return a mapping ``{symbol: mid_price}``."""
        raw = await self._post(self._info_url, {"type": "allMids"})
        if not isinstance(raw, dict):
            raise HyperliquidAPIError(f"Unexpected allMids response: {raw!r}")
        return {k: _safe_float(v) for k, v in raw.items()}

    async def meta_and_asset_ctxs(self) -> Dict[str, Any]:
        """Return raw ``metaAndAssetCtxs`` payload.

        The response contains ``universe`` (asset metadata) and
        ``assetCtxs`` (per-asset mark price, OI, funding, etc.).
        """
        raw = await self._post(self._info_url, {"type": "metaAndAssetCtxs"})
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            return raw[0]
        if isinstance(raw, dict):
            return raw
        raise HyperliquidAPIError(f"Unexpected metaAndAssetCtxs response: {type(raw)!r}")

    async def funding_rates(self) -> List[HlFundingRate]:
        """Return the most recent funding rates for all assets."""
        raw = await self._post(self._info_url, {"type": "fundingRates"})
        if not isinstance(raw, list):
            raise HyperliquidAPIError(f"Unexpected fundingRates response: {raw!r}")
        ts = int(time.time() * 1000)
        out: List[HlFundingRate] = []
        for item in raw:
            try:
                out.append(
                    HlFundingRate(
                        symbol=str(item["coin"]),
                        funding_rate=_safe_float(item["fundingRate"]),
                        timestamp_ms=ts,
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.debug("Skipping malformed funding rate entry: %s", exc)
                continue
        return out

    async def predicted_fundings(
        self,
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, HlSymbolFundingSnapshot]:
        """Cross-venue predicted funding (HL INFO ``predictedFundings``)."""
        raw = await self._post(self._info_url, {"type": "predictedFundings"})
        parsed = parse_predicted_fundings_response(raw)
        if symbols is None:
            return parsed
        want = set(symbols)
        return {k: v for k, v in parsed.items() if k in want}

    async def l2_book(self, symbol: str) -> Dict[str, Any]:
        """Return the L2 orderbook for a single asset.

        Response shape::
            {"coin": "BTC", "levels": [[["bid_px", "bid_sz"], ...], [["ask_px", "ask_sz"], ...]]}
        """
        raw = await self._post(self._info_url, {"type": "l2Book", "coin": symbol})
        if not isinstance(raw, dict):
            raise HyperliquidAPIError(f"Unexpected l2Book response: {raw!r}")
        return raw

    async def candle_snapshot(
        self,
        coin: str,
        interval: str,
        start_time: int,
        end_time: int,
    ) -> List[Dict[str, Any]]:
        """Historical OHLCV via ``candleSnapshot`` (max 5000 candles per call)."""
        raw = await self._post(
            self._info_url,
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": start_time,
                    "endTime": end_time,
                },
            },
        )
        if not isinstance(raw, list):
            raise HyperliquidAPIError(f"Unexpected candleSnapshot response: {raw!r}")
        return raw

    async def recent_trades(self, coin: str) -> List[Dict[str, Any]]:
        """Most recent public trades for *coin* (venue tape sample)."""
        raw = await self._post(
            self._info_url,
            {"type": "recentTrades", "coin": coin},
        )
        if not isinstance(raw, list):
            raise HyperliquidAPIError(f"Unexpected recentTrades response: {raw!r}")
        return raw

    # ── Order placement ──

    async def place_order(
        self,
        action: Dict[str, Any],
        signature: Dict[str, Any],
        nonce: int,
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit an order (or bundle) to the exchange.

        *action* and *signature* are exactly the shapes expected by the
        Hyperliquid ``/exchange`` endpoint.  This method is intentionally
        thin — signing & order construction live in the execution layer.
        """
        payload: Dict[str, Any] = {
            "action": action,
            "signature": signature,
            "nonce": nonce,
        }
        if vault_address is not None:
            payload["vaultAddress"] = vault_address
        return await self._post(self._exchange_url, payload)

    # ── Health / utility ──

    async def health_check(self) -> bool:
        """Quick sanity check — try ``allMids``."""
        try:
            await self.all_mids()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Health check failed: %s", exc)
            return False


class HyperliquidAPIError(Exception):
    """Raised when a REST call exhausts retries or returns unexpected data."""


# ────────────────────────────────
# Helpers
# ────────────────────────────────


def _safe_float(value: Any) -> float:
    if value is None:
        raise ValueError("value is None")
    return float(value)
