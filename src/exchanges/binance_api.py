"""Binance REST + WebSocket client (free public API).

Provides klines, 24h ticker, order book, and aggregate-trade WebSocket
streams.  A small TTL cache protects against redundant REST calls and
helps stay under Binance’s IP-based rate limits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import websockets
from websockets.typing import Data

logger = logging.getLogger(__name__)

REST_BASE = "https://api.binance.com"
WS_BASE = "wss://stream.binance.com:9443/ws"
AGG_TRADE_STREAM = "{symbol}@aggTrade"

DEFAULT_CACHE_TTL_SECONDS = 2.0
MAX_WS_BACKOFF = 30.0
INITIAL_WS_BACKOFF = 1.0


@dataclass(frozen=True, slots=True)
class BinanceKline:
    """Single candle from Binance klines endpoint."""

    open_time_ms: int
    close_time_ms: int
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    quote_volume: float
    trades: int


@dataclass(frozen=True, slots=True)
class BinanceAggTrade:
    """Aggregate trade from the WebSocket stream."""

    symbol: str
    price: float
    quantity: float
    is_buyer_maker: bool
    timestamp_ms: int
    agg_id: int


@dataclass(frozen=True, slots=True)
class BinanceTicker24h:
    """24-hour rolling statistics."""

    symbol: str
    price_change: float
    price_change_percent: float
    weighted_avg_price: float
    last_price: float
    volume: float
    quote_volume: float


@dataclass(frozen=True, slots=True)
class BinanceOrderBook:
    """Top-N snapshot (REST)."""

    symbol: str
    bids: List[Tuple[float, float]]  # (price, qty)
    asks: List[Tuple[float, float]]
    last_update_id: int


# ────────────────────────────────
# REST client
# ────────────────────────────────


class BinanceRESTClient:
    """Async REST wrapper around Binance public market data.

    Every GET request is cached for ``cache_ttl`` seconds to reduce the
    chance of hitting Binance’s IP-based limits.
    """

    def __init__(
        self,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        base_url: str = REST_BASE,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._cache_ttl = cache_ttl
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Accept": "application/json"},
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "BinanceRESTClient":
        await self.open()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ── Low-level ──

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if self._session is None:
            raise RuntimeError("Session not open — call .open() first")
        cache_key = f"{path}?{json.dumps(params, sort_keys=True) if params else ''}"
        async with self._lock:
            entry = self._cache.get(cache_key)
            if entry is not None:
                inserted, data = entry
                if time.monotonic() - inserted < self._cache_ttl:
                    return data
        url = f"{self._base}{path}"
        async with self._session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
            async with self._lock:
                self._cache[cache_key] = (time.monotonic(), data)
            return data

    # ── Public endpoints ──

    async def klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 500,
    ) -> List[BinanceKline]:
        """Fetch klines (candles) for a Binance *symbol* (e.g. ``BTCUSDT``).

        *interval* is a Binance interval string (1m, 5m, 15m, 1h, etc.).
        """
        raw = await self._get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        out: List[BinanceKline] = []
        for row in raw:
            try:
                out.append(
                    BinanceKline(
                        open_time_ms=int(row[0]),
                        close_time_ms=int(row[6]),
                        open_price=float(row[1]),
                        high_price=float(row[2]),
                        low_price=float(row[3]),
                        close_price=float(row[4]),
                        volume=float(row[5]),
                        quote_volume=float(row[7]),
                        trades=int(row[8]),
                    )
                )
            except (IndexError, ValueError, TypeError) as exc:
                logger.debug("Skipping malformed kline row: %s", exc)
                continue
        return out

    async def ticker_24h(self, symbol: Optional[str] = None) -> List[BinanceTicker24h]:
        """24h ticker for one or all symbols."""
        params = {"symbol": symbol} if symbol else {}
        raw = await self._get("/api/v3/ticker/24hr", params)
        # Binance returns a dict for single symbol, list for all
        rows = [raw] if isinstance(raw, dict) else raw
        out: List[BinanceTicker24h] = []
        for row in rows:
            try:
                out.append(
                    BinanceTicker24h(
                        symbol=str(row["symbol"]),
                        price_change=float(row["priceChange"]),
                        price_change_percent=float(row["priceChangePercent"]),
                        weighted_avg_price=float(row["weightedAvgPrice"]),
                        last_price=float(row["lastPrice"]),
                        volume=float(row["volume"]),
                        quote_volume=float(row["quoteVolume"]),
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.debug("Skipping malformed ticker row: %s", exc)
                continue
        return out

    async def order_book(
        self,
        symbol: str,
        limit: int = 100,
    ) -> BinanceOrderBook:
        """Top-*limit* order book snapshot."""
        raw = await self._get(
            "/api/v3/depth",
            {"symbol": symbol, "limit": limit},
        )
        try:
            bids = [(float(p), float(q)) for p, q in raw.get("bids", [])]
            asks = [(float(p), float(q)) for p, q in raw.get("asks", [])]
            return BinanceOrderBook(
                symbol=symbol,
                bids=bids,
                asks=asks,
                last_update_id=int(raw["lastUpdateId"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise BinanceAPIError(f"Malformed order book response: {exc}") from exc

    async def exchange_info(self) -> Dict[str, Any]:
        """Raw exchange info (symbol list, filters, etc.)."""
        return await self._get("/api/v3/exchangeInfo")


# ────────────────────────────────
# Symbol helpers
# ────────────────────────────────


def to_binance_symbol(hl_symbol: str, quote: str = "USDT") -> str:
    """Convert a Hyperliquid-style symbol (``BTC``) to a Binance spot
    symbol (``BTCUSDT``).
    """
    if hl_symbol.endswith(quote):
        return hl_symbol
    return f"{hl_symbol}{quote}"


def from_binance_symbol(binance_symbol: str, quote: str = "USDT") -> str:
    """Strip the quote asset to recover the base symbol."""
    if binance_symbol.endswith(quote):
        return binance_symbol[: -len(quote)]
    return binance_symbol


# ────────────────────────────────
# WebSocket client
# ────────────────────────────────


class BinanceWSClient:
    """Streams aggregate trades from Binance for volume-flow analysis.

    Uses a single combined stream so we only maintain one WebSocket
    regardless of how many symbols we track.
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        ws_base: str = WS_BASE,
        quote_asset: str = "USDT",
    ) -> None:
        self.symbols = set(symbols or ["BTC", "ETH", "SOL"])
        self._ws_base = ws_base
        self._quote_asset = quote_asset

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._run_task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()
        self._shutdown = asyncio.Event()

        self._backoff = INITIAL_WS_BACKOFF
        self._last_heartbeat = 0.0
        self.reconnect_count = 0
        self.connected_at: Optional[float] = None

        # callback registry: symbol -> list of async callbacks
        self._callbacks: Dict[str, List[asyncio.Queue[BinanceAggTrade]]] = {}

    # ── Public API ──

    async def start(self) -> None:
        if self._run_task is not None:
            raise RuntimeError("Client already started")
        self._run_task = asyncio.create_task(self._connection_loop())
        await self._connected.wait()

    async def stop(self) -> None:
        logger.info("BinanceWSClient stopping …")
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
        logger.info("BinanceWSClient stopped")

    async def subscribe_symbol(
        self, symbol: str
    ) -> asyncio.Queue[BinanceAggTrade]:
        """Register interest in *symbol* and return an async Queue that
        will receive every aggregate trade for that symbol.
        """
        q: asyncio.Queue[BinanceAggTrade] = asyncio.Queue()
        self._callbacks.setdefault(symbol, []).append(q)
        return q

    async def unsubscribe_symbol(
        self, symbol: str, queue: asyncio.Queue[BinanceAggTrade]
    ) -> None:
        listeners = self._callbacks.get(symbol, [])
        if queue in listeners:
            listeners.remove(queue)
            if not listeners:
                del self._callbacks[symbol]

    # ── Internals ──

    def _stream_path(self) -> str:
        streams = "/".join(
            AGG_TRADE_STREAM.format(symbol=to_binance_symbol(s, self._quote_asset).lower())
            for s in sorted(self.symbols)
        )
        return f"{self._ws_base}/{streams}"

    async def _connection_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                url = self._stream_path()
                logger.info("Connecting to Binance WS %s", url)
                self._ws = await websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                )
                self.connected_at = time.time()
                self._backoff = INITIAL_WS_BACKOFF
                self._last_heartbeat = time.time()
                self._connected.set()
                logger.info("Binance WS connected")
                await self._read_loop()
            except (websockets.ConnectionClosed, websockets.InvalidHandshake) as exc:
                logger.warning("Binance WS connection error: %s", exc)
            except asyncio.CancelledError:
                logger.info("Binance WS loop cancelled")
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unexpected Binance WS error: %s", exc)

            self._connected.clear()
            if self._ws is not None:
                await self._ws.close()
                self._ws = None
            if self._shutdown.is_set():
                return

            jitter = (asyncio.get_event_loop().time() % 1) * 2.0
            wait = min(self._backoff + jitter, MAX_WS_BACKOFF)
            logger.info("Binance reconnecting in %.1fs", wait)
            await asyncio.sleep(wait)
            self._backoff = min(self._backoff * 2, MAX_WS_BACKOFF)
            self.reconnect_count += 1

    async def _read_loop(self) -> None:
        if self._ws is None:
            return
        try:
            async for raw in self._ws:
                self._last_heartbeat = time.time()
                try:
                    self._on_message(raw)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to parse Binance message: %s", exc)
        except websockets.ConnectionClosed:
            logger.info("Binance WS closed normally")

    def _on_message(self, raw: Data) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)

        # combined stream wraps each message in a "stream" envelope
        stream = payload.get("stream", "")
        data = payload.get("data", payload)

        # Determine base symbol from stream name, e.g. "btcusdt@aggTrade"
        base = stream.replace("@aggtrade", "").replace("@aggTrade", "").upper()
        base = from_binance_symbol(base, self._quote_asset)

        # Parse aggregate trade
        try:
            trade = BinanceAggTrade(
                symbol=base,
                price=float(data["p"]),
                quantity=float(data["q"]),
                is_buyer_maker=bool(data["m"]),
                timestamp_ms=int(data["T"]),
                agg_id=int(data["a"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("Skipping malformed aggTrade: %s", exc)
            return

        # Dispatch
        for q in self._callbacks.get(base, []):
            try:
                q.put_nowait(trade)
            except asyncio.QueueFull:
                pass


class BinanceAPIError(Exception):
    """Raised when a Binance REST call returns unexpected data."""
