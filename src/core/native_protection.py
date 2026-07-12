"""Native reduce-only SL/TP trigger management (Phase 03)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.exchanges.hl_positions import ExchangeTriggerOrder, parse_trigger_orders
from src.strategies.base import Position
from src.utils.helpers import safe_float

if TYPE_CHECKING:
    from src.data.database import Database

logger = logging.getLogger(__name__)


@dataclass
class ProtectionState:
    """Tracked native triggers for one symbol."""

    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    protected_size: float = 0.0
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None


@dataclass
class ProtectionResult:
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    protected_size: float = 0.0
    cancelled_ids: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class NativeProtectionManager:
    """Place, resize, and cancel HL trigger orders idempotently."""

    def __init__(self, live_client: Any, db: Optional["Database"] = None) -> None:
        self._client = live_client
        self._db = db
        self._by_symbol: Dict[str, ProtectionState] = {}

    def get_state(self, symbol: str) -> ProtectionState:
        return self._by_symbol.setdefault(symbol.upper(), ProtectionState())

    def load_from_metadata(self, symbol: str, metadata: Optional[Dict[str, Any]]) -> None:
        meta = metadata or {}
        st = self.get_state(symbol)
        st.sl_order_id = meta.get("native_sl_oid")
        st.tp_order_id = meta.get("native_tp_oid")
        st.protected_size = safe_float(meta.get("native_protected_size"))
        st.stop_price = safe_float(meta.get("native_stop_price"), default=0.0) or None
        st.take_profit_price = safe_float(meta.get("native_tp_price"), default=0.0) or None

    def sync_from_open_orders(self, open_orders: List[Dict[str, Any]]) -> None:
        """Refresh in-memory trigger ids from exchange open orders."""
        triggers = parse_trigger_orders(open_orders)
        seen: set[str] = set()
        for symbol, orders in triggers.items():
            st = self.get_state(symbol)
            sl_ids: List[str] = []
            tp_ids: List[str] = []
            protected = 0.0
            for tr in orders:
                seen.add(tr.order_id)
                if tr.tpsl == "sl":
                    sl_ids.append(tr.order_id)
                    st.stop_price = tr.trigger_price
                else:
                    tp_ids.append(tr.order_id)
                    st.take_profit_price = tr.trigger_price
                protected = max(protected, tr.size)
            if sl_ids:
                st.sl_order_id = sl_ids[-1]
            if tp_ids:
                st.tp_order_id = tp_ids[-1]
            if protected > 0:
                st.protected_size = protected

    async def ensure_protection(
        self,
        position: Position,
        *,
        filled_size: float,
        stop_price: Optional[float],
        take_profit_price: Optional[float],
        trade_id: Optional[int] = None,
    ) -> ProtectionResult:
        """Place or resize native SL/TP for *filled_size* (idempotent)."""
        symbol = position.symbol.upper()
        size = safe_float(filled_size)
        if size <= 0:
            return ProtectionResult()

        st = self.get_state(symbol)
        result = ProtectionResult(protected_size=size)

        needs_replace = (
            abs(st.protected_size - size) > max(1e-8, size * 1e-4)
            or (stop_price and st.stop_price and abs(st.stop_price - stop_price) > 1e-6)
            or (
                take_profit_price
                and st.take_profit_price
                and abs(st.take_profit_price - take_profit_price) > 1e-6
            )
        )

        if not needs_replace and st.sl_order_id and (not take_profit_price or st.tp_order_id):
            result.sl_order_id = st.sl_order_id
            result.tp_order_id = st.tp_order_id
            result.protected_size = st.protected_size
            return result

        if needs_replace:
            cancel_ids = [oid for oid in (st.sl_order_id, st.tp_order_id) if oid]
            cancelled = await self._cancel_ids(symbol, cancel_ids)
            result.cancelled_ids.extend(cancelled)
            st.sl_order_id = None
            st.tp_order_id = None

        if stop_price and stop_price > 0:
            try:
                resp = await self._client.place_trigger_order(
                    symbol,
                    position.side,
                    size,
                    trigger_price=float(stop_price),
                    tpsl="sl",
                )
                oid = self._extract_oid(resp)
                if oid:
                    st.sl_order_id = oid
                    st.stop_price = float(stop_price)
                    result.sl_order_id = oid
                else:
                    result.errors.append("sl_missing_oid")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Native SL placement failed for %s: %s", symbol, exc)
                result.errors.append(f"sl_error:{exc}")

        if take_profit_price and take_profit_price > 0:
            try:
                resp = await self._client.place_trigger_order(
                    symbol,
                    position.side,
                    size,
                    trigger_price=float(take_profit_price),
                    tpsl="tp",
                )
                oid = self._extract_oid(resp)
                if oid:
                    st.tp_order_id = oid
                    st.take_profit_price = float(take_profit_price)
                    result.tp_order_id = oid
                else:
                    result.errors.append("tp_missing_oid")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Native TP placement failed for %s: %s", symbol, exc)
                result.errors.append(f"tp_error:{exc}")

        st.protected_size = size
        result.protected_size = size
        self._persist_trade_protection(trade_id, st)
        return result

    async def cancel_protection(self, symbol: str) -> List[str]:
        """Cancel all known native triggers for *symbol*."""
        st = self.get_state(symbol)
        ids = [oid for oid in (st.sl_order_id, st.tp_order_id) if oid]
        cancelled = await self._cancel_ids(symbol.upper(), ids)
        st.sl_order_id = None
        st.tp_order_id = None
        st.protected_size = 0.0
        return cancelled

    async def cancel_sibling_trigger(
        self,
        symbol: str,
        *,
        filled_oid: Optional[str] = None,
    ) -> List[str]:
        """Cancel the SL/TP trigger that did not fire."""
        st = self.get_state(symbol)
        sibling: Optional[str] = None
        if filled_oid:
            if st.sl_order_id and str(filled_oid) == str(st.sl_order_id):
                sibling = st.tp_order_id
            elif st.tp_order_id and str(filled_oid) == str(st.tp_order_id):
                sibling = st.sl_order_id
        if sibling:
            cancelled = await self._cancel_ids(symbol.upper(), [sibling])
            if sibling == st.tp_order_id:
                st.tp_order_id = None
            elif sibling == st.sl_order_id:
                st.sl_order_id = None
            return cancelled
        # Fallback: cancel any resting triggers for a flat symbol.
        return await self.cancel_protection(symbol)

    async def purge_residual_triggers(
        self,
        open_orders: List[Dict[str, Any]],
        exchange_positions: Dict[str, Any],
    ) -> List[str]:
        """Cancel triggers resting without a matching exchange position."""
        triggers = parse_trigger_orders(open_orders)
        ex_syms = set(exchange_positions.keys())
        cancelled: List[str] = []
        for sym, orders in triggers.items():
            if sym in ex_syms:
                continue
            for tr in orders:
                try:
                    await self._client.cancel_order(sym, int(tr.order_id))
                    cancelled.append(tr.order_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "purge_residual_triggers %s oid=%s failed: %s",
                        sym, tr.order_id, exc,
                    )
            st = self.get_state(sym)
            st.sl_order_id = None
            st.tp_order_id = None
            st.protected_size = 0.0
        return cancelled

    async def adopt_exchange_triggers(
        self,
        symbol: str,
        triggers: List[ExchangeTriggerOrder],
    ) -> ProtectionState:
        """Record existing exchange triggers after restart (no new placement)."""
        st = self.get_state(symbol)
        for tr in triggers:
            if tr.tpsl == "sl":
                st.sl_order_id = tr.order_id
                st.stop_price = tr.trigger_price
            else:
                st.tp_order_id = tr.order_id
                st.take_profit_price = tr.trigger_price
            st.protected_size = max(st.protected_size, tr.size)
        return st

    async def _cancel_ids(self, symbol: str, order_ids: List[str]) -> List[str]:
        cancelled: List[str] = []
        for oid in order_ids:
            if not oid:
                continue
            try:
                await self._client.cancel_order(symbol, int(oid))
                cancelled.append(oid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cancel native trigger %s %s failed: %s", symbol, oid, exc)
        return cancelled

    def _persist_trade_protection(self, trade_id: Optional[int], st: ProtectionState) -> None:
        if self._db is None or trade_id is None or trade_id <= 0:
            return
        try:
            self._db.update_trade_native_protection(
                int(trade_id),
                sl_order_id=st.sl_order_id,
                tp_order_id=st.tp_order_id,
                protected_size=st.protected_size,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist native protection trade_id=%s: %s", trade_id, exc)

    @staticmethod
    def _extract_oid(response: Dict[str, Any]) -> Optional[str]:
        if not isinstance(response, dict):
            return None
        from src.core.order_lifecycle import extract_order_id

        return extract_order_id(response)
