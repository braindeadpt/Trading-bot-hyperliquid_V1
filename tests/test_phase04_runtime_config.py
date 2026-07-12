"""Phase 04: runtime config, governance, persistence, and graceful shutdown."""

from __future__ import annotations

import asyncio
import signal
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.core.engine import TradingEngine
from src.core.risk_manager import RiskManager
from src.core.runtime_state import persist_runtime_state, rebuild_cooldown_state, restore_runtime_state
from src.data.database import Database
from src.exchanges.hyperliquid_ws import DataBus
from src.strategies.base import MarketEvent, Signal
from src.utils.config import Config, ConfigError, get_strategy_section, get_trading_symbols, load_config
from src.utils.helpers import utc_timestamp_ms
import pytest

pytestmark = pytest.mark.integration_offline


def _minimal_engine_config(extra: dict | None = None) -> Config:
    data = {
        "symbols": ["BTC"],
        "strategy": {
            "kelly": {
                "enabled": True,
                "min_trades": 5,
                "lookback_trades": 20,
            },
            "cooldown": {
                "base_minutes": 30,
                "max_minutes": 120,
                "multiplier": 2.0,
            },
            "portfolio_governance": {
                "daily_drawdown_circuit_pct": 3,
                "daily_drawdown_halt_entries": True,
                "daily_drawdown_flatten": True,
                "daily_drawdown_alert": False,
            },
        },
        "risk": {
            "max_positions": 3,
            "max_daily_stop_losses": 4,
            "max_position_size_pct": 5.0,
            "leverage_max": 5.0,
            "funding_blackout": {"enabled": False},
            "chase_filter": {"enabled": False},
            "volatility_circuit_breaker": {"enabled": False},
        },
        "market_data": {
            "block_entries_on_stale": True,
            "block_entries_on_ws_unhealthy": True,
        },
        "engine": {"close_positions_on_shutdown": False},
        "execution": {"flatten_on_stop": False},
    }
    if extra:
        for key, val in extra.items():
            data[key] = val
    return Config(data)


def _make_engine(cfg: Config | None = None) -> TradingEngine:
    cfg = cfg or _minimal_engine_config()
    db = Database(":memory:")
    bus = DataBus()
    risk = RiskManager(cfg, db)
    from src.core.execution import ExecutionEngine

    executor = ExecutionEngine(cfg, db, "paper")
    return TradingEngine(cfg, db, bus, [], risk, executor)


def test_effective_config_mode_overrides() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = """
assets: [BTC, ETH]
risk:
  leverage_max: 10
  max_daily_loss_pct: 3.0
mode_overrides:
  mainnet:
    risk:
      leverage_max: 5
      max_daily_loss_pct: 2.0
  testnet:
    risk:
      max_daily_trades: 50
exchange: {}
backtest: {}
logging: {}
"""
        paper_path = Path(tmp) / "paper.yaml"
        paper_path.write_text("mode: paper\n" + base, encoding="utf-8")
        paper = load_config(str(paper_path))
        assert float(paper.get("risk.leverage_max")) == 10.0

        mainnet_path = Path(tmp) / "mainnet.yaml"
        mainnet_path.write_text("mode: mainnet\n" + base, encoding="utf-8")
        mainnet = load_config(str(mainnet_path))
        assert float(mainnet.get("risk.leverage_max")) == 5.0
        assert float(mainnet.get("risk.max_daily_loss_pct")) == 2.0

        testnet_path = Path(tmp) / "testnet.yaml"
        testnet_path.write_text("mode: testnet\n" + base, encoding="utf-8")
        testnet = load_config(str(testnet_path))
        assert int(testnet.get("risk.max_daily_trades")) == 50


def test_trading_symbols_unified_from_assets() -> None:
    cfg = Config({"assets": ["SOL", "BTC"], "symbols": ["OLD"]})
    from src.utils.config import _normalize_trading_symbols

    _normalize_trading_symbols(cfg.raw)
    assert get_trading_symbols(cfg) == ["SOL", "BTC"]
    assert cfg.get("symbols") == ["SOL", "BTC"]


