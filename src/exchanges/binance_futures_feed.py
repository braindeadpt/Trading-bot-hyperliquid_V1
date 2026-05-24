"""Binance USD-M futures feeds: force-order liquidations + long/short ratio.

Publishes to DataBus:
  * ``liquidation:{symbol}`` → :class:`LiquidationEvent`
  * ``ls_ratio:{symbol}``     → :class:`LongShortRatio`
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from src.exchanges.binance_api import to_binance_symbol
from src.exchanges.hyperliquid_ws import DataBus

logger = logging.getLogger(__name__)

FUTURES_REST = "https://fapi.binance.com"
FUTURES_WS = "wss://fstream.binance.com/stream"
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 30.0


@dataclass(frozen=True, slots=True)
class LiquidationEvent:
    """Single forced liquidation from Binance USD-M futures."""
    symbol: str          # base asset, e.g. BTC
    timestamp_ms: int
    notional_usd: float
    side: str            # "long" = longs liquidated, "short" = shorts liquidated
    source: str = "binance"


@dataclass(frozen=True, slots=True)
class LongShortRatio:
    """Global long/short account ratio snapshot."""
    symbol: str
    long_ratio: float    # 0–1 fraction of accounts long
    short_ratio: float   # 0–1 fraction of accounts short
    long_short_ratio: float
    timestamp_ms: int


class BinanceFuturesFeed:
    """Streams liquidations and polls long/short ratio for configured symbols."""

    def __init__(
        self,
        bus: DataBus,
        symbols: Optional[List[str]] = None,
        poll_interval_sec: float = 300.0,
    ) -> None:
        self._bus = bus
        self._symbols = list(symbols or ["BTC", "ETH", "SOL"])
        self._poll_interval_sec = poll_interval_sec
        self._shutdown = False
        self._ws_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        if self._ws_task is not None:
            return
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"Accept": "application/json"},
        )
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._poll_task = asyncio.create_task(self._poll_long_short_loop())
        logger.info(
            "BinanceFuturesFeed started (symbols=%s, ls_poll=%ds)",
            self._symbols,
            int(self._poll_interval_sec),
        )

    async def stop(self) -> None:
        self._shutdown = True
        for task in (self._ws_task, self._poll_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._ws_task = None
        self._poll_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        logger.info("BinanceFuturesFeed stopped")

    def _stream_url(self) -> str:
        streams = "/".join(
            f"{to_binance_symbol(sym).lower()}@forceOrder"
            for sym in sorted(self._symbols)
        )
        return f"{FUTURES_WS}?streams={streams}"

    async def _ws_loop(self) -> None:
        backoff = INITIAL_BACKOFF
        while not self._shutdown:
            try:
                url = self._stream_url()
                logger.info("Binance futures WS connecting: %s", url)
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    backoff = INITIAL_BACKOFF
                    async for raw in ws:
                        if self._shutdown:
                            break
                        self._on_ws_message(raw)
            except ConnectionClosed as exc:
                logger.warning("Binance futures WS closed: %s", exc)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Binance futures WS error: %s", exc)

            if self._shutdown:
                return
            await asyncio.sleep(min(backoff, MAX_BACKOFF))
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _on_ws_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        data = payload.get("data", payload)
        if data.get("e") != "forceOrder":
            return

        order = data.get("o") or {}
        binance_sym = str(order.get("s", ""))
        base = binance_sym.replace("USDT", "")
        if base not in self._symbols:
            return

        side_raw = str(order.get("S", "")).upper()
        # SELL force order → long position liquidated; BUY → short liquidated
        if side_raw == "SELL":
            liq_side = "long"
        elif side_raw == "BUY":
            liq_side = "short"
        else:
            return

        qty = float(order.get("l") or order.get("z") or order.get("q") or 0.0)
        price = float(order.get("ap") or order.get("p") or 0.0)
        notional = qty * price
        if notional <= 0:
            return

        ts = int(data.get("E") or order.get("T") or time.time() * 1000)
        event = LiquidationEvent(
            symbol=base,
            timestamp_ms=ts,
            notional_usd=notional,
            side=liq_side,
        )
        self._bus.publish(f"liquidation:{base}", event)
        logger.debug(
            "Binance liquidation %s %s $%.0f",
            base, liq_side, notional,
        )

    async def _poll_long_short_loop(self) -> None:
        while not self._shutdown:
            try:
                await self._poll_long_short_ratios()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Long/short ratio poll failed: %s", exc)
            try:
                await asyncio.sleep(self._poll_interval_sec)
            except asyncio.CancelledError:
                return

    async def _poll_long_short_ratios(self) -> None:
        if self._session is None:
            return
        for sym in self._symbols:
            binance_sym = to_binance_symbol(sym)
            url = f"{FUTURES_REST}/futures/data/globalLongShortAccountRatio"
            params = {"symbol": binance_sym, "period": "5m", "limit": 1}
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.debug("LS ratio HTTP %s for %s", resp.status, sym)
                        continue
                    rows = await resp.json()
                    if not rows:
                        continue
                    row = rows[0]
                    long_ratio = float(row.get("longAccount", 0.5))
                    short_ratio = float(row.get("shortAccount", 0.5))
                    ls_ratio = float(row.get("longShortRatio", 1.0))
                    ts = int(row.get("timestamp", time.time() * 1000))
                    snap = LongShortRatio(
                        symbol=sym,
                        long_ratio=long_ratio,
                        short_ratio=short_ratio,
                        long_short_ratio=ls_ratio,
                        timestamp_ms=ts,
                    )
                    self._bus.publish(f"ls_ratio:{sym}", snap)
            except Exception as exc:
                logger.debug("LS ratio fetch failed for %s: %s", sym, exc)
