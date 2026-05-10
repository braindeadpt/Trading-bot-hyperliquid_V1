"""Candle builder — aggregates WebSocket price ticks into OHLCV candles.

Maintains in-flight candles for 1m, 5m, 15m, 1h timeframes and emits
``candle_complete`` events to the shared :class:`~exchanges.hyperliquid_ws.DataBus`.
Each completed candle is also stored in a small ring buffer for fast
historical access.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.exchanges.hyperliquid_ws import DataBus, HlAssetCtx, HlPriceTick, HlTrade

logger = logging.getLogger(__name__)

# Timeframe constants (seconds)
TF_1M = 60
TF_5M = 300
TF_15M = 900
TF_1H = 3600
TIMEFRAMES = (TF_1M, TF_5M, TF_15M, TF_1H)
RING_BUFFER_SIZE = 288  # e.g. 288 × 5m = 24h


@dataclass(slots=True)
class Candle:
    """Unified candle with Hyperliquid-derived context."""

    symbol: str
    timeframe_s: int
    open_time_ms: int
    close_time_ms: int
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    oi_open: float = 0.0
    oi_close: float = 0.0
    funding: float = 0.0
    predicted_funding: float = 0.0
    trade_count: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0

    @property
    def oi_delta(self) -> float:
        return self.oi_close - self.oi_open

    # ── Aliases for strategy compatibility (indicators.py uses short names) ──
    @property
    def open(self) -> float:
        return self.open_price

    @property
    def high(self) -> float:
        return self.high_price

    @property
    def low(self) -> float:
        return self.low_price

    @property
    def close(self) -> float:
        return self.close_price

    @property
    def timestamp_ms(self) -> int:
        return self.close_time_ms

    @property
    def open_interest(self) -> Optional[float]:
        return self.oi_close if self.oi_close != 0.0 else None


@dataclass(slots=True)
class _InFlight:
    """Mutable accumulator for a candle currently being built."""

    symbol: str
    timeframe_s: int
    bucket_start_ms: int

    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    close_price: float = 0.0
    volume: float = 0.0
    trade_count: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0

    # Context captured at open + latest seen
    oi_open: float = 0.0
    oi_latest: float = 0.0
    funding_open: float = 0.0
    predicted_funding_open: float = 0.0
    funding_latest: float = 0.0
    predicted_funding_latest: float = 0.0

    def merge_trade(self, price: float, size: float, side: str) -> None:
        if self.open_price == 0.0:
            self.open_price = price
            self.high_price = price
            self.low_price = price
        self.close_price = price
        if price > self.high_price:
            self.high_price = price
        if price < self.low_price:
            self.low_price = price
        self.volume += size
        self.trade_count += 1
        if side == "B":
            self.buy_volume += size
        else:
            self.sell_volume += size

    def merge_price(self, price: float) -> None:
        if self.open_price == 0.0:
            self.open_price = price
            self.high_price = price
            self.low_price = price
        self.close_price = price
        if price > self.high_price:
            self.high_price = price
        if price < self.low_price:
            self.low_price = price

    def to_candle(self, bucket_end_ms: int) -> Candle:
        return Candle(
            symbol=self.symbol,
            timeframe_s=self.timeframe_s,
            open_time_ms=self.bucket_start_ms,
            close_time_ms=bucket_end_ms,
            open_price=self.open_price,
            high_price=self.high_price,
            low_price=self.low_price,
            close_price=self.close_price,
            volume=self.volume,
            oi_open=self.oi_open,
            oi_close=self.oi_latest,
            funding=self.funding_open,
            predicted_funding=self.predicted_funding_open,
            trade_count=self.trade_count,
            buy_volume=self.buy_volume,
            sell_volume=self.sell_volume,
        )


# ────────────────────────────────
# CandleBuilder
# ────────────────────────────────


class CandleBuilder:
    """Consumes :class:`HlPriceTick`, :class:`HlTrade`, and :class:`HlAssetCtx`
    events from the :class:`DataBus` and builds multi-timeframe candles.

    Completed candles are published on ``candle_complete:<tf>:<symbol>``
    and stored in a per-symbol ring buffer.
    """

    def __init__(
        self,
        bus: DataBus,
        symbols: Optional[List[str]] = None,
        timeframes: Tuple[int, ...] = TIMEFRAMES,
        ring_size: int = RING_BUFFER_SIZE,
    ) -> None:
        self.bus = bus
        self.symbols = set(symbols or ["BTC", "ETH", "SOL"])
        self.timeframes = timeframes
        self.ring_size = ring_size

        # (symbol, tf) -> _InFlight
        self._inflight: Dict[Tuple[str, int], _InFlight] = {}

        # (symbol, tf) -> deque[Candle]
        self._history: Dict[Tuple[str, int], deque[Candle]] = {}

        self._subscribed = False

    # ── Lifecycle ──

    async def start(self) -> None:
        """Subscribe to the DataBus topics we need."""
        if self._subscribed:
            return
        for sym in self.symbols:
            await self.bus.subscribe(f"price:{sym}", self._on_price)
            await self.bus.subscribe(f"trade:{sym}", self._on_trade)
            await self.bus.subscribe(f"ctx:{sym}", self._on_ctx)
            logger.info("CandleBuilder subscribed to price:%s, trade:%s, ctx:%s", sym, sym, sym)
        self._subscribed = True
        logger.info("CandleBuilder started for %s", self.symbols)

    async def stop(self) -> None:
        """No-op — subscriptions are fire-and-forget via DataBus."""
        logger.info("CandleBuilder stopped")

    # ── DataBus callbacks ──

    async def _on_price(self, tick: Any) -> None:
        # Simplified duck-typing check to avoid cross-module isinstance issues
        if not hasattr(tick, 'symbol') or not hasattr(tick, 'mid'):
            return
        if tick.symbol not in self.symbols:
            return
        logger.info("CandleBuilder received price: %s = %.2f", tick.symbol, tick.mid)
        self._update(tick.symbol, getattr(tick, 'timestamp_ms', int(time.time() * 1000)), price=tick.mid)

    async def _on_trade(self, trade: Any) -> None:
        # Simplified duck-typing check
        if not hasattr(trade, 'symbol') or not hasattr(trade, 'size'):
            return
        if trade.symbol not in self.symbols:
            return
        self._update(
            trade.symbol,
            getattr(trade, 'timestamp_ms', int(time.time() * 1000)),
            price=getattr(trade, 'price', None),
            size=trade.size,
            side=getattr(trade, 'side', 'buy'),
        )

    async def _on_ctx(self, ctx: Any) -> None:
        # Simplified duck-typing check
        if not hasattr(ctx, 'symbol'):
            return
        if ctx.symbol not in self.symbols:
            return
        for tf in self.timeframes:
            key = (ctx.symbol, tf)
            inflight = self._inflight.get(key)
            if inflight is None:
                continue
            inflight.oi_latest = getattr(ctx, 'open_interest', 0)
            inflight.funding_latest = getattr(ctx, 'funding_rate', 0)
            inflight.predicted_funding_latest = getattr(ctx, 'predicted_funding', 0)
            # Seed open values if they haven't been set yet
            if inflight.oi_open == 0.0 and getattr(ctx, 'open_interest', 0) > 0:
                inflight.oi_open = getattr(ctx, 'open_interest', 0)
            if inflight.funding_open == 0.0:
                inflight.funding_open = getattr(ctx, 'funding_rate', 0)
            if inflight.predicted_funding_open == 0.0:
                inflight.predicted_funding_open = getattr(ctx, 'predicted_funding', 0)

    # ── Core logic ──

    def _update(
        self,
        symbol: str,
        timestamp_ms: int,
        price: Optional[float] = None,
        size: Optional[float] = None,
        side: Optional[str] = None,
    ) -> None:
        for tf in self.timeframes:
            bucket_start = _bucket_start(timestamp_ms, tf)
            key = (symbol, tf)
            inflight = self._inflight.get(key)

            # Candle rollover
            if inflight is not None and bucket_start > inflight.bucket_start_ms:
                candle = inflight.to_candle(bucket_start - 1)
                self._store_and_emit(candle)
                inflight = None

            if inflight is None:
                inflight = _InFlight(
                    symbol=symbol,
                    timeframe_s=tf,
                    bucket_start_ms=bucket_start,
                )
                self._inflight[key] = inflight

            if price is not None:
                if size is not None and side is not None:
                    inflight.merge_trade(price, size, side)
                else:
                    inflight.merge_price(price)

    def _store_and_emit(self, candle: Candle) -> None:
        key = (candle.symbol, candle.timeframe_s)
        self._history.setdefault(key, deque(maxlen=self.ring_size)).append(candle)
        self.bus.publish(
            f"candle_complete:{candle.timeframe_s}:{candle.symbol}",
            candle,
        )
        logger.debug(
            "Candle complete %s %dm O=%.2f H=%.2f L=%.2f C=%.2f V=%.4f",
            candle.symbol,
            candle.timeframe_s // 60,
            candle.open_price,
            candle.high_price,
            candle.low_price,
            candle.close_price,
            candle.volume,
        )

    # ── Public query API ──

    def get_latest_candle(self, symbol: str, timeframe_s: int) -> Optional[Candle]:
        """Return the most recent **completed** candle, or *None*."""
        history = self._history.get((symbol, timeframe_s))
        return history[-1] if history else None

    def get_history(
        self, symbol: str, timeframe_s: int, n: int
    ) -> List[Candle]:
        """Return up to *n* most recent completed candles, oldest first."""
        history = self._history.get((symbol, timeframe_s), deque())
        return list(history)[-n:]

    def flush_inflight(self, symbol: str) -> List[Candle]:
        """Force-close any open candle for *symbol* (useful on shutdown)."""
        now_ms = int(time.time() * 1000)
        out: List[Candle] = []
        for tf in self.timeframes:
            key = (symbol, tf)
            inflight = self._inflight.pop(key, None)
            if inflight:
                out.append(inflight.to_candle(now_ms))
        return out


# ────────────────────────────────
# Helpers
# ────────────────────────────────


def _bucket_start(timestamp_ms: int, timeframe_s: int) -> int:
    """Return the unix-millisecond start of the bucket that contains
    *timestamp_ms* for the given *timeframe_s*.
    """
    return (timestamp_ms // (timeframe_s * 1000)) * (timeframe_s * 1000)