def test_unknown_config_key_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "settings.yaml"
        cfg_path.write_text(
            """
mode: paper
assets: [BTC]
not_a_real_key: true
exchange: {}
risk: {}
backtest: {}
logging: {}
""",
            encoding="utf-8",
        )
        try:
            load_config(str(cfg_path))
            raise AssertionError("expected ConfigError")
        except ConfigError as exc:
            assert "not_a_real_key" in str(exc)


def test_live_cooldown_uses_strategy_paths() -> None:
    engine = _make_engine()
    assert engine._cooldown_base_ms == 30 * 60_000
    assert engine._cooldown_max_ms == 120 * 60_000


def _mark_feeds_healthy(engine: TradingEngine) -> None:
    engine._feed_health_evaluated = True
    engine._feed_health_ready = True
    engine._market_data_health_summary.overall = "green"


def test_kelly_disabled_skips_multiplier() -> None:
    cfg = _minimal_engine_config()
    cfg._data["strategy"]["kelly"]["enabled"] = False  # type: ignore[index]
    engine = _make_engine(cfg)
    assert engine._kelly_enabled is False
    captured: dict = {}

    async def _run() -> None:
        from src.core.portfolio import PortfolioState

        base_signal = Signal(
            strategy="VolatilityBreakout",
            symbol="BTC",
            side="long",
            confidence=0.8,
            size_pct=0.01,
            entry_price=50_000.0,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            reason="test",
        )
        event = MarketEvent(symbol="BTC", price=50_000.0, timestamp_ms=utc_timestamp_ms())
        engine._portfolio = PortfolioState(10_000.0)
        _mark_feeds_healthy(engine)
        engine._db.save_signal = MagicMock()
        engine._persist_decision = MagicMock()
        engine._latest_orderbook_raw = {"BTC": MagicMock()}

        def _can_enter(sig, _portfolio):
            captured["size_pct"] = sig.size_pct
            return False, "blocked_for_test"

        engine._risk.can_enter = _can_enter
        await engine._process_entry_signal(base_signal, event)

    asyncio.run(_run())
    assert captured.get("size_pct") == 0.01


def test_governor_blocks_direct_strategy_with_audit_reason() -> None:
    engine = _make_engine()
    engine._strategy_governor._disabled.add("VolatilityBreakout")
    sig = Signal(
        strategy="VolatilityBreakout",
        symbol="BTC",
        side="long",
        confidence=0.9,
        size_pct=0.01,
        reason="test",
    )
    reason = engine._governor_blocks_signal(sig)
    assert reason == "governor_disabled:VolatilityBreakout"


