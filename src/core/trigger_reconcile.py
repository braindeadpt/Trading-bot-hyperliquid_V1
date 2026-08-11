"""Reconcile native SL/TP trigger closes from Hyperliquid user fill tape."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.utils.helpers import safe_divide, safe_float

logger = logging.getLogger(__name__)

CLOSE_PENDING_RECONCILIATION = "close_pending_reconciliation"


@dataclass(frozen=True)
class ClosingFillMatch:
    """Matched reduce-only close fill(s) from the exchange tape."""

    exit_price: float
    closed_size: float
    exit_fee: float
    closed_pnl: float
    exit_reason: str
    trigger_oid: Optional[str]
    fill_timestamps_ms: List[int]


def _fill_symbol(fill: Dict[str, Any]) -> str:
    return str(fill.get("coin", fill.get("symbol", ""))).upper()


def _fill_side_is_buy(fill: Dict[str, Any]) -> bool:
    side = str(fill.get("side", "")).upper()
    if side in ("B", "BUY"):
        return True
    if side in ("A", "SELL", "S"):
        return False
    return bool(fill.get("is_buy", False))


def _is_closing_fill_for_position(fill: Dict[str, Any], position_side: str) -> bool:
    """True when fill direction closes *position_side*."""
    is_buy = _fill_side_is_buy(fill)
    if position_side == "long":
        return not is_buy
    return is_buy


def fill_is_liquidation(fill: Dict[str, Any]) -> bool:
    """True when a Hyperliquid user fill is a forced liquidation leg.

    HL attaches a ``liquidation`` object and/or ``liquidationMarkPx`` on
    fills that participate in an account liquidation (Freqtrade-style
    own-account detection — not cross-venue pressure feeds).
    """
    if not isinstance(fill, dict):
        return False
    mark = fill.get("liquidationMarkPx")
    if mark not in (None, "", 0, "0"):
        try:
            if float(mark) > 0:
                return True
        except (TypeError, ValueError):
            return True
    liq = fill.get("liquidation")
    if isinstance(liq, dict) and liq:
        return True
    if liq not in (None, False, "", 0, "0"):
        return True
    return False


def match_closing_fills(
    fills: List[Dict[str, Any]],
    *,
    symbol: str,
    position_side: str,
    expected_size: float,
    entry_time_ms: int,
    sl_oid: Optional[str] = None,
    tp_oid: Optional[str] = None,
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
) -> Optional[ClosingFillMatch]:
    """Find the most recent closing fill tape for *symbol* after entry."""
    sym = symbol.upper()
    target = safe_float(expected_size)
    if target <= 0:
        return None

    candidates: List[Dict[str, Any]] = []
    for fill in fills or []:
        if not isinstance(fill, dict):
            continue
        if _fill_symbol(fill) != sym:
            continue
        ts = int(fill.get("time", fill.get("timestamp", 0)) or 0)
        if ts and ts < int(entry_time_ms):
            continue
        if not _is_closing_fill_for_position(fill, position_side):
            continue
        sz = safe_float(fill.get("sz", fill.get("size")))
        if sz <= 0:
            continue
        candidates.append(fill)

    if not candidates:
        return None

    oid_matches = [
        f for f in candidates
        if str(f.get("oid", f.get("orderId", ""))) in {
            oid for oid in (sl_oid, tp_oid) if oid
        }
    ]
    pool = oid_matches if oid_matches else candidates
    pool.sort(key=lambda f: int(f.get("time", f.get("timestamp", 0)) or 0))

    total_sz = 0.0
    notional = 0.0
    fee_total = 0.0
    pnl_total = 0.0
    timestamps: List[int] = []
    trigger_oid: Optional[str] = None
    last_px = 0.0
    saw_liquidation = False
    liq_mark_px = 0.0

    for fill in pool:
        sz = safe_float(fill.get("sz", fill.get("size")))
        px = safe_float(fill.get("px", fill.get("price")))
        if sz <= 0 or px <= 0:
            continue
        if fill_is_liquidation(fill):
            saw_liquidation = True
            mark = safe_float(fill.get("liquidationMarkPx"))
            if mark <= 0 and isinstance(fill.get("liquidation"), dict):
                mark = safe_float(fill["liquidation"].get("markPx"))
            if mark > 0:
                liq_mark_px = mark
        total_sz += sz
        notional += sz * px
        fee_total += abs(safe_float(fill.get("fee")))
        pnl_total += safe_float(fill.get("closedPnl", fill.get("closed_pnl")))
        ts = int(fill.get("time", fill.get("timestamp", 0)) or 0)
        if ts:
            timestamps.append(ts)
        last_px = px
        oid = fill.get("oid", fill.get("orderId"))
        if oid is not None:
            trigger_oid = str(oid)

    if total_sz + 1e-9 < target * 0.999:
        return None

    avg_px = safe_divide(notional, total_sz, last_px)
    if saw_liquidation and liq_mark_px > 0:
        avg_px = liq_mark_px
    if saw_liquidation:
        exit_reason = "liquidation"
    else:
        exit_reason = _infer_exit_reason(
            avg_px,
            position_side,
            sl_price=sl_price,
            tp_price=tp_price,
            trigger_oid=trigger_oid,
            sl_oid=sl_oid,
            tp_oid=tp_oid,
        )
    return ClosingFillMatch(
        exit_price=avg_px,
        closed_size=min(total_sz, target),
        exit_fee=fee_total,
        closed_pnl=pnl_total,
        exit_reason=exit_reason,
        trigger_oid=trigger_oid,
        fill_timestamps_ms=timestamps,
    )


def _infer_exit_reason(
    exit_price: float,
    side: str,
    *,
    sl_price: Optional[float],
    tp_price: Optional[float],
    trigger_oid: Optional[str],
    sl_oid: Optional[str],
    tp_oid: Optional[str],
) -> str:
    if trigger_oid and sl_oid and trigger_oid == str(sl_oid):
        return "stop_loss_native"
    if trigger_oid and tp_oid and trigger_oid == str(tp_oid):
        return "take_profit_native"
    if sl_price and tp_price and exit_price > 0:
        if side == "long":
            if abs(exit_price - sl_price) <= abs(exit_price - tp_price):
                return "stop_loss_native"
            return "take_profit_native"
        if abs(exit_price - sl_price) <= abs(exit_price - tp_price):
            return "stop_loss_native"
        return "take_profit_native"
    if sl_price and exit_price > 0:
        return "stop_loss_native"
    if tp_price and exit_price > 0:
        return "take_profit_native"
    return "native_trigger_close"


def compute_realized_pnl(
    *,
    side: str,
    entry_price: float,
    size: float,
    exit_price: float,
    entry_fee: float,
    exit_fee: float,
    closed_pnl_exchange: float,
) -> Tuple[float, float]:
    """Return (pnl_usd, pnl_pct_notional)."""
    notional = entry_price * size
    if abs(closed_pnl_exchange) > 1e-12:
        pnl_usd = closed_pnl_exchange - safe_float(exit_fee)
    elif side == "long":
        pnl_usd = (exit_price - entry_price) * size - entry_fee - exit_fee
    else:
        pnl_usd = (entry_price - exit_price) * size - entry_fee - exit_fee
    pnl_pct = safe_divide(pnl_usd, notional, 0.0) if notional > 0 else 0.0
    return pnl_usd, pnl_pct


@dataclass
class TriggerReconcileResult:
    reconciled: bool
    pending: bool = False
    already_done: bool = False
    exit_reason: str = ""
    pnl_usd: float = 0.0


async def reconcile_trigger_close_once(
    *,
    symbol: str,
    position: Any,
    live_client: Any,
    db: Any,
    portfolio: Any,
    protection: Any,
    executor: Any,
    db_row: Optional[Dict[str, Any]] = None,
    allow_pending: bool = True,
    lookback_ms: int = 86_400_000,
) -> TriggerReconcileResult:
    """Close a native-triggered / liquidated exit exactly once using fill tape.

    When *allow_pending* is False and no closing fill is found, returns
    ``pending=False`` so callers can fall through to phantom-cancel (used
    for orphan local without native protection).
    """
    meta = position.metadata or {}
    trade_id = int(meta.get("trade_id") or 0)
    row = db_row or {}
    if trade_id <= 0 and row:
        trade_id = int(row.get("id", 0))

    if trade_id > 0 and db is not None:
        fresh = db.get_trade_by_id(trade_id) or row
        status = str(fresh.get("status", ""))
        if status == "closed":
            return TriggerReconcileResult(reconciled=False, already_done=True)
        if fresh.get("exit_time") and status != CLOSE_PENDING_RECONCILIATION:
            return TriggerReconcileResult(reconciled=False, already_done=True)

    sl_oid = str(meta.get("native_sl_oid") or row.get("native_sl_order_id") or "") or None
    tp_oid = str(meta.get("native_tp_oid") or row.get("native_tp_order_id") or "") or None
    entry_fee = safe_float(meta.get("entry_fee", row.get("entry_fee")))
    entry_price = safe_float(position.entry_price, row.get("entry_price"))
    size = safe_float(position.size, row.get("size"))

    fills: List[Dict[str, Any]] = []
    if hasattr(live_client, "get_user_fills"):
        try:
            fills = await live_client.get_user_fills(lookback_ms=int(lookback_ms))
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_user_fills failed for %s: %s", symbol, exc)

    match = match_closing_fills(
        fills,
        symbol=symbol,
        position_side=position.side,
        expected_size=size,
        entry_time_ms=int(position.entry_time_ms or row.get("entry_time") or 0),
        sl_oid=sl_oid,
        tp_oid=tp_oid,
        sl_price=position.stop_loss_price,
        tp_price=position.take_profit_price,
    )

    if match is None:
        if not allow_pending:
            return TriggerReconcileResult(reconciled=False, pending=False)
        if trade_id > 0 and db is not None:
            db.update_trade_status(
                trade_id,
                status=CLOSE_PENDING_RECONCILIATION,
                reason="awaiting_fill_tape",
            )
        logger.warning(
            "TRIGGER CLOSE %s — no fill tape; status=%s",
            symbol,
            CLOSE_PENDING_RECONCILIATION,
        )
        return TriggerReconcileResult(reconciled=False, pending=True)

    pnl_usd, pnl_pct = compute_realized_pnl(
        side=position.side,
        entry_price=entry_price,
        size=match.closed_size,
        exit_price=match.exit_price,
        entry_fee=entry_fee,
        exit_fee=match.exit_fee,
        closed_pnl_exchange=match.closed_pnl,
    )
    exit_ms = max(match.fill_timestamps_ms) if match.fill_timestamps_ms else int(time.time() * 1000)

    if protection is not None:
        await protection.cancel_sibling_trigger(symbol, filled_oid=match.trigger_oid)

    from src.data.database import TradeExit

    if trade_id > 0 and db is not None:
        db.update_trade_exit(TradeExit(
            trade_id=trade_id,
            exit_price=match.exit_price,
            exit_time=exit_ms,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            exit_reason=match.exit_reason,
            status="closed",
            funding_paid=safe_float(meta.get("funding_total", row.get("funding_paid"))),
        ))
        db.update_trade_native_protection(
            trade_id,
            sl_order_id=None,
            tp_order_id=None,
            protected_size=0.0,
        )

    await portfolio.remove_position(
        symbol,
        exit_price=match.exit_price,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        reason=match.exit_reason,
    )

    if executor is not None:
        async with executor._lock:
            executor._open_trades.pop(symbol, None)

    if protection is not None:
        await protection.cancel_protection(symbol)

    logger.info(
        "TRIGGER CLOSE reconciled %s once: px=%.4f pnl=%.2f fee=%.4f reason=%s",
        symbol, match.exit_price, pnl_usd, match.exit_fee, match.exit_reason,
    )
    return TriggerReconcileResult(
        reconciled=True,
        exit_reason=match.exit_reason,
        pnl_usd=pnl_usd,
    )
