"""Tests for the v3.1.22 mainnet readiness fixes 5+6.

  * ``execution.flatten_on_stop=False`` preserves positions
    across restart instead of force-closing at the last tick.
  * The WS health monitoring task is actually started in
    ``TradingEngine.start()`` (it was a no-op before).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

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


# ── config wiring ────────────────────────────────────────────────


def test_settings_yaml_paper_preserves_on_shutdown() -> None:
    cfg = load_config("config/settings.yaml")
    _pass("settings_yaml_paper_preserves_on_shutdown",
          cfg.get("engine.close_positions_on_shutdown") is False)
    _pass("settings_yaml_flatten_on_stop_false_paper",
          cfg.get("execution.flatten_on_stop") is False)


def test_settings_yaml_mainnet_override_closes_on_shutdown() -> None:
    cfg = load_config("config/settings.yaml")
    cfg._data["mode"] = "mainnet"  # type: ignore[attr-defined]
    from src.utils.config import _apply_mode_overrides
    _apply_mode_overrides(cfg._data)
    _pass("settings_yaml_mainnet_close_on_shutdown",
          cfg.get("engine.close_positions_on_shutdown") is True)
    _pass("settings_yaml_mainnet_flatten_on_stop",
          cfg.get("execution.flatten_on_stop") is True)


def test_settings_yaml_testnet_override_preserves() -> None:
    cfg = load_config("config/settings.yaml")
    cfg._data["mode"] = "testnet"  # type: ignore[attr-defined]
    from src.utils.config import _apply_mode_overrides
    _apply_mode_overrides(cfg._data)
    _pass("settings_yaml_testnet_preserves_on_shutdown",
          cfg.get("engine.close_positions_on_shutdown") is False)
    _pass("settings_yaml_testnet_flatten_on_stop_false",
          cfg.get("execution.flatten_on_stop") is False)


# ── WS health task is started in start() ─────────────────────────


def _build_paper_engine(cfg, *, with_ws: bool) -> "TradingEngine":
    """Build a fully-stubbed engine for start() unit tests."""
    from src.core.engine import TradingEngine

    engine = TradingEngine.__new__(TradingEngine)
    engine._running = False
    engine._shutdown_event = None
    engine._symbols = []
    engine._strategies = []
    engine._config = cfg
    engine._db = MagicMock()
    engine._bus = MagicMock()
    engine._bus.subscribe = AsyncMock()
    engine._hl_ws_client = MagicMock() if with_ws else None
    if with_ws:
        engine._hl_ws_client.is_healthy = True
        engine._hl_ws_client.subscribe = AsyncMock()

    class FakePortfolio:
        @property
        def positions(self):
            async def _f():
                return {}
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

    engine._portfolio = FakePortfolio()
    engine._executor = MagicMock()
    engine._executor.open = AsyncMock()
    engine._funding_aggregator = MagicMock()
    engine._notifier = None
    engine._ws_health_check_task = None
    engine._funding_poll_task = None
    engine._summary_task = None
    engine._reconcile_task = None
    engine._subscribed_callbacks = {}
    engine._latest_price = {}
    engine._mode = "paper"
    engine._notify_tasks = []
    engine._ws_health_warned = False
    engine._ws_disconnect_start = None
    engine._last_ws_healthy_time = None
    engine._risk = MagicMock()
    engine._risk.reset_circuit_breaker = MagicMock()
    engine._risk.check_drawdown = MagicMock()
    engine._risk.is_circuit_breaker_tripped = MagicMock(return_value=False)
    engine._kelly_sizer = MagicMock()
    engine._kelly_sizer.seed_history = MagicMock(return_value=0)
    engine._kelly_sizer.get_size_multiplier = MagicMock(return_value=1.0)
    engine._kelly_enabled = False
    engine._reconciliation_enabled = False
    engine._reconciler = None

    # Stub the recovery / background loops
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


def test_start_creates_ws_health_task() -> None:
    """When start() is called with a HL WS client, the WS health
    monitoring task must be created (it was a no-op before v3.1.22)."""
    cfg = load_config("config/settings.yaml")
    cfg._data["mode"] = "paper"  # type: ignore[attr-defined]
    engine = _build_paper_engine(cfg, with_ws=True)

    async def _start_and_stop():
        await engine.start()
        started = engine._ws_health_check_task is not None
        # Cancel the WS health loop we just spawned so the test exits
        if engine._ws_health_check_task is not None:
            engine._ws_health_check_task.cancel()
            try:
                await engine._ws_health_check_task
            except (asyncio.CancelledError, Exception):
                pass
        return started

    started = asyncio.run(_start_and_stop())
    _pass("start_creates_ws_health_task", started)


def test_start_skips_ws_health_task_without_ws_client() -> None:
    """If no HL WS client is wired, skip the health task."""
    cfg = load_config("config/settings.yaml")
    engine = _build_paper_engine(cfg, with_ws=False)

    async def _start_and_check():
        await engine.start()
        result = engine._ws_health_check_task
        if result is not None:
            result.cancel()
            try:
                await result
            except (asyncio.CancelledError, Exception):
                pass
        return result

    task = asyncio.run(_start_and_check())
    _pass("start_skips_ws_health_task_without_ws_client", task is None)


# ── stop() respects flatten_on_stop ─────────────────────────────


def test_stop_skips_close_when_flatten_false() -> None:
    """When flatten_on_stop=False, stop() must NOT call
    _execute_exit on the open positions."""
    from src.core.engine import TradingEngine

    cfg = load_config("config/settings.yaml")
    cfg._data["execution"]["flatten_on_stop"] = False  # type: ignore[index]
    cfg._data.setdefault("engine", {})["close_positions_on_shutdown"] = False  # type: ignore[index]

    engine = TradingEngine.__new__(TradingEngine)
    engine._running = True
    engine._shutdown_event = asyncio.Event()
    engine._config = cfg
    engine._mode = "paper"
    engine._db = MagicMock()
    engine._cooldown_state = {}
    engine._risk = MagicMock()
    engine._risk.snapshot_for_persist = MagicMock(return_value={})
    engine._funding_poll_task = None
    engine._ws_health_check_task = None
    engine._summary_task = None
    engine._reconcile_task = None

    # Two open positions
    pos_btc = MagicMock()
    pos_btc.symbol = "BTC"
    pos_btc.side = "long"
    pos_btc.size = 0.01
    pos_eth = MagicMock()
    pos_eth.symbol = "ETH"
    pos_eth.side = "short"
    pos_eth.size = 0.5

    class FakePortfolio:
        @property
        def positions(self):
            async def _f():
                return {"BTC": pos_btc, "ETH": pos_eth}
            return _f()

        async def to_dict(self):
            return {}

        @property
        def current_capital(self):
            async def _f():
                return 10_000.0
            return _f()

    engine._portfolio = FakePortfolio()
    engine._latest_price = {"BTC": MagicMock(mid=50_000.0),
                            "ETH": MagicMock(mid=3_000.0)}
    engine._execute_exit = AsyncMock()
    engine._executor = MagicMock()
    engine._executor.close = AsyncMock()
    engine._notify_tasks = []
    engine._subscribed_callbacks = {}
    engine._bus = MagicMock()
    engine._bus.unsubscribe = AsyncMock()

    async def _run():
        await engine.stop()
        return engine._execute_exit.await_count

    calls = asyncio.run(_run())
    _pass("stop_skips_close_when_flatten_false",
          calls == 0,
          f"_execute_exit was called {calls} times, expected 0")


def test_stop_closes_when_flatten_true() -> None:
    """When close_positions_on_shutdown=True, stop() closes all positions."""
    from src.core.engine import TradingEngine

    cfg = load_config("config/settings.yaml")
    cfg._data.setdefault("engine", {})["close_positions_on_shutdown"] = True  # type: ignore[index]

    engine = TradingEngine.__new__(TradingEngine)
    engine._running = True
    engine._shutdown_event = asyncio.Event()
    engine._config = cfg
    engine._mode = "paper"
    engine._db = MagicMock()
    engine._cooldown_state = {}
    engine._risk = MagicMock()
    engine._risk.snapshot_for_persist = MagicMock(return_value={})
    engine._funding_poll_task = None
    engine._ws_health_check_task = None
    engine._summary_task = None
    engine._reconcile_task = None

    pos_btc = MagicMock()
    pos_btc.symbol = "BTC"
    pos_btc.side = "long"
    pos_btc.size = 0.01

    class FakePortfolio:
        @property
        def positions(self):
            async def _f():
                return {"BTC": pos_btc}
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

    async def _run():
        await engine.stop()
        return engine._execute_exit.await_count

    calls = asyncio.run(_run())
    _pass("stop_closes_when_flatten_true",
          calls == 1,
          f"_execute_exit was called {calls} times, expected 1")


def main() -> int:
    print("=" * 70)
    print("Mainnet readiness v3.1.22 5-6 (safe shutdown + WS health)")
    print("=" * 70)
    tests = [
        test_settings_yaml_paper_preserves_on_shutdown,
        test_settings_yaml_mainnet_override_closes_on_shutdown,
        test_settings_yaml_testnet_override_preserves,
        test_start_creates_ws_health_task,
        test_start_skips_ws_health_task_without_ws_client,
        test_stop_skips_close_when_flatten_false,
        test_stop_closes_when_flatten_true,
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
