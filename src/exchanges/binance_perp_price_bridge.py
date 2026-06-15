"""Bridge Binance USD-M perpetual mark prices into the DataBus.

Binance price source
--------------------
Streams ``<symbol>@markPrice@1s`` from ``wss://fstream.binance.com`` (USD-M
futures). Mark price is the fair perp reference — comparable to Hyperliquid
perp mids without spot–perp basis contamination.

Publishes :class:`BinancePerpMidTick` on ``binance_perp_price:{symbol}`` for
engine injection into ``MarketEvent.binance_perp_mid``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import List

import websockets
from websockets.exceptions import ConnectionClosed

from src.exchanges.binance_api import to_binance_symbol
from src.exchanges.hyperliquid_ws import DataBus

logger = logging.getLogger(__name__)

FUTURES_STREAM = "wss://fstream.binance.com/stream"
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 30.0


@dataclass(frozen=True, slots=True)
class BinancePerpMidTick:
    """Latest Binance USD-M mark price for one base symbol (e.g. BTC)."""

    symbol: str
    price: float
    timestamp_ms: int


def _stream_url(symbols: List[str]) -> str:
    streams = "/".join(
        f"{to_binance_symbol(sym).lower()}@markPrice@1s"
        for sym in sorted(symbols)
    )
    return f"{FUTURES_STREAM}?streams={streams}"


def _parse_mark_price(raw: str | bytes, allowed: set[str]) -> BinancePerpMidTick | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    payload = json.loads(raw)
    data = payload.get("data", payload)
    if data.get("e") != "markPriceUpdate":
        return None
    binance_sym = str(data.get("s", ""))
    base = binance_sym.replace("USDT", "")
    if base not in allowed:
        return None
    price = float(data.get("p") or 0.0)
    if price <= 0:
        return None
    ts = int(data.get("E") or data.get("T") or 0)
    return BinancePerpMidTick(symbol=base, price=price, timestamp_ms=ts)


async def forward_binance_perp_prices(
    bus: DataBus,
    symbols: List[str],
) -> None:
    """Stream mark prices until cancelled."""
    allowed = set(symbols)
    backoff = INITIAL_BACKOFF
    url = _stream_url(symbols)
    while True:
        try:
            logger.info("Binance perp mark-price WS connecting: %s", url)
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                backoff = INITIAL_BACKOFF
                async for raw in ws:
                    tick = _parse_mark_price(raw, allowed)
                    if tick is None:
                        continue
                    bus.publish(f"binance_perp_price:{tick.symbol}", tick)
        except ConnectionClosed as exc:
            logger.warning("Binance perp price WS closed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Binance perp price WS error: %s", exc)

        await asyncio.sleep(min(backoff, MAX_BACKOFF))
        backoff = min(backoff * 2, MAX_BACKOFF)
