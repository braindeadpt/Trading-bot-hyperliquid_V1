"""Quick test for Task 2.4: Cooldown inteligente.

Validates:
  - Cooldown blocks entry during active cooldown
  - Cooldown doubles after loss (1h -> 2h -> 4h)
  - Cooldown resets after win
  - Cooldown resets when funding normalizes
  - Cooldown resets when ADX regime changes
"""

import sys
sys.path.insert(0, r"C:\Users\Braindead\Documents\trading-bot-hyperliquid")

from src.strategies.base import MarketEvent
from src.core.engine import TradingEngine
import time


def make_engine_for_cooldown_test():
    """Build a minimal engine instance for cooldown testing."""
    # We can't fully instantiate TradingEngine without all deps,
    # but we can test the cooldown methods by creating a minimal mock.
    from src.data.database import Database
    from src.core.portfolio import PortfolioState
    from src.core.risk_manager import RiskManager
    from src.core.execution import ExecutionEngine
    from src.exchanges.hyperliquid_ws import DataBus
    from src.utils.config import Config

    # Use a minimal config
    config = Config({
        "symbols": ["BTC"],
        "cooldown.base_minutes": 60,
        "cooldown.max_minutes": 240,
        "cooldown.multiplier": 2.0,
        "risk.max_position_size_pct": 20.0,
        "risk.leverage_max": 10.0,
        "risk.max_slippage_pct": 0.2,
        "risk.min_fill_ratio": 0.8,
        "strategy.adx_trend_threshold": 25.0,
        "strategy.adx_range_threshold": 20.0,
    })

    db = Database(":memory:")
    portfolio = PortfolioState(config)
    risk = RiskManager(config, db)
    bus = DataBus()
    executor = ExecutionEngine(config, db, "paper")
    strategies = []  # empty list for cooldown test

    engine = TradingEngine(config, db, bus, strategies, risk, executor)
    return engine


def test_cooldown_blocks_entry():
    print("=" * 60)
    print("TEST: Cooldown blocks entry")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    # Simulate an entry 30 minutes ago
    engine._cooldown_state["SmartMoneyFlow:BTC"] = {
        "last_trade_ms": now - 30 * 60_000,  # 30 min ago
        "duration_ms": 60 * 60_000,  # 1h cooldown
        "consecutive_losses": 0,
        "adx": 15.0,
        "funding": 0.005,
    }

    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=now,
        funding=0.005, predicted_funding=0.005,
    )

    in_cooldown, reason = engine._is_in_cooldown("SmartMoneyFlow", "BTC", event)
    assert in_cooldown, "Should be in cooldown (30 min < 60 min)"
    assert "30.0min remaining" in reason, f"Unexpected reason: {reason}"
    print(f"In cooldown: {in_cooldown}, reason: {reason}")
    print("[PASS]\n")


def test_cooldown_expires():
    print("=" * 60)
    print("TEST: Cooldown expires after time")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    # Simulate an entry 90 minutes ago (cooldown = 60 min)
    engine._cooldown_state["SmartMoneyFlow:BTC"] = {
        "last_trade_ms": now - 90 * 60_000,
        "duration_ms": 60 * 60_000,
        "consecutive_losses": 0,
        "adx": 15.0,
        "funding": 0.005,
    }

    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=now,
        funding=0.005, predicted_funding=0.005,
    )

    in_cooldown, reason = engine._is_in_cooldown("SmartMoneyFlow", "BTC", event)
    assert not in_cooldown, "Cooldown should have expired (90 min > 60 min)"
    assert "SmartMoneyFlow:BTC" not in engine._cooldown_state, "State should be deleted"
    print("Cooldown expired correctly")
    print("[PASS]\n")


