"""Exchange-as-source-of-truth reconciliation (Phase 03).

Uses Hyperliquid ``user_state``, open orders, and user fills — no invented
REST endpoints. Policies are configurable for orphan/mismatch handling.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.exchanges.hl_positions import (
    ExchangePosition,
    ExchangeTriggerOrder,
    parse_exchange_positions,
    parse_trigger_orders,
    positions_match,
)
from src.core.trigger_reconcile import (
    CLOSE_PENDING_RECONCILIATION,
    reconcile_trigger_close_once,
)
from src.utils.helpers import safe_float

if TYPE_CHECKING:
    from src.core.native_protection import NativeProtectionManager
    from src.core.portfolio import PortfolioState
    from src.data.database import Database

logger = logging.getLogger(__name__)


class OrphanExchangePolicy(str, Enum):
    ADOPT_AND_PROTECT = "ADOPT_AND_PROTECT"
    FLATTEN = "FLATTEN"
    HALT = "HALT"


class MismatchPolicy(str, Enum):
    HALT = "HALT"


@dataclass
class ReconciliationReport:
    success: bool
    timestamp: float
    exchange_positions: Dict[str, ExchangePosition] = field(default_factory=dict)
    local_symbols: List[str] = field(default_factory=list)
    orphan_exchange: List[str] = field(default_factory=list)
    orphan_local: List[str] = field(default_factory=list)
    mismatches: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    trigger_closes: List[str] = field(default_factory=list)


@dataclass
class ReconciliationHealth:
    last_success_ts: float = 0.0
    last_failure_ts: float = 0.0
    consecutive_failures: int = 0
    halted: bool = False
    halt_reason: Optional[str] = None
    stale: bool = True
    drift_symbols: List[str] = field(default_factory=list)


class ExchangeReconciler:
    """Diff local portfolio vs HL user_state and apply configured policies."""

    def __init__(
        self,
        *,
        live_client: Any,
        portfolio: "PortfolioState",
        db: Optional["Database"] = None,
        protection: Optional["NativeProtectionManager"] = None,
        orphan_exchange_policy: str = OrphanExchangePolicy.ADOPT_AND_PROTECT.value,
        mismatch_policy: str = MismatchPolicy.HALT.value,
        stale_threshold_sec: float = 120.0,
        alert_callback: Optional[Any] = None,
    ) -> None:
        self._client = live_client
        self._portfolio = portfolio
        self._db = db
        self._protection = protection
        self._orphan_exchange_policy = OrphanExchangePolicy(orphan_exchange_policy)
        self._mismatch_policy = MismatchPolicy(mismatch_policy)
        self._stale_threshold_sec = float(stale_threshold_sec)
        self._alert = alert_callback
        self._health = ReconciliationHealth()

    @property
    def health(self) -> ReconciliationHealth:
        return self._health

    def is_stale(self) -> bool:
        if self._health.last_success_ts <= 0:
            return True
        return (time.time() - self._health.last_success_ts) > self._stale_threshold_sec

    def entries_blocked(self) -> bool:
        return (
            self._health.halted
            or self.is_stale()
            or bool(self._health.drift_symbols)
            or self._health.consecutive_failures >= 3
        )

    def block_reason(self) -> Optional[str]:
        if self._health.halted:
            return self._health.halt_reason or "reconciliation_halted"
        if self.is_stale():
            return "reconciliation_stale"
        if self._health.drift_symbols:
            return f"reconciliation_drift:{','.join(self._health.drift_symbols)}"
        if self._health.consecutive_failures >= 3:
            return "reconciliation_failing"
        return None

    def clear_halt(self) -> None:
        self._health.halted = False
        self._health.halt_reason = None
        self._health.drift_symbols = []

    async def reconcile_once(
        self,
        *,
        executor: Optional[Any] = None,
    ) -> ReconciliationReport:
        """Single reconciliation pass."""
        report = ReconciliationReport(success=False, timestamp=time.time())
        try:
            user_state = await self._client.get_user_state()
            open_orders = await self._client.get_open_orders()
            ex_positions = parse_exchange_positions(user_state)
            triggers = parse_trigger_orders(open_orders)
            report.exchange_positions = ex_positions

            if self._protection is not None:
                self._protection.sync_from_open_orders(open_orders)
                purged = await self._protection.purge_residual_triggers(
                    open_orders, ex_positions,
                )
                if purged:
                    report.actions.append(f"purged_residual_triggers:{','.join(purged)}")

            await self._retry_close_pending_trades(ex_positions, executor, report)

            local = await self._portfolio.positions
            report.local_symbols = list(local.keys())

            ex_syms = set(ex_positions.keys())
            loc_syms = set(local.keys())

            for sym in sorted(loc_syms - ex_syms):
                report.orphan_local.append(sym)
                await self._handle_orphan_local(sym, local[sym], executor, report)

            for sym in sorted(ex_syms - loc_syms):
                report.orphan_exchange.append(sym)
                await self._handle_orphan_exchange(
                    sym, ex_positions[sym], triggers.get(sym, []), report,
                )

            for sym in sorted(ex_syms & loc_syms):
                loc = local[sym]
                ex = ex_positions[sym]
                if not positions_match(loc.side, loc.size, ex):
                    report.mismatches.append(sym)
                    await self._handle_mismatch(sym, loc, ex, report)
                elif executor is not None and hasattr(executor, "is_symbol_blocked"):
                    # Confirmed local↔exchange agreement — clear ambiguous-response blocks
                    if executor.is_symbol_blocked(sym):
                        executor.unblock_symbol(sym)
                        report.actions.append(f"unblocked_consistent:{sym}")
                        logger.info(
                            "RECONCILE unblocked %s — local and exchange positions agree",
                            sym,
                        )

            # Both-flat agreement: blocked symbol with no local and no exchange position
            if executor is not None and hasattr(executor, "get_blocked_symbols"):
                ex_upper = {s.upper() for s in ex_syms}
                loc_upper = {s.upper() for s in loc_syms}
                mismatch_upper = {s.upper() for s in report.mismatches}
                orphan_ex_upper = {s.upper() for s in report.orphan_exchange}
                orphan_loc_upper = {s.upper() for s in report.orphan_local}
                for sym in list(executor.get_blocked_symbols()):
                    key = sym.upper()
                    if (
                        key not in ex_upper
                        and key not in loc_upper
                        and key not in mismatch_upper
                        and key not in orphan_ex_upper
                        and key not in orphan_loc_upper
                    ):
                        executor.unblock_symbol(sym)
                        report.actions.append(f"unblocked_both_flat:{sym}")
                        logger.info(
                            "RECONCILE unblocked %s — both local and exchange flat",
                            sym,
                        )

            # Detect trigger closes: local open but exchange flat handled above;
            # also check fills for recently closed trades when we had triggers.
            await self._detect_trigger_fills(ex_positions, local, report)

            report.success = len(report.errors) == 0
            if report.success:
                self._health.last_success_ts = report.timestamp
                self._health.consecutive_failures = 0
                self._health.stale = False
                if not report.mismatches and not report.orphan_exchange:
                    self._health.drift_symbols = [
                        s for s in self._health.drift_symbols
                        if s in local
                    ]
            else:
                self._record_failure("reconcile_errors")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Reconciliation failed: %s", exc)
            report.errors.append(str(exc))
            self._record_failure(str(exc))

        return report

    async def _handle_orphan_local(
        self,
        symbol: str,
        position: Any,
        executor: Optional[Any],
        report: ReconciliationReport,
    ) -> None:
        """Local position without exchange position — reconcile or audit."""
        meta = position.metadata or {}
        trade_id = meta.get("trade_id")
        db_row: Dict[str, Any] = {}
        if self._db is not None and trade_id:
            db_row = self._db.get_trade_by_id(int(trade_id)) or {}

        had_native = bool(
            meta.get("native_protection_active")
            or db_row.get("native_sl_order_id")
            or db_row.get("native_tp_order_id")
            or meta.get("native_sl_oid")
            or meta.get("native_tp_oid")
        )

        if had_native:
            result = await reconcile_trigger_close_once(
                symbol=symbol,
                position=position,
                live_client=self._client,
                db=self._db,
                portfolio=self._portfolio,
                protection=self._protection,
                executor=executor,
                db_row=db_row,
            )
            if result.already_done:
                report.actions.append(f"trigger_close_already_done:{symbol}")
                return
            if result.reconciled:
                report.trigger_closes.append(symbol)
                report.actions.append(
                    f"trigger_close_reconciled:{symbol}:{result.exit_reason}"
                )
                self._audit(
                    "trigger_close_reconciled",
                    symbol,
                    {"trade_id": trade_id, "pnl_usd": result.pnl_usd},
                )
                return
            if result.pending:
                await self._portfolio.suspend_position(symbol)
                if executor is not None:
                    async with executor._lock:
                        executor._open_trades.pop(symbol, None)
                self._halt(f"close_pending_reconciliation:{symbol}")
                report.actions.append(f"close_pending_reconciliation:{symbol}")
                self._audit("close_pending_reconciliation", symbol, {"trade_id": trade_id})
                return

        logger.error(
            "RECONCILE orphan_local %s side=%s size=%.6f — closing phantom local",
            symbol, position.side, position.size,
        )
        try:
            await self._portfolio.cancel_position(symbol)
            if executor is not None:
                async with executor._lock:
                    executor._open_trades.pop(symbol, None)
            if self._protection is not None:
                await self._protection.cancel_protection(symbol)
            if self._db is not None and trade_id:
                self._db.update_trade_status(
                    int(trade_id),
                    status="cancelled",
                    reason="reconcile_orphan_local_phantom",
                )
            report.actions.append(f"orphan_local_closed:{symbol}")
            self._audit("orphan_local", symbol, {"trade_id": trade_id})
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"orphan_local_{symbol}:{exc}")

    async def _retry_close_pending_trades(
        self,
        ex_positions: Dict[str, ExchangePosition],
        executor: Optional[Any],
        report: ReconciliationReport,
    ) -> None:
        """Retry fill-tape reconciliation for trades awaiting close."""
        if self._db is None:
            return
        pending = self._db.get_trades_by_status(CLOSE_PENDING_RECONCILIATION)
        for row in pending:
            sym = str(row.get("symbol", "")).upper()
            if not sym or sym in ex_positions:
                continue
            from src.strategies.base import Position
            from src.utils.helpers import resolve_trade_stop_levels

            sl, tp = resolve_trade_stop_levels(
                entry_price=safe_float(row.get("entry_price")),
                side=str(row.get("side", "long")),
                signal_metadata=row.get("signal_metadata"),
            )
            pos = Position(
                symbol=sym,
                side=str(row.get("side", "long")),
                entry_price=safe_float(row.get("entry_price")),
                size=safe_float(row.get("filled_size", row.get("size"))),
                entry_time_ms=int(row.get("entry_time") or 0),
                stop_loss_price=sl,
                take_profit_price=tp,
                metadata={
                    "trade_id": row.get("id"),
                    "entry_fee": row.get("entry_fee"),
                    "native_sl_oid": row.get("native_sl_order_id"),
                    "native_tp_oid": row.get("native_tp_order_id"),
                    "native_protection_active": True,
                },
            )
            result = await reconcile_trigger_close_once(
                symbol=sym,
                position=pos,
                live_client=self._client,
                db=self._db,
                portfolio=self._portfolio,
                protection=self._protection,
                executor=executor,
                db_row=row,
            )
            if result.reconciled:
                report.trigger_closes.append(sym)
                report.actions.append(f"pending_close_reconciled:{sym}")
                self.clear_halt()
            elif result.pending:
                report.actions.append(f"still_close_pending:{sym}")

    async def _handle_orphan_exchange(
        self,
        symbol: str,
        ex_pos: ExchangePosition,
        triggers: List[ExchangeTriggerOrder],
        report: ReconciliationReport,
    ) -> None:
        policy = self._orphan_exchange_policy
        logger.warning(
            "RECONCILE orphan_exchange %s side=%s size=%.6f policy=%s",
            symbol, ex_pos.side, ex_pos.size, policy.value,
        )
        if policy == OrphanExchangePolicy.HALT:
            self._halt(f"orphan_exchange:{symbol}")
            report.actions.append(f"orphan_exchange_halt:{symbol}")
            return

        if policy == OrphanExchangePolicy.FLATTEN:
            try:
                await self._client.close_position(symbol, ex_pos.size)
                report.actions.append(f"orphan_exchange_flattened:{symbol}")
                self._audit("orphan_exchange_flatten", symbol, {"size": ex_pos.size})
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"orphan_flatten_{symbol}:{exc}")
                self._halt(f"orphan_flatten_failed:{symbol}")
            return

        # ADOPT_AND_PROTECT
        await self._adopt_exchange_position(symbol, ex_pos, triggers, report)

    async def _adopt_exchange_position(
        self,
        symbol: str,
        ex_pos: ExchangePosition,
        triggers: List[ExchangeTriggerOrder],
        report: ReconciliationReport,
    ) -> None:
        from src.strategies.base import Position
        from src.utils.helpers import resolve_trade_stop_levels

        sl_price: Optional[float] = None
        tp_price: Optional[float] = None
        trade_id: Optional[int] = None

        # Try to find open trade row for metadata
        db_row: Dict[str, Any] = {}
        if self._db is not None:
            for row in self._db.get_open_trades():
                if str(row.get("symbol", "")).upper() == symbol:
                    db_row = row
                    trade_id = int(row["id"])
                    break

        sl_price, tp_price = resolve_trade_stop_levels(
            entry_price=ex_pos.entry_price,
            side=ex_pos.side,
            signal_metadata=db_row.get("signal_metadata"),
        )
        for tr in triggers:
            if tr.tpsl == "sl":
                sl_price = tr.trigger_price
            elif tr.tpsl == "tp":
                tp_price = tr.trigger_price

        pos = Position(
            symbol=symbol,
            side=ex_pos.side,
            entry_price=ex_pos.entry_price,
            size=ex_pos.size,
            entry_time_ms=int(time.time() * 1000),
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            metadata={
                "strategy": db_row.get("strategy", "reconciled_adopt"),
                "trade_id": trade_id,
                "adopted_from_exchange": True,
                "native_protection_active": bool(triggers),
            },
        )
        cost = ex_pos.entry_price * ex_pos.size
        await self._portfolio.apply_entry_fill(
            symbol,
            filled_size=ex_pos.size,
            avg_fill_price=ex_pos.entry_price,
            additional_cost=cost,
            position=pos,
        )
        if self._protection is not None and triggers:
            await self._protection.adopt_exchange_triggers(symbol, triggers)
        elif self._protection is not None and sl_price:
            await self._protection.ensure_protection(
                pos,
                filled_size=ex_pos.size,
                stop_price=sl_price,
                take_profit_price=tp_price,
                trade_id=trade_id,
            )
        report.actions.append(f"orphan_exchange_adopted:{symbol}")
        self._audit("orphan_exchange_adopt", symbol, {"size": ex_pos.size})

    async def _handle_mismatch(
        self,
        symbol: str,
        local: Any,
        ex: ExchangePosition,
        report: ReconciliationReport,
    ) -> None:
        msg = (
            f"RECONCILE mismatch {symbol}: local {local.side} "
            f"{local.size:.6f} vs exchange {ex.side} {ex.size:.6f}"
        )
        logger.error(msg)
        self._halt(f"mismatch:{symbol}")
        if symbol not in self._health.drift_symbols:
            self._health.drift_symbols.append(symbol)
        report.actions.append(f"mismatch_halt:{symbol}")
        self._send_alert(msg, level="error")
        self._audit(
            "size_side_mismatch",
            symbol,
            {"local_side": local.side, "local_size": local.size, "ex_side": ex.side, "ex_size": ex.size},
        )

    async def _detect_trigger_fills(
        self,
        ex_positions: Dict[str, ExchangePosition],
        local: Dict[str, Any],
        report: ReconciliationReport,
    ) -> None:
        """When exchange is flat for a symbol we still track, may be trigger fill."""
        # orphan_local path already handles local-without-exchange;
        # mark as potential trigger close for PnL reconciliation upstream.
        for sym in report.orphan_local:
            report.trigger_closes.append(sym)

    def _halt(self, reason: str) -> None:
        self._health.halted = True
        self._health.halt_reason = reason
        self._send_alert(f"Reconciliation HALT: {reason}", level="error")

    def _record_failure(self, reason: str) -> None:
        self._health.last_failure_ts = time.time()
        self._health.consecutive_failures += 1
        self._health.stale = True
        if self._health.consecutive_failures >= 3:
            self._halt(reason)

    def _send_alert(self, message: str, level: str = "warning") -> None:
        if self._alert is None:
            return
        try:
            self._alert(message, level)
        except Exception:  # noqa: BLE001
            pass

    def _audit(self, event: str, symbol: str, metadata: Dict[str, Any]) -> None:
        if self._db is None:
            return
        try:
            self._db.save_decision(
                timestamp=int(time.time() * 1000),
                decision_type="reconciliation",
                symbol=symbol,
                side="",
                strategy="system",
                result=event,
                reason=str(metadata)[:200],
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Reconciliation audit write failed: %s", exc)
