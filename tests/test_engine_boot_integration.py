"""Integration-offline: TradingEngine boot/shutdown with feeds, OMS, and
reconciliation fully mocked.

Exercises start() -> feed subscription + OMS poller start + startup
reconciliation, then stop() -> graceful shutdown (background task
cancellation, unsubscribe, OMS stop, snapshot persistence), all against
a fully-stubbed engine (no real network, no real exchange).

Companion to test_mainnet_readiness_5_6.py, which covers start()/stop()
in isolation; this test wires feeds + OMS + reconciliation together in
one boot/shutdown cycle.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_config

pytestmark = pytest.mark.integration_offline


class FakePortfolio:
    def __init__(self):
        self._positions = {}

    @property
    def positions(self):
        async def _f():
            return dict(self._positions)
        return _f()

    async def reconcile_peaks(self):
        return None

    async def get_max_drawdown(self):
        return 0.0

    @property
    def current_capital(self):
        async def _f():
            return 10_000.0
        return _f()

    async def to_dict(self):
        return {
            "current_capital": 10_000.0,
            "peak_capital": 10_000.0,
            "daily_pnl": 0.0,
            "positions": {},
        }


def _build_testnet_engine(cfg):
    """Build a fully-stubbed testnet engine wiring feeds + OMS + reconciliation mocks."""
    from src.core.engine import TradingEngine

    engine = TradingEngine.__new__(TradingEngine)
    engine._running = False
    engine._shutdown_event = None
    engine._start_time = None
    engine._symbols = ["BTC"]
    engine._strategies = []
    engine._config = cfg
    engine._db = MagicMock()
    engine._cooldown_state = {}

    engine._bus = MagicMock()
    engine._bus.subscribe = AsyncMock()
    engine._bus.unsubscribe = AsyncMock()

    engine._hl_ws_client = None
    engine._portfolio = FakePortfolio()

    engine._executor = MagicMock()
    engine._executor.open = AsyncMock()
    engine._executor.close = AsyncMock()
    engine._executor.start_oms_loop = AsyncMock()
    engine._executor.stop_oms_loop = AsyncMock()
    engine._executor.set_oms_alert_callback = MagicMock()
    engine._executor.register_order_callback = MagicMock()

    # Reconciliation is pre-wired (skip the live _init_live_reconciliation path)
    engine._reconciliation_enabled = False
    engine._reconciliation_interval_sec = 60.0
    report = MagicMock(success=True, exchange_positions=[], local_symbols=[], actions=[])
    engine._reconciler = MagicMock()
    engine._reconciler.reconcile_once = AsyncMock(return_value=report)

    engine._funding_aggregator = MagicMock()
    engine._notifier = None
    engine._notify_tasks = []
    engine._latest_price = {}
    engine._mode = "testnet"
    engine._subscribed_callbacks = {}

    engine._ws_health_warned = False
    engine._ws_disconnect_start = None
    engine._last_ws_healthy_time = None
    engine._ws_health_check_task = None
    engine._funding_poll_task = None
    engine._summary_task = None
    engine._reconcile_task = None

    engine._risk = MagicMock()
    engine._risk.reset_circuit_breaker = MagicMock()
    engine._risk.check_drawdown = MagicMock()
    engine._risk.is_circuit_breaker_tripped = MagicMock(return_value=False)
    engine._risk.is_daily_drawdown_circuit_tripped = MagicMock(return_value=False)
    engine._risk.daily_stop_loss_count = 0
    engine._risk.snapshot_for_persist = MagicMock(return_value={})

    engine._kelly_enabled = False
    engine._kelly_sizer = MagicMock()

    async def _recover_state():
        return None
    engine._recover_state = _recover_state

    async def _no_loop():
        while True:
            await asyncio.sleep(1)
    engine._poll_funding_loop = _no_loop
    engine._periodic_summary_loop = _no_loop
    engine._ws_health_loop = _no_loop
    engine._reconcile_loop = _no_loop

    return engine


async def _cancel_task(task):
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def test_boot_wires_feeds_oms_and_reconciliation_then_shuts_down_cleanly():
    """One full boot/shutdown cycle: feeds subscribed, OMS started,
    startup reconciliation run, then a clean graceful stop."""
    cfg = load_config("config/settings.yaml")
    engine = _build_testnet_engine(cfg)

    async def _run():
        await engine.start()

        # Feeds: per-symbol topic subscriptions were made on the DataBus
        subscribed_topics = {call.args[0] for call in engine._bus.subscribe.await_args_list}
        assert any(t.startswith("price:BTC") for t in subscribed_topics)
        assert any(t.startswith("orderbook:BTC") for t in subscribed_topics)
        assert engine._subscribed_callbacks, "expected callbacks to be tracked for unsubscribe on stop"

        # OMS: poller started via the executor
        engine._executor.start_oms_loop.assert_awaited_once()

        # Reconciliation: startup reconciliation ran against the mocked reconciler
        engine._reconciler.reconcile_once.assert_awaited_once()

        # Graceful shutdown
        await engine.stop()

        assert engine._running is False
        engine._executor.stop_oms_loop.assert_awaited_once()
        engine._executor.close.assert_awaited_once()
        assert engine._subscribed_callbacks == {}, "unsubscribe should clear tracked callbacks"

        # Background tasks spawned during start() must be cancelled/finished by stop()
        for task_name in ("_funding_poll_task", "_summary_task"):
            task = getattr(engine, task_name)
            assert task is None or task.done()

    asyncio.run(_run())


def test_boot_skips_reconciliation_when_reconciler_not_wired():
    """If reconciliation never got wired (e.g. signing not ready), start()
    must not crash and must simply skip the reconcile step."""
    cfg = load_config("config/settings.yaml")
    engine = _build_testnet_engine(cfg)
    engine._reconciler = None

    async def _run():
        await engine.start()
        await engine.stop()
        assert engine._running is False

    asyncio.run(_run())
