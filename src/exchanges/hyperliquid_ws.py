import json
import logging
import time
from typing import Any, Dict, Optional

import websockets
from websockets import Data

from src.utils.config import Config
from src.utils.helpers import JSONSafetyError, safe_json_loads, safe_float

logger = logging.getLogger(__name__)

INITIAL_BACKOFF_SECONDS = 1
MAX_BACKOFF_SECONDS = 30
RECONNECT_JITTER_MAX = 2
HEARTBEAT_INTERVAL_SECONDS = 30

class HyperliquidWSClient:
    def __init__(self, config: Config, symbols: list, bus=None, candle_builder: Optional[Any] = None):
        self.symbols = symbols
        self.ws_url = config.get("exchange.hyperliquid_ws_url", "wss://api.hyperliquid.xyz/ws")
        self._bus = bus
        self._candle_builder = candle_builder
        self._ws = None
        self._shutdown = False
        self._connected = False
        self._last_heartbeat = 0
        self._backoff = INITIAL_BACKOFF_SECONDS
        self.reconnect_count = 0
        self.connected_at = 0.0
        self.messages_received = 0

    async def start(self):
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

    async def _subscribe_all(self):
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        for sym in self.symbols:
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": sym}}))
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": sym}}))
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": sym}}))

    async def _read_loop(self):
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

    def _parse_all_mids(self, data):
        if not isinstance(data, dict):
            return
        for symbol, price_str in data.items():
            try:
                price = safe_float(price_str)
                if price > 0:
                    self._bus.publish(f"price:{symbol}", {"symbol": symbol, "price": price})
            except (ValueError, TypeError):
                continue

    def _parse_active_asset_ctx(self, data):
        if not isinstance(data, dict):
            return
        coin = data.get("coin")
        if not coin:
            return
        ctx = data.get("ctx", {})
        funding = safe_float(ctx.get("funding"), 0.0)
        open_interest = safe_float(ctx.get("openInterest"), 0.0)
        self._bus.publish(f"funding:{coin}", {"symbol": coin, "funding": funding, "open_interest": open_interest})

    def _parse_trades(self, data):
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

    def _parse_l2_book(self, data):
        if not isinstance(data, dict):
            return
        coin = data.get("coin")
        if not coin:
            return
        levels = data.get("levels", [])
        bids = []
        asks = []
        if isinstance(levels, list) and len(levels) >= 2:
            for lvl in levels[0]:
                if isinstance(lvl, dict):
                    bids.append({"price": safe_float(lvl.get("px")), "size": safe_float(lvl.get("sz"))})
            for lvl in levels[1]:
                if isinstance(lvl, dict):
                    asks.append({"price": safe_float(lvl.get("px")), "size": safe_float(lvl.get("sz"))})
        self._bus.publish(f"l2book:{coin}", {"symbol": coin, "bids": bids, "asks": asks})

    async def stop(self):
        self._shutdown = True
        if self._ws:
            await self._ws.close()
