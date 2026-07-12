"""Phase 02 — OMS behavioural tests (partial fills, idempotency, recovery)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.execution import (
    ExecutionEngine,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_FILLED,
    ORDER_STATUS_OPEN,
    ORDER_STATUS_PARTIAL,
    ORDER_STATUS_REJECTED,
    ORDER_STATUS_TIMEOUT,
)
from src.core.order_lifecycle import (
    ORDER_PARTIAL,
    ORDER_RESTING,
    OrderFillSnapshot,
    parse_order_fill_snapshot,
)
from src.core.portfolio import PortfolioState
from src.data.database import Database, TradeEntry
from src.strategies.base import Position, Signal
from src.utils.config import load_config
import pytest

pytestmark = pytest.mark.integration_offline

FAILED = 0


def _pass(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILED += 1


def _make_engine(db: Database | None = None) -> ExecutionEngine:
    cfg = load_config("config/settings.yaml")
    engine = ExecutionEngine(cfg, db or Database(":memory:"), mode="testnet")
    engine._oms_poll_interval_s = 0.01
    engine._live_order_timeout_s = 0.15
    engine._live_client = MagicMock()
    engine._live_signing_ready = True
    engine._rest_client = MagicMock()
    return engine


def _snapshot(
    *,
    status: str,
    lifecycle: str,
    filled: float,
    target: float,
    avg_px: float = 50_000.0,
    fee: float = 1.0,
) -> OrderFillSnapshot:
    return OrderFillSnapshot(
        status=status,
        lifecycle_state=lifecycle,
        filled_size=filled,
        remaining_size=max(0.0, target - filled),
        avg_fill_price=avg_px,
        cumulative_fee=fee,
        last_fill_at_ms=int(time.time() * 1000),
    )


def _base_record(target: float = 0.02, filled: float = 0.0) -> dict:
    return {
        "symbol": "BTC",
        "side": "long",
        "size": target,
        "price": 50_000.0,
        "filled_size": filled,
        "remaining_size": target - filled,
        "avg_fill_price": 50_000.0 if filled else 0.0,
        "cumulative_fee": 0.0,
        "status": ORDER_STATUS_OPEN,
        "lifecycle_state": ORDER_RESTING,
        "timestamp": time.time(),
        "submitted_at_ms": int(time.time() * 1000),
        "trade_id": 42,
        "order_type": "limit_maker",
        "client_order_id": "hlbot-BTC-long-1",
        "exchange_order_id": "1001",
        "applied_fill_size": filled,
        "terminal_handled": False,
        "processed_events": [],
    }


class TrackingPortfolio(PortfolioState):
    def __init__(self) -> None:
        super().__init__(initial_capital=100_000.0)
        self.fill_calls: list = []

    async def apply_entry_fill(self, symbol, **kwargs) -> None:  # type: ignore[override]
        self.fill_calls.append((symbol, kwargs))
        await super().apply_entry_fill(symbol, **kwargs)

    async def cancel_position(self, symbol: str) -> None:
        await super().cancel_position(symbol)


# ── parse_order_fill_snapshot ─────────────────────────────────────


def test_parse_full_fill_from_fills() -> None:
    fills = [{"oid": 7, "sz": "0.02", "px": "50000", "fee": "0.5", "time": 1}]
    snap = parse_order_fill_snapshot(
        {"status": "filled"},
        fills,
        order_id="7",
        target_size=0.02,
        reference_price=50_000.0,
    )
    _pass(
        "parse_full_fill_from_fills",
        snap.status == ORDER_STATUS_FILLED and abs(snap.filled_size - 0.02) < 1e-9,
        f"filled={snap.filled_size}",
    )


def test_parse_partial_fill() -> None:
    fills = [{"oid": 8, "sz": "0.01", "px": "50000", "fee": "0.25", "time": 2}]
    snap = parse_order_fill_snapshot(
        {"status": "open"},
        fills,
        order_id="8",
        target_size=0.02,
        reference_price=50_000.0,
    )
    _pass(
        "parse_partial_fill",
        snap.status == ORDER_STATUS_PARTIAL and abs(snap.remaining_size - 0.01) < 1e-9,
    )


# ── OMS lifecycle scenarios ─────────────────────────────────────────


def test_full_fill() -> None:
    async def _run() -> bool:
        e = _make_engine()
        e._live_client.get_order_status = AsyncMock(return_value={"status": "filled"})
        e._live_client.get_order_fills = AsyncMock(
            return_value=[{"oid": 1001, "sz": "0.02", "px": "50000", "fee": "1", "time": 1}]
        )
        record = _base_record()
        e._live_orders["1001"] = record
        await e._poll_one_order("1001", record)
        return (
            "1001" not in e._live_orders
            and record.get("terminal_handled")
            and e._open_trades.get("BTC") is not None
            and abs(e._open_trades["BTC"].size - 0.02) < 1e-9
        )

    _pass("full_fill", asyncio.run(_run()))


def test_resting_to_fill() -> None:
    async def _run() -> bool:
        e = _make_engine()
        e._live_client.get_order_status = AsyncMock(return_value={"status": "open"})
        e._live_client.get_order_fills = AsyncMock(return_value=[])
        record = _base_record()
        e._live_orders["1002"] = record
        await e._poll_one_order("1002", record)
        still_open = record.get("status") == ORDER_STATUS_OPEN

        e._live_client.get_order_status = AsyncMock(return_value={"status": "filled"})
        e._live_client.get_order_fills = AsyncMock(
            return_value=[{"oid": 1002, "sz": "0.02", "px": "50000", "fee": "1", "time": 2}]
        )
        await e._poll_one_order("1002", record)
        return still_open and "1002" not in e._live_orders

    _pass("resting_to_fill", asyncio.run(_run()))


def test_partial_to_full() -> None:
    async def _run() -> bool:
        from src.utils.helpers import safe_float

        portfolio = TrackingPortfolio()
        e = _make_engine()
        e.set_portfolio(portfolio)

        record = _base_record()
        record["exchange_order_id"] = "1003"
        e._live_orders["1003"] = record

        async def _cb(oid, status, rec):
            prev = safe_float(rec.get("_pf", 0))
            delta = safe_float(rec.get("filled_size")) - prev
            if delta <= 0:
                return
            rec["_pf"] = safe_float(rec.get("filled_size"))
            await portfolio.apply_entry_fill(
                rec["symbol"],
                filled_size=delta,
                avg_fill_price=50_000.0,
                additional_cost=50_000.0 * delta,
                position=Position(
                    symbol="BTC", side="long", entry_price=50_000.0, size=delta,
                    entry_time_ms=1, stop_loss_price=49_000.0,
                    take_profit_price=52_000.0, unrealized_pnl=0.0,
                ),
            )

        e.register_order_callback(
            lambda oid, status, rec: asyncio.create_task(_cb(oid, status, rec))
        )

        e._live_client.get_order_status = AsyncMock(return_value={"status": "open"})
        e._live_client.get_order_fills = AsyncMock(
            return_value=[{"oid": 1003, "sz": "0.01", "px": "50000", "fee": "0.5", "time": 3}]
        )
        await e._poll_one_order("1003", record)
        partial_ok = record.get("status") == ORDER_STATUS_PARTIAL

        e._live_client.get_order_fills = AsyncMock(
            return_value=[
                {"oid": 1003, "sz": "0.01", "px": "50000", "fee": "0.5", "time": 3},
                {"oid": 1003, "sz": "0.01", "px": "50100", "fee": "0.5", "time": 4},
            ]
        )
        e._live_client.get_order_status = AsyncMock(return_value={"status": "filled"})
        await e._poll_one_order("1003", record)
        await asyncio.sleep(0.05)
        pos = (await portfolio.positions).get("BTC")
        return partial_ok and pos is not None and abs(pos.size - 0.02) < 1e-9

    _pass("partial_to_full", asyncio.run(_run()))


def test_partial_timeout_cancel_residual() -> None:
    async def _run() -> bool:
        e = _make_engine()
        e._live_order_timeout_s = 0.05
        e._live_client.cancel_order = AsyncMock(return_value={"status": "ok"})
        record = _base_record(filled=0.01)
        record.update({
            "exchange_order_id": "1004",
            "filled_size": 0.01,
            "remaining_size": 0.01,
            "status": ORDER_STATUS_PARTIAL,
            "lifecycle_state": ORDER_PARTIAL,
            "applied_fill_size": 0.01,
            "timestamp": time.time() - 1.0,
        })
        e._live_orders["1004"] = record
        e._live_client.get_order_status = AsyncMock(return_value={"status": "open"})
        e._live_client.get_order_fills = AsyncMock(
            return_value=[{"oid": 1004, "sz": "0.01", "px": "50000", "fee": "0.5", "time": 5}]
        )
        await e._poll_one_order("1004", record)
        return (
            "1004" not in e._live_orders
            and e._open_trades.get("BTC") is not None
            and abs(e._open_trades["BTC"].size - 0.01) < 1e-9
            and e._live_client.cancel_order.await_count >= 1
        )

    _pass("partial_timeout_cancel_residual", asyncio.run(_run()))


def test_reject_zero_fill_rollback() -> None:
    async def _run() -> bool:
        db = Database(":memory:")
        e = _make_engine(db)
        portfolio = TrackingPortfolio()
        e.set_portfolio(portfolio)
        e._db.update_trade_status = MagicMock()
        record = _base_record()
        record["exchange_order_id"] = "1005"
        e._live_orders["1005"] = record
        e.fetch_order_snapshot = AsyncMock(  # type: ignore[method-assign]
            return_value=_snapshot(
                status=ORDER_STATUS_REJECTED,
                lifecycle="rejected",
                filled=0.0,
                target=0.02,
            )
        )
        await e._poll_one_order("1005", record)
        return e._live_orders.get("1005") is None and e._db.update_trade_status.called

    _pass("reject_zero_fill_rollback", asyncio.run(_run()))


def test_duplicate_callback_idempotent() -> None:
    async def _run() -> bool:
        portfolio = TrackingPortfolio()
        e = _make_engine()
        e.set_portfolio(portfolio)
        record = _base_record()
        record["filled_size"] = 0.02
        snap = _snapshot(
            status=ORDER_STATUS_FILLED,
            lifecycle="filled",
            filled=0.02,
            target=0.02,
        )

        async def _portfolio_cb(oid, status, rec):
            from src.utils.helpers import safe_float
            prev = safe_float(rec.get("_portfolio_applied_fill", 0))
            delta = safe_float(rec.get("filled_size")) - prev
            if delta <= 0:
                return
            rec["_portfolio_applied_fill"] = safe_float(rec.get("filled_size"))
            await portfolio.apply_entry_fill(
                "BTC",
                filled_size=delta,
                avg_fill_price=50_000.0,
                additional_cost=50_000.0 * delta,
                position=Position(
                    symbol="BTC", side="long", entry_price=50_000.0, size=delta,
                    entry_time_ms=1, stop_loss_price=49_000.0,
                    take_profit_price=52_000.0, unrealized_pnl=0.0,
                ),
            )

        from src.utils.helpers import safe_float

        e.register_order_callback(
            lambda oid, status, rec: asyncio.create_task(_portfolio_cb(oid, status, rec))
        )
        await e._apply_fill_delta("1006", record, snap, 0.02)
        await asyncio.sleep(0.05)
        e._fire_order_callbacks("1006", ORDER_STATUS_FILLED, record)
        await asyncio.sleep(0.05)
        pos = (await portfolio.positions).get("BTC")
        return pos is not None and abs(pos.size - 0.02) < 1e-9 and len(portfolio.fill_calls) == 1

    _pass("duplicate_callback_idempotent", asyncio.run(_run()))


def test_restart_restores_resting_and_partial() -> None:
    async def _run() -> bool:
        db = Database(":memory:")
        tid_rest = db.save_trade_entry(TradeEntry(
            symbol="BTC", side="long", entry_price=50_000.0, entry_time=1,
            size=0.02, strategy="t", status=ORDER_RESTING,
        ))
        db.update_trade_order_tracking(
            int(tid_rest), exchange_order_id="100", client_order_id="c-100",
            order_submitted_at=1,
        )
        tid_part = db.save_trade_entry(TradeEntry(
            symbol="ETH", side="short", entry_price=3_000.0, entry_time=2,
            size=0.5, strategy="t", status=ORDER_PARTIAL,
        ))
        db.update_trade_order_tracking(
            int(tid_part),
            exchange_order_id="200",
            client_order_id="c-200",
            filled_size=0.25,
            order_submitted_at=2,
        )
        e = _make_engine(db)
        n = await e.load_pending_orders()
        return (
            n == 2
            and "100" in e._live_orders
            and "200" in e._live_orders
            and e._live_orders["200"]["filled_size"] == 0.25
        )

    _pass("restart_restores_resting_and_partial", asyncio.run(_run()))


def test_cancel_failure_preserves_partial_exposure() -> None:
    async def _run() -> bool:
        e = _make_engine()
        alerts: list = []
        e.set_oms_alert_callback(lambda ev, oid, rec: alerts.append(ev))
        e._live_client.cancel_order = AsyncMock(side_effect=RuntimeError("hl_down"))
        record = _base_record(filled=0.01)
        record.update({
            "exchange_order_id": "1007",
            "filled_size": 0.01,
            "remaining_size": 0.01,
            "applied_fill_size": 0.01,
            "timestamp": time.time() - 1.0,
            "status": ORDER_STATUS_PARTIAL,
            "lifecycle_state": ORDER_PARTIAL,
        })
        e._live_orders["1007"] = record
        snap = _snapshot(
            status=ORDER_STATUS_PARTIAL,
            lifecycle=ORDER_PARTIAL,
            filled=0.01,
            target=0.02,
        )
        await e._handle_partial_timeout("1007", record, 120.0)
        return (
            e._open_trades.get("BTC") is not None
            and abs(e._open_trades["BTC"].size - 0.01) < 1e-9
            and "cancel_failed" in alerts
        )

    _pass("cancel_failure_preserves_partial_exposure", asyncio.run(_run()))


def test_oms_start_stop_wired() -> None:
    async def _run() -> bool:
        e = _make_engine()
        await e.start_oms_loop()
        started = e._oms_task is not None
        await e.stop_oms_loop()
        stopped = e._oms_task is None
        return started and stopped

    _pass("oms_start_stop_wired", asyncio.run(_run()))


def test_paper_mode_skips_oms() -> None:
    cfg = load_config("config/settings.yaml")
    e = ExecutionEngine(cfg, Database(":memory:"), mode="paper")
    asyncio.run(e.start_oms_loop())
    _pass("paper_mode_skips_oms", e._oms_task is None)


def main() -> int:
    print("=" * 70)
    print("Phase 02 OMS behavioural tests")
    print("=" * 70)
    tests = [
        test_parse_full_fill_from_fills,
        test_parse_partial_fill,
        test_full_fill,
        test_resting_to_fill,
        test_partial_to_full,
        test_partial_timeout_cancel_residual,
        test_reject_zero_fill_rollback,
        test_duplicate_callback_idempotent,
        test_restart_restores_resting_and_partial,
        test_cancel_failure_preserves_partial_exposure,
        test_oms_start_stop_wired,
        test_paper_mode_skips_oms,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            _pass(t.__name__, False, f"{type(exc).__name__}: {exc}")
    print("=" * 70)
    if FAILED == 0:
        print(f"ALL TESTS PASSED ({len(tests)}/{len(tests)})")
        return 0
    print(f"FAILED: {FAILED}/{len(tests)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
