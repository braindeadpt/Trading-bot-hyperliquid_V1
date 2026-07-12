"""Data-series provenance constants for research / live separation (Phase 07)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

RESEARCH_PROTOCOL_VERSION = "phase07-hl-data-contract-v1"
HL_API_VERSION = "info-v1"
VENUE_HYPERLIQUID = "hyperliquid"
VENUE_BINANCE = "binance"
SOURCE_HL_CANDLE_SNAPSHOT = "hl_candleSnapshot"
SOURCE_BINANCE_KLINES = "binance_klines"
SOURCE_BINANCE_FUNDING = "binance_funding"
SOURCE_BINANCE_OI = "binance_oi"
SOURCE_HL_L2_SAMPLE = "hl_l2Book_sample"
SOURCE_HL_L2_WS = "hl_l2Book_ws"
SOURCE_HL_TRADE_TAPE = "hl_trade_tape"
SOURCE_HL_TRADE_WS = "hl_trades_ws"
SOURCE_HL_WS_CANDLE_AGG = "hl_ws_1m_tape_agg"
SOURCE_GOLDRUSH = "goldrush"
GOLDRUSH_API_VERSION = "hypercore-info-v1"

PROTECTED_OFFICIAL_SOURCES = frozenset({
    SOURCE_HL_CANDLE_SNAPSHOT,
    SOURCE_HL_TRADE_WS,
    SOURCE_HL_WS_CANDLE_AGG,
})


@dataclass(frozen=True)
class SeriesMetadata:
    """Provenance attached to every persisted research series row."""

    source: str
    venue: str
    api_version: str
    ingested_at_ms: int
    quality_flags: Optional[Dict[str, Any]] = None
    volume_unit: str = "base"

    def quality_json(self) -> Optional[str]:
        if not self.quality_flags:
            return None
        return json.dumps(self.quality_flags, sort_keys=True)

    @classmethod
    def goldrush_candles(cls) -> "SeriesMetadata":
        return cls(
            source=SOURCE_GOLDRUSH,
            venue=VENUE_HYPERLIQUID,
            api_version=GOLDRUSH_API_VERSION,
            ingested_at_ms=int(time.time() * 1000),
            quality_flags={
                "taker_split": False,
                "volume_unit": "base",
                "close_time_key": "T",
                "provider": "goldrush_hypercore",
            },
            volume_unit="base",
        )

    @classmethod
    def hl_candles(cls, *, taker_split: bool = False) -> "SeriesMetadata":
        return cls(
            source=SOURCE_HL_CANDLE_SNAPSHOT,
            venue=VENUE_HYPERLIQUID,
            api_version=HL_API_VERSION,
            ingested_at_ms=int(time.time() * 1000),
            quality_flags={
                "taker_split": taker_split,
                "volume_unit": "base",
                "close_time_key": "T",
            },
            volume_unit="base",
        )

    @classmethod
    def binance_klines(cls, *, futures: bool = False) -> "SeriesMetadata":
        return cls(
            source=SOURCE_BINANCE_KLINES,
            venue=VENUE_BINANCE,
            api_version="spot-v3" if not futures else "fapi-v1",
            ingested_at_ms=int(time.time() * 1000),
            quality_flags={
                "taker_split": True,
                "volume_unit": "base",
                "close_time_key": "k[6]",
                "futures": futures,
            },
            volume_unit="base",
        )
