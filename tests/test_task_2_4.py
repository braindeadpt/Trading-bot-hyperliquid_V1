"""Quick test for Task 2.4: Cooldown inteligente.

Validates strategy.cooldown paths (30/120) and legacy top-level fallback.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, r"C:\Users\Braindead\Documents\trading-bot-hyperliquid")

from src.strategies.base import MarketEvent
from src.core.engine import TradingEngine
from src.utils.config import Config, get_strategy_section
import pytest

pytestmark = pytest.mark.integration_offline


def make_engine_for_cooldown_test(config: Config | None = None) -> TradingEngine:
    """Build a minimal engine instance for cooldown testing."""
    from src.data.database import Database
    from src.core.risk_manager import RiskManager
    from src.core.execution import ExecutionEngine
    from src.exchanges.hyperliquid_ws import DataBus

    if config is None:
        config = Config({
            "symbols": ["BTC"],
            "strategy": {
                "cooldown": {
                    "base_minutes": 30,
                    "max_minutes": 120,
                    "multiplier": 2.0,
                },
                "adx_trend_threshold": 25.0,
                "adx_range_threshold": 20.0,
            },
            "risk": {
                "max_position_size_pct": 20.0,
                "leverage_max": 10.0,
                "max_slippage_pct": 0.2,
                "min_fill_ratio": 0.8,
            },
        })

    db = Database(":memory:")
    risk = RiskManager(config, db)
    bus = DataBus()
    executor = ExecutionEngine(config, db, "paper")
    return TradingEngine(config, db, bus, [], risk, executor)


def test_cooldown_reads_strategy_section_30_120() -> None:
    print("=" * 60)
    print("TEST: strategy.cooldown 30/120 paths")
    print("=" * 60)

    cfg = Config({
        "symbols": ["BTC"],
        "strategy": {
            "cooldown": {
                "base_minutes": 30,
                "max_minutes": 120,
                "multiplier": 2.0,
            },
        },
        "risk": {"max_position_size_pct": 5.0, "leverage_max": 5.0},
    })
    section = get_strategy_section(cfg, "cooldown")
    assert section["base_minutes"] == 30
    assert section["max_minutes"] == 120

    engine = make_engine_for_cooldown_test(cfg)
    assert engine._cooldown_base_ms == 30 * 60_000
    assert engine._cooldown_max_ms == 120 * 60_000
    print("[PASS]\n")


def test_cooldown_legacy_top_level_fallback() -> None:
    print("=" * 60)
    print("TEST: legacy top-level cooldown fallback")
    print("=" * 60)

    cfg = Config({
        "symbols": ["BTC"],
        "cooldown": {
            "base_minutes": 45,
            "max_minutes": 180,
            "multiplier": 3.0,
        },
        "risk": {"max_position_size_pct": 5.0, "leverage_max": 5.0},
    })
    assert "cooldown" not in (cfg.get("strategy") or {})
    section = get_strategy_section(cfg, "cooldown")
    assert section["base_minutes"] == 45
    assert section["max_minutes"] == 180

    engine = make_engine_for_cooldown_test(cfg)
    assert engine._cooldown_base_ms == 45 * 60_000
    assert engine._cooldown_max_ms == 180 * 60_000
    assert engine._cooldown_multiplier == 3.0
    print("[PASS]\n")


def test_cooldown_blocks_entry() -> None:
    print("=" * 60)
    print("TEST: Cooldown blocks entry")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    engine._cooldown_state["SmartMoneyFlow:BTC"] = {
        "last_trade_ms": now - 15 * 60_000,
        "duration_ms": 30 * 60_000,
        "consecutive_losses": 0,
        "adx": 15.0,
        "funding": 0.005,
    }

    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=now,
        funding=0.005, predicted_funding=0.005,
    )

    in_cooldown, reason = engine._is_in_cooldown("SmartMoneyFlow", "BTC", event)
    assert in_cooldown, "Should be in cooldown (15 min < 30 min base)"
    assert "15.0min remaining" in reason, f"Unexpected reason: {reason}"
    print(f"In cooldown: {in_cooldown}, reason: {reason}")
    print("[PASS]\n")


def test_cooldown_expires() -> None:
    print("=" * 60)
    print("TEST: Cooldown expires after time")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    engine._cooldown_state["SmartMoneyFlow:BTC"] = {
        "last_trade_ms": now - 90 * 60_000,
        "duration_ms": 30 * 60_000,
        "consecutive_losses": 0,
        "adx": 15.0,
        "funding": 0.005,
    }

    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=now,
        funding=0.005, predicted_funding=0.005,
    )

    in_cooldown, _reason = engine._is_in_cooldown("SmartMoneyFlow", "BTC", event)
    assert not in_cooldown, "Cooldown should have expired (90 min > 30 min)"
    assert "SmartMoneyFlow:BTC" not in engine._cooldown_state
    print("Cooldown expired correctly")
    print("[PASS]\n")


def test_cooldown_doubles_after_loss() -> None:
    print("=" * 60)
    print("TEST: Cooldown doubles after loss (30 -> 60 -> 120 cap)")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    engine._update_cooldown_on_entry("SmartMoneyFlow", "BTC", MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=now,
        funding=0.005, predicted_funding=0.005,
    ))
    assert engine._cooldown_state["SmartMoneyFlow:BTC"]["duration_ms"] == 30 * 60_000

    engine._update_cooldown_on_exit("SmartMoneyFlow", "BTC", pnl_pct=-0.01)
    state = engine._cooldown_state["SmartMoneyFlow:BTC"]
    assert state["duration_ms"] == 60 * 60_000
    assert state["consecutive_losses"] == 1

    engine._update_cooldown_on_exit("SmartMoneyFlow", "BTC", pnl_pct=-0.02)
    state = engine._cooldown_state["SmartMoneyFlow:BTC"]
    assert state["duration_ms"] == 120 * 60_000
    assert state["consecutive_losses"] == 2

    engine._update_cooldown_on_exit("SmartMoneyFlow", "BTC", pnl_pct=-0.03)
    state = engine._cooldown_state["SmartMoneyFlow:BTC"]
    assert state["duration_ms"] == 120 * 60_000
    assert state["consecutive_losses"] == 3
    print("[PASS]\n")


def test_cooldown_resets_after_win() -> None:
    print("=" * 60)
    print("TEST: Cooldown resets after win")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    engine._cooldown_state["SmartMoneyFlow:BTC"] = {
        "last_trade_ms": now,
        "duration_ms": 120 * 60_000,
        "consecutive_losses": 2,
        "adx": 15.0,
        "funding": 0.005,
    }

    engine._update_cooldown_on_exit("SmartMoneyFlow", "BTC", pnl_pct=0.05)
    state = engine._cooldown_state["SmartMoneyFlow:BTC"]
    assert state["duration_ms"] == 30 * 60_000
    assert state["consecutive_losses"] == 0
    print("[PASS]\n")


def test_cooldown_resets_on_funding_normalization() -> None:
    print("=" * 60)
    print("TEST: Cooldown resets on funding normalization (funding strategies)")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    engine._cooldown_state["FundingExtreme:BTC"] = {
        "last_trade_ms": now - 10 * 60_000,
        "duration_ms": 30 * 60_000,
        "consecutive_losses": 1,
        "adx": 15.0,
        "funding": 0.008,
    }

    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=now,
        funding=0.00001, predicted_funding=0.00001,
    )

    in_cooldown, _ = engine._is_in_cooldown("FundingExtreme", "BTC", event)
    assert not in_cooldown
    assert "FundingExtreme:BTC" not in engine._cooldown_state
    print("[PASS]\n")


def test_cooldown_persists_with_near_zero_funding() -> None:
    print("=" * 60)
    print("TEST: Cooldown persists when funding ~0 (non-funding strategy)")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    engine._cooldown_state["ChecklistMeta:SOL"] = {
        "last_trade_ms": now - 5 * 60_000,
        "duration_ms": 30 * 60_000,
        "consecutive_losses": 1,
        "adx": 18.0,
        "funding": 0.0001,
    }

    event = MarketEvent(
        symbol="SOL", price=150.0, timestamp_ms=now,
        funding=0.0001, predicted_funding=0.0001,
        adx_14=18.0,
    )

    in_cooldown, reason = engine._is_in_cooldown("ChecklistMeta", "SOL", event)
    assert in_cooldown
    assert "25.0min remaining" in reason
    print("[PASS]\n")


def test_cooldown_resets_on_regime_change() -> None:
    print("=" * 60)
    print("TEST: Cooldown resets on ADX regime change")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    engine._cooldown_state["SmartMoneyFlow:BTC"] = {
        "last_trade_ms": now - 10 * 60_000,
        "duration_ms": 30 * 60_000,
        "consecutive_losses": 1,
        "adx": 15.0,
        "funding": 0.005,
    }

    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=now,
        funding=0.005, predicted_funding=0.005,
        adx_14=30.0,
    )

    in_cooldown, _ = engine._is_in_cooldown("SmartMoneyFlow", "BTC", event)
    assert not in_cooldown
    assert "SmartMoneyFlow:BTC" not in engine._cooldown_state
    print("[PASS]\n")


if __name__ == "__main__":
    test_cooldown_reads_strategy_section_30_120()
    test_cooldown_legacy_top_level_fallback()
    test_cooldown_blocks_entry()
    test_cooldown_expires()
    test_cooldown_doubles_after_loss()
    test_cooldown_resets_after_win()
    test_cooldown_resets_on_funding_normalization()
    test_cooldown_persists_with_near_zero_funding()
    test_cooldown_resets_on_regime_change()
    print("=" * 60)
    print("ALL TESTS PASSED [OK]")
    print("=" * 60)
