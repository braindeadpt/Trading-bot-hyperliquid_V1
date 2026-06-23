"""Tests for the v3.1.22 execution layer add-ons.

  * Paper slippage uses the L2 estimate when supplied.
  * OMS tracks live order ids, polls status, and rolls back on
    reject/cancel/timeout.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.execution import (
    ExecutionEngine,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_FILLED,
    ORDER_STATUS_OPEN,
    ORDER_STATUS_REJECTED,
    ORDER_STATUS_TIMEOUT,
    TradeResult,
)
from src.data.database import Database
from src.strategies.base import Signal
from src.utils.config import load_config

FAILED = 0


def _pass(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILED += 1


# ── TradeResult has new field ──────────────────────────────────────


def test_trade_result_has_exchange_order_id() -> None:
    r = TradeResult(
        trade_id=1, symbol="BTC", side="long",
        entry_price=100.0, exit_price=None, size=1.0,
        pnl_usd=0.0, pnl_pct=0.0, status="open",
        reason="test", timestamp_ms=0,
    )
    _pass("trade_result_has_exchange_order_id",
          hasattr(r, "exchange_order_id")
          and r.exchange_order_id is None)


# ── Extract order id from HL response ──────────────────────────────


def test_extract_order_id_direct() -> None:
    _pass("extract_order_id_direct",
          ExecutionEngine._extract_order_id({"oid": 12345}) == "12345")


def test_extract_order_id_nested_filled() -> None:
    """HL success: {"response": {"data": {"statuses": [{"filled": {"oid": 7}}]}}}"""
    resp = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": [
                    {"filled": {"oid": 7, "totalSz": "0.01", "avgPx": "50000"}}
                ]
            },
        },
    }
    _pass("extract_order_id_nested_filled",
          ExecutionEngine._extract_order_id(resp) == "7")


def test_extract_order_id_nested_resting() -> None:
    """HL resting: {"response": {"data": {"statuses": [{"resting": {"oid": 11}}]}}}"""
    resp = {
        "response": {"data": {"statuses": [{"resting": {"oid": 11}}]}}
    }
    _pass("extract_order_id_nested_resting",
          ExecutionEngine._extract_order_id(resp) == "11")


def test_extract_order_id_none() -> None:
    _pass("extract_order_id_none",
          ExecutionEngine._extract_order_id(None) is None
          and ExecutionEngine._extract_order_id({}) is None
          and ExecutionEngine._extract_order_id("not a dict") is None)


# ── Normalise HL status ────────────────────────────────────────────


def test_normalise_hl_status() -> None:
    n = ExecutionEngine._normalise_hl_status
    _pass("normalise_hl_status",
          n("filled") == ORDER_STATUS_FILLED
          and n("open") == ORDER_STATUS_OPEN
          and n("canceled") == ORDER_STATUS_CANCELLED
          and n("cancelled") == ORDER_STATUS_CANCELLED
          and n("rejected") == ORDER_STATUS_REJECTED
          and n("partial") == "partial"
          and n("") == ORDER_STATUS_OPEN)


# ── Constructor exposes OMS config ────────────────────────────────


def test_oms_config_loaded() -> None:
    cfg = load_config("config/settings.yaml")
    e = ExecutionEngine(cfg, None, mode="paper")
    _pass("oms_config_loaded",
          e._oms_poll_interval_s == 5.0
          and e._live_order_timeout_s == 60.0
          and e._live_orders == {})


# ── enter_position accepts new params ─────────────────────────────


def test_enter_position_signature_has_new_params() -> None:
    import inspect
    sig = inspect.signature(ExecutionEngine.enter_position)
    params = list(sig.parameters)
    _pass("enter_position_signature_has_new_params",
          "candles_1m" in params
          and "orderbook" in params
          and "estimated_slippage_bps" in params)


# ── Paper slippage uses L2 estimate when provided ──────────────────


def test_paper_slippage_uses_l2_estimate() -> None:
    """Build a fake engine and call enter_position with an L2
    slippage override; the resulting fill price must reflect it,
    not the flat default."""
    cfg = load_config("config/settings.yaml")
    db = Database(":memory:")
    e = ExecutionEngine(cfg, db, mode="paper")

    class FakePortfolio:
        @property
        def current_capital(self):
            async def _f():
                return 100_000.0
            return _f()

    portfolio = FakePortfolio()
    s = Signal(
        symbol="BTC", side="long",
        entry_price=50_000.0, size_pct=0.05,
        strategy="trend", confidence=0.8,
        metadata={"calculated_size": 0.01, "order_type": "market"},
    )
    # 5 bps L2 estimate → 0.05% slippage
    result = asyncio.run(
        e.enter_position(
            s, portfolio, market_event=None, estimated_slippage_bps=5.0
        )
    )
    # fill_price = 50_000 * (1 + 5/10_000) = 50_025
    _pass("paper_slippage_uses_l2_estimate",
          abs(result.entry_price - 50_025.0) < 1e-3,
          f"fill_price={result.entry_price}, expected 50025.0")


def test_paper_slippage_falls_back_to_default() -> None:
    """When no L2 estimate is given, the flat default applies."""
    cfg = load_config("config/settings.yaml")
    db = Database(":memory:")
    e = ExecutionEngine(cfg, db, mode="paper")

    class FakePortfolio:
        @property
        def current_capital(self):
            async def _f():
                return 100_000.0
            return _f()

    portfolio = FakePortfolio()
    s = Signal(
        symbol="BTC", side="long",
        entry_price=50_000.0, size_pct=0.05,
        strategy="trend", confidence=0.8,
        metadata={"calculated_size": 0.01, "order_type": "market"},
    )
    result = asyncio.run(e.enter_position(s, portfolio, market_event=None))
    # Default paper_slippage_pct in settings.yaml is 0.02%
    expected = 50_000.0 * (1.0 + 0.0002)
    _pass("paper_slippage_falls_back_to_default",
          abs(result.entry_price - expected) < 1e-3,
          f"fill_price={result.entry_price}, expected {expected}")


def test_paper_slippage_short_side() -> None:
    """Short fill: subtract slippage."""
    cfg = load_config("config/settings.yaml")
    db = Database(":memory:")
    e = ExecutionEngine(cfg, db, mode="paper")

    class FakePortfolio:
        @property
        def current_capital(self):
            async def _f():
                return 100_000.0
            return _f()

    portfolio = FakePortfolio()
    s = Signal(
        symbol="BTC", side="short",
        entry_price=50_000.0, size_pct=0.05,
        strategy="trend", confidence=0.8,
        metadata={"calculated_size": 0.01, "order_type": "market"},
    )
    result = asyncio.run(
        e.enter_position(
            s, portfolio, market_event=None, estimated_slippage_bps=10.0
        )
    )
    # 10 bps → 0.10% slippage, short = 50_000 * (1 - 0.001) = 49_950
    _pass("paper_slippage_short_side",
          abs(result.entry_price - 49_950.0) < 1e-3,
          f"fill_price={result.entry_price}")


# ── OMS loop + timeout + reject + cancel ──────────────────────────


def _make_engine() -> ExecutionEngine:
    cfg = load_config("config/settings.yaml")
    e = ExecutionEngine(cfg, None, mode="testnet")
    e._oms_poll_interval_s = 0.01  # fast for tests
    e._live_order_timeout_s = 0.1  # 100ms for tests
    return e


def test_oms_polls_open_order_and_rolls_back_on_reject() -> None:
    """REST returns 'rejected' → OMS marks rejected, calls rollback."""
    e = _make_engine()
    e._rest_client = MagicMock()
    e._rest_client.get_order_status = AsyncMock(
        return_value={"order": {"status": "rejected"}}
    )
    e._db = MagicMock()
    e._db.update_trade_status = MagicMock()
    e._portfolio = MagicMock()
    e._portfolio.cancel_position = AsyncMock()
    e._live_orders["oid-1"] = {
        "symbol": "BTC", "side": "long", "size": 0.01,
        "price": 50_000.0, "filled_size": 0.0,
        "status": ORDER_STATUS_OPEN,
        "timestamp": time.time(),
        "trade_id": 99,
        "order_type": "market",
    }
    # Spawn one poll cycle
    asyncio.run(e._poll_one_order("oid-1", e._live_orders["oid-1"]))
    _pass("oms_polls_open_order_and_rolls_back_on_reject",
          e._live_orders.get("oid-1") is None
          and e._db.update_trade_status.called
          and e._portfolio.cancel_position.await_count >= 1)


def test_oms_polls_open_order_and_marks_filled() -> None:
    e = _make_engine()
    e._rest_client = MagicMock()
    e._rest_client.get_order_status = AsyncMock(
        return_value={"order": {"status": "filled"}}
    )
    e._db = MagicMock()
    e._db.update_trade_status = MagicMock()
    e._live_orders["oid-2"] = {
        "symbol": "ETH", "side": "short", "size": 0.5,
        "price": 3_000.0, "filled_size": 0.0,
        "status": ORDER_STATUS_OPEN,
        "timestamp": time.time(),
        "trade_id": 100,
        "order_type": "limit_maker",
    }
    asyncio.run(e._poll_one_order("oid-2", e._live_orders["oid-2"]))
    _pass("oms_polls_open_order_and_marks_filled",
          e._live_orders["oid-2"]["status"] == ORDER_STATUS_FILLED)


def test_oms_triggers_timeout_rollback() -> None:
    e = _make_engine()
    e._rest_client = MagicMock()
    e._rest_client.get_order_status = AsyncMock(
        return_value={"order": {"status": "open"}}
    )
    e._rest_client.cancel_order = AsyncMock()
    e._db = MagicMock()
    e._db.update_trade_status = MagicMock()
    e._portfolio = MagicMock()
    e._portfolio.cancel_position = AsyncMock()
    # Make it look old
    e._live_orders["oid-3"] = {
        "symbol": "BTC", "side": "long", "size": 0.01,
        "price": 50_000.0, "filled_size": 0.0,
        "status": ORDER_STATUS_OPEN,
        "timestamp": time.time() - 10.0,  # 10s old, > 0.1s timeout
        "trade_id": 101,
        "order_type": "market",
    }
    asyncio.run(e._poll_one_order("oid-3", e._live_orders["oid-3"]))
    _pass("oms_triggers_timeout_rollback",
          e._live_orders.get("oid-3") is None
          and e._db.update_trade_status.called)


def test_oms_callback_fires_on_status_change() -> None:
    e = _make_engine()
    e._rest_client = MagicMock()
    e._rest_client.get_order_status = AsyncMock(
        return_value={"order": {"status": "filled"}}
    )
    e._db = MagicMock()
    e._db.update_trade_status = MagicMock()
    events: list = []
    e.register_order_callback(lambda oid, status, rec: events.append((oid, status)))
    e._live_orders["oid-4"] = {
        "symbol": "SOL", "side": "long", "size": 1.0,
        "price": 100.0, "filled_size": 0.0,
        "status": ORDER_STATUS_OPEN,
        "timestamp": time.time(),
        "trade_id": 102,
        "order_type": "market",
    }
    asyncio.run(e._poll_one_order("oid-4", e._live_orders["oid-4"]))
    _pass("oms_callback_fires_on_status_change",
          ("oid-4", ORDER_STATUS_FILLED) in events)


def test_oms_start_is_idempotent() -> None:
    """Idempotent: starting twice gives the same task back."""
    async def _run() -> bool:
        e = _make_engine()
        e._rest_client = MagicMock()
        e._rest_client.get_order_status = AsyncMock(
            return_value={"order": {"status": "open"}}
        )
        await e.start_oms_loop()
        first = e._oms_task
        await e.start_oms_loop()
        same = e._oms_task is first
        await e.stop_oms_loop()
        return same
    _pass("oms_start_is_idempotent", asyncio.run(_run()))


def test_oms_stop_is_idempotent() -> None:
    e = _make_engine()
    asyncio.run(e.stop_oms_loop())  # never started — must not raise
    asyncio.run(e.stop_oms_loop())
    _pass("oms_stop_is_idempotent", True)


def test_oms_loop_does_not_start_in_paper_mode() -> None:
    e = ExecutionEngine(load_config("config/settings.yaml"), None, mode="paper")
    asyncio.run(e.start_oms_loop())
    _pass("oms_loop_does_not_start_in_paper_mode", e._oms_task is None)


# ── get_open_orders filters correctly ──────────────────────────────


def test_get_open_orders_filters_terminal() -> None:
    e = _make_engine()
    e._live_orders["a"] = {
        "symbol": "BTC", "side": "long", "size": 0.01, "price": 100.0,
        "filled_size": 0.0, "status": ORDER_STATUS_OPEN,
        "timestamp": time.time(), "trade_id": 1, "order_type": "market",
    }
    e._live_orders["b"] = {
        "symbol": "ETH", "side": "long", "size": 0.5, "price": 100.0,
        "filled_size": 0.5, "status": ORDER_STATUS_FILLED,
        "timestamp": time.time(), "trade_id": 2, "order_type": "market",
    }
    e._live_orders["c"] = {
        "symbol": "SOL", "side": "short", "size": 1.0, "price": 100.0,
        "filled_size": 0.5, "status": "partial",
        "timestamp": time.time(), "trade_id": 3, "order_type": "market",
    }
    open_orders = e.get_open_orders()
    _pass("get_open_orders_filters_terminal",
          "a" in open_orders and "b" not in open_orders and "c" in open_orders)


def main() -> int:
    print("=" * 70)
    print("Execution layer v3.1.22 tests (L2 slippage + OMS)")
    print("=" * 70)
    tests = [
        test_trade_result_has_exchange_order_id,
        test_extract_order_id_direct,
        test_extract_order_id_nested_filled,
        test_extract_order_id_nested_resting,
        test_extract_order_id_none,
        test_normalise_hl_status,
        test_oms_config_loaded,
        test_enter_position_signature_has_new_params,
        test_paper_slippage_uses_l2_estimate,
        test_paper_slippage_falls_back_to_default,
        test_paper_slippage_short_side,
        test_oms_polls_open_order_and_rolls_back_on_reject,
        test_oms_polls_open_order_and_marks_filled,
        test_oms_triggers_timeout_rollback,
        test_oms_callback_fires_on_status_change,
        test_oms_start_is_idempotent,
        test_oms_stop_is_idempotent,
        test_oms_loop_does_not_start_in_paper_mode,
        test_get_open_orders_filters_terminal,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            _pass(t.__name__, False, f"AssertionError: {e}")
        except Exception as e:  # noqa: BLE001
            _pass(t.__name__, False, f"{type(e).__name__}: {e}")
    print("=" * 70)
    if FAILED == 0:
        print(f"ALL TESTS PASSED ({len(tests)}/{len(tests)})")
        return 0
    print(f"FAILED: {FAILED}/{len(tests)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
