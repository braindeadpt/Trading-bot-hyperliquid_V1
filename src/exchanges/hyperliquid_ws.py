"""Hyperliquid WebSocket client with DataBus distribution."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

import websockets
from websockets.typing import Data

logger = logging.getLogger(__name__)

# ────────────────────────────────
# Data structures
# ────────────────────────────────


@dataclass(frozen=True, slots=True)
class HlPriceTick:
    """Snapshot from Hyperliquid allMids."""

    symbol: str
    mid: float
    timestamp_ms: int


@dataclass(frozen=True, slots=True)
class HlAssetCtx:
    """Per-asset context from activeAssetCtxs."""

    symbol: str
    open_interest: float
    funding_rate: float
    predicted_funding: float
    mark_price: float
    mid_price: float = 0.0
    timestamp_ms: int = 0


@dataclass(frozen=True, slots=True)
class HlTrade:
    """Individual trade from the trades channel."""

    symbol: str
    side: str  # "B" buy / "S" sell
    price: float
    size: float
    timestamp_ms: int
    hash: str


@dataclass(frozen=True, slots=True)
class HlPriceLevel:
    """Single price level in L2 orderbook."""

    price: float
    size: float


@dataclass(frozen=True, slots=True)
class HlOrderbook:
    """L2 orderbook snapshot from Hyperliquid.

    Bids are sorted descending by price.
    Asks are sorted ascending by price.
    """

    symbol: str
    bids: List[HlPriceLevel]
    asks: List[HlPriceLevel]
    timestamp_ms: int


# ────────────────────────────────
# DataBus
# ────────────────────────────────


class DataBus:
    """Async-safe pub/sub message broker.

    Topics are simple strings, e.g. ``"price:BTC"``, ``"candle:5m:ETH"``.
    Callbacks are invoked via ``asyncio.create_task`` so the publisher
    never blocks.  A small in-memory LRU holds the latest value per topic
    for synchronous consumers.
    """

    def __init__(self, latest_cache_size: int = 256) -> None:
        self._subs: Dict[str, Set[Callable[[Any], Any]]] = {}
        self._latest: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._latest_cache_size = latest_cache_size

    async def subscribe(self, topic: str, callback: Callable[[Any], Any]) -> None:
        """Register *callback* for *topic*.

        The callback may be sync or async; async callbacks are awaited.
        """
        async with self._lock:
            self._subs.setdefault(topic, set()).add(callback)
        logger.debug("Subscribed to topic=%s", topic)

    async def unsubscribe(self, topic: str, callback: Callable[[Any], Any]) -> None:
        async with self._lock:
            listeners = self._subs.get(topic)
            if listeners:
                listeners.discard(callback)
                if not listeners:
                    del self._subs[topic]

    async def publish(self, topic: str, data: Any) -> None:
        """Emit *data* to every subscriber of *topic*."""
        async with self._lock:
            self._latest[topic] = data
            listeners = list(self._subs.get(topic, []))

        for cb in listeners:
            try:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(data))
                else:
                    cb(data)
            except Exception as exc:  # noqa: BLE001
                logger.warning("DataBus callback error on %s: %s", topic, exc)

    def get_latest(self, topic: str) -> Optional[Any]:
        """Return the most recently published value for *topic*, or *None*."""
        return self._latest.get(topic)

    def topics(self) -> List[str]:
        """Return a snapshot of all topics that currently have subscribers."""
        return list(self._subs.keys())


# ────────────────────────────────
# Hyperliquid WS client
# ────────────────────────────────

WS_URL = "wss://api.hyperliquid.xyz/ws"
MAX_BACKOFF_SECONDS = 30.0
INITIAL_BACKOFF_SECONDS = 1.0
HEARTBEAT_INTERVAL_SECONDS = 20.0
RECONNECT_JITTER_MAX = 2.0


class HyperliquidWSClient:
    """WebSocket client for the Hyperliquid real-time API.

    Automatically reconnects with capped exponential backoff and dispatches
    parsed messages through the shared :class:`DataBus`.
    """

    def __init__(
        self,
        bus: DataBus,
        symbols: Optional[List[str]] = None,
        ws_url: str = WS_URL,
    ) -> None:
        self.bus = bus
        self.symbols = set(symbols or ["BTC", "ETH", "SOL"])
        self.ws_url = ws_url

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._run_task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()
        self._shutdown = asyncio.Event()

        # Reconnection state
        self._backoff = INITIAL_BACKOFF_SECONDS
        self._last_heartbeat = 0.0

        # Connection metadata (exposed for health checks)
        self.connected_at: Optional[float] = None
        self.messages_received = 0
        self.reconnect_count = 0

    # ── Public API ──

    async def start(self) -> None:
        """Begin the connection loop (non-blocking)."""
        if self._run_task is not None:
            raise RuntimeError("Client already started")
        self._run_task = asyncio.create_task(self._connection_loop())
        await self._connected.wait()

    async def stop(self) -> None:
        """Gracefully close the WebSocket and cancel the loop."""
        logger.info("HyperliquidWSClient stopping …")
        self._shutdown.set()
        if self._ws is not None:
            await self._ws.close()
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        self._run_task = None
        logger.info("HyperliquidWSClient stopped")

    async def wait_connected(self) -> bool:
        """Block until the WebSocket is open."""
        return await self._connected.wait()

    # ── Internals ──

    async def _connection_loop(self) -> None:
        """Outer loop: connect → run → back-off → repeat."""
        while not self._shutdown.is_set():
            try:
                logger.info("Connecting to %s", self.ws_url)
                self._ws = await websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    open_timeout=10,
                )
                self.connected_at = time.time()
                self._backoff = INITIAL_BACKOFF_SECONDS
                self._last_heartbeat = time.time()
                self._connected.set()
                logger.info("Hyperliquid WS connected")

                await self._subscribe_all()
                await self._read_loop()

            except (websockets.ConnectionClosed, websockets.InvalidHandshake) as exc:
                logger.warning("WS connection error: %s", exc)
            except asyncio.CancelledError:
                logger.info("Connection loop cancelled")
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unexpected WS error: %s", exc)

            # teardown
            self._connected.clear()
            if self._ws is not None:
                await self._ws.close()
                self._ws = None

            if self._shutdown.is_set():
                return

            # exponential back-off with jitter
            jitter = (asyncio.get_event_loop().time() % 1) * RECONNECT_JITTER_MAX
            wait = min(self._backoff + jitter, MAX_BACKOFF_SECONDS)
            logger.info("Reconnecting in %.1fs (attempt #%d)", wait, self.reconnect_count)
            await asyncio.sleep(wait)
            self._backoff = min(self._backoff * 2, MAX_BACKOFF_SECONDS)
            self.reconnect_count += 1

    async def _subscribe_all(self) -> None:
        """Send subscription messages for every channel we need."""
        if self._ws is None:
            return
        # Subscribe to allMids (prices for all assets)
        await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        logger.info("Subscribed to allMids")
        # Subscribe to activeAssetCtx per symbol (funding, OI)
        for sym in self.symbols:
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": sym}}))
            logger.info("Subscribed to activeAssetCtx for %s", sym)
        # Subscribe to trades per symbol
        for sym in self.symbols:
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": sym}}))
            logger.info("Subscribed to trades for %s", sym)
        # Subscribe to L2 orderbook per symbol
        for sym in self.symbols:
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": sym}}))
            logger.info("Subscribed to l2Book for %s", sym)

    async def _read_loop(self) -> None:
        """Read messages, heartbeat, and parse until disconnect."""
        if self._ws is None:
            return
        logger.info("_read_loop started")
        try:
            async for raw in self._ws:
                self.messages_received += 1
                self._last_heartbeat = time.time()
                if self.messages_received <= 5:
                    logger.info("WS raw msg #%d: %s...", self.messages_received, str(raw)[:80])
                try:
                    self._on_message(raw)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to parse message: %s", exc)
                # periodic heartbeat check (soft)
                if time.time() - self._last_heartbeat > HEARTBEAT_INTERVAL_SECONDS * 3:
                    logger.warning("Heartbeat timeout — forcing reconnect")
                    break
        except websockets.ConnectionClosed:
            logger.info("WebSocket closed normally")

    # ── Message parsing ──

    def _on_message(self, raw: Data) -> None:
        """Route raw JSON to the correct parser."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)

        channel = payload.get("channel")
        data = payload.get("data")
        if channel is None or data is None:
            return
        self.messages_received += 1
        logger.debug("WS channel=%s data_len=%d", channel, len(str(data)))
        if channel == "allMids":
            self._parse_all_mids(data)
        elif channel == "activeAssetCtx":
            self._parse_active_asset_ctx(data)
        elif channel == "trades":
            self._parse_trades(data)
        elif channel == "l2Book":
            self._parse_l2_book(data)
        else:
            logger.debug("Ignored unknown channel: %s", channel)

    def _parse_all_mids(self, data: Dict[str, Any]) -> None:
        """Emit ``price:<symbol>`` topics."""
        ts = int(time.time() * 1000)
        mids = data.get("mids", data)  # Handle both {"mids": {...}} and raw dict
        if not isinstance(mids, dict):
            return
        for sym, mid_str in mids.items():
            try:
                mid = float(mid_str)
            except (ValueError, TypeError):
                continue
            tick = HlPriceTick(symbol=sym, mid=mid, timestamp_ms=ts)
            asyncio.create_task(self.bus.publish(f"price:{sym}", tick))
            logger.info("price:%s = %.2f", sym, mid)

    def _parse_active_asset_ctx(self, data: Dict[str, Any]) -> None:
        """Emit ``ctx:<symbol>`` topics."""
        sym = data.get("coin")
        ctx = data.get("ctx")
        if sym is None or ctx is None:
            return
        ts = int(time.time() * 1000)
        try:
            parsed = HlAssetCtx(
                symbol=sym,
                open_interest=_safe_float(ctx.get("openInterest")),
                funding_rate=_safe_float(ctx.get("funding")),
                predicted_funding=_safe_float(ctx.get("premium")),
                mark_price=_safe_float(ctx.get("markPx")),
                mid_price=_safe_float(ctx.get("midPx")),
                timestamp_ms=ts,
            )
        except (ValueError, TypeError) as exc:
            logger.debug("Skipping malformed asset ctx for %s: %s", sym, exc)
            return
        asyncio.create_task(self.bus.publish(f"ctx:{sym}", parsed))
        logger.info("ctx:%s funding=%.6f oi=%.2f", sym, parsed.funding_rate, parsed.open_interest)

    def _parse_trades(self, data: List[Dict[str, Any]]) -> None:
        """Emit ``trade:<symbol>`` topics."""
        for t in data:
            sym = t.get("coin")
            if sym is None:
                continue
            try:
                trade = HlTrade(
                    symbol=sym,
                    side=t.get("side", ""),
                    price=_safe_float(t.get("px")),
                    size=_safe_float(t.get("sz")),
                    timestamp_ms=int(t.get("time", time.time() * 1000)),
                    hash=t.get("hash", ""),
                )
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping malformed trade for %s: %s", sym, exc)
                continue
            asyncio.create_task(self.bus.publish(f"trade:{sym}", trade))

    def _parse_l2_book(self, data: Dict[str, Any]) -> None:
        """Emit ``orderbook:<symbol>`` topics."""
        sym = data.get("coin")
        levels = data.get("levels")
        if sym is None or levels is None:
            return
        ts = int(time.time() * 1000)
        try:
            # levels[0] = bids, levels[1] = asks
            # Each level is [price, size]
            raw_bids = levels[0] if len(levels) > 0 else []
            raw_asks = levels[1] if len(levels) > 1 else []
            bids = [HlPriceLevel(price=float(p), size=float(s)) for p, s in raw_bids]
            asks = [HlPriceLevel(price=float(p), size=float(s)) for p, s in raw_asks]
            book = HlOrderbook(
                symbol=sym,
                bids=bids,
                asks=asks,
                timestamp_ms=ts,
            )
        except (ValueError, TypeError, IndexError) as exc:
            logger.debug("Skipping malformed l2Book for %s: %s", sym, exc)
            return
        asyncio.create_task(self.bus.publish(f"orderbook:{sym}", book))
        logger.info("orderbook:%s bids=%d asks=%d", sym, len(bids), len(asks))


# ────────────────────────────────
# Helpers
# ────────────────────────────────


def _safe_float(value: Any) -> float:
    """Coerce *value* to float, raising on failure so callers can skip."""
    if value is None:
        raise ValueError("value is None")
    return float(value)
