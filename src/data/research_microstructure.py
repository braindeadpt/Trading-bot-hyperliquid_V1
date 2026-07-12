"""WebSocket microstructure recorder — continuous trade tape + L2 (Phase 08).

Trade tape uses the WS client's *raw* listener path (bypasses DataBus rate
limits). L2 uses DataBus ``orderbook:{symbol}``. Not REST polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, TYPE_CHECKING

from src.data.database import Candle
from src.data.hl_l2_parse import l2_snapshot_from_hl_l2book
from src.data.research_database import (
    ResearchDatabase,
    TradeTapeRecord,
)
from src.data.series_metadata import (
    HL_API_VERSION,
    SOURCE_HL_TRADE_WS,
    SOURCE_HL_WS_CANDLE_AGG,
    SeriesMetadata,
    VENUE_HYPERLIQUID,
)
from src.exchanges.hyperliquid_ws import DataBus, HlTrade

if TYPE_CHECKING:
    from src.exchanges.hyperliquid_ws import HyperliquidWSClient

logger = logging.getLogger(__name__)

TF_1M_MS = 60_000


@dataclass
class _MinuteAgg:
    symbol: str
    bucket_start_ms: int
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    close_price: float = 0.0
    volume: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    trade_count: int = 0

    def merge(self, price: float, size: float, side: str) -> None:
        if self.open_price == 0.0:
            self.open_price = price
            self.high_price = price
            self.low_price = price
        self.close_price = price
        self.high_price = max(self.high_price, price)
        self.low_price = min(self.low_price, price)
        self.volume += size
        self.trade_count += 1
        side_u = str(side).upper()
        if side_u in ("B", "BUY"):
            self.buy_volume += size
        else:
            self.sell_volume += size

    def to_candle(self, close_time_ms: int) -> Candle:
        return Candle(
            symbol=self.symbol,
            timestamp_ms=close_time_ms,
            open=self.open_price,
            high=self.high_price,
            low=self.low_price,
            close=self.close_price,
            volume=self.volume,
            buy_volume=self.buy_volume,
            sell_volume=self.sell_volume,
            trade_count=self.trade_count,
        )


class ResearchMicrostructureRecorder:
    """Persist HL WS trade tape + L2 into the research DB."""

    def __init__(
        self,
        bus: DataBus,
        db: ResearchDatabase,
        symbols: Sequence[str],
        *,
        l2_min_interval_ms: float = 250.0,
        tape_gap_threshold_ms: int = 5_000,
        l2_stale_threshold_ms: int = 10_000,
        health_interval_sec: float = 30.0,
        flush_interval_sec: float = 0.5,
        tape_queue_max: int = 10_000,
    ) -> None:
        self._bus = bus
        self._db = db
        self._symbols = [s.strip().upper() for s in symbols]
        self._l2_min_interval = max(50.0, float(l2_min_interval_ms)) / 1000.0
        self._tape_gap_threshold_ms = max(1_000, int(tape_gap_threshold_ms))
        self._l2_stale_threshold_ms = max(2_000, int(l2_stale_threshold_ms))
        self._health_interval = max(5.0, float(health_interval_sec))
        self._flush_interval = max(0.1, float(flush_interval_sec))
        self._tape_queue_max = max(1000, int(tape_queue_max))

        self._running = False
        self._health_task: Optional[asyncio.Task] = None
        self._tape_consumer_task: Optional[asyncio.Task] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._subscribed = False

        self._tape_queue: Optional[asyncio.Queue[TradeTapeRecord]] = None
        self._tape_dropped = 0
        self._retry_buffer: List[TradeTapeRecord] = []
        self._tape_lock = threading.Lock()
        self._ws_client: Optional[HyperliquidWSClient] = None

        self._last_trade_ts: Dict[str, int] = {}
        self._last_trade_tid: Dict[str, int] = {}
        self._seen_tids: Dict[str, Set[str]] = {s: set() for s in self._symbols}
        self._last_l2_mono: Dict[str, float] = {}
        self._last_l2_ts: Dict[str, int] = {}
        self._minute_agg: Dict[Tuple[str, int], _MinuteAgg] = {}

        self._trade_count: Dict[str, int] = {s: 0 for s in self._symbols}
        self._l2_count: Dict[str, int] = {s: 0 for s in self._symbols}
        self._gap_count: Dict[str, int] = {s: 0 for s in self._symbols}

    def attach_ws_client(self, ws_client: HyperliquidWSClient) -> None:
        """Register raw trade tap — no DataBus drops on this path."""
        self._ws_client = ws_client
        ws_client.add_raw_trade_listener(self._on_trade_raw)

    async def start(self) -> None:
        if self._subscribed:
            return
        for sym in self._symbols:
            await self._bus.subscribe(f"orderbook:{sym}", self._on_orderbook)
            logger.info("ResearchMicrostructureRecorder subscribed L2: %s", sym)
        if self._ws_client is None:
            logger.warning(
                "ResearchMicrostructureRecorder: no WS raw tap — "
                "call attach_ws_client() before start()",
            )
        self._subscribed = True
        self._running = True
        self._tape_queue = asyncio.Queue(maxsize=self._tape_queue_max)
        self._tape_consumer_task = asyncio.create_task(
            self._tape_consumer_loop(), name="research_tape_consumer",
        )
        self._health_task = asyncio.create_task(
            self._health_loop(), name="research_micro_health",
        )
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="research_micro_flush",
        )
        logger.info(
            "ResearchMicrostructureRecorder started — %s raw tape + L2≥%.0fms → %s",
            ", ".join(self._symbols),
            self._l2_min_interval * 1000,
            self._db.db_path,
        )

    async def stop(self) -> None:
        if self._ws_client is not None:
            self._ws_client.remove_raw_trade_listener(self._on_trade_raw)
        self._running = False
        for task in (self._health_task, self._flush_task, self._tape_consumer_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._health_task = None
        self._flush_task = None
        self._tape_consumer_task = None
        await self._drain_tape_queue()
        self._flush_open_minute_aggs()

    def _on_trade_raw(self, trade: HlTrade) -> None:
        """Synchronous raw WS tap — enqueues without DataBus rate limits."""
        record = self._build_tape_record(trade)
        if record is None:
            return
        q = self._tape_queue
        if q is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            with self._tape_lock:
                self._retry_buffer.append(record)
            return

        def _enqueue() -> None:
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                self._tape_dropped += 1

        loop.call_soon(_enqueue)
        self._trade_count[record.symbol] = self._trade_count.get(record.symbol, 0) + 1
        self._update_minute_agg(
            record.symbol,
            record.timestamp_ms,
            record.price,
            record.size,
            record.side,
        )

    def _build_tape_record(self, trade: Any) -> Optional[TradeTapeRecord]:
        symbol = str(getattr(trade, "symbol", "")).upper()
        if symbol not in self._symbols:
            return None
        ts_ms = int(getattr(trade, "timestamp_ms", time.time() * 1000))
        price = float(getattr(trade, "price", 0.0))
        size = float(getattr(trade, "size", 0.0))
        side = str(getattr(trade, "side", "unknown"))
        tid = int(getattr(trade, "tid", 0))
        tid_key = str(tid) if tid else f"{ts_ms}:{price}:{size}"

        if tid_key in self._seen_tids.setdefault(symbol, set()):
            return None
        if len(self._seen_tids[symbol]) > 50_000:
            self._seen_tids[symbol].clear()
        self._seen_tids[symbol].add(tid_key)

        prev_ts = self._last_trade_ts.get(symbol)
        if prev_ts is not None:
            delta = ts_ms - prev_ts
            if delta > self._tape_gap_threshold_ms:
                self._record_gap(
                    symbol, "trade_tape", prev_ts, ts_ms,
                    detail={"reason": "inter_trade_gap", "delta_ms": delta, "raw_tap": True},
                )
            elif delta < 0:
                self._record_gap(
                    symbol, "trade_tape", ts_ms, prev_ts,
                    detail={"reason": "timestamp_regression", "delta_ms": delta, "raw_tap": True},
                )
        self._last_trade_ts[symbol] = ts_ms
        if tid:
            self._last_trade_tid[symbol] = tid

        ingested = int(time.time() * 1000)
        return TradeTapeRecord(
            symbol=symbol,
            timestamp_ms=ts_ms,
            price=price,
            size=size,
            side=side,
            trade_id=tid_key if tid else None,
            source=SOURCE_HL_TRADE_WS,
            venue=VENUE_HYPERLIQUID,
            api_version=HL_API_VERSION,
            ingested_at_ms=ingested,
        )

    async def _tape_consumer_loop(self) -> None:
        while self._running:
            try:
                assert self._tape_queue is not None
                record = await asyncio.wait_for(
                    self._tape_queue.get(), timeout=self._flush_interval,
                )
                batch = [record]
                while len(batch) < 500:
                    try:
                        batch.append(self._tape_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                await self._persist_tape_batch(batch)
            except asyncio.TimeoutError:
                await self._flush_retry_buffer()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Tape consumer: %s", exc)

    async def _drain_tape_queue(self) -> None:
        if self._tape_queue is None:
            return
        batch: List[TradeTapeRecord] = []
        while not self._tape_queue.empty():
            try:
                batch.append(self._tape_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        with self._tape_lock:
            batch.extend(self._retry_buffer)
            self._retry_buffer.clear()
        if batch:
            await self._persist_tape_batch(batch)

    async def _flush_retry_buffer(self) -> None:
        with self._tape_lock:
            if not self._retry_buffer:
                return
            batch = list(self._retry_buffer)
            self._retry_buffer.clear()
        await self._persist_tape_batch(batch)

    async def _persist_tape_batch(self, batch: List[TradeTapeRecord]) -> None:
        if not batch:
            return
        try:
            self._db.save_trade_tape(batch)
        except Exception as exc:
            logger.warning(
                "Trade tape batch persist failed (%d rows): %s — re-queued",
                len(batch),
                exc,
            )
            with self._tape_lock:
                self._retry_buffer.extend(batch)

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush_retry_buffer()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Tape flush loop: %s", exc)

    async def _flush_tape_batch(self) -> None:
        """Legacy hook — drain retry buffer."""
        await self._flush_retry_buffer()

    async def _on_orderbook(self, book: Any) -> None:
        symbol = str(getattr(book, "symbol", "")).upper()
        if symbol not in self._symbols:
            return
        now_mono = time.monotonic()
        last_mono = self._last_l2_mono.get(symbol, 0.0)
        if now_mono - last_mono < self._l2_min_interval:
            return
        self._last_l2_mono[symbol] = now_mono

        ingested = int(time.time() * 1000)
        snap = l2_snapshot_from_hl_l2book(
            book,
            ingested,
            quality_flags=json.dumps({"ws": True, "continuous": True}),
        )
        if snap is None:
            return
        ts_ms = snap.timestamp_ms
        prev_ts = self._last_l2_ts.get(symbol)
        if prev_ts is not None and ts_ms - prev_ts > self._l2_stale_threshold_ms * 3:
            self._record_gap(
                symbol, "l2_book", prev_ts, ts_ms,
                detail={"reason": "l2_timestamp_gap", "delta_ms": ts_ms - prev_ts},
            )
        self._last_l2_ts[symbol] = ts_ms
        self._l2_count[symbol] = self._l2_count.get(symbol, 0) + 1
        try:
            self._db.save_l2_snapshots([snap])
        except Exception as exc:
            logger.debug("L2 persist %s failed: %s", symbol, exc)

    def _update_minute_agg(
        self, symbol: str, ts_ms: int, price: float, size: float, side: str,
    ) -> None:
        bucket = (ts_ms // TF_1M_MS) * TF_1M_MS
        key = (symbol, bucket)
        agg = self._minute_agg.get(key)
        if agg is None:
            for (sym, bkt), old in list(self._minute_agg.items()):
                if sym == symbol and bkt < bucket:
                    self._persist_minute_agg(old, bkt + TF_1M_MS - 1)
                    del self._minute_agg[(sym, bkt)]
            agg = _MinuteAgg(symbol=symbol, bucket_start_ms=bucket)
            self._minute_agg[key] = agg
        agg.merge(price, size, side)

    def _persist_minute_agg(self, agg: _MinuteAgg, close_time_ms: int) -> None:
        if agg.trade_count == 0:
            return
        candle = agg.to_candle(close_time_ms)
        meta = SeriesMetadata(
            source=SOURCE_HL_WS_CANDLE_AGG,
            venue=VENUE_HYPERLIQUID,
            api_version=HL_API_VERSION,
            ingested_at_ms=int(time.time() * 1000),
            quality_flags={
                "taker_split": True,
                "volume_unit": "base",
                "aggregated_from": SOURCE_HL_TRADE_WS,
            },
        )
        try:
            self._db.save_research_candles([candle], "1m", meta)
        except Exception as exc:
            logger.debug("WS 1m agg persist %s failed: %s", agg.symbol, exc)

    def _flush_open_minute_aggs(self) -> None:
        now_ms = int(time.time() * 1000)
        for (sym, bkt), agg in list(self._minute_agg.items()):
            self._persist_minute_agg(agg, min(bkt + TF_1M_MS - 1, now_ms))
        self._minute_agg.clear()

    def _record_gap(
        self,
        symbol: str,
        feed: str,
        gap_start_ms: int,
        gap_end_ms: int,
        *,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._gap_count[symbol] = self._gap_count.get(symbol, 0) + 1
        try:
            self._db.save_microstructure_gap(
                symbol, feed, gap_start_ms, gap_end_ms, detail=detail,
            )
        except Exception as exc:
            logger.debug("Gap record failed %s %s: %s", symbol, feed, exc)
        logger.info(
            "Microstructure gap %s %s %dms (%s)",
            symbol, feed, gap_end_ms - gap_start_ms, (detail or {}).get("reason", "?"),
        )

    async def _health_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._health_interval)
                await self._report_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Health loop: %s", exc)

    async def _report_health(self) -> None:
        dropped = self._bus.dropped_counts()
        now_ms = int(time.time() * 1000)
        for sym in self._symbols:
            trade_age = self._bus.last_publish_age_sec(f"trade:{sym}")
            book_age = self._bus.last_publish_age_sec(f"orderbook:{sym}")
            last_trade = self._last_trade_ts.get(sym)
            last_l2 = self._last_l2_ts.get(sym)

            if book_age is not None and book_age * 1000 > self._l2_stale_threshold_ms:
                if last_l2 is not None:
                    self._record_gap(
                        sym, "l2_book", last_l2, now_ms,
                        detail={
                            "reason": "l2_stale",
                            "staleness_ms": int(book_age * 1000),
                        },
                    )

            snap = {
                "feed": "microstructure_ws",
                "symbol": sym,
                "trade_tape_path": "ws_raw_tap",
                "tape_queue_depth": self._tape_queue.qsize() if self._tape_queue else 0,
                "tape_dropped_backpressure": self._tape_dropped,
                "tape_retry_buffer": len(self._retry_buffer),
                "trade_count_session": self._trade_count.get(sym, 0),
                "l2_count_session": self._l2_count.get(sym, 0),
                "gap_count_session": self._gap_count.get(sym, 0),
                "last_trade_ts_ms": last_trade,
                "last_l2_ts_ms": last_l2,
                "trade_publish_age_sec": trade_age,
                "orderbook_publish_age_sec": book_age,
                "dropped_trade": dropped.get(f"trade:{sym}", 0),
                "dropped_orderbook": dropped.get(f"orderbook:{sym}", 0),
                "dropped_total": sum(dropped.values()),
            }
            try:
                self._db.save_feed_health_snapshot(snap, symbol=sym)
            except Exception as exc:
                logger.debug("Health snapshot %s: %s", sym, exc)

        if dropped:
            logger.warning(
                "DataBus dropped events (session): %s",
                {k: v for k, v in dropped.items() if v > 0},
            )


def start_microstructure_recorder_from_config(
    bus: DataBus,
    cfg: Any,
) -> Optional[ResearchMicrostructureRecorder]:
    """Build WS microstructure recorder from config; None when disabled."""
    research = cfg.get("research", {}) or {}
    if not bool(research.get("ws_microstructure_enabled", True)):
        return None
    if not bool(research.get("continuous_sampling_enabled", True)):
        return None
    from src.utils.config import get_trading_symbols

    symbols = research.get("sampler_symbols") or get_trading_symbols(cfg)
    db = ResearchDatabase(ResearchDatabase.resolve_path(cfg))
    return ResearchMicrostructureRecorder(
        bus,
        db,
        symbols,
        l2_min_interval_ms=float(research.get("l2_min_interval_ms", 250.0)),
        tape_gap_threshold_ms=int(research.get("tape_gap_threshold_ms", 5_000)),
        l2_stale_threshold_ms=int(research.get("l2_stale_threshold_ms", 10_000)),
        health_interval_sec=float(research.get("health_report_interval_sec", 30.0)),
        tape_queue_max=int(research.get("tape_queue_max", 10_000)),
    )
