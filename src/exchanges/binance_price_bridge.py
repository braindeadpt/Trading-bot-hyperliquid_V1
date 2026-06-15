"""Bridge Binance spot aggregate-trade prices into the DataBus.

Binance price source
--------------------
``BinanceWSClient`` streams ``@aggTrade`` from Binance spot (``wss://stream.binance.com``).
Each trade's ``p`` field is treated as the Binance mid proxy (last trade price).

The bridge publishes ``BinanceMidTick`` on topic ``binance_price:{symbol}`` so the
trading engine can inject ``binance_mid`` into :class:`~src.strategies.base.MarketEvent`.

Assumed latency: ~50–200 ms WebSocket delivery vs Hyperliquid ``allMids``; the
LeadLag strategy targets HL catching up over ~1–5 s after a Binance impulse.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List

from src.exchanges.binance_api import BinanceWSClient
from src.exchanges.hyperliquid_ws import DataBus

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BinanceMidTick:
    """Latest Binance spot trade price for one base symbol (e.g. BTC)."""

    symbol: str
    price: float
    timestamp_ms: int


async def _forward_symbol(
    ws: BinanceWSClient,
    bus: DataBus,
    symbol: str,
) -> None:
    """Forward every aggregate trade for *symbol* to the DataBus."""
    queue = await ws.subscribe_symbol(symbol)
    logger.info("Binance price bridge active for %s", symbol)
    while True:
        try:
            trade = await queue.get()
        except asyncio.CancelledError:
            raise
        tick = BinanceMidTick(
            symbol=symbol,
            price=trade.price,
            timestamp_ms=trade.timestamp_ms,
        )
        bus.publish(f"binance_price:{symbol}", tick)


async def forward_binance_prices(
    ws: BinanceWSClient,
    bus: DataBus,
    symbols: List[str],
) -> None:
    """Run one forwarding task per symbol until cancelled."""
    tasks = [
        asyncio.create_task(_forward_symbol(ws, bus, sym), name=f"bn_price_{sym}")
        for sym in symbols
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
