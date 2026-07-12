"""Phase 01 — live execution fail-closed behavioural tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.execution import ExecutionEngine, LiveExecutionError
from src.core.order_lifecycle import AmbiguousOrderResponse
from src.data.database import Database
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


def _filled_hl_response(oid: int = 42) -> dict:
    return {
        "status": "ok",
        "response": {
            "data": {
                "statuses": [
                    {"filled": {"oid": oid, "totalSz": "0.01", "avgPx": "50000"}}
                ]
            }
        },
    }


def _rejected_hl_response(msg: str = "insufficient_margin") -> dict:
    return {"status": "err", "response": msg}


class FakePortfolio:
  @property
  def current_capital(self):
      async def _f():
          return 10_000.0
      return _f()


def _make_live_engine(db: Database) -> ExecutionEngine:
    cfg = load_config("config/settings.yaml")
    engine = ExecutionEngine(cfg, db, mode="testnet")
    engine._rest_client = MagicMock()
    engine._live_client = AsyncMock()
    engine._live_signing_ready = True
    return engine


def _entry_signal() -> Signal:
    return Signal(
        symbol="BTC",
        side="long",
        entry_price=50_000.0,
        size_pct=0.05,
        strategy="test",
        confidence=0.8,
        metadata={"calculated_size": 0.01, "order_type": "market"},
        reason="phase01_test",
    )


def test_missing_signing_raises_without_db_row() -> None:
    db = Database(":memory:")
    engine = _make_live_engine(db)
    engine._live_signing_ready = False
    engine._live_client = None

    raised = False
    try:
        asyncio.run(engine.enter_position(_entry_signal(), FakePortfolio()))
    except LiveExecutionError as exc:
        raised = "signing_not_configured" in str(exc)
    open_rows = db.get_open_trades()
    _pass(
        "missing_signing_raises_without_db_row",
        raised and len(open_rows) == 0 and "BTC" not in engine._open_trades,
        f"raised={raised} open_rows={len(open_rows)}",
    )


def test_submit_reject_raises_without_db_row() -> None:
    db = Database(":memory:")
    engine = _make_live_engine(db)
    engine._live_client.place_entry = AsyncMock(
        return_value=_rejected_hl_response()
    )

    raised = False
    try:
        asyncio.run(engine.enter_position(_entry_signal(), FakePortfolio()))
    except LiveExecutionError:
        raised = True
    _pass(
        "submit_reject_raises_without_db_row",
        raised and len(db.get_open_trades()) == 0,
    )


def test_successful_live_entry_persists_open_row() -> None:
    db = Database(":memory:")
    engine = _make_live_engine(db)
    engine._live_client.place_entry = AsyncMock(return_value=_filled_hl_response(99))

    result = asyncio.run(engine.enter_position(_entry_signal(), FakePortfolio()))
    rows = db.get_open_trades()
    _pass(
        "successful_live_entry_persists_open_row",
        result.status == "open"
        and len(rows) == 1
        and rows[0]["status"] == "open"
        and result.exchange_order_id == "99",
        f"trade_id={result.trade_id}",
    )


def test_close_exception_keeps_db_open() -> None:
    db = Database(":memory:")
    engine = _make_live_engine(db)
    engine._live_client.place_entry = AsyncMock(return_value=_filled_hl_response(7))
    signal = _entry_signal()
    result = asyncio.run(engine.enter_position(signal, FakePortfolio()))

    engine._live_client.close_position = AsyncMock(side_effect=RuntimeError("network_down"))
    position = Position(
        symbol="BTC",
        side="long",
        entry_price=result.entry_price,
        size=result.size,
        entry_time_ms=result.timestamp_ms,
        stop_loss_price=49_000.0,
        take_profit_price=52_000.0,
        unrealized_pnl=0.0,
        current_price=result.entry_price,
        metadata={"trade_id": result.trade_id},
    )

    kept_open = False
    try:
        asyncio.run(engine.close_position(position, 50_100.0, "test_exit"))
    except LiveExecutionError:
        kept_open = True

    row = db.get_open_trades()[0]
    _pass(
        "close_exception_keeps_db_open",
        kept_open
        and row["status"] == "open"
        and "BTC" in engine._open_trades,
        f"status={row['status']}",
    )


def test_ambiguous_response_blocks_symbol() -> None:
    db = Database(":memory:")
    engine = _make_live_engine(db)
    engine._live_client.place_entry = AsyncMock(return_value={})

    result = asyncio.run(engine.enter_position(_entry_signal(), FakePortfolio()))

    _pass(
        "ambiguous_response_blocks_symbol",
        engine.is_symbol_blocked("BTC")
        and len(db.get_open_trades()) == 0
        and result.status == "pending",
    )


def test_duplicate_client_order_id_rejected() -> None:
    db = Database(":memory:")
    engine = _make_live_engine(db)
    engine._live_client.place_entry = AsyncMock(return_value=_filled_hl_response(1))

    sig = _entry_signal()
    fixed_ms = 1_700_000_000_000
    client_id = ExecutionEngine._make_client_order_id(sig.symbol, sig.side, fixed_ms)
    engine._submitted_client_order_ids[client_id] = 99

    with patch("src.core.execution.utc_timestamp_ms", return_value=fixed_ms):
        second = asyncio.run(engine.enter_position(sig, FakePortfolio()))
    _pass(
        "duplicate_client_order_id_rejected",
        second.status == "rejected" and second.reason == "duplicate_client_order_id",
        f"reason={second.reason}",
    )


def test_position_size_clamp_uses_config_not_hardcoded_20pct() -> None:
    """5% × 10x leverage on $10k → max notional $5k, not $2k (20% cap)."""
    db = Database(":memory:")
    cfg = load_config("config/settings.yaml")
    engine = ExecutionEngine(cfg, db, mode="paper")

    class BigPortfolio:
        @property
        def current_capital(self):
            async def _f():
                return 10_000.0
            return _f()

    sig = Signal(
        symbol="BTC",
        side="long",
        entry_price=1_000.0,
        size_pct=1.0,
        strategy="test",
        confidence=0.8,
        metadata={"calculated_size": 10.0, "order_type": "market"},
        reason="clamp_test",
    )
    result = asyncio.run(engine.enter_position(sig, BigPortfolio()))
    notional = result.entry_price * result.size
    max_allowed = 10_000.0 * engine._max_position_size_pct * engine._leverage_max
    _pass(
        "position_size_clamp_uses_config_not_hardcoded_20pct",
        abs(notional - max_allowed) < 1.0,
        f"notional={notional:.2f} max={max_allowed:.2f}",
    )


def test_resting_order_does_not_open_local_position() -> None:
    db = Database(":memory:")
    engine = _make_live_engine(db)
    resting = {
        "status": "ok",
        "response": {"data": {"statuses": [{"resting": {"oid": 55}}]}},
    }
    engine._live_client.place_entry = AsyncMock(return_value=resting)

    result = asyncio.run(engine.enter_position(_entry_signal(), FakePortfolio()))
    rows = db.get_open_trades()
    _pass(
        "resting_order_does_not_open_local_position",
        result.status == "pending"
        and "BTC" not in engine._open_trades
        and len(rows) == 0,
        f"result_status={result.status} open_rows={len(rows)}",
    )


def test_close_missing_signing_raises_and_keeps_open() -> None:
    db = Database(":memory:")
    engine = _make_live_engine(db)
    engine._live_client.place_entry = AsyncMock(return_value=_filled_hl_response(3))
    result = asyncio.run(engine.enter_position(_entry_signal(), FakePortfolio()))
    engine._live_signing_ready = False
    engine._live_client = None

    position = Position(
        symbol="BTC",
        side="long",
        entry_price=result.entry_price,
        size=result.size,
        entry_time_ms=result.timestamp_ms,
        stop_loss_price=49_000.0,
        take_profit_price=52_000.0,
        unrealized_pnl=0.0,
        current_price=result.entry_price,
        metadata={"trade_id": result.trade_id},
    )

    raised = False
    try:
        asyncio.run(engine.close_position(position, 50_000.0, "test"))
    except LiveExecutionError as exc:
        raised = "signing_not_configured" in str(exc)

    _pass(
        "close_missing_signing_raises_and_keeps_open",
        raised and db.get_open_trades()[0]["status"] == "open",
    )


def main() -> int:
    print("=" * 70)
    print("Phase 01 — execution fail-closed tests")
    print("=" * 70)
    tests = [
        test_missing_signing_raises_without_db_row,
        test_submit_reject_raises_without_db_row,
        test_successful_live_entry_persists_open_row,
        test_close_exception_keeps_db_open,
        test_ambiguous_response_blocks_symbol,
        test_duplicate_client_order_id_rejected,
        test_position_size_clamp_uses_config_not_hardcoded_20pct,
        test_resting_order_does_not_open_local_position,
        test_close_missing_signing_raises_and_keeps_open,
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
