"""Hyperliquid public ``/info`` candleSnapshot provider."""

from __future__ import annotations

from typing import Any, Dict, List

from src.data.candle_providers.base import (
    CandlePage,
    CandleProvider,
    CandleProviderError,
    ProviderName,
)
from src.data.candle_providers.validation import validate_page_order
from src.exchanges.hyperliquid_rest import HyperliquidAPIError, HyperliquidRESTClient


class HyperliquidPublicCandleProvider(CandleProvider):
    """Official ``api.hyperliquid.xyz/info`` — no auth, ~5000 recent bars per call."""

    def __init__(self, *, use_testnet: bool = False) -> None:
        self._client = HyperliquidRESTClient(use_testnet=use_testnet)
        self._owns_client = True

    @property
    def name(self) -> ProviderName:
        return "hyperliquid_public"

    async def connect(self) -> None:
        await self._client.open()

    async def disconnect(self) -> None:
        if self._owns_client:
            await self._client.close()

    async def fetch_page(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> CandlePage:
        try:
            rows: List[Dict[str, Any]] = await self._client.candle_snapshot(
                symbol.upper(), interval, int(start_ms), int(end_ms),
            )
        except HyperliquidAPIError as exc:
            raise CandleProviderError(str(exc)) from exc
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
