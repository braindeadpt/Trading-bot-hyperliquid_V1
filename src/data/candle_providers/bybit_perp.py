"""Bybit USD-M perp ``/v5/market/kline`` provider — deep-history PROXY.

Free, no auth, full listing history (HYPEUSDT verified back to 2026-01).
This is a **cross-venue proxy**: prices/volume are Bybit's, not
Hyperliquid's — for thin-book symbols like HYPE the divergence can be
material. Series written through this provider carry
``venue="bybit"`` + ``source="bybit_klines"`` provenance and must never be
treated as HL venue-of-record data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

from src.data.candle_providers.base import (
    CandlePage,
    CandleProvider,
    CandleProviderError,
    INTERVAL_MS,
    ProviderName,
)
from src.data.candle_providers.validation import validate_page_order
from src.utils.http import make_client_session

logger = logging.getLogger(__name__)

BYBIT_BASE_URL = "https://api.bybit.com"
BYBIT_API_VERSION = "bybit-v5"
DEFAULT_MAX_RPS = 5.0
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 1.0

INTERVAL_TO_BYBIT: Dict[str, str] = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}


class BybitAPIError(CandleProviderError):
    """Bybit HTTP/API failure."""


class BybitPerpCandleProvider(CandleProvider):
    """Bybit linear perp klines — no auth needed."""

    def __init__(
        self,
        *,
        base_url: str = BYBIT_BASE_URL,
        max_requests_per_second: float = DEFAULT_MAX_RPS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._tokens = max_requests_per_second
        self._max_tokens = max_requests_per_second
        self._last_token_update = time.monotonic()
        self._rate_lock = asyncio.Lock()

    @property
    def name(self) -> ProviderName:
        return "bybit_perp"

    @property
    def api_version(self) -> str:
        return BYBIT_API_VERSION

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = make_client_session(
                timeout=aiohttp.ClientTimeout(total=60),
            )

    async def disconnect(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _acquire_token(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_token_update
            self._last_token_update = now
            self._tokens = min(self._max_tokens, self._tokens + elapsed * self._max_tokens)
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._max_tokens
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0

    async def _get(self, path: str, params: Dict[str, Any]) -> Any:
        if self._session is None:
            raise BybitAPIError("Session not connected — call connect() first")
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES + 1):
            await self._acquire_token()
            try:
                async with self._session.get(
                    f"{self._base_url}{path}", params=params,
                ) as resp:
                    body = await resp.text()
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", RETRY_BACKOFF_BASE))
                        logger.warning(
                            "Bybit rate limited (429) — sleeping %.1fs (attempt %d)",
                            retry_after, attempt + 1,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status >= 500:
                        raise BybitAPIError(f"Bybit server error {resp.status}")
                    if resp.status >= 400:
                        raise BybitAPIError(
                            f"Bybit client error {resp.status}: {body[:200]}",
                        )
                    data = await resp.json(content_type=None)
                    if int(data.get("retCode", -1)) != 0:
                        raise BybitAPIError(
                            f"Bybit retCode {data.get('retCode')}: {data.get('retMsg')}",
                        )
                    return data
            except BybitAPIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt >= MAX_RETRIES:
                    break
                delay = RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "Bybit request failed (%s) — retry %d/%d in %.1fs",
                    type(exc).__name__, attempt + 1, MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
        raise BybitAPIError(
            f"Bybit GET failed after {MAX_RETRIES + 1} attempts: {last_exc}",
        ) from last_exc

    async def fetch_page(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> CandlePage:
        """Fetch one page; emits HL-wire-shaped rows (proxy provenance).

        Bybit returns newest-first ``[start_ms, o, h, l, c, volume,
        turnover]``; we reverse to oldest-first. No taker-buy split —
        ``bv`` is absent so downstream buy/sell stay NULL.
        """
        iv = INTERVAL_TO_BYBIT.get(interval)
        if iv is None:
            raise BybitAPIError(f"unsupported interval {interval!r}")
        gap_ms = INTERVAL_MS[interval]
        data = await self._get("/v5/market/kline", {
            "category": "linear",
            "symbol": f"{symbol.upper()}USDT",
            "interval": iv,
            "start": int(start_ms),
            "end": int(end_ms),
            "limit": 1000,
        })
        raw: List[List[str]] = list((data.get("result") or {}).get("list") or [])

        rows: List[Dict[str, Any]] = []
        for k in reversed(raw):  # newest-first -> oldest-first
            t_open = int(k[0])
            rows.append({
                "t": t_open,
                "T": t_open + gap_ms - 1,
                "s": symbol.upper(),
                "i": interval,
                "o": str(k[1]),
                "h": str(k[2]),
                "l": str(k[3]),
                "c": str(k[4]),
                "v": str(k[5]),
                "n": 0,
            })
        if rows:
            validate_page_order(rows, interval=interval)
        return CandlePage(
            rows=rows,
            symbol=symbol.upper(),
            interval=interval,
            request_start_ms=int(start_ms),
            request_end_ms=int(end_ms),
            provider=self.name,
        )
