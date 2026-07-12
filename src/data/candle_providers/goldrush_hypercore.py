"""GoldRush HyperCore drop-in ``/info`` candle provider."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp

from src.data.candle_providers.base import (
    CandlePage,
    CandleProvider,
    CandleProviderError,
    MAX_CANDLES_PER_PAGE,
    ProviderName,
)
from src.data.candle_providers.validation import validate_page_order

logger = logging.getLogger(__name__)

GOLDRUSH_INFO_URL = "https://hypercore.goldrushdata.com/info"
GOLDRUSH_API_VERSION = "hypercore-info-v1"
DEFAULT_MAX_RPS = 10.0
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 1.0


class GoldrushConfigError(CandleProviderError):
    """Missing or invalid GoldRush configuration."""


class GoldrushAPIError(CandleProviderError):
    """GoldRush HTTP/API failure."""


class GoldrushHypercoreCandleProvider(CandleProvider):
    """GoldRush HyperCore historical store — Bearer auth via ``GOLDRUSH_API_KEY``."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        info_url: str = GOLDRUSH_INFO_URL,
        max_requests_per_second: float = DEFAULT_MAX_RPS,
    ) -> None:
        key = (api_key if api_key is not None else os.environ.get("GOLDRUSH_API_KEY", "")).strip()
        if not key:
            raise GoldrushConfigError(
                "GOLDRUSH_API_KEY environment variable is required for GoldRush provider",
            )
        self._api_key = key
        self._info_url = info_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._tokens = max_requests_per_second
        self._max_tokens = max_requests_per_second
        self._last_token_update = time.monotonic()
        self._rate_lock = asyncio.Lock()

    @property
    def name(self) -> ProviderName:
        return "goldrush_hypercore"

    @property
    def api_version(self) -> str:
        return GOLDRUSH_API_VERSION

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
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

    async def _post(self, payload: Dict[str, Any]) -> Any:
        if self._session is None:
            raise GoldrushAPIError("Session not connected — call connect() first")
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES + 1):
            await self._acquire_token()
            try:
                async with self._session.post(self._info_url, json=payload) as resp:
                    body = await resp.text()
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", RETRY_BACKOFF_BASE))
                        logger.warning(
                            "GoldRush rate limited (429) — sleeping %.1fs (attempt %d)",
                            retry_after,
                            attempt + 1,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status == 402:
                        raise GoldrushAPIError(
                            f"GoldRush billing error 402: {body[:200]}",
                        )
                    if resp.status >= 500:
                        raise GoldrushAPIError(
                            f"GoldRush server error {resp.status}",
                        )
                    if resp.status >= 400:
                        raise GoldrushAPIError(
                            f"GoldRush client error {resp.status}: {body[:200]}",
                        )
                    return await resp.json(content_type=None)
            except GoldrushAPIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt >= MAX_RETRIES:
                    break
                delay = RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "GoldRush request failed (%s) — retry %d/%d in %.1fs",
                    type(exc).__name__,
                    attempt + 1,
                    MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
        raise GoldrushAPIError(
            f"GoldRush POST failed after {MAX_RETRIES + 1} attempts: {last_exc}",
        ) from last_exc

    async def fetch_page(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> CandlePage:
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol.upper(),
                "interval": interval,
                "startTime": int(start_ms),
                "endTime": int(end_ms),
            },
        }
        raw = await self._post(payload)
        if isinstance(raw, dict) and raw.get("error"):
            raise GoldrushAPIError(f"GoldRush API error: {raw}")
        if not isinstance(raw, list):
            raise GoldrushAPIError(f"Unexpected candleSnapshot response type: {type(raw)}")
        if len(raw) > MAX_CANDLES_PER_PAGE:
            raise GoldrushAPIError(
                f"Page exceeded {MAX_CANDLES_PER_PAGE} candles ({len(raw)})",
            )
        if raw:
            validate_page_order(raw, interval=interval)
        return CandlePage(
            rows=raw,
            symbol=symbol.upper(),
            interval=interval,
            request_start_ms=int(start_ms),
            request_end_ms=int(end_ms),
            provider=self.name,
        )
