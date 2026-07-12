"""Order lifecycle states and Hyperliquid response interpretation.

Phase 01: explicit state machine for live execution paths. Native SL/TP
placement is deferred to Phase 03 — see ``NativeProtectionHooks`` in
``execution.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.utils.helpers import safe_float, safe_divide


# Persistible / trackable order states (Phase 01 contract)
ORDER_PENDING_SUBMISSION: str = "pending_submission"
ORDER_RESTING: str = "resting"
ORDER_PARTIAL: str = "partial"
ORDER_FILLED: str = "filled"
ORDER_CANCELLED: str = "cancelled"
ORDER_REJECTED: str = "rejected"
ORDER_CLOSE_PENDING: str = "close_pending"
ORDER_CLOSED: str = "closed"
ORDER_SUBMISSION_UNKNOWN: str = "submission_unknown"
ORDER_CLOSE_PENDING_RECONCILIATION: str = "close_pending_reconciliation"

TERMINAL_ORDER_STATES = frozenset({
    ORDER_FILLED,
    ORDER_CANCELLED,
    ORDER_REJECTED,
    ORDER_CLOSED,
})

ACTIVE_ORDER_STATES = frozenset({
    ORDER_PENDING_SUBMISSION,
    ORDER_RESTING,
    ORDER_PARTIAL,
    ORDER_CLOSE_PENDING,
    ORDER_SUBMISSION_UNKNOWN,
})

PENDING_LIVE_TRADE_STATUSES = frozenset({
    ORDER_PENDING_SUBMISSION,
    ORDER_RESTING,
    ORDER_PARTIAL,
    ORDER_SUBMISSION_UNKNOWN,
})


class LiveExecutionError(RuntimeError):
    """Raised when live signing, client, REST, or HL response is invalid."""


class AmbiguousOrderResponse(LiveExecutionError):
    """Raised when the exchange response cannot be interpreted safely."""


@dataclass(frozen=True)
class ParsedEntryResponse:
    """Normalised result of a Hyperliquid entry submission."""

    lifecycle_state: str
    exchange_order_id: Optional[str]
    error_message: Optional[str] = None
    ambiguous: bool = False


@dataclass(frozen=True)
class OrderFillSnapshot:
    """Normalised fill state from exchange status + fill tape."""

    status: str
    lifecycle_state: str
    filled_size: float
    remaining_size: float
    avg_fill_price: float
    cumulative_fee: float
    last_fill_at_ms: Optional[int] = None
    raw_status: str = ""


@dataclass(frozen=True)
class ParsedCloseResponse:
    """Normalised result of a Hyperliquid close submission."""

    lifecycle_state: str
    error_message: Optional[str] = None
    ambiguous: bool = False


def extract_order_id(hl_response: Optional[Dict[str, Any]]) -> Optional[str]:
    """Pull the HL order id out of a (potentially nested) response."""
    if not isinstance(hl_response, dict):
        return None
    for key in ("oid", "orderId", "order_id", "id"):
        if key in hl_response and hl_response[key] is not None:
            return str(hl_response[key])
    try:
        statuses = (
            hl_response.get("response", {})
            .get("data", {})
            .get("statuses", [])
        )
        if statuses:
            first = statuses[0]
            if isinstance(first, dict):
                for sub in first.values():
                    if isinstance(sub, dict) and "oid" in sub:
                        return str(sub["oid"])
    except (AttributeError, TypeError):
        pass
    return None


def _first_status_entry(hl_response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    statuses = hl_response.get("response", {}).get("data", {}).get("statuses", [])
    if not statuses:
        return None
    first = statuses[0]
    return first if isinstance(first, dict) else None


def parse_hyperliquid_entry_response(
    hl_response: Any,
) -> ParsedEntryResponse:
    """Interpret a Hyperliquid entry response without assuming a fill."""
    if not isinstance(hl_response, dict):
        return ParsedEntryResponse(
            lifecycle_state=ORDER_SUBMISSION_UNKNOWN,
            exchange_order_id=None,
            error_message="non_dict_response",
            ambiguous=True,
        )

    top_status = str(hl_response.get("status", "")).lower()
    if top_status == "err":
        err = hl_response.get("response", hl_response.get("error", "hl_error"))
        return ParsedEntryResponse(
            lifecycle_state=ORDER_REJECTED,
            exchange_order_id=None,
            error_message=str(err),
        )

    entry = _first_status_entry(hl_response)
    if entry is not None:
        if "error" in entry:
            return ParsedEntryResponse(
                lifecycle_state=ORDER_REJECTED,
                exchange_order_id=None,
                error_message=str(entry["error"]),
            )
        if "filled" in entry:
            oid = extract_order_id(hl_response)
            sub = entry.get("filled")
            if isinstance(sub, dict) and "oid" in sub:
                oid = str(sub["oid"])
            return ParsedEntryResponse(
                lifecycle_state=ORDER_FILLED,
                exchange_order_id=oid,
            )
        if "resting" in entry:
            oid = extract_order_id(hl_response)
            sub = entry.get("resting")
            if isinstance(sub, dict) and "oid" in sub:
                oid = str(sub["oid"])
            return ParsedEntryResponse(
                lifecycle_state=ORDER_RESTING,
                exchange_order_id=oid,
            )

    oid = extract_order_id(hl_response)
    if oid:
        return ParsedEntryResponse(
            lifecycle_state=ORDER_RESTING,
            exchange_order_id=oid,
        )

    if top_status in ("ok", "success"):
        # Market fills sometimes return ok without nested oid — treat as filled.
        if hl_response.get("raw") is not None:
            return ParsedEntryResponse(
                lifecycle_state=ORDER_FILLED,
                exchange_order_id=None,
            )
        return ParsedEntryResponse(
            lifecycle_state=ORDER_SUBMISSION_UNKNOWN,
            exchange_order_id=None,
            error_message="ok_without_order_status",
            ambiguous=True,
        )

    if not top_status and not hl_response:
        return ParsedEntryResponse(
            lifecycle_state=ORDER_SUBMISSION_UNKNOWN,
            exchange_order_id=None,
            error_message="empty_response",
            ambiguous=True,
        )

    return ParsedEntryResponse(
        lifecycle_state=ORDER_SUBMISSION_UNKNOWN,
        exchange_order_id=None,
        error_message=f"unrecognised_response:{top_status or 'unknown'}",
        ambiguous=True,
    )


def parse_hyperliquid_close_response(hl_response: Any) -> ParsedCloseResponse:
    """Interpret a Hyperliquid close / reduce response."""
    if not isinstance(hl_response, dict):
        return ParsedCloseResponse(
            lifecycle_state=ORDER_REJECTED,
            error_message="non_dict_response",
            ambiguous=True,
        )

    top_status = str(hl_response.get("status", "")).lower()
    if top_status == "err":
        err = hl_response.get("response", hl_response.get("error", "hl_error"))
        return ParsedCloseResponse(
            lifecycle_state=ORDER_REJECTED,
            error_message=str(err),
        )

    entry = _first_status_entry(hl_response)
    if entry is not None:
        if "error" in entry:
            return ParsedCloseResponse(
                lifecycle_state=ORDER_REJECTED,
                error_message=str(entry["error"]),
            )
        if "filled" in entry or "resting" in entry:
            return ParsedCloseResponse(lifecycle_state=ORDER_FILLED)

    if top_status in ("ok", "success") or hl_response.get("raw") is not None:
        return ParsedCloseResponse(lifecycle_state=ORDER_FILLED)

    if not top_status and not hl_response:
        return ParsedCloseResponse(
            lifecycle_state=ORDER_REJECTED,
            error_message="empty_response",
            ambiguous=True,
        )

    return ParsedCloseResponse(
        lifecycle_state=ORDER_REJECTED,
        error_message=f"unrecognised_response:{top_status or 'unknown'}",
        ambiguous=True,
    )


def _map_exchange_status_to_lifecycle(raw_status: str, filled_size: float, target_size: float) -> str:
    """Map HL status string + fill progress to lifecycle state."""
    raw = (raw_status or "").lower()
    if "reject" in raw or raw == "error":
        return ORDER_REJECTED
    if "cancel" in raw:
        return ORDER_CANCELLED if filled_size <= 0 else ORDER_PARTIAL
    if target_size > 0 and filled_size >= target_size * 0.999:
        return ORDER_FILLED
    if filled_size > 0:
        return ORDER_PARTIAL
    if raw in ("open", "resting", "pending"):
        return ORDER_RESTING
    if "filled" in raw or raw == "closed":
        return ORDER_FILLED
    return ORDER_RESTING


def _map_lifecycle_to_oms_status(lifecycle_state: str) -> str:
    mapping = {
        ORDER_PENDING_SUBMISSION: "open",
        ORDER_RESTING: "open",
        ORDER_PARTIAL: "partial",
        ORDER_FILLED: "filled",
        ORDER_CANCELLED: "cancelled",
        ORDER_REJECTED: "rejected",
        ORDER_CLOSE_PENDING: "open",
        ORDER_SUBMISSION_UNKNOWN: "open",
    }
    return mapping.get(lifecycle_state, "open")


def aggregate_fills_for_order(
    fills: List[Dict[str, Any]],
    order_id: str,
) -> Tuple[float, float, float, Optional[int]]:
    """Return (filled_size, avg_fill_price, cumulative_fee, last_fill_ms) for *order_id*."""
    oid = str(order_id)
    total_sz = 0.0
    notional = 0.0
    fee_total = 0.0
    last_ms: Optional[int] = None
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        fill_oid = fill.get("oid", fill.get("orderId", fill.get("order_id")))
        if fill_oid is not None and str(fill_oid) != oid:
            continue
        sz = safe_float(fill.get("sz", fill.get("size", 0.0)))
        px = safe_float(fill.get("px", fill.get("price", 0.0)))
        if sz <= 0:
            continue
        total_sz += sz
        notional += sz * px
        fee_total += abs(safe_float(fill.get("fee", 0.0)))
        ts = fill.get("time", fill.get("timestamp", fill.get("timestamp_ms")))
        if ts is not None:
            ts_i = int(ts)
            if last_ms is None or ts_i > last_ms:
                last_ms = ts_i
    avg_px = safe_divide(notional, total_sz, 0.0)
    return total_sz, avg_px, fee_total, last_ms


def parse_order_fill_snapshot(
    status_response: Any,
    fills: List[Dict[str, Any]],
    *,
    order_id: str,
    target_size: float,
    reference_price: float = 0.0,
) -> OrderFillSnapshot:
    """Build an :class:`OrderFillSnapshot` from HL orderStatus + user fills."""
    target = max(safe_float(target_size), 0.0)
    fill_sz, fill_avg, fill_fee, last_ms = aggregate_fills_for_order(fills, order_id)

    raw_status = ""
    order_block: Optional[Dict[str, Any]] = None
    if isinstance(status_response, dict):
        order_block = status_response.get("order")
        if isinstance(order_block, dict):
            inner = order_block.get("order")
            if isinstance(inner, dict):
                remaining = safe_float(inner.get("sz", inner.get("size", 0.0)))
                if remaining > 0 and fill_sz <= 0:
                    fill_sz = max(0.0, target - remaining)
                px = safe_float(inner.get("limitPx", inner.get("px", 0.0)))
                if px > 0 and fill_sz > 0 and fill_avg <= 0:
                    fill_avg = px
            raw_status = str(order_block.get("status", status_response.get("status", "")))
        else:
            raw_status = str(status_response.get("status", ""))

    if fill_sz <= 0 and isinstance(status_response, dict):
        filled_block = status_response.get("filled")
        if isinstance(filled_block, dict):
            fill_sz = safe_float(filled_block.get("totalSz", filled_block.get("sz", 0.0)))
            fill_avg = safe_float(filled_block.get("avgPx", filled_block.get("px", 0.0)))
            fill_fee += abs(safe_float(filled_block.get("fee", 0.0)))

    if fill_avg <= 0 and reference_price > 0:
        fill_avg = reference_price

    remaining = max(0.0, target - fill_sz) if target > 0 else 0.0
    lifecycle = _map_exchange_status_to_lifecycle(raw_status, fill_sz, target)
    oms_status = _map_lifecycle_to_oms_status(lifecycle)

    if lifecycle == ORDER_PARTIAL and remaining <= target * 0.001:
        lifecycle = ORDER_FILLED
        oms_status = "filled"
        remaining = 0.0

    return OrderFillSnapshot(
        status=oms_status,
        lifecycle_state=lifecycle,
        filled_size=fill_sz,
        remaining_size=remaining,
        avg_fill_price=fill_avg,
        cumulative_fee=fill_fee,
        last_fill_at_ms=last_ms,
        raw_status=raw_status,
    )

