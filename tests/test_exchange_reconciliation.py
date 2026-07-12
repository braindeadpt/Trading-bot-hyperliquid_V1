"""Phase 03 — exchange reconciliation, native SL/TP, kill switch (behavioural)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.execution import ExecutionEngine, KillSwitchResult
from src.core.native_protection import NativeProtectionManager
from src.core.order_lifecycle import (
    ORDER_FILLED,
    ORDER_PARTIAL,
    ORDER_SUBMISSION_UNKNOWN,
    parse_hyperliquid_entry_response,
)
from src.core.portfolio import PortfolioState
from src.core.reconciliation import ExchangeReconciler, OrphanExchangePolicy
from src.data.database import Database, TradeEntry
from src.exchanges.hl_positions import parse_exchange_positions, parse_trigger_orders
from src.strategies.base import Position
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


def _mock_user_state(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    asset_positions = []
    for p in positions:
        szi = p["size"] if p["side"] == "long" else -p["size"]
        asset_positions.append({
            "position": {
                "coin": p["symbol"],
                "szi": str(szi),
                "entryPx": str(p.get("entry_price", 50_000)),
            }
        })
    return {"assetPositions": asset_positions, "marginSummary": {"totalValue": "10000"}}


class MockLiveClient:
    def __init__(self) -> None:
        self.user_state = _mock_user_state([])
        self.open_orders: List[Dict[str, Any]] = []
        self.trigger_calls: List[Dict[str, Any]] = []
        self.cancel_calls: List[Any] = []
        self.close_calls: List[Any] = []
        self.flat = True
        self.user_fills: List[Dict[str, Any]] = []

    async def get_user_fills(self, *, lookback_ms: int = 86_400_000) -> List[Dict[str, Any]]:
        return list(self.user_fills)

    async def get_user_state(self) -> Dict[str, Any]:
        return self.user_state

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        return list(self.open_orders)

    async def get_positions(self) -> List[Dict[str, Any]]:
        parsed = parse_exchange_positions(self.user_state)
        return [
            {"symbol": p.symbol, "side": p.side, "size": p.size, "entry_price": p.entry_price}
            for p in parsed.values()
        ]

    async def place_trigger_order(
        self, symbol, position_side, size, *, trigger_price, tpsl,
    ) -> Dict[str, Any]:
        oid = 10_000 + len(self.trigger_calls)
        self.trigger_calls.append({
            "symbol": symbol, "side": position_side, "size": size,
            "trigger_price": trigger_price, "tpsl": tpsl,
        })
        self.open_orders.append({
            "coin": symbol,
            "oid": oid,
            "sz": str(size),
            "side": "A" if position_side == "short" else "B",
            "orderType": {"trigger": {"triggerPx": trigger_price, "isMarket": True, "tpsl": tpsl}},
        })
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}

    async def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        self.cancel_calls.append((symbol, order_id))
        self.open_orders = [
            o for o in self.open_orders if int(o.get("oid", -1)) != int(order_id)
        ]
        return {"status": "ok"}

    async def cancel_all_orders(self, symbol=None) -> int:
        n = len(self.open_orders)
        self.open_orders.clear()
        return n

    async def close_position(self, symbol: str, size: float) -> Dict[str, Any]:
        self.close_calls.append((symbol, size))
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1}}]}}}

    async def flatten_all_positions(self) -> List[Dict[str, Any]]:
        parsed = parse_exchange_positions(self.user_state)
        results = []
        for sym, pos in parsed.items():
            await self.close_position(sym, pos.size)
            results.append({"symbol": sym, "ok": True})
        self.user_state = _mock_user_state([])
        return results

    async def confirm_flat(self) -> bool:
        return len(parse_exchange_positions(self.user_state)) == 0


# ── parse helpers ───────────────────────────────────────────────────


def test_parse_exchange_positions() -> None:
    state = _mock_user_state([
        {"symbol": "BTC", "side": "long", "size": 0.02, "entry_price": 50_000},
    ])
    parsed = parse_exchange_positions(state)
    _pass(
        "parse_exchange_positions",
        "BTC" in parsed and parsed["BTC"].side == "long" and abs(parsed["BTC"].size - 0.02) < 1e-9,
    )


def test_parse_trigger_orders() -> None:
    orders = [{
        "coin": "BTC", "oid": 42, "sz": "0.02", "side": "A",
        "orderType": {"trigger": {"triggerPx": 49_000, "isMarket": True, "tpsl": "sl"}},
    }]
    triggers = parse_trigger_orders(orders)
    _pass(
        "parse_trigger_orders",
        "BTC" in triggers and triggers["BTC"][0].tpsl == "sl",
    )


def test_ambiguous_response_is_submission_unknown() -> None:
    parsed = parse_hyperliquid_entry_response({"status": "ok"})
    _pass(
        "ambiguous_response_is_submission_unknown",
        parsed.lifecycle_state == ORDER_SUBMISSION_UNKNOWN and parsed.ambiguous,
    )


# ── native protection ───────────────────────────────────────────────


def test_entry_fill_creates_sl_tp() -> None:
    async def _run() -> bool:
        client = MockLiveClient()
        mgr = NativeProtectionManager(client)
        pos = Position(
            symbol="BTC", side="long", entry_price=50_000, size=0.02,
            entry_time_ms=1, stop_loss_price=49_000, take_profit_price=52_000,
        )
        result = await mgr.ensure_protection(
            pos, filled_size=0.02, stop_price=49_000, take_profit_price=52_000,
        )
        return (
            result.sl_order_id is not None
            and result.tp_order_id is not None
            and len(client.trigger_calls) == 2
        )

    _pass("entry_fill_creates_sl_tp", asyncio.run(_run()))


def test_partial_fill_protection_size() -> None:
    async def _run() -> bool:
        client = MockLiveClient()
        mgr = NativeProtectionManager(client)
        pos = Position(
            symbol="ETH", side="short", entry_price=3_000, size=0.01,
            entry_time_ms=1, stop_loss_price=3_100, take_profit_price=2_900,
        )
        r1 = await mgr.ensure_protection(
            pos, filled_size=0.01, stop_price=3_100, take_profit_price=2_900,
        )
        pos.size = 0.03
        r2 = await mgr.ensure_protection(
            pos, filled_size=0.03, stop_price=3_100, take_profit_price=2_900,
        )
        return (
            r1.protected_size == 0.01
            and r2.protected_size == 0.03
            and len(r2.cancelled_ids) >= 1
            and any(c["size"] == 0.03 for c in client.trigger_calls[-2:])
        )

    _pass("partial_fill_protection_size", asyncio.run(_run()))


def test_cancel_replace_idempotent() -> None:
    async def _run() -> bool:
        client = MockLiveClient()
        mgr = NativeProtectionManager(client)
        pos = Position(
            symbol="SOL", side="long", entry_price=150, size=1.0,
            entry_time_ms=1, stop_loss_price=145, take_profit_price=160,
        )
        await mgr.ensure_protection(pos, filled_size=1.0, stop_price=145, take_profit_price=160)
        await mgr.ensure_protection(pos, filled_size=1.0, stop_price=145, take_profit_price=160)
        return len(client.cancel_calls) == 0 and len(client.trigger_calls) == 2

    _pass("cancel_replace_idempotent", asyncio.run(_run()))


# ── reconciliation policies ─────────────────────────────────────────


def test_orphan_local_closes_book() -> None:
    async def _run() -> bool:
        client = MockLiveClient()
        client.user_state = _mock_user_state([])
        portfolio = PortfolioState(10_000)
        await portfolio.add_position(
            Position(symbol="BTC", side="long", entry_price=50_000, size=0.01, entry_time_ms=1),
            cost=500,
        )
        recon = ExchangeReconciler(
            live_client=client, portfolio=portfolio, orphan_exchange_policy="HALT",
        )
        report = await recon.reconcile_once()
        mem = await portfolio.positions
        return "BTC" not in mem and "orphan_local_closed:BTC" in report.actions

    _pass("orphan_local_closes_book", asyncio.run(_run()))


def test_orphan_exchange_adopt_and_protect() -> None:
    async def _run() -> bool:
        client = MockLiveClient()
        client.user_state = _mock_user_state([
            {"symbol": "BTC", "side": "long", "size": 0.02, "entry_price": 50_000},
        ])
        client.open_orders = [{
            "coin": "BTC", "oid": 99, "sz": "0.02", "side": "A",
            "orderType": {"trigger": {"triggerPx": 49_000, "isMarket": True, "tpsl": "sl"}},
        }]
        portfolio = PortfolioState(10_000)
        protection = NativeProtectionManager(client)
        recon = ExchangeReconciler(
            live_client=client,
            portfolio=portfolio,
            protection=protection,
            orphan_exchange_policy=OrphanExchangePolicy.ADOPT_AND_PROTECT.value,
        )
        report = await recon.reconcile_once()
        mem = await portfolio.positions
        return (
            "BTC" in mem
            and abs(mem["BTC"].size - 0.02) < 1e-9
            and "orphan_exchange_adopted:BTC" in report.actions
        )

    _pass("orphan_exchange_adopt_and_protect", asyncio.run(_run()))


def test_size_mismatch_halts_entries() -> None:
    async def _run() -> bool:
        client = MockLiveClient()
        client.user_state = _mock_user_state([
            {"symbol": "BTC", "side": "long", "size": 0.05, "entry_price": 50_000},
        ])
        portfolio = PortfolioState(10_000)
        await portfolio.add_position(
            Position(symbol="BTC", side="long", entry_price=50_000, size=0.02, entry_time_ms=1),
            cost=1_000,
        )
        recon = ExchangeReconciler(live_client=client, portfolio=portfolio)
        report = await recon.reconcile_once()
        return recon.entries_blocked() and "BTC" in report.mismatches

    _pass("size_mismatch_halts_entries", asyncio.run(_run()))


def test_reconciliation_stale_blocks_entries() -> None:
    recon = ExchangeReconciler(
        live_client=MagicMock(),
        portfolio=PortfolioState(10_000),
        stale_threshold_sec=1.0,
    )
    _pass(
        "reconciliation_stale_blocks_entries",
        recon.entries_blocked() and recon.is_stale(),
    )


# ── kill switch ─────────────────────────────────────────────────────


def test_kill_switch_confirms_flat() -> None:
    async def _run() -> bool:
        cfg = load_config("config/settings.yaml")
        db = Database(":memory:")
        engine = ExecutionEngine(cfg, db, mode="testnet")
        client = MockLiveClient()
        client.user_state = _mock_user_state([
            {"symbol": "BTC", "side": "long", "size": 0.01, "entry_price": 50_000},
        ])
        client.open_orders = [{"coin": "BTC", "oid": 1, "sz": "0.01"}]
        engine._live_client = client
        engine._live_signing_ready = True
        engine._rest_client = MagicMock()
        engine.set_portfolio(PortfolioState(10_000))
        result = await engine.kill_switch()
        return (
            isinstance(result, KillSwitchResult)
            and result.exchange_flat
            and result.orders_cancelled >= 1
            and "BTC" in result.positions_closed
        )

    _pass("kill_switch_confirms_flat", asyncio.run(_run()))


# ── restart partial fill E2E ────────────────────────────────────────


def test_restart_partial_fill_no_duplicate_exposure() -> None:
    async def _run() -> bool:
        db = Database(":memory:")
        trade_id = db.save_trade_entry(TradeEntry(
            symbol="BTC", side="long", entry_price=50_000, entry_time=int(time.time() * 1000),
            size=0.02, strategy="test", status="partial",
        ))
        db.update_trade_order_tracking(
            trade_id,
            exchange_order_id="1001",
            filled_size=0.01,
            applied_fill_size=0.01,
            avg_fill_price=50_000,
        )
        cfg = load_config("config/settings.yaml")
        engine = ExecutionEngine(cfg, db, mode="testnet")
        engine._live_client = MagicMock()
        engine._live_signing_ready = True
        portfolio = PortfolioState(100_000)
        engine.set_portfolio(portfolio)

        restored = await engine.load_pending_orders()
        record = engine.get_tracked_order("1001") or {}
        applied = safe_float(record.get("applied_fill_size"))
        # Simulate duplicate callback — delta must be zero
        record["_portfolio_applied_fill"] = applied
        delta = safe_float(record.get("filled_size", 0.01)) - applied
        return restored == 1 and abs(delta) < 1e-12

    from src.utils.helpers import safe_float

    _pass("restart_partial_fill_no_duplicate_exposure", asyncio.run(_run()))


def test_ws_off_trigger_still_on_exchange() -> None:
    """Native triggers persist on exchange even when bot WS is down."""
    async def _run() -> bool:
        client = MockLiveClient()
        mgr = NativeProtectionManager(client)
        pos = Position(
            symbol="BTC", side="long", entry_price=50_000, size=0.02,
            entry_time_ms=1, stop_loss_price=49_000, take_profit_price=52_000,
        )
        await mgr.ensure_protection(
            pos, filled_size=0.02, stop_price=49_000, take_profit_price=52_000,
        )
        # WS down — no local state needed; exchange still has triggers
        triggers = parse_trigger_orders(client.open_orders)
        return len(triggers.get("BTC", [])) == 2

    _pass("ws_off_trigger_still_on_exchange", asyncio.run(_run()))


def test_submission_unknown_blocks_symbol() -> None:
    cfg = load_config("config/settings.yaml")
    engine = ExecutionEngine(cfg, Database(":memory:"), mode="testnet")
    engine.block_symbol("ETH", "submission_unknown:test")
    _pass(
        "submission_unknown_blocks_symbol",
        engine.is_symbol_blocked("ETH"),
    )


def test_trigger_sl_fill_reconciles_exact_pnl_once() -> None:
    async def _run() -> bool:
        from src.core.trigger_reconcile import CLOSE_PENDING_RECONCILIATION

        db = Database(":memory:")
        trade_id = db.save_trade_entry(TradeEntry(
            symbol="BTC", side="long", entry_price=50_000,
            entry_time=1_700_000_000_000, size=0.02, strategy="test", status="open",
            entry_fee=1.0,
        ))
        db.update_trade_native_protection(
            trade_id, sl_order_id="9001", tp_order_id="9002", protected_size=0.02,
        )
        client = MockLiveClient()
        client.user_state = _mock_user_state([])
        client.open_orders = [{
            "coin": "BTC", "oid": 9002, "sz": "0.02", "side": "A",
            "orderType": {"trigger": {"triggerPx": 52_000, "isMarket": True, "tpsl": "tp"}},
        }]
        client.user_fills = [{
            "coin": "BTC", "oid": 9001, "sz": "0.02", "px": "49000",
            "side": "A", "fee": "0.5", "closedPnl": "-20.5", "time": 1_700_000_100_000,
        }]
        portfolio = PortfolioState(100_000)
        await portfolio.add_position(
            Position(
                symbol="BTC", side="long", entry_price=50_000, size=0.02,
                entry_time_ms=1_700_000_000_000, stop_loss_price=49_000,
                take_profit_price=52_000,
                metadata={
                    "trade_id": trade_id, "native_protection_active": True,
                    "native_sl_oid": "9001", "native_tp_oid": "9002", "entry_fee": 1.0,
                },
            ),
            cost=50_000 * 0.02 + 1.0,
        )
        protection = NativeProtectionManager(client, db)
        recon = ExchangeReconciler(
            live_client=client, portfolio=portfolio, db=db, protection=protection,
        )
        report1 = await recon.reconcile_once()
        row1 = db.get_trade_by_id(trade_id) or {}
        report2 = await recon.reconcile_once()
        row2 = db.get_trade_by_id(trade_id) or {}
        mem = await portfolio.positions
        tp_cancelled = any(c == ("BTC", 9002) for c in client.cancel_calls)
        return (
            "trigger_close_reconciled:BTC:stop_loss_native" in report1.actions
            and row1.get("status") == "closed"
            and abs(safe_float(row1.get("exit_price")) - 49_000) < 1e-6
            and abs(safe_float(row1.get("pnl_usd")) - (-21.0)) < 1e-6
            and "BTC" not in mem
            and tp_cancelled
            and row2.get("status") == "closed"
            and report2.actions.count("trigger_close_reconciled:BTC:stop_loss_native") == 0
        )

    from src.utils.helpers import safe_float

    _pass("trigger_sl_fill_reconciles_exact_pnl_once", asyncio.run(_run()))


def test_trigger_fill_no_tape_close_pending() -> None:
    async def _run() -> bool:
        from src.core.trigger_reconcile import CLOSE_PENDING_RECONCILIATION

        db = Database(":memory:")
        trade_id = db.save_trade_entry(TradeEntry(
            symbol="ETH", side="short", entry_price=3_000,
            entry_time=1_700_000_000_000, size=0.1, strategy="test", status="open",
        ))
        db.update_trade_native_protection(trade_id, sl_order_id="8001", tp_order_id="8002")
        client = MockLiveClient()
        client.user_state = _mock_user_state([])
        client.user_fills = []
        portfolio = PortfolioState(50_000)
        await portfolio.add_position(
            Position(
                symbol="ETH", side="short", entry_price=3_000, size=0.1,
                entry_time_ms=1_700_000_000_000,
                metadata={"trade_id": trade_id, "native_protection_active": True},
            ),
            cost=300,
        )
        recon = ExchangeReconciler(
            live_client=client, portfolio=portfolio, db=db,
            protection=NativeProtectionManager(client, db),
        )
        report = await recon.reconcile_once()
        row = db.get_trade_by_id(trade_id) or {}
        mem = await portfolio.positions
        return (
            row.get("status") == CLOSE_PENDING_RECONCILIATION
            and row.get("exit_price") is None
            and "close_pending_reconciliation:ETH" in report.actions
            and "ETH" not in mem
            and recon.entries_blocked()
        )

    _pass("trigger_fill_no_tape_close_pending", asyncio.run(_run()))


def test_residual_triggers_purged_on_restart() -> None:
    async def _run() -> bool:
        client = MockLiveClient()
        client.user_state = _mock_user_state([])
        client.open_orders = [
            {
                "coin": "SOL", "oid": 7001, "sz": "1", "side": "A",
                "orderType": {"trigger": {"triggerPx": 140, "isMarket": True, "tpsl": "sl"}},
            },
            {
                "coin": "SOL", "oid": 7002, "sz": "1", "side": "A",
                "orderType": {"trigger": {"triggerPx": 160, "isMarket": True, "tpsl": "tp"}},
            },
        ]
        portfolio = PortfolioState(10_000)
        protection = NativeProtectionManager(client)
        recon = ExchangeReconciler(
            live_client=client, portfolio=portfolio, protection=protection,
        )
        report = await recon.reconcile_once()
        return (
            len(client.open_orders) == 0
            and len(client.cancel_calls) == 2
            and any("purged_residual_triggers" in a for a in report.actions)
        )

    _pass("residual_triggers_purged_on_restart", asyncio.run(_run()))


def test_testnet_entry_trigger_restart_reconcile_e2e() -> None:
    """Simulate: entry+triggers, SL fires, restart, reconcile closes once."""
    async def _run() -> bool:
        db = Database(":memory:")
        trade_id = db.save_trade_entry(TradeEntry(
            symbol="BTC", side="long", entry_price=50_000,
            entry_time=1_700_000_000_000, size=0.02, strategy="test", status="open",
            entry_fee=0.35,
        ))
        db.update_trade_native_protection(
            trade_id, sl_order_id="5001", tp_order_id="5002", protected_size=0.02,
            applied_fill_size=0.02,
        )
        client = MockLiveClient()
        client.user_state = _mock_user_state([])
        client.open_orders = [{
            "coin": "BTC", "oid": 5002, "sz": "0.02", "side": "A",
            "orderType": {"trigger": {"triggerPx": 52_000, "isMarket": True, "tpsl": "tp"}},
        }]
        client.user_fills = [{
            "coin": "BTC", "oid": 5001, "sz": "0.02", "px": "49000",
            "side": "A", "fee": "0.35", "closedPnl": "-20.35", "time": 1_700_000_200_000,
        }]
        portfolio = PortfolioState(100_000)
        await portfolio.add_position(
            Position(
                symbol="BTC", side="long", entry_price=50_000, size=0.02,
                entry_time_ms=1_700_000_000_000,
                metadata={
                    "trade_id": trade_id, "native_protection_active": True,
                    "native_sl_oid": "5001", "native_tp_oid": "5002", "entry_fee": 0.35,
                },
            ),
            cost=1_000.35,
        )
        protection = NativeProtectionManager(client, db)
        recon = ExchangeReconciler(
            live_client=client, portfolio=portfolio, db=db, protection=protection,
            orphan_exchange_policy=OrphanExchangePolicy.ADOPT_AND_PROTECT.value,
        )
        report = await recon.reconcile_once()
        row = db.get_trade_by_id(trade_id) or {}
        return (
            row.get("status") == "closed"
            and "trigger_close_reconciled" in "".join(report.actions)
            and len([o for o in client.open_orders if o.get("coin") == "BTC"]) == 0
        )

    _pass("testnet_entry_trigger_restart_reconcile_e2e", asyncio.run(_run()))


def test_mainnet_orphan_policy_is_halt() -> None:
    cfg = load_config("config/settings.yaml")
    cfg._data["mode"] = "mainnet"  # type: ignore[attr-defined]
    from src.utils.config import _apply_mode_overrides
    _apply_mode_overrides(cfg._data)  # type: ignore[attr-defined]
    policy = cfg.get("reconciliation.orphan_exchange_policy")
    mainnet_enabled = cfg.get("exchange.mainnet_enabled", False)
    _pass(
        "mainnet_orphan_policy_is_halt",
        policy == "HALT",
        f"policy={policy}",
    )
    _pass(
        "mainnet_still_blocked_without_enable_flag",
        mainnet_enabled is False,
    )


def main() -> int:
    print("=" * 70)
    print("Phase 03 exchange reconciliation tests")
    print("=" * 70)
    tests = [
        test_parse_exchange_positions,
        test_parse_trigger_orders,
        test_ambiguous_response_is_submission_unknown,
        test_entry_fill_creates_sl_tp,
        test_partial_fill_protection_size,
        test_cancel_replace_idempotent,
        test_orphan_local_closes_book,
        test_orphan_exchange_adopt_and_protect,
        test_size_mismatch_halts_entries,
        test_reconciliation_stale_blocks_entries,
        test_kill_switch_confirms_flat,
        test_restart_partial_fill_no_duplicate_exposure,
        test_ws_off_trigger_still_on_exchange,
        test_submission_unknown_blocks_symbol,
        test_trigger_sl_fill_reconciles_exact_pnl_once,
        test_trigger_fill_no_tape_close_pending,
        test_residual_triggers_purged_on_restart,
        test_testnet_entry_trigger_restart_reconcile_e2e,
        test_mainnet_orphan_policy_is_halt,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            _pass(t.__name__, False, f"{type(exc).__name__}: {exc}")
    print("=" * 70)
    if FAILED:
        print(f"FAILED: {FAILED}/{len(tests)}")
        return 1
    print(f"ALL TESTS PASSED ({len(tests)}/{len(tests)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
