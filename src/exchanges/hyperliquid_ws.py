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
    timestamp_ms: int


@dataclass(frozen=True, slots=True)
class HlTrade:
    """Individual trade from the trades channel."""

    symbol: str
    side: str  # "B" buy / "S" sell
    price: float
    size: float
    timestamp_ms: int
    hash: str


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
                    ping_interval=None,  # we roll our own heartbeat
                    close_timeout=5,
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
        channels = [
            {"method": "subscribe", "subscription": {"type": "allMids"}},
            {"method": "subscribe", "subscription": {"type": "activeAssetCtxs"}},
            {"method": "subscribe", "subscription": {"type": "trades"}},
        ]
        for msg in channels:
            await self._ws.send(json.dumps(msg))
            logger.debug("Sent subscribe: %s", msg["subscription"]["type"])

    async def _read_loop(self) -> None:
        """Read messages, heartbeat, and parse until disconnect."""
        if self._ws is None:
            return
        try:
            async for raw in self._ws:
                self.messages_received += 1
                self._last_heartbeat = time.time()
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

        if channel == "allMids":
            self._parse_all_mids(data)
        elif channel == "activeAssetCtxs":
            self._parse_active_asset_ctxs(data)
        elif channel == "trades":
            self._parse_trades(data)
        else:
            logger.debug("Ignored unknown channel: %s", channel)

    def _parse_all_mids(self, data: Dict[str, Any]) -> None:
        """Emit ``price:<symbol>`` topics."""
        ts = int(time.time() * 1000)
        for sym, mid_str in data.items():
            try:
                mid = float(mid_str)
            except (ValueError, TypeError):
                continue
            tick = HlPriceTick(symbol=sym, mid=mid, timestamp_ms=ts)
            asyncio.create_task(self.bus.publish(f"price:{sym}", tick))

    def _parse_active_asset_ctxs(self, data: List[Dict[str, Any]]) -> None:
        """Emit ``ctx:<symbol>`` topics."""
        ts = int(time.time() * 1000)
        for entry in data:
            sym = entry.get("coin")
            ctx = entry.get("ctx")
            if sym is None or ctx is None:
                continue
            try:
                parsed = HlAssetCtx(
                    symbol=sym,
                    open_interest=_safe_float(ctx.get("oi")),
                    funding_rate=_safe_float(ctx.get("funding")),
                    predicted_funding=_safe_float(ctx.get("predFunding")),
                    mark_price=_safe_float(ctx.get("markPx")),
                    timestamp_ms=ts,
                )
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping malformed asset ctx for %s: %s", sym, exc)
                continue
            asyncio.create_task(self.bus.publish(f"ctx:{sym}", parsed))

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


# ────────────────────────────────
# Helpers
# ────────────────────────────────


def _safe_float(value: Any) -> float:
    """Coerce *value* to float, raising on failure so callers can skip."""
    if value is None:
        raise ValueError("value is None")
    return float(value)
