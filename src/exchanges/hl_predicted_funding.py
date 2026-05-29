"""Hyperliquid INFO ``predictedFundings`` — cross-venue predicted funding."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

import aiohttp

from src.exchanges.funding_normalize import (
    HL_VENUE_BINANCE,
    HL_VENUE_BYBIT,
    HL_VENUE_HL,
    normalize_funding_to_8h,
    parse_optional_rate,
)

logger = logging.getLogger(__name__)

INFO_URL_MAINNET = "https://api.hyperliquid.xyz/info"
INFO_URL_TESTNET = "https://api.hyperliquid-testnet.xyz/info"


@dataclass
class HlVenueFunding:
    """Single venue row from predictedFundings."""

    venue: str
    funding_rate: float
    interval_hours: float
    funding_rate_8h: float
    next_funding_time_ms: Optional[int] = None


@dataclass
class HlSymbolFundingSnapshot:
    """Parsed predicted + current funding for one coin."""

    symbol: str
    venues: Dict[str, HlVenueFunding] = field(default_factory=dict)
    timestamp_ms: int = 0
    stale: bool = False
    age_sec: float = 0.0

    @property
    def hl_perp(self) -> Optional[HlVenueFunding]:
        return self.venues.get(HL_VENUE_HL)

    @property
    def predicted_funding_hl(self) -> Optional[float]:
        """HL predicted rate (raw interval, usually 1h)."""
        v = self.hl_perp
        return v.funding_rate if v else None

    @property
    def predicted_funding_hl_8h(self) -> Optional[float]:
        v = self.hl_perp
        return v.funding_rate_8h if v else None


def _parse_venue_row(venue: str, payload: Any) -> Optional[HlVenueFunding]:
    if payload is None or not isinstance(payload, dict):
        return None
    rate = parse_optional_rate(payload.get("fundingRate"))
    if rate is None:
        return None
    interval = float(payload.get("fundingIntervalHours") or 8)
    next_ms = payload.get("nextFundingTime")
    next_funding_time_ms = int(next_ms) if next_ms is not None else None
    return HlVenueFunding(
        venue=venue,
        funding_rate=rate,
        interval_hours=interval,
        funding_rate_8h=normalize_funding_to_8h(rate, interval),
        next_funding_time_ms=next_funding_time_ms,
    )


def parse_predicted_fundings_response(raw: Any) -> Dict[str, HlSymbolFundingSnapshot]:
    """Parse ``POST /info`` body for ``type: predictedFundings``."""
    now_ms = int(time.time() * 1000)
    out: Dict[str, HlSymbolFundingSnapshot] = {}
    if not isinstance(raw, list):
        return out

    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        symbol = str(entry[0])
        venue_rows = entry[1]
        if not isinstance(venue_rows, list):
            continue
        snap = HlSymbolFundingSnapshot(symbol=symbol, timestamp_ms=now_ms)
        for row in venue_rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            venue_name = str(row[0])
            parsed = _parse_venue_row(venue_name, row[1])
            if parsed is not None:
                snap.venues[venue_name] = parsed
        if snap.venues:
            out[symbol] = snap
    return out


class HyperliquidPredictedFundingClient:
    """Poll Hyperliquid predictedFundings (official cross-venue funding)."""

    def __init__(
        self,
        use_testnet: bool = False,
        *,
        stale_max_sec: float = 300.0,
        connect_timeout: float = 10.0,
        total_timeout: float = 25.0,
    ) -> None:
        self._info_url = INFO_URL_TESTNET if use_testnet else INFO_URL_MAINNET
        self._cache: Dict[str, HlSymbolFundingSnapshot] = {}
        self._stale_max_sec = float(stale_max_sec)
        self._connect_timeout = float(connect_timeout)
        self._total_timeout = float(total_timeout)

    @property
    def cache(self) -> Dict[str, HlSymbolFundingSnapshot]:
        return self._cache

    async def poll(
        self,
        symbols: Optional[List[str]] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> Dict[str, HlSymbolFundingSnapshot]:
        """Fetch and cache predicted fundings; return stale cache on failure."""
        own_session = session is None
        if own_session:
            timeout = aiohttp.ClientTimeout(
                total=self._total_timeout,
                connect=self._connect_timeout,
            )
            session = aiohttp.ClientSession(timeout=timeout)

        now_ms = int(time.time() * 1000)
        parsed: Dict[str, HlSymbolFundingSnapshot] = {}
        try:
            assert session is not None
            async with session.post(
                self._info_url,
                json={"type": "predictedFundings"},
                headers={"Content-Type": "application/json"},
            ) as resp:
                resp.raise_for_status()
                raw = await resp.json()
            parsed = parse_predicted_fundings_response(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Hyperliquid predictedFundings fetch failed: %s", exc)
        finally:
            if own_session and session is not None:
                await session.close()

        if symbols is not None:
            want = set(symbols)
            if parsed:
                parsed = {k: v for k, v in parsed.items() if k in want}

        out: Dict[str, HlSymbolFundingSnapshot] = {}
        target_symbols = list(symbols) if symbols is not None else list(parsed.keys())

        if parsed:
            self._cache.update(parsed)

        for sym in target_symbols:
            fresh = self._cache.get(sym)
            if fresh is not None and sym in parsed:
                out[sym] = fresh
                continue
            cached = self._cache.get(sym)
            if cached is None:
                continue
            age_sec = (now_ms - cached.timestamp_ms) / 1000.0 if cached.timestamp_ms else 0.0
            if sym not in parsed and age_sec > self._stale_max_sec:
                logger.warning(
                    "HL predictedFundings %s: cache expired (age=%.0fs)",
                    sym,
                    age_sec,
                )
                continue
            if sym not in parsed:
                out[sym] = replace(cached, stale=True, age_sec=age_sec)
                logger.warning(
                    "HL predictedFundings %s: STALE (age=%.0fs)",
                    sym,
                    age_sec,
                )
            else:
                out[sym] = cached

        if symbols is None:
            return dict(self._cache)
        return out

    def get(self, symbol: str) -> Optional[HlSymbolFundingSnapshot]:
        return self._cache.get(symbol)
