"""Parse Hyperliquid user_state and open orders into normalised structures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.utils.helpers import safe_float


@dataclass(frozen=True)
class ExchangePosition:
    """Normalised perp position from ``user_state``."""

    symbol: str
    side: str  # long | short
    size: float
    entry_price: float
    unrealized_pnl: float = 0.0


@dataclass(frozen=True)
class ExchangeTriggerOrder:
    """Reduce-only SL/TP trigger resting on the exchange."""

    order_id: str
    symbol: str
    tpsl: str  # sl | tp
    size: float
    trigger_price: float
    limit_price: float
    is_buy: bool


def parse_exchange_positions(user_state: Dict[str, Any]) -> Dict[str, ExchangePosition]:
    """Build ``{symbol: ExchangePosition}`` from HL ``user_state``."""
    out: Dict[str, ExchangePosition] = {}
    if not isinstance(user_state, dict):
        return out

    for item in user_state.get("assetPositions", []) or []:
        if not isinstance(item, dict):
            continue
        pos = item.get("position")
        if not isinstance(pos, dict):
            continue
        symbol = str(pos.get("coin", "")).upper()
        if not symbol:
            continue
        szi = safe_float(pos.get("szi"))
        if abs(szi) < 1e-12:
            continue
        side = "long" if szi > 0 else "short"
        entry_px = safe_float(pos.get("entryPx"))
        upnl = safe_float(pos.get("unrealizedPnl"))
        out[symbol] = ExchangePosition(
            symbol=symbol,
            side=side,
            size=abs(szi),
            entry_price=entry_px,
            unrealized_pnl=upnl,
        )
    return out


def _extract_trigger_from_order(order: Dict[str, Any]) -> Optional[Tuple[str, float, float]]:
    """Return (tpsl, trigger_px, limit_px) when *order* is a trigger."""
    for key in ("orderType", "order_type", "type"):
        block = order.get(key)
        if not isinstance(block, dict):
            continue
        trigger = block.get("trigger")
        if isinstance(trigger, dict) and trigger.get("tpsl") in ("sl", "tp"):
            return (
                str(trigger["tpsl"]),
                safe_float(trigger.get("triggerPx")),
                safe_float(order.get("limitPx", order.get("px", trigger.get("triggerPx")))),
            )
    # Flat HL open-order shape used in tests / some SDK versions
    if order.get("tpsl") in ("sl", "tp"):
        return (
            str(order["tpsl"]),
            safe_float(order.get("triggerPx", order.get("trigger_px"))),
            safe_float(order.get("limitPx", order.get("px"))),
        )
    return None


def parse_trigger_orders(open_orders: List[Dict[str, Any]]) -> Dict[str, List[ExchangeTriggerOrder]]:
    """Group trigger orders by symbol."""
    grouped: Dict[str, List[ExchangeTriggerOrder]] = {}
    for order in open_orders or []:
        if not isinstance(order, dict):
            continue
        parsed = _extract_trigger_from_order(order)
        if parsed is None:
            continue
        tpsl, trigger_px, limit_px = parsed
        symbol = str(order.get("coin", order.get("symbol", ""))).upper()
        oid = order.get("oid", order.get("orderId"))
        if not symbol or oid is None:
            continue
        side_raw = str(order.get("side", "")).upper()
        is_buy = side_raw in ("B", "BUY", "LONG") or bool(order.get("is_buy", False))
        sz = safe_float(order.get("sz", order.get("size")))
        entry = ExchangeTriggerOrder(
            order_id=str(oid),
            symbol=symbol,
            tpsl=tpsl,
            size=sz,
            trigger_price=trigger_px,
            limit_price=limit_px,
            is_buy=is_buy,
        )
        grouped.setdefault(symbol, []).append(entry)
    return grouped


def positions_match(
    local_side: str,
    local_size: float,
    exchange: ExchangePosition,
    *,
    size_tolerance: float = 1e-6,
) -> bool:
    """True when side and size agree within tolerance."""
    if local_side != exchange.side:
        return False
    return abs(local_size - exchange.size) <= max(size_tolerance, exchange.size * 1e-4)
