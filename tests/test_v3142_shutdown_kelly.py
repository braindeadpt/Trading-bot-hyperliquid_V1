"""v3.1.42: preserve positions on shutdown, Kelly DB seed, stop restore."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.core.kelly_sizer import KellySizer
from src.core.engine import TradingEngine
from src.data.database import Database
from src.utils.config import load_config
from src.utils.helpers import resolve_trade_stop_levels


def test_resolve_trade_stop_levels_from_pct() -> None:
    sl, tp = resolve_trade_stop_levels(
        entry_price=100.0,
        side="long",
        signal_metadata={"stop_loss_pct": 0.02, "take_profit_pct": 0.04},
    )
    assert sl == 98.0
    assert tp == 104.0


def test_resolve_trade_stop_levels_from_prices() -> None:
    sl, tp = resolve_trade_stop_levels(
        entry_price=100.0,
        side="short",
        signal_metadata={"stop_loss_price": 102.0, "take_profit_price": 96.0},
    )
    assert sl == 102.0
    assert tp == 96.0


def test_stop_skips_close_when_close_on_shutdown_false() -> None:
    cfg = load_config("config/settings.yaml")
    cfg._data.setdefault("engine", {})["close_positions_on_shutdown"] = False  # type: ignore[index]

    engine = TradingEngine.__new__(TradingEngine)
    engine._running = True
    engine._shutdown_event = asyncio.Event()
    engine._config = cfg

    pos = MagicMock()
    pos.symbol = "BTC"
    pos.side = "short"
    pos.size = 0.01

    class FakePortfolio:
        @property
        def positions(self):
            async def _f():
                return {"BTC": pos}
            return _f()

        async def to_dict(self):
            return {}

        @property
        def current_capital(self):
            async def _f():
                return 10_000.0
            return _f()

    engine._portfolio = FakePortfolio()
    engine._latest_price = {"BTC": MagicMock(mid=50_000.0)}
    engine._execute_exit = AsyncMock()
    engine._executor = MagicMock()
    engine._executor.close = AsyncMock()
    engine._notify_tasks = []
    engine._subscribed_callbacks = {}
    engine._bus = MagicMock()
    engine._bus.unsubscribe = AsyncMock()
    engine._save_portfolio_snapshot = AsyncMock()

    async def _run():
        await engine.stop()
        return engine._execute_exit.await_count

    assert asyncio.run(_run()) == 0


def test_stop_closes_when_close_on_shutdown_true() -> None:
    cfg = load_config("config/settings.yaml")
    cfg._data.setdefault("engine", {})["close_positions_on_shutdown"] = True  # type: ignore[index]

    engine = TradingEngine.__new__(TradingEngine)
    engine._running = True
    engine._shutdown_event = asyncio.Event()
    engine._config = cfg

    pos = MagicMock()
    pos.symbol = "BTC"
    pos.side = "short"
    pos.size = 0.01

    class FakePortfolio:
        @property
        def positions(self):
            async def _f():
                return {"BTC": pos}
            return _f()

        async def to_dict(self):
            return {}

        @property
        def current_capital(self):
            async def _f():
                return 10_000.0
            return _f()

    engine._portfolio = FakePortfolio()
    engine._latest_price = {"BTC": MagicMock(mid=50_000.0)}
    engine._execute_exit = AsyncMock()
    engine._executor = MagicMock()
    engine._executor.close = AsyncMock()
    engine._notify_tasks = []
    engine._subscribed_callbacks = {}
    engine._bus = MagicMock()
    engine._bus.unsubscribe = AsyncMock()
    engine._save_portfolio_snapshot = AsyncMock()

    async def _run():
        await engine.stop()
        return engine._execute_exit.await_count

    assert asyncio.run(_run()) == 1


def test_kelly_seed_from_db_multiplier_not_one() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        db = Database(str(db_path))
        now = int(time.time() * 1000)
        wins = 14
        losses = 11
        for i in range(wins):
            db._conn().execute(
                """
                INSERT INTO trades (
                    symbol, side, entry_price, exit_price, entry_time, exit_time,
                    size, pnl_usd, pnl_pct, strategy, exit_reason, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "BTC", "long", 100.0, 101.0,
                    now + i, now + i + 1000,
                    0.01, 1.0, 0.02, "ChecklistMeta", "tp", "closed",
                ),
            )
        for i in range(losses):
            db._conn().execute(
                """
                INSERT INTO trades (
                    symbol, side, entry_price, exit_price, entry_time, exit_time,
                    size, pnl_usd, pnl_pct, strategy, exit_reason, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "BTC", "long", 100.0, 99.0,
                    now + 100 + i, now + 101 + i + 1000,
                    0.01, -0.5, -0.01, "ChecklistMeta", "sl", "closed",
                ),
            )
        db._conn().commit()

        pnl_pcts = db.get_recent_closed_trade_pnl_pcts(limit=50)
        assert len(pnl_pcts) == 25

        kelly = KellySizer(min_trades=20, half_kelly=True, lookback_trades=50)
        n = kelly.seed_history(pnl_pcts)
        assert n == 25
        mult = kelly.get_size_multiplier()
        assert mult != 1.0
        db.close()


def test_paper_config_preserves_on_shutdown() -> None:
    cfg = load_config("config/settings.yaml")
    assert cfg.get("engine.close_positions_on_shutdown") is False


if __name__ == "__main__":
    test_resolve_trade_stop_levels_from_pct()
    test_resolve_trade_stop_levels_from_prices()
    test_stop_skips_close_when_close_on_shutdown_false()
    test_stop_closes_when_close_on_shutdown_true()
    test_kelly_seed_from_db_multiplier_not_one()
    test_paper_config_preserves_on_shutdown()
    print("All v3.1.42 tests passed.")
