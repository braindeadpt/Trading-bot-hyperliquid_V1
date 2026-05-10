"""Hyperliquid WebSocket client with automatic reconnect and L2 book support."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable

import websockets
from websockets import Data

from src.utils.config import Config
from src.utils.helpers import JSONSafetyError, safe_json_loads, safe_float

logger = logging.getLogger(__name__)

INITIAL_BACKOFF_SECONDS = 1
MAX_BACKOFF_SECONDS = 30
RECONNECT_JITTER_MAX = 2
HEARTBEAT_INTERVAL_SECONDS = 30


# ---------------------------------------------------------------------------
# Simple DataBus for intra-process pub/sub
# ---------------------------------------------------------------------------

class DataBus:
    """Simple publish/subscribe message bus (thread-safe)."""

    def __init__(self) -> None:
        self._listeners: Dict[str, List[Callable[[Any], Any]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str, callback: Callable[[Any], Any]) -> None:
        async with self._lock:
            self._listeners.setdefault(topic, []).append(callback)

    def publish(self, topic: str, data: Any) -> None:
        listeners = self._listeners.get(topic, [])
        for cb in listeners:
            try:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(data))
                else:
                    cb(data)
            except Exception:
                logger.exception("DataBus callback error on %s", topic)


# ---------------------------------------------------------------------------
# Hyperliquid data types
# ---------------------------------------------------------------------------

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
class HlL2Level:
    """Single level in the L2 orderbook."""
    price: float
    size: float
    timestamp_ms: int = 0


@dataclass(frozen=True, slots=True)
class HlL2Book:
    """L2 orderbook snapshot."""
    symbol: str
    bids: List[HlL2Level]
    asks: List[HlL2Level]
    timestamp_ms: int = 0


# Backward compatibility alias
HlOrderbook = HlL2Book


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------

class HyperliquidWSClient:
    """Hyperliquid WebSocket client with auto-reconnect."""

    def __init__(
        self,
        config: Config,
        symbols: list,
        bus: Optional[DataBus] = None,
        candle_builder: Optional[Any] = None,
    ) -> None:
        self.symbols = symbols
        self.ws_url = config.get("exchange.hyperliquid_ws_url", "wss://api.hyperliquid.xyz/ws")
        self._bus = bus
        self._candle_builder = candle_builder
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._shutdown = False
        self._connected = False
        self._last_heartbeat = 0.0
        self._backoff = INITIAL_BACKOFF_SECONDS
        self.reconnect_count = 0
        self.connected_at = 0.0
        self.messages_received = 0

    async def start(self) -> None:
        """Main connection loop with exponential backoff reconnect."""
        while not self._shutdown:
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
                self._connected = True
                logger.info("Hyperliquid WS connected")
                await self._subscribe_all()
                await self._read_loop()
            except (websockets.ConnectionClosed, websockets.InvalidHandshake) as exc:
                logger.warning("WS connection error: %s", exc)
            except asyncio.CancelledError:
                logger.info("Connection loop cancelled")
                return
            except OSError as exc:
                logger.error("WS OS error: %s", exc)
            except Exception as exc:
                logger.exception("Unexpected WS error: %s", exc)

            self._connected = False
            if self._ws is not None:
                await self._ws.close()
                self._ws = None
            if self._shutdown:
                return
            jitter = (asyncio.get_event_loop().time() % 1) * RECONNECT_JITTER_MAX
            wait = min(self._backoff + jitter, MAX_BACKOFF_SECONDS)
            logger.info("Reconnecting in %.1fs (attempt #%d)", wait, self.reconnect_count)
            await asyncio.sleep(wait)
            self._backoff = min(self._backoff * 2, MAX_BACKOFF_SECONDS)
            self.reconnect_count += 1

    async def _subscribe_all(self) -> None:
        """Subscribe to all relevant channels."""
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        for sym in self.symbols:
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": sym}}))
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": sym}}))
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": sym}}))

    async def _read_loop(self) -> None:
        """Read messages from the WebSocket until disconnect."""
        if self._ws is None:
            return
        logger.info("_read_loop started")
        try:
            async for raw in self._ws:
                self.messages_received += 1
                self._last_heartbeat = time.time()
                try:
                    self._on_message(raw)
                except JSONSafetyError as exc:
                    logger.warning("JSON safety violation from WS: %s", exc)
                except json.JSONDecodeError as exc:
                    logger.warning("Failed to decode WS JSON: %s", exc)
                except KeyError as exc:
                    logger.warning("Missing key in WS message: %s", exc)
                except (ValueError, TypeError) as exc:
                    logger.warning("Invalid data in WS message: %s", exc)
                if time.time() - self._last_heartbeat > HEARTBEAT_INTERVAL_SECONDS * 3:
                    logger.warning("Heartbeat timeout — forcing reconnect")
                    break
        except websockets.ConnectionClosed:
            logger.info("WebSocket closed normally")

    def _on_message(self, raw: Data) -> None:
        """Dispatch a single WebSocket message to the appropriate parser."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = safe_json_loads(raw)
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
            logger.debug("Unknown WS channel: %s", channel)

    def _parse_all_mids(self, data: Any) -> None:
        """Parse allMids snapshot and publish price ticks."""
        if not isinstance(data, dict):
            return
        for symbol, price_str in data.items():
            try:
                price = safe_float(price_str)
                if price > 0:
                    self._bus.publish(f"price:{symbol}", {"symbol": symbol, "price": price})
            except (ValueError, TypeError):
                continue

    def _parse_active_asset_ctx(self, data: Any) -> None:
        """Parse activeAssetCtx and publish funding/oi data."""
        if not isinstance(data, dict):
            return
        coin = data.get("coin")
        if not coin:
            return
        ctx = data.get("ctx", {})
        funding = safe_float(ctx.get("funding"), 0.0)
        open_interest = safe_float(ctx.get("openInterest"), 0.0)
        self._bus.publish(f"funding:{coin}", {"symbol": coin, "funding": funding, "open_interest": open_interest})

    def _parse_trades(self, data: Any) -> None:
        """Parse trades channel and publish individual trades."""
        if not isinstance(data, list):
            return
        for trade in data:
            if not isinstance(trade, dict):
                continue
            try:
                self._bus.publish("trade", {
                    "symbol": trade.get("coin"),
                    "price": safe_float(trade.get("px")),
                    "size": safe_float(trade.get("sz")),
                    "side": trade.get("side"),
                    "timestamp_ms": int(trade.get("time", 0)),
                })
            except (ValueError, TypeError, KeyError):
                continue

    def _parse_l2_book(self, data: Any) -> None:
        """Parse L2 orderbook and publish bids/asks."""
        if not isinstance(data, dict):
            return
        coin = data.get("coin")
        if not coin:
            return
        levels = data.get("levels", [])
        bids: List[Dict[str, float]] = []
        asks: List[Dict[str, float]] = []
        if isinstance(levels, list) and len(levels) >= 2:
            for lvl in levels[0]:
                if isinstance(lvl, dict):
                    bids.append({"price": safe_float(lvl.get("px")), "size": safe_float(lvl.get("sz"))})
            for lvl in levels[1]:
                if isinstance(lvl, dict):
                    asks.append({"price": safe_float(lvl.get("px")), "size": safe_float(lvl.get("sz"))})
        self._bus.publish(f"l2book:{coin}", {"symbol": coin, "bids": bids, "asks": asks})

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._shutdown = True
        if self._ws:
            await self._ws.close()
