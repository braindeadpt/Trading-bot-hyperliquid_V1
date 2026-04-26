#!/usr/bin/env python3
"""
verify_refactored.py — Verifica se a arquitetura refatorada importa corretamente.
"""
import sys
from pathlib import Path

# Adicionar parent ao path para imports absolutos
sys.path.insert(0, str(Path(__file__).parent))

TESTS = []

def test(name):
    def decorator(fn):
        TESTS.append((name, fn))
        return fn
    return decorator

@test("EventBus import")
def test_event_bus():
    from refactored.core.event_bus import EventBus, Event
    bus = EventBus()
    bus.subscribe("test", lambda e: None)
    bus.publish("test", {"x": 1})
    assert bus.stats()['total_events'] == 1
    return True

@test("ServiceContainer import")
def test_container():
    from refactored.core.container import ServiceContainer
    from refactored.core.event_bus import EventBus
    container = ServiceContainer({}, EventBus()).boot()
    assert container.has('event_bus')
    return True

@test("StateMachine import")
def test_state_machine():
    from refactored.core.state_machine import StateMachine, BotState
    sm = StateMachine()
    assert sm.state == BotState.IDLE
    assert sm.transition(BotState.SCANNING, "test")
    return True

@test("HyperliquidClient import")
def test_api_client():
    from refactored.api.hyperliquid_client import HyperliquidClient, MarketData, CircuitBreaker
    cb = CircuitBreaker()
    assert cb.can_execute()
    cb.record_failure()
    return True

@test("DataCache import")
def test_cache():
    from refactored.data.cache import DataCache
    print(f"    DEBUG: DataCache type = {type(DataCache)}")
    cache = DataCache()
    cache.set("x", 42)
    assert cache.get("x") == 42
    assert cache.stats['hits'] >= 0
    return True

@test("DataAggregator import")
def test_aggregator():
    from refactored.data.aggregator import DataAggregator
    from refactored.data.cache import DataCache
    from refactored.core.event_bus import EventBus
    agg = DataAggregator({}, cache=DataCache(), event_bus=EventBus())
    assert agg is not None
    return True

@test("BotDatabase import")
def test_database():
    from refactored.data.database import BotDatabase
    import tempfile, os
    db_path = tempfile.mktemp(suffix='.db')
    try:
        db = BotDatabase(db_path)
        db.save_candles("BTC", "15m", [{
            'timestamp': 1, 'open': 1, 'high': 2, 'low': 0, 'close': 1.5, 'volume': 100
        }])
        candles = db.get_candles("BTC", "15m")
        assert len(candles) == 1
        return True
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

@test("BaseStrategy import")
def test_strategy_base():
    from refactored.strategy.base import BaseStrategy, Signal
    s = Signal("LONG", confidence=0.8)
    assert s.type == "LONG"
    return True

@test("GhostMethodStrategy import")
def test_strategy_ghost():
    from refactored.strategy.ghost import GhostMethodStrategy
    from refactored.core.event_bus import EventBus
    strategy = GhostMethodStrategy({}, EventBus())
    result = strategy.analyze({
        'oi_total': 1e9, 'oi_change_pct': 0.05, 'funding_avg': 0.001,
        'volume_ratio': 5.0, 'price': 50000, 'sma_200': 49000
    }, 50000)
    # Pode ou não gerar sinal — ambos são válidos
    return True

@test("RiskManager import")
def test_risk():
    from refactored.execution.risk import RiskManager
    from refactored.strategy.base import Signal
    risk = RiskManager({'risk': {'max_daily_trades': 5, 'initial_capital': 10000}})
    signal = Signal("LONG", confidence=0.8)
    assert risk.allow_trade(signal, daily_pnl=0) == True
    return True

@test("PaperTrader import")
def test_trader():
    from refactored.execution.trader import PaperTrader
    from refactored.api.hyperliquid_client import HyperliquidClient
    from refactored.strategy.ghost import GhostMethodStrategy
    from refactored.execution.risk import RiskManager
    from refactored.data.database import BotDatabase
    from refactored.core.event_bus import EventBus
    import tempfile, os
    
    db_path = tempfile.mktemp(suffix='.db')
    try:
        event_bus = EventBus()
        api = HyperliquidClient({'api': {}}, paper_trading=True)
        strategy = GhostMethodStrategy({}, event_bus)
        db = BotDatabase(db_path)
        risk = RiskManager({'risk': {}}, db)
        
        trader = PaperTrader({}, api, strategy, risk, db, event_bus)
        assert trader.capital == 10000
        return True
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

@test("WebApp import")
def test_web():
    from refactored.web.app import WebApp
    from refactored.core.event_bus import EventBus
    from refactored.data.database import BotDatabase
    import tempfile, os
    db_path = tempfile.mktemp(suffix='.db')
    try:
        web = WebApp({}, EventBus(), BotDatabase(db_path))
        assert web is not None
        return True
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

@test("TerminalCLI import")
def test_cli():
    from refactored.cli.terminal import TerminalCLI
    from refactored.core.event_bus import EventBus
    cli = TerminalCLI(EventBus())
    assert cli is not None
    return True

@test("Config import")
def test_config():
    from refactored.utils.config import load_config, DEFAULT_CONFIG
    assert 'bot' in DEFAULT_CONFIG
    return True

@test("run.py imports")
def test_run():
    import ast
    run_path = Path(__file__).parent / "run.py"
    with open(run_path) as f:
        ast.parse(f.read())
    return True


def main():
    print("=" * 70)
    print("  🔍 VERIFICAÇÃO DA ARQUITETURA REFATORADA v2.0")
    print("=" * 70)
    
    passed = 0
    failed = 0
    errors = []
    
    for name, fn in TESTS:
        try:
            result = fn()
            status = "✅ PASS" if result else "⚠️  WARN"
            passed += 1
        except Exception as e:
            status = f"❌ FAIL: {e}"
            failed += 1
            errors.append((name, str(e)))
        print(f"  {status} — {name}")
    
    print("=" * 70)
    print(f"  Resultado: {passed}/{len(TESTS)} passaram")
    
    if errors:
        print("\n  Detalhes dos erros:")
        for name, err in errors:
            print(f"    • {name}: {err}")
    
    if failed == 0:
        print("\n  🎉 ARQUITETURA REFATORADA IMPORTA CORRETAMENTE!")
        print("  Pronto para usar: python3 run.py web")
        return 0
    else:
        print(f"\n  ⚠️  {failed} teste(s) falharam.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
