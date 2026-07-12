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
import collections
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, Union

import websockets
from websockets.exceptions import ConnectionClosed

from src.exchanges.funding_normalize import (
    DEFAULT_HL_FUNDING_INTERVAL_HOURS,
    parse_optional_rate,
)

logger = logging.getLogger(__name__)

# Reconnect / heartbeat constants
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
RECONNECT_JITTER_MAX = 1.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
MAX_RECONNECT_ATTEMPTS = 999999  # Never give up — keep trying forever
MAX_RECONNECT_WARN_INTERVAL = 20  # Log a warning every this many attempts

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
    funding_rate: Optional[float] = None
    predicted_funding: Optional[float] = None
    funding_interval_hours: float = 1.0
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
    """Simple publish/subscribe message bus (thread-safe).

    B13: adds a per-topic publish-rate limiter. When a topic exceeds
    ``rate_limit_hz`` messages within a 1-second window, subsequent
    publishes are dropped (and counted) to prevent a reconnect storm
    from queueing thousands of tasks on the event loop. Set
    ``rate_limit_hz=0`` to disable.

    QW4 (v3.1.13): per-topic overrides via ``topic_rate_limits`` dict.
    Maps a topic prefix (e.g. ``"trade:"``) to a higher Hz limit so
    high-frequency trade ticks on BTC/ETH/SOL don't get dropped while
    the 200 Hz global cap still protects noisy control topics.
    """

    DEFAULT_RATE_LIMIT_HZ = 200
    _RATE_WINDOW_SEC = 1.0

    def __init__(
        self,
        rate_limit_hz: int = DEFAULT_RATE_LIMIT_HZ,
        topic_rate_limits: Optional[Dict[str, int]] = None,
    ) -> None:
        self._listeners: Dict[str, List[Callable[[Any], Any]]] = {}
        self._lock = asyncio.Lock()
        self._tasks: set = set()  # Keep strong refs to prevent Python 3.14 deallocation crash
        self._rate_limit_hz = max(0, int(rate_limit_hz))
        self._topic_rate_limits: Dict[str, int] = dict(topic_rate_limits or {})
        # v3.1.21: per-topic sliding window uses collections.deque so
        # trimming the front (popleft) is O(1) instead of O(n).
        # At 2000 Hz trade rate the old list.pop(0) was ~4M ops/sec
        # and starved the event loop.
        self._rate_window: Dict[str, Deque[float]] = {}
        self._dropped_total: Dict[str, int] = {}
        self._last_drop_warn: Dict[str, float] = {}

    def _limit_for(self, topic: str) -> int:
        """Return the per-topic rate limit (most specific prefix match)."""
        if not self._topic_rate_limits:
            return self._rate_limit_hz
        best = self._rate_limit_hz
        best_len = -1
        for prefix, limit in self._topic_rate_limits.items():
            if topic.startswith(prefix) and len(prefix) > best_len:
                best = int(limit)
                best_len = len(prefix)
        return best

    async def subscribe(self, topic: str, callback: Callable[[Any], Any]) -> None:
        async with self._lock:
            self._listeners.setdefault(topic, []).append(callback)

    def publish(self, topic: str, data: Any) -> None:
        # ── B13 backpressure gate (with per-topic override) ──
        effective_limit = self._limit_for(topic)
        if effective_limit > 0:
            now = time.time()
            window = self._rate_window.setdefault(topic, collections.deque())
            cutoff = now - self._RATE_WINDOW_SEC
            # Trim window to current second — O(1) per popleft on deque
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= effective_limit:
                self._dropped_total[topic] = self._dropped_total.get(topic, 0) + 1
                # Warn at most once per minute per topic
                last_warn = self._last_drop_warn.get(topic, 0.0)
                if now - last_warn >= 60.0:
                    self._last_drop_warn[topic] = now
                    logger.warning(
                        "DataBus rate limit hit on '%s' (>=%d msg/s) — "
                        "dropping publishes to prevent event-loop flood",
                        topic,
                        effective_limit,
                    )
                return
            window.append(now)

        # v3.1.21: copy the listeners list before iterating so a
        # callback that subscribes / unsubscribes from inside the call
        # doesn't mutate the list we're walking.
        listeners = list(self._listeners.get(topic, []))
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

    def last_publish_age_sec(self, topic: str) -> Optional[float]:
        """Return seconds since the last successful publish on *topic*."""
        window = self._rate_window.get(topic)
        if not window:
            return None
        return max(0.0, time.time() - window[-1])

    def dropped_counts(self) -> Dict[str, int]:
        """Return a snapshot of dropped-message counters per topic."""
        return dict(self._dropped_total)

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
        self.last_message_time = 0.0
        # Raw trade tap — bypasses DataBus rate limiting (research tape / CVD path).
        self._raw_trade_listeners: List[Callable[[HlTrade], None]] = []

    def add_raw_trade_listener(self, callback: Callable[[HlTrade], None]) -> None:
        """Register a synchronous listener invoked before DataBus publish."""
        if callback not in self._raw_trade_listeners:
            self._raw_trade_listeners.append(callback)

    def remove_raw_trade_listener(self, callback: Callable[[HlTrade], None]) -> None:
        try:
            self._raw_trade_listeners.remove(callback)
        except ValueError:
            pass

    @property
    def is_healthy(self) -> bool:
        """True if connected and received a message in the last 90 seconds."""
        if not self._connected:
            return False
        if self.last_message_time > 0 and time.time() - self.last_message_time > 90:
            return False
        return True

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
            self.reconnect_count += 1
            if self.reconnect_count >= MAX_RECONNECT_ATTEMPTS:
                logger.error("WS reconnect exceeded max attempts (%d) — should never happen with MAX_RECONNECT_ATTEMPTS=%d", MAX_RECONNECT_ATTEMPTS, MAX_RECONNECT_ATTEMPTS)
                return
            if self.reconnect_count % MAX_RECONNECT_WARN_INTERVAL == 0:
                logger.warning("WS reconnect attempt #%d — possible network issue", self.reconnect_count)
            elif self.reconnect_count == 1:
                logger.warning("WS reconnect attempt #1")
            logger.info("Reconnecting in %.1fs (attempt #%d)", wait, self.reconnect_count)
            await asyncio.sleep(wait)
            self._backoff = min(self._backoff * 2, MAX_BACKOFF_SECONDS)

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
                self.last_message_time = time.time()
                self._last_heartbeat = self.last_message_time
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
            logger.debug("Parsed allMids: %d prices published", count)

    def _parse_active_asset_ctx(self, data: Any) -> None:
        """Parse active asset context (mark, index, funding, OI)."""
        symbol = data.get("coin", data.get("asset", ""))
        if not symbol:
            return
        ctx_data = data.get("ctx", data)
        # HL WS activeAssetCtx has ``funding`` only (no predFunding) — see predictedFundings INFO API.
        ctx = HlAssetCtx(
            symbol=symbol,
            timestamp_ms=int(time.time() * 1000),
            mark_price=float(ctx_data.get("markPx", 0.0)),
            index_price=float(ctx_data.get("indexPx", 0.0)),
            oracle_price=float(ctx_data.get("oraclePx", 0.0)),
            open_interest=float(ctx_data.get("openInterest", 0.0)),
            funding_rate=parse_optional_rate(ctx_data.get("funding")),
            predicted_funding=parse_optional_rate(ctx_data.get("predFunding")),
            funding_interval_hours=DEFAULT_HL_FUNDING_INTERVAL_HOURS,
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
            for listener in self._raw_trade_listeners:
                try:
                    listener(t)
                except Exception:
                    logger.exception("Raw trade listener error on %s", trade_symbol)
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
