"""Continuous L2 / trade-tape sampling into research DB (Phase 07/08)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Optional, Sequence

from src.data.hl_l2_parse import l2_snapshot_from_book
from src.data.research_database import (
    ResearchDatabase,
    TradeTapeRecord,
)
from src.data.series_metadata import (
    HL_API_VERSION,
    SOURCE_HL_L2_SAMPLE,
    SOURCE_HL_TRADE_TAPE,
    VENUE_HYPERLIQUID,
)
from src.exchanges.hyperliquid_rest import HyperliquidRESTClient
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)


class ResearchSampler:
    """Background task: sample HL L2 book + recent trades into research DB."""

    def __init__(
        self,
        db: ResearchDatabase,
        symbols: Sequence[str],
        *,
        interval_sec: float = 60.0,
        rest_client: Optional[HyperliquidRESTClient] = None,
    ) -> None:
        self._db = db
        self._symbols = [s.strip().upper() for s in symbols]
        self._interval = max(5.0, float(interval_sec))
        self._rest = rest_client
        self._owns_rest = rest_client is None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="research_sampler")
        logger.info(
            "ResearchSampler started — %s every %.0fs → %s",
            ", ".join(self._symbols),
            self._interval,
            self._db.db_path,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._owns_rest and self._rest is not None:
            await self._rest.close()
            self._rest = None

    async def _loop(self) -> None:
        if self._rest is None:
            self._rest = HyperliquidRESTClient()
            await self._rest.open()
        while self._running:
            try:
                await self._sample_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ResearchSampler cycle failed: %s", exc)
            await asyncio.sleep(self._interval)

    async def _sample_once(self) -> None:
        assert self._rest is not None
        ts = int(time.time() * 1000)
        for sym in self._symbols:
            await self._sample_symbol(sym, ts)

    async def _sample_symbol(self, symbol: str, ts_ms: int) -> None:
        assert self._rest is not None
        ingested = int(time.time() * 1000)
        try:
            book = await self._rest.l2_book(symbol)
            snap = l2_snapshot_from_book(
                symbol, book, ts_ms, ingested,
                quality_flags='{"sampled":true,"continuous":true}',
            )
            if snap is not None:
                self._db.save_l2_snapshots([snap])
        except Exception as exc:
            logger.debug("L2 sample %s: %s", symbol, exc)

        try:
            trades = await self._rest.recent_trades(symbol)
            rows: List[TradeTapeRecord] = []
            for t in trades:
                rows.append(
                    TradeTapeRecord(
                        symbol=symbol,
                        timestamp_ms=int(t.get("time", t.get("T", ts_ms))),
                        price=safe_float(t.get("px", t.get("p")), 0.0),
                        size=safe_float(t.get("sz", t.get("s")), 0.0),
                        side=str(t.get("side", t.get("S", "unknown"))),
                        trade_id=str(t.get("tid", t.get("id", ""))) or None,
                        source=SOURCE_HL_TRADE_TAPE,
                        venue=VENUE_HYPERLIQUID,
                        api_version=HL_API_VERSION,
                        ingested_at_ms=ingested,
                    )
                )
            if rows:
                self._db.save_trade_tape(rows)
        except Exception as exc:
            logger.debug("Trade tape sample %s: %s", symbol, exc)


def start_research_sampler_from_config(cfg: Any) -> Optional[ResearchSampler]:
    """Build REST sampler from config; returns None when disabled.

    REST polling (60s) is not Tier A — use WS microstructure recorder instead.
    """
    research = cfg.get("research", {}) or {}
    if not bool(research.get("rest_sampling_enabled", False)):
        return None
    from src.utils.config import get_trading_symbols

    symbols = research.get("sampler_symbols") or get_trading_symbols(cfg)
    db = ResearchDatabase(ResearchDatabase.resolve_path(cfg))
    interval = float(research.get("sampler_interval_sec", 60.0))
    return ResearchSampler(db, symbols, interval_sec=interval)
