"""Characterization tests for src/core/engine.py, written BEFORE any further
extraction (RiskState / BackgroundTasks are not yet split out of
TradingEngine — see AGENTS.md Fase 09 notes).

Purpose: pin down current behavior of engine-owned logic that is a
candidate for future extraction, so a refactor can be verified against
these tests without changing behavior, gate ordering, sizing, or config.

Covers:
  - _entry_feed_block_reason (feed/WS/reconciliation staleness gating)
  - _seed_kelly_from_db (Kelly sizer bootstrap from DB history)
  - _governor_blocks_signal (strategy governor gate)
  - kill_switch (emergency flatten path)

Does NOT modify src/. Pure characterization: assertions describe what the
code does today, not what it should do.
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

pytestmark = pytest.mark.unit


def _bare_engine(cfg):
    from src.core.engine import TradingEngine

    engine = TradingEngine.__new__(TradingEngine)
    engine._config = cfg
    engine._hl_ws_client = None
    engine._block_entries_on_ws_unhealthy = False
    engine._block_entries_on_feed_stale = False
    engine._reconciliation_block_when_stale = False
    engine._feed_health_evaluated = False
    engine._feed_health_ready = False
    engine._market_data_health = {}
    engine._reconciler = None

    class _Summary:
        overall = "green"
    engine._market_data_health_summary = _Summary()
    return engine


# ── _entry_feed_block_reason ──────────────────────────────────────


def test_entry_feed_block_reason_none_when_all_gates_disabled():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    assert engine._entry_feed_block_reason("BTC") is None


def test_entry_feed_block_reason_ws_unhealthy():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._block_entries_on_ws_unhealthy = True
    engine._hl_ws_client = MagicMock(is_healthy=False)
    assert engine._entry_feed_block_reason("BTC") == "ws_unhealthy"


def test_entry_feed_block_reason_ws_healthy_no_block():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._block_entries_on_ws_unhealthy = True
    engine._hl_ws_client = MagicMock(is_healthy=True)
    assert engine._entry_feed_block_reason("BTC") is None


def test_entry_feed_block_reason_feed_health_pending():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._block_entries_on_feed_stale = True
    engine._feed_health_evaluated = False
    assert engine._entry_feed_block_reason("BTC") == "feed_health_pending"


def test_entry_feed_block_reason_feed_health_not_ready():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._block_entries_on_feed_stale = True
    engine._feed_health_evaluated = True
    engine._feed_health_ready = False
    assert engine._entry_feed_block_reason("BTC") == "feed_health_not_ready"


def test_entry_feed_block_reason_symbol_red():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._block_entries_on_feed_stale = True
    engine._feed_health_evaluated = True
    engine._feed_health_ready = True
    engine._market_data_health["BTC"] = MagicMock(status="red")
    assert engine._entry_feed_block_reason("BTC") == "feed_red:BTC"


def test_entry_feed_block_reason_overall_red():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._block_entries_on_feed_stale = True
    engine._feed_health_evaluated = True
    engine._feed_health_ready = True

    class _Summary:
        overall = "red"
    engine._market_data_health_summary = _Summary()
    assert engine._entry_feed_block_reason("BTC") == "feed_red:overall"


def test_entry_feed_block_reason_reconciliation_stale():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._reconciliation_block_when_stale = True
    engine._reconciler = MagicMock()
    engine._reconciler.entries_blocked.return_value = True
    engine._reconciler.block_reason.return_value = "recon_stale_120s"
    assert engine._entry_feed_block_reason("BTC") == "recon_stale_120s"


def test_entry_feed_block_reason_reconciliation_not_blocked():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._reconciliation_block_when_stale = True
    engine._reconciler = MagicMock()
    engine._reconciler.entries_blocked.return_value = False
    assert engine._entry_feed_block_reason("BTC") is None


# ── _seed_kelly_from_db ────────────────────────────────────────────


def test_seed_kelly_from_db_skips_when_disabled():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._kelly_enabled = False
    engine._db = MagicMock()
    engine._kelly_sizer = MagicMock()
    engine._seed_kelly_from_db()
    engine._db.get_recent_closed_trade_pnl_pcts.assert_not_called()
    engine._kelly_sizer.seed_history.assert_not_called()


def test_seed_kelly_from_db_seeds_history_when_trades_exist():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._kelly_enabled = True
    engine._db = MagicMock()
    engine._db.get_recent_closed_trade_pnl_pcts.return_value = [0.01, -0.02, 0.03]
    engine._kelly_sizer = MagicMock()
    engine._kelly_sizer.seed_history.return_value = 3
    engine._kelly_sizer.get_size_multiplier.return_value = 1.0

    engine._seed_kelly_from_db()

    engine._kelly_sizer.seed_history.assert_called_once_with([0.01, -0.02, 0.03])


def test_seed_kelly_from_db_no_trades_does_not_seed():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._kelly_enabled = True
    engine._db = MagicMock()
    engine._db.get_recent_closed_trade_pnl_pcts.return_value = []
    engine._kelly_sizer = MagicMock()

    engine._seed_kelly_from_db()

    engine._kelly_sizer.seed_history.assert_not_called()


def test_seed_kelly_from_db_swallows_db_errors():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._kelly_enabled = True
    engine._db = MagicMock()
    engine._db.get_recent_closed_trade_pnl_pcts.side_effect = RuntimeError("db locked")
    engine._kelly_sizer = MagicMock()

    engine._seed_kelly_from_db()  # must not raise

    engine._kelly_sizer.seed_history.assert_not_called()


# ── _governor_blocks_signal ────────────────────────────────────────


def test_governor_blocks_signal_when_disabled():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._strategy_governor = MagicMock()
    engine._strategy_governor.is_enabled.return_value = False

    from src.strategies.base import Signal

    signal = Signal(
        strategy="TrendPyramid", symbol="BTC", side="long", confidence=0.8,
        size_pct=0.1, entry_price=50_000.0, stop_loss_pct=0.02,
        take_profit_pct=0.04, reason="test",
    )
    reason = engine._governor_blocks_signal(signal)
    assert reason == "governor_disabled:TrendPyramid"


def test_governor_allows_signal_when_enabled():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._strategy_governor = MagicMock()
    engine._strategy_governor.is_enabled.return_value = True

    from src.strategies.base import Signal

    signal = Signal(
        strategy="TrendPyramid", symbol="BTC", side="long", confidence=0.8,
        size_pct=0.1, entry_price=50_000.0, stop_loss_pct=0.02,
        take_profit_pct=0.04, reason="test",
    )
    assert engine._governor_blocks_signal(signal) is None


# ── kill_switch ─────────────────────────────────────────────────────


def test_kill_switch_raises_when_executor_lacks_kill_switch():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._executor = object()  # no kill_switch attribute

    with pytest.raises(RuntimeError):
        asyncio.run(engine.kill_switch())


def test_kill_switch_invokes_executor_and_reconciles():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._executor = MagicMock()
    engine._executor.kill_switch = AsyncMock(return_value={"flattened": True})
    engine._reconciler = MagicMock()
    engine._reconciler.reconcile_once = AsyncMock()

    result = asyncio.run(engine.kill_switch())

    assert result == {"flattened": True}
    engine._reconciler.reconcile_once.assert_awaited_once_with(executor=engine._executor)


def test_kill_switch_skips_reconcile_when_no_reconciler():
    cfg = load_config("config/settings.yaml")
    engine = _bare_engine(cfg)
    engine._executor = MagicMock()
    engine._executor.kill_switch = AsyncMock(return_value={"flattened": True})
    engine._reconciler = None

    result = asyncio.run(engine.kill_switch())
    assert result == {"flattened": True}
