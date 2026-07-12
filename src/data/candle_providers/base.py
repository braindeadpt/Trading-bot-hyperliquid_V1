"""Abstract candle provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

ProviderName = Literal["hyperliquid_public", "goldrush_hypercore"]

INTERVAL_MS: Dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}

MAX_CANDLES_PER_PAGE = 5000


class CandleProviderError(RuntimeError):
    """Raised when a provider cannot satisfy a candle request."""


@dataclass(frozen=True)
class CandlePage:
    """One page of raw HL-wire candle rows (oldest first)."""

    rows: List[Dict[str, Any]]
    symbol: str
    interval: str
    request_start_ms: int
    request_end_ms: int
    provider: ProviderName


@dataclass
class PaginatedCandleResult:
    """Full forward-paginated download."""

    rows: List[Dict[str, Any]] = field(default_factory=list)
    pages_fetched: int = 0
    duplicates_skipped: int = 0
    resumed_from_checkpoint: bool = False


class CandleProvider(ABC):
    """Fetch historical OHLCV candles from a single upstream."""

    @property
    @abstractmethod
    def name(self) -> ProviderName:
        ...

    @abstractmethod
    async def fetch_page(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> CandlePage:
        """Fetch one page (<=5000 candles) for [start_ms, end_ms]."""

    async def connect(self) -> None:
        """Optional session setup."""

    async def disconnect(self) -> None:
        """Optional session teardown."""

    async def __aenter__(self) -> "CandleProvider":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()
