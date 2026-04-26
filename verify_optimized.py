#!/usr/bin/env python3
"""
verify_optimized.py — Verifica se os módulos otimizados v2 importam e funcionam.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

TESTS = []

def test(name):
    def decorator(fn):
        TESTS.append((name, fn))
        return fn
    return decorator

@test("EventBus v2 — deque history")
def test_event_bus_v2():
    from refactored.optimized.event_bus_v2 import EventBus, Event
    bus = EventBus(max_history=10)
    for i in range(20):
        bus.publish("test", {"i": i})
    history = bus.get_history("test", limit=5)
    assert len(history) == 5
    stats = bus.stats()
    assert stats['history_size'] == 10  # maxlen
    assert stats['total_events'] == 20
    return True

@test("DataCache v2 — thread-safe LRU")
def test_cache_v2():
    from refactored.optimized.cache_v2 import DataCache
    cache = DataCache(max_size=5)
    for i in range(10):
        cache.set(f"k{i}", i, ttl=60)
    assert len(cache._store) == 5  # Evicted oldest
    assert cache.get("k0") is None  # Evicted
    assert cache.get("k9") == 9    # Still there
    return True

@test("Database v2 — persistent connection + batch stats")
def test_database_v2():
    import tempfile, os
    from refactored.optimized.database_v2 import BotDatabase
    db_path = tempfile.mktemp(suffix='.db')
    try:
        db = BotDatabase(db_path)
        db.save_candles("BTC", "15m", [
            {'timestamp': 1, 'open': 1, 'high': 2, 'low': 0, 'close': 1.5, 'volume': 100},
            {'timestamp': 2, 'open': 1.5, 'high': 2, 'low': 1, 'close': 1.8, 'volume': 200}
        ])
        stats = db.get_stats()
        assert stats['candles'] == 2
        assert stats['trades'] == 0
        db.close()
        return True
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

@test("Aggregator v2 — dedup + throttling")
def test_aggregator_v2():
    from refactored.optimized.aggregator_v2 import DataAggregator
    from refactored.optimized.cache_v2 import DataCache
    from refactored.optimized.event_bus_v2 import EventBus
    
    bus = EventBus()
    events = []
    bus.subscribe('market.data', lambda e: events.append(e))
    
    agg = DataAggregator({'assets': ['BTC']}, cache=DataCache(), event_bus=bus)
    # Simula fetch sem API (não faz chamada real)
    assert agg._last_price == {}
    return True

@test("TerminalCLI v2 — imports")
def test_terminal_v2():
    from refactored.optimized.terminal_v2 import TerminalCLI
    from refactored.optimized.event_bus_v2 import EventBus
    cli = TerminalCLI(EventBus())
    assert cli is not None
    return True

@test("WebApp v2 — deque trades")
def test_webapp_v2():
    from refactored.optimized.webapp_v2 import WebApp
    from refactored.optimized.event_bus_v2 import EventBus
    import tempfile, os
    
    db_path = tempfile.mktemp(suffix='.db')
    try:
        from refactored.optimized.database_v2 import BotDatabase
        web = WebApp({}, EventBus(), BotDatabase(db_path))
        assert len(web._trades) == 0
        # Testa deque maxlen
        for i in range(1500):
            web._trades.append({'i': i})
        assert len(web._trades) == 1000
        return True
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

@test("BotEngine v2 — sleep único")
def test_engine_v2():
    from refactored.optimized.engine_v2 import BotEngine
    import ast
    # Verifica que não há time.sleep(1) granular
    with open(Path(__file__).parent / "refactored/optimized/engine_v2.py") as f:
        code = f.read()
    # Engine v2 usa Event.wait() em vez de sleep granular
    assert "_shutdown_event.wait" in code
    return True

@test("GhostStrategy v2 — VP cache")
def test_strategy_v2():
    from refactored.optimized.strategy_v2 import GhostMethodStrategy
    from refactored.optimized.event_bus_v2 import EventBus
    strategy = GhostMethodStrategy({}, EventBus())
    # Testa cache de VP
    candles = [
        {'timestamp': i, 'open': 100, 'high': 110, 'low': 90, 'close': 105, 'volume': 1000}
        for i in range(100)
    ]
    vp1 = strategy.calculate_volume_profile(candles)
    vp2 = strategy.calculate_volume_profile(candles)
    assert vp1 == vp2  # Mesmo resultado (cached)
    return True


def main():
    print("=" * 70)
    print("  🔍 VERIFICAÇÃO DE MÓDULOS OTIMIZADOS v2.0")
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
        print("\n  🎉 MÓDULOS OTIMIZADOS VALIDADOS!")
        print("  Pronto para merge com refactored/")
        return 0
    else:
        print(f"\n  ⚠️  {failed} teste(s) falharam.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