def test_cooldown_doubles_after_loss():
    print("=" * 60)
    print("TEST: Cooldown doubles after loss")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    # Enter with base cooldown
    engine._update_cooldown_on_entry("SmartMoneyFlow", "BTC", MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=now,
        funding=0.005, predicted_funding=0.005,
    ))
    assert engine._cooldown_state["SmartMoneyFlow:BTC"]["duration_ms"] == 60 * 60_000
    print(f"Initial cooldown: {engine._cooldown_state['SmartMoneyFlow:BTC']['duration_ms'] / 60_000} min")

    # Loss -> doubles
    engine._update_cooldown_on_exit("SmartMoneyFlow", "BTC", pnl_pct=-0.01)
    state = engine._cooldown_state["SmartMoneyFlow:BTC"]
    assert state["duration_ms"] == 120 * 60_000, f"Expected 120 min, got {state['duration_ms'] / 60_000}"
    assert state["consecutive_losses"] == 1
    print(f"After 1 loss: {state['duration_ms'] / 60_000} min, losses={state['consecutive_losses']}")

    # Another loss -> doubles again (capped at 240 min)
    engine._update_cooldown_on_exit("SmartMoneyFlow", "BTC", pnl_pct=-0.02)
    state = engine._cooldown_state["SmartMoneyFlow:BTC"]
    assert state["duration_ms"] == 240 * 60_000, f"Expected 240 min, got {state['duration_ms'] / 60_000}"
    assert state["consecutive_losses"] == 2
    print(f"After 2 losses: {state['duration_ms'] / 60_000} min, losses={state['consecutive_losses']}")

    # Third loss -> stays at cap
    engine._update_cooldown_on_exit("SmartMoneyFlow", "BTC", pnl_pct=-0.03)
    state = engine._cooldown_state["SmartMoneyFlow:BTC"]
    assert state["duration_ms"] == 240 * 60_000
    assert state["consecutive_losses"] == 3
    print(f"After 3 losses: {state['duration_ms'] / 60_000} min (capped), losses={state['consecutive_losses']}")

    print("[PASS]\n")


def test_cooldown_resets_after_win():
    print("=" * 60)
    print("TEST: Cooldown resets after win")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    # Setup: 2 losses -> 4h cooldown
    engine._cooldown_state["SmartMoneyFlow:BTC"] = {
        "last_trade_ms": now,
        "duration_ms": 240 * 60_000,
        "consecutive_losses": 2,
        "adx": 15.0,
        "funding": 0.005,
    }

    # Win -> reset
    engine._update_cooldown_on_exit("SmartMoneyFlow", "BTC", pnl_pct=0.05)
    state = engine._cooldown_state["SmartMoneyFlow:BTC"]
    assert state["duration_ms"] == 60 * 60_000, f"Expected 60 min, got {state['duration_ms'] / 60_000}"
    assert state["consecutive_losses"] == 0
    print(f"After win: {state['duration_ms'] / 60_000} min, losses={state['consecutive_losses']}")
    print("[PASS]\n")


def test_cooldown_resets_on_funding_normalization():
    print("=" * 60)
    print("TEST: Cooldown resets on funding normalization")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    engine._cooldown_state["SmartMoneyFlow:BTC"] = {
        "last_trade_ms": now - 30 * 60_000,
        "duration_ms": 60 * 60_000,
        "consecutive_losses": 1,
        "adx": 15.0,
        "funding": 0.008,
    }

    # Funding normalizes to 0.1% (very mild)
    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=now,
        funding=0.001, predicted_funding=0.001,
    )

    in_cooldown, _ = engine._is_in_cooldown("SmartMoneyFlow", "BTC", event)
    assert not in_cooldown, "Cooldown should reset when funding normalizes"
    assert "SmartMoneyFlow:BTC" not in engine._cooldown_state
    print("Cooldown reset on funding normalization (0.1%)")
    print("[PASS]\n")


def test_cooldown_resets_on_regime_change():
    print("=" * 60)
    print("TEST: Cooldown resets on ADX regime change")
    print("=" * 60)

    engine = make_engine_for_cooldown_test()
    now = int(time.time() * 1000)

    # Last trade in RANGE regime (ADX=15)
    engine._cooldown_state["SmartMoneyFlow:BTC"] = {
        "last_trade_ms": now - 30 * 60_000,
        "duration_ms": 60 * 60_000,
        "consecutive_losses": 1,
        "adx": 15.0,  # range
        "funding": 0.005,
    }

    # Now ADX=30 (trend regime)
    event = MarketEvent(
        symbol="BTC", price=50000.0, timestamp_ms=now,
        funding=0.005, predicted_funding=0.005,
        adx_14=30.0,
    )

    in_cooldown, _ = engine._is_in_cooldown("SmartMoneyFlow", "BTC", event)
    assert not in_cooldown, "Cooldown should reset when regime changes"
    assert "SmartMoneyFlow:BTC" not in engine._cooldown_state
    print("Cooldown reset on regime change: range (ADX=15) -> trend (ADX=30)")
    print("[PASS]\n")


if __name__ == "__main__":
    test_cooldown_blocks_entry()
    test_cooldown_expires()
    test_cooldown_doubles_after_loss()
    test_cooldown_resets_after_win()
    test_cooldown_resets_on_funding_normalization()
    test_cooldown_resets_on_regime_change()
    print("=" * 60)
    print("ALL TESTS PASSED [OK]")
    print("=" * 60)
