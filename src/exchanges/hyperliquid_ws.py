"""
Hyperliquid WebSocket client with auto-reconnect, heartbeat, and JSON safety.

Publishes parsed events to the shared :class:`~exchanges.hyperliquid_ws.DataBus`:
  * ``price:{symbol}``      → :class:`HlPriceTick`
  * ``trade:{symbol}``      → :class:`HlTrade`
  * ``ctx:{symbol}``        → :class:`HlAssetCtx`
  * ``book:{symbol}``       → :class:`HlL2Book`

Usage::

    bus = DataBus()
    client = HyperliquidWSClient(
        bus=bus,
        symbols=["BTC", "ETH", "SOL"],
        ws_url="wss://api.hyperliquid.xyz/ws",
    )
    await client.start()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# Reconnect / heartbeat constants
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
RECONNECT_JITTER_MAX = 1.0
HEARTBEAT_INTERVAL_SECONDS = 30.0

Data = Union[str, bytes]


# ---------------------------------------------------------------------------
# Data models emitted to the bus
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class HlPriceTick:
    symbol: str
    timestamp_ms: int
    mid: float
    bid: float
    ask: float
    mark: float
    index: float = 0.0
    oracle_px: float = 0.0
    spread_bps: float = 0.0

@dataclass(slots=True)
class HlTrade:
    symbol: str
    timestamp_ms: int
    price: float
    size: float
    side: str  # "B" = buy, "S" = sell
    tid: int = 0

@dataclass(slots=True)
class HlAssetCtx:
    symbol: str
    timestamp_ms: int
    mark_price: float
    index_price: float
    oracle_price: float
    open_interest: float
    funding_rate: float
    predicted_funding: float
    funding_interval_hours: int = 8
    leverage: Dict[str, Any] = None  # type: ignore[assignment]
    available_margin: float = 0.0
    position: Optional[Dict[str, Any]] = None  # type: ignore[assignment]
    daily_volume: float = 0.0
    daily_trades: int = 0

@dataclass(slots=True)
class HlLevel:
    price: float
    size: float

@dataclass(slots=True)
class HlL2Book:
    symbol: str
    timestamp_ms: int
    bids: List[HlLevel]
    asks: List[HlLevel]

# Alias for backward compatibility with engine.py
HlOrderbook = HlL2Book


# ---------------------------------------------------------------------------
# DataBus
# ---------------------------------------------------------------------------

class DataBus:
    """Simple publish/subscribe message bus (thread-safe)."""

    def __init__(self) -> None:
        self._listeners: Dict[str, List[Callable[[Any], Any]]] = {}
        self._lock = asyncio.Lock()
        self._tasks: set = set()  # Keep strong refs to prevent Python 3.14 deallocation crash

    async def subscribe(self, topic: str, callback: Callable[[Any], Any]) -> None:
        async with self._lock:
            self._listeners.setdefault(topic, []).append(callback)

    def publish(self, topic: str, data: Any) -> None:
        listeners = self._listeners.get(topic, [])
        for cb in listeners:
            try:
                if asyncio.iscoroutinefunction(cb):
                    task = asyncio.create_task(cb(data))
                    # Keep strong reference to prevent task deallocation crash (Python 3.14)
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                else:
                    cb(data)
            except Exception:
                logger.exception("DataBus callback error on %s", topic)

    async def unsubscribe(self, topic: str, callback: Callable[[Any], Any]) -> None:
        async with self._lock:
            if topic in self._listeners:
                try:
                    self._listeners[topic].remove(callback)
                except ValueError:
                    pass


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------

class HyperliquidWSClient:
    """WebSocket client for Hyperliquid real-time market data."""

    def __init__(
        self,
        bus: DataBus,
        symbols: Optional[List[str]] = None,
        ws_url: str = "wss://api.hyperliquid.xyz/ws",
    ) -> None:
        self.bus = bus
        self.symbols = symbols or ["BTC", "ETH", "SOL"]
        self.ws_url = ws_url

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._shutdown = False
        self._connected = False
        self._backoff = INITIAL_BACKOFF_SECONDS
        self._last_heartbeat = 0.0

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
            while True:
                raw = await self._ws.recv()
                self.messages_received += 1
                self._last_heartbeat = time.time()
                try:
                    self._on_message(raw)
                except Exception:
                    logger.exception("WS message processing error")
                if self.messages_received % 1000 == 0:
                    logger.info("WS received %d messages", self.messages_received)
                if time.time() - self._last_heartbeat > HEARTBEAT_INTERVAL_SECONDS * 3:
                    logger.warning("Heartbeat timeout — forcing reconnect")
                    break
        except websockets.ConnectionClosed:
            logger.info("WebSocket closed normally")

    def _on_message(self, raw: Data) -> None:
        """Dispatch a single WebSocket message to the appropriate parser."""
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
            logger.debug("Unknown WS channel: %s", channel)

    def _parse_all_mids(self, data: Any) -> None:
        """Parse mid-prices for all assets (snapshot or update)."""
        # Hyperliquid API wraps allMids in {"mids": {"coin": "price", ...}}
        if isinstance(data, dict) and "mids" in data:
            data = data["mids"]
        count = 0
        if isinstance(data, dict):
            for symbol, mid in data.items():
                if not isinstance(symbol, str):
                    continue
                # mid can be a number or a dict (extract the value)
                if isinstance(mid, dict):
                    mid = mid.get("mid", mid.get("price", 0.0))
                if isinstance(mid, (int, float, str)):
                    try:
                        mid_f = float(mid)
                    except (ValueError, TypeError):
                        continue
                else:
                    continue
                tick = HlPriceTick(
                    symbol=symbol,
                    timestamp_ms=int(time.time() * 1000),
                    mid=mid_f,
                    bid=0.0,
                    ask=0.0,
                    mark=0.0,
                )
                self.bus.publish(f"price:{symbol}", tick)
                count += 1
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    symbol = item.get("coin", item.get("asset", ""))
                    if not symbol:
                        continue
                    mid = float(item.get("mid", 0.0))
                    bid = float(item.get("bid", 0.0))
                    ask = float(item.get("ask", 0.0))
                    mark = float(item.get("mark", 0.0))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    symbol, mid = item[0], float(item[1])
                    bid, ask, mark = 0.0, 0.0, 0.0
                else:
                    continue
                tick = HlPriceTick(
                    symbol=symbol,
                    timestamp_ms=int(time.time() * 1000),
                    mid=mid,
                    bid=bid,
                    ask=ask,
                    mark=mark,
                )
                self.bus.publish(f"price:{symbol}", tick)
                count += 1
        else:
            logger.debug("Unexpected allMids data type: %s", type(data).__name__)
        if count > 0:
            logger.info("Parsed allMids: %d prices published", count)

    def _parse_active_asset_ctx(self, data: Any) -> None:
        """Parse active asset context (mark, index, funding, OI)."""
        symbol = data.get("coin", data.get("asset", ""))
        if not symbol:
            return
        ctx_data = data.get("ctx", data)
        ctx = HlAssetCtx(
            symbol=symbol,
            timestamp_ms=int(time.time() * 1000),
            mark_price=float(ctx_data.get("markPx", 0.0)),
            index_price=float(ctx_data.get("indexPx", 0.0)),
            oracle_price=float(ctx_data.get("oraclePx", 0.0)),
            open_interest=float(ctx_data.get("openInterest", 0.0)),
            funding_rate=float(ctx_data.get("funding", 0.0)),
            predicted_funding=float(ctx_data.get("predFunding", 0.0)),
        )
        self.bus.publish(f"ctx:{symbol}", ctx)

    def _parse_trades(self, data: Any) -> None:
        """Parse recent trades for a symbol."""
        # trades can be either a list of trade dicts or a dict with trades inside
        if isinstance(data, list):
            # data is a list of trades, no symbol info at top level
            trades = data
            symbol = ""
        elif isinstance(data, dict):
            symbol = data.get("coin", data.get("asset", ""))
            trades = data.get("trades", [])
            if not trades and "px" in data:
                # single trade wrapped in dict
                trades = [data]
        else:
            logger.debug("Unexpected trades data type: %s", type(data).__name__)
            return

        for trade in trades:
            if not isinstance(trade, dict):
                continue
            trade_symbol = trade.get("coin", trade.get("asset", symbol))
            if not trade_symbol:
                continue
            t = HlTrade(
                symbol=trade_symbol,
                timestamp_ms=trade.get("time", int(time.time() * 1000)),
                price=float(trade.get("px", 0.0)),
                size=float(trade.get("sz", 0.0)),
                side=trade.get("side", ""),
                tid=trade.get("tid", 0),
            )
            self.bus.publish(f"trade:{trade_symbol}", t)

    def _parse_l2_book(self, data: Any) -> None:
        """Parse L2 order book for a symbol."""
        # Hyperliquid API format: {"coin": "BTC", "time": ..., "levels": [[bids], [asks]]}
        # Each level is {"px": "...", "sz": "...", "n": ...}
        symbol = data.get("coin", data.get("asset", ""))
        if not symbol:
            return
        levels = data.get("levels", [])
        if len(levels) >= 2:
            bids = [HlLevel(price=float(b["px"]), size=float(b["sz"])) for b in levels[0] if isinstance(b, dict)]
            asks = [HlLevel(price=float(a["px"]), size=float(a["sz"])) for a in levels[1] if isinstance(a, dict)]
        else:
            bids = []
            asks = []
        book = HlL2Book(
            symbol=symbol,
            timestamp_ms=data.get("time", int(time.time() * 1000)),
            bids=bids,
            asks=asks,
        )
        # Engine subscribes to "orderbook:{symbol}"
        self.bus.publish(f"orderbook:{symbol}", book)

    async def stop(self) -> None:
        """Graceful disconnect."""
        self._shutdown = True
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self._connected = False
        logger.info("Hyperliquid WS stopped")
