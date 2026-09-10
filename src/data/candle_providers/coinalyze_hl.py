"""Coinalyze ``/v1/ohlcv-history`` provider for native Hyperliquid perps.

Coinalyze lists Hyperliquid perp markets as ``{BASE}USDT_PERP.A`` (exchange
code ``A`` = Hyperliquid). Free with an API key (``COINALYZE_API_KEY`` — the
same key the funding/OI aggregator already uses). Measured depth on
2026-09-10: ~60-90 days of 1h history — deeper than ``candleSnapshot``'s
~5000-bar window for 1m/5m/15m, shallower than the retired GoldRush 180d.

Bonus fields the official HL snapshot does not have: ``bv`` (taker buy
volume, base units), ``tx``/``btx`` (trade counts). ``bv`` is emitted on the
row so the converter can populate ``buy_volume``/``sell_volume`` — real
aggressor flow for Q4/CVD research instead of NULL.
"""

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
    INTERVAL_MS,
    ProviderName,
)
from src.data.candle_providers.validation import validate_page_order
from src.utils.http import make_client_session

logger = logging.getLogger(__name__)

COINALYZE_BASE_URL = "https://api.coinalyze.net/v1"
COINALYZE_API_VERSION = "coinalyze-v1"
DEFAULT_MAX_RPS = 4.0  # free tier is conservative; keep headroom
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 1.0

# Coinalyze interval labels differ from the repo's (1m -> 1min, 1h -> 1hour)
INTERVAL_TO_COINALYZE: Dict[str, str] = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1hour",
    "4h": "4hour",
    "1d": "daily",
}


class CoinalyzeConfigError(CandleProviderError):
    """Missing or invalid Coinalyze configuration."""


class CoinalyzeAPIError(CandleProviderError):
    """Coinalyze HTTP/API failure."""


def coinalyze_symbol(symbol: str) -> str:
    """HL base symbol -> Coinalyze perpetual id (``A`` = Hyperliquid)."""
    return f"{symbol.upper()}USDT_PERP.A"


class CoinalyzeCandleProvider(CandleProvider):
    """Coinalyze OHLCV history — ``api_key`` header auth, HL-native venue."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = COINALYZE_BASE_URL,
        max_requests_per_second: float = DEFAULT_MAX_RPS,
    ) -> None:
        key = (api_key if api_key is not None else os.environ.get("COINALYZE_API_KEY", "")).strip()
        if not key:
            raise CoinalyzeConfigError(
                "COINALYZE_API_KEY environment variable is required for Coinalyze provider",
            )
        self._api_key = key
        self._base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._tokens = max_requests_per_second
        self._max_tokens = max_requests_per_second
        self._last_token_update = time.monotonic()
        self._rate_lock = asyncio.Lock()

    @property
    def name(self) -> ProviderName:
        return "coinalyze_hl"

    @property
    def api_version(self) -> str:
        return COINALYZE_API_VERSION

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = make_client_session(
                headers={"api_key": self._api_key},
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
            raise CoinalyzeAPIError("Session not connected — call connect() first")
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
                            "Coinalyze rate limited (429) — sleeping %.1fs (attempt %d)",
                            retry_after, attempt + 1,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status in (401, 403):
                        raise CoinalyzeAPIError(
                            f"Coinalyze auth error {resp.status}: {body[:200]}",
                        )
                    if resp.status >= 500:
                        raise CoinalyzeAPIError(f"Coinalyze server error {resp.status}")
                    if resp.status >= 400:
                        raise CoinalyzeAPIError(
                            f"Coinalyze client error {resp.status}: {body[:200]}",
                        )
                    return await resp.json(content_type=None)
            except CoinalyzeAPIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt >= MAX_RETRIES:
                    break
                delay = RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "Coinalyze request failed (%s) — retry %d/%d in %.1fs",
                    type(exc).__name__, attempt + 1, MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
        raise CoinalyzeAPIError(
            f"Coinalyze GET failed after {MAX_RETRIES + 1} attempts: {last_exc}",
        ) from last_exc

    async def fetch_page(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> CandlePage:
        """Fetch candles for [start_ms, end_ms]; emits HL-wire-shaped rows.

        Row shape mirrors ``candleSnapshot`` (t/T in ms, o/h/l/c/v as
        strings) plus ``bv`` (taker buy volume) and ``btx`` (buy trade
        count) when Coinalyze provides them — the converter downstream maps
        bv -> buy_volume / sell_volume.
        """
        iv = INTERVAL_TO_COINALYZE.get(interval)
        if iv is None:
            raise CoinalyzeAPIError(f"unsupported interval {interval!r}")
        gap_ms = INTERVAL_MS[interval]
        data = await self._get("/ohlcv-history", {
            "symbols": coinalyze_symbol(symbol),
            "interval": iv,
            # Coinalyze uses unix SECONDS
            "from": int(start_ms // 1000),
            "to": int(end_ms // 1000),
        })
        history: List[Dict[str, Any]] = []
        if isinstance(data, list) and data:
            history = list(data[0].get("history") or [])

        rows: List[Dict[str, Any]] = []
        for h in history:
            t_open = int(h["t"]) * 1000          # seconds -> ms
            row: Dict[str, Any] = {
                "t": t_open,
                "T": t_open + gap_ms - 1,         # close ms, HL convention
                "s": symbol.upper(),
                "i": interval,
                "o": str(h.get("o", "")),
                "h": str(h.get("h", "")),
                "l": str(h.get("l", "")),
                "c": str(h.get("c", "")),
                "v": str(h.get("v", "")),
                "n": int(h.get("tx", 0) or 0),
            }
            if h.get("bv") is not None:
                row["bv"] = str(h["bv"])
            if h.get("btx") is not None:
                row["btx"] = int(h["btx"])
            rows.append(row)

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