def test_restart_preserves_stop_streak_and_cooldown() -> None:
    from src.core.runtime_state import _utc_midnight_ms

    db = Database(":memory:")
    midnight = _utc_midnight_ms()
    for i in range(4):
        exit_ms = midnight + (i + 1) * 60_000
        db._conn().execute(
            """
            INSERT INTO trades (
                symbol, side, entry_price, exit_price, entry_time, exit_time,
                size, pnl_usd, pnl_pct, strategy, exit_reason, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "BTC",
                "long",
                100.0,
                99.0,
                exit_ms - 30_000,
                exit_ms,
                1.0,
                -10.0,
                -0.01,
                "VolatilityBreakout",
                "stop_loss_hit",
                "closed",
            ),
        )
    db._commit()

    cooldown = rebuild_cooldown_state(
        db,
        base_ms=30 * 60_000,
        max_ms=120 * 60_000,
        multiplier=2.0,
    )
    key = "VolatilityBreakout:BTC"
    assert key in cooldown
    assert cooldown[key]["consecutive_losses"] == 4
    assert cooldown[key]["duration_ms"] == 120 * 60_000

    risk = RiskManager(_minimal_engine_config(), db)
    restored = restore_runtime_state(
        db,
        risk,
        base_ms=30 * 60_000,
        max_ms=120 * 60_000,
        multiplier=2.0,
        portfolio_daily_peak=10_000.0,
        portfolio_capital=9_500.0,
    )
    assert restored[key]["duration_ms"] == 120 * 60_000
    assert risk.daily_stop_loss_count == 4
    assert risk._daily_stop_streak_tripped is True


def test_cold_start_blocks_entries_until_healthy_feed() -> None:
    engine = _make_engine()
    assert engine._entry_feed_block_reason("BTC") == "feed_health_pending"

    engine._feed_health_evaluated = True
    engine._feed_health_ready = False
    assert engine._entry_feed_block_reason("BTC") == "feed_health_not_ready"

    engine._feed_health_ready = True
    engine._market_data_health_summary.overall = "green"
    assert engine._entry_feed_block_reason("BTC") is None


def test_stale_feed_blocks_entry_but_not_exit_path() -> None:
    engine = _make_engine()
    engine._hl_ws_client = MagicMock()
    engine._hl_ws_client.is_healthy = False
    reason = engine._entry_feed_block_reason("BTC")
    assert reason == "ws_unhealthy"

    engine._hl_ws_client.is_healthy = True
    engine._feed_health_evaluated = True
    engine._feed_health_ready = True
    from src.data.market_data_health import SymbolFeedHealth

    engine._market_data_health["BTC"] = SymbolFeedHealth(
        symbol="BTC",
        cex_ok=False,
        cex_stale=True,
        cex_age_sec=999.0,
        cex_exchanges=[],
        cex_exchange_count=0,
        hl_predicted_ok=False,
        hl_predicted_stale=True,
        hl_predicted_age_sec=999.0,
        hl_venues=[],
        status="red",
        polls_1h=0,
        failed_polls_1h=0,
        failure_rate_1h=1.0,
    )
    engine._market_data_health_summary.overall = "red"
    assert engine._entry_feed_block_reason("BTC") == "feed_red:BTC"


def test_daily_dd_flatten_once_per_trip() -> None:
    risk = RiskManager(_minimal_engine_config(), None)
    today = __import__("src.utils.helpers", fromlist=["utc_now"]).utc_now().strftime("%Y-%m-%d")
    risk._daily_drawdown_circuit_tripped = True
    risk._daily_drawdown_circuit_date = today

    assert risk.request_daily_dd_flatten() is True
    assert risk.request_daily_dd_flatten() is False
    assert risk.should_flatten_on_daily_drawdown is False


def test_daily_dd_flatten_resets_on_new_day() -> None:
    risk = RiskManager(_minimal_engine_config(), None)
    risk._daily_drawdown_circuit_tripped = True
    risk._daily_drawdown_circuit_date = "2000-01-01"
    risk._daily_dd_flatten_consumed = True
    assert risk.is_daily_drawdown_circuit_tripped() is False
    assert risk._daily_dd_flatten_consumed is False


def test_symbol_risk_multiplier_map_from_config() -> None:
    cfg = _minimal_engine_config({
        "risk": {
            "max_positions": 3,
            "max_daily_stop_losses": 4,
            "max_position_size_pct": 5.0,
            "leverage_max": 5.0,
            "symbol_risk_multiplier": {"SOL": 0.5, "BTC": 1.0},
        },
    })
    engine = _make_engine(cfg)
    assert engine._symbol_risk_multipliers["SOL"] == 0.5
    assert engine._symbol_risk_multipliers["BTC"] == 1.0
    assert engine._symbol_risk_multipliers.get("ETH", 1.0) == 1.0


def test_sigterm_stops_engine_and_cancels_tasks_within_timeout() -> None:
    async def _slow_loop() -> None:
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    async def _run() -> tuple[bool, float]:
        from src.core.portfolio import PortfolioState

        engine = _make_engine()
        engine._running = True
        engine._shutdown_event = asyncio.Event()
        engine._subscribed_callbacks = {}
        engine._portfolio = PortfolioState(10_000.0)
        engine._executor.close = AsyncMock()
        engine._funding_poll_task = asyncio.create_task(_slow_loop())
        engine._summary_task = asyncio.create_task(_slow_loop())
        engine._ws_health_check_task = asyncio.create_task(_slow_loop())

        started = time.monotonic()
        await asyncio.wait_for(engine.stop(), timeout=2.0)
        elapsed = time.monotonic() - started
        tasks_done = all(
            t.done() for t in (
                engine._funding_poll_task,
                engine._summary_task,
                engine._ws_health_check_task,
            )
        )
        return tasks_done and engine._executor.close.called, elapsed

    ok, elapsed = asyncio.run(_run())
    assert ok, "stop() must cancel background tasks and close executor"
    assert elapsed < 2.0, f"stop() took too long: {elapsed:.2f}s"


def test_sigterm_handler_sets_shutdown_flag() -> None:
    import main as main_mod

    main_mod._shutdown_requested = False
    main_mod._request_shutdown(signal.SIGTERM, None)
    assert main_mod._shutdown_requested is True


def test_restore_runs_exactly_once_in_start() -> None:
    async def _run() -> int:
        from src.core.portfolio import PortfolioState

        engine = _make_engine()
        calls = {"n": 0}

        async def _count_recover() -> None:
            calls["n"] += 1

        engine._recover_state = _count_recover
        engine._portfolio = PortfolioState(10_000.0)
        engine._executor.open = AsyncMock()
        engine._bus.subscribe = AsyncMock()
        engine._seed_kelly_from_db = MagicMock()
        await engine.start()
        return calls["n"]

    assert asyncio.run(_run()) == 1


def test_sigterm_triggers_engine_stop_cleanup() -> None:
    async def _run() -> bool:
        from src.core.portfolio import PortfolioState

        engine = _make_engine()
        engine._running = True
        engine._shutdown_event = asyncio.Event()
        engine._subscribed_callbacks = {}
        engine._portfolio = PortfolioState(10_000.0)
        engine._executor.close = AsyncMock()
        engine._persist_runtime_state = MagicMock()
        await asyncio.wait_for(engine.stop(), timeout=2.0)
        return engine._executor.close.called and engine._persist_runtime_state.called

    assert asyncio.run(_run()) is True


def test_daily_drawdown_flatten_flag() -> None:
    risk = RiskManager(_minimal_engine_config(), None)
    risk._daily_drawdown_circuit_tripped = True
    risk._daily_drawdown_circuit_date = __import__(
        "src.utils.helpers", fromlist=["utc_now"]
    ).utc_now().strftime("%Y-%m-%d")
    assert risk.should_flatten_on_daily_drawdown is True
    assert risk.request_daily_dd_flatten() is True
    assert risk.should_flatten_on_daily_drawdown is False


if __name__ == "__main__":
    test_effective_config_mode_overrides()
    test_trading_symbols_unified_from_assets()
    test_unknown_config_key_raises()
    test_live_cooldown_uses_strategy_paths()
    test_kelly_disabled_skips_multiplier()
    test_governor_blocks_direct_strategy_with_audit_reason()
    test_restart_preserves_stop_streak_and_cooldown()
    test_cold_start_blocks_entries_until_healthy_feed()
    test_stale_feed_blocks_entry_but_not_exit_path()
    test_daily_dd_flatten_once_per_trip()
    test_daily_dd_flatten_resets_on_new_day()
    test_symbol_risk_multiplier_map_from_config()
    test_sigterm_handler_sets_shutdown_flag()
    test_sigterm_stops_engine_and_cancels_tasks_within_timeout()
    test_restore_runs_exactly_once_in_start()
    test_sigterm_triggers_engine_stop_cleanup()
    test_daily_drawdown_flatten_flag()
    print("ALL PHASE 04 RUNTIME TESTS PASSED [OK]")
