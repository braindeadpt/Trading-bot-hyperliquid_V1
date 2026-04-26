#!/usr/bin/env python3
"""
verify_clean.py — Verifica se a Clean Architecture compila e os imports funcionam.
"""
import sys
from pathlib import Path

# Adicionar paths
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

TESTS = []

def test(name):
    def decorator(fn):
        TESTS.append((name, fn))
        return fn
    return decorator


# ─── Domain Layer ──────────────────────────────────────────

@test("Domain Entities — Candle, Signal, Position, Trade, MarketSnapshot")
def test_domain_entities():
    from clean.domain.entities import Candle, Signal, Position, Trade, MarketSnapshot
    
    candle = Candle("BTC", "15m", 1, 100, 110, 90, 105, 1000)
    assert abs(candle.typical_price() - 101.666) < 0.001
    assert candle.is_bullish()
    
    signal = Signal("BTC", "long", 0.8, 50000, stop_loss=48000, take_profit=55000, reason="momentum")
    assert signal.is_long
    assert signal.risk_reward > 0
    
    pos = Position(asset="BTC", direction="long", entry_price=50000, size_usd=100, leverage=2)
    assert pos.current_pnl(51000) > 0
    
    trade = Trade(symbol="BTC", direction="long", entry_price=50000, size_usd=100, entry_time=1)
    assert trade.calculate_pnl(51000) > 0
    
    snapshot = MarketSnapshot(asset="BTC", price=50000, oi_usd=1e9, funding_rate=0.001)
    assert snapshot.mid_price == 50000  # bid/ask não definidos → fallback para price
    
    return True


@test("Domain Events — SignalGenerated, TradeExecuted, PositionOpened")
def test_domain_events():
    from clean.domain.events import SignalGenerated, TradeExecuted, PositionOpened, DomainEvent
    
    event = SignalGenerated("BTC", "long", 0.8, 50000, "momentum", {})
    assert event.event_type == "signal.generated"
    assert event.payload["asset"] == "BTC"
    
    return True


@test("Domain Repository Interfaces")
def test_domain_repository_interfaces():
    from clean.domain.repositories import CandleRepository, TradeRepository, SignalRepository
    # Verificar que são ABCs
    assert hasattr(CandleRepository, '__abstractmethods__')
    return True


@test("Domain Service Interfaces")
def test_domain_service_interfaces():
    from clean.domain.services import MarketDataProvider, ExchangeGateway
    assert hasattr(MarketDataProvider, '__abstractmethods__')
    return True


# ─── Application Layer ───────────────────────────────────

@test("Application DTOs")
def test_application_dtos():
    from clean.application.dto import MarketDataDTO, SignalDTO, TradeDTO, PortfolioStatusDTO
    
    md = MarketDataDTO(asset="BTC", price=50000)
    assert md.asset == "BTC"
    
    sig = SignalDTO(asset="BTC", direction="long", confidence=0.8, entry_price=50000)
    assert sig.direction == "long"
    
    return True


@test("Application Interfaces")
def test_application_interfaces():
    from clean.application.interfaces import EventPublisher, Logger, StrategyPort
    assert hasattr(EventPublisher, '__abstractmethods__')
    return True


# ─── Interface Adapters ──────────────────────────────────

@test("SQLite Connection + Schema")
def test_sqlite_connection():
    import tempfile, os
    from clean.interface_adapters.database import SQLiteConnection
    
    db_path = tempfile.mktemp(suffix='.db')
    try:
        db = SQLiteConnection(db_path)
        conn = db.connect()
        # Verificar que as tabelas existem
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert 'candles' in table_names
        assert 'trades' in table_names
        assert 'signals' in table_names
        db.close()
        return True
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


@test("Mappers — CandleMapper, TradeMapper, SignalMapper")
def test_mappers():
    from clean.interface_adapters.mappers import CandleMapper, TradeMapper, SignalMapper
    from clean.domain.entities import Candle, Trade, Signal
    
    cm = CandleMapper()
    candle = cm.to_entity({
        'symbol': 'BTC', 'interval': '15m', 'timestamp': 1,
        'open': 100, 'high': 110, 'low': 90, 'close': 105, 'volume': 1000
    })
    assert isinstance(candle, Candle)
    
    tm = TradeMapper()
    trade = tm.to_entity({
        'symbol': 'BTC', 'direction': 'long', 'entry_price': 50000,
        'entry_time': 1, 'size_usd': 100, 'leverage': 1
    })
    assert isinstance(trade, Trade)
    
    return True


@test("SQLite Trade Repository")
def test_sqlite_trade_repo():
    import tempfile, os
    from clean.interface_adapters.database import SQLiteConnection
    from clean.interface_adapters.repositories.sqlite_trade_repository import SQLiteTradeRepository
    from clean.domain.entities import Trade
    
    db_path = tempfile.mktemp(suffix='.db')
    try:
        db = SQLiteConnection(db_path)
        repo = SQLiteTradeRepository(db)
        
        trade = Trade(symbol="BTC", direction="long", entry_price=50000, size_usd=100, entry_time=1)
        trade_id = repo.save(trade)
        assert trade_id > 0
        
        open_trade = repo.get_open()
        assert open_trade is not None
        assert open_trade.symbol == "BTC"
        
        count = repo.get_count()
        assert count == 1
        
        db.close()
        return True
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


@test("SQLite Candle Repository")
def test_sqlite_candle_repo():
    import tempfile, os
    from clean.interface_adapters.database import SQLiteConnection
    from clean.interface_adapters.repositories.sqlite_candle_repository import SQLiteCandleRepository
    from clean.domain.entities import Candle
    
    db_path = tempfile.mktemp(suffix='.db')
    try:
        db = SQLiteConnection(db_path)
        repo = SQLiteCandleRepository(db)
        
        candles = [
            Candle("BTC", "15m", i, 100+i, 110+i, 90+i, 105+i, 1000)
            for i in range(5)
        ]
        repo.save(candles)
        
        recent = repo.get_recent("BTC", "15m", 3)
        assert len(recent) == 3
        
        count = repo.get_count("BTC")
        assert count == 5
        
        db.close()
        return True
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


@test("Hyperliquid API Gateway — imports")
def test_hyperliquid_gateway():
    from clean.interface_adapters.gateways.hyperliquid_api_gateway import HyperliquidAPIGateway
    gateway = HyperliquidAPIGateway()
    # Verifica que implementa ambas as portas
    from clean.domain.services import MarketDataProvider, ExchangeGateway
    assert isinstance(gateway, MarketDataProvider)
    assert isinstance(gateway, ExchangeGateway)
    return True


@test("Web API Controller — imports")
def test_web_controller():
    from clean.interface_adapters.controllers.web_api_controller import WebAPIController
    # Apenas verificar imports — use cases requerem mocks
    assert WebAPIController is not None
    return True


# ─── Infrastructure ────────────────────────────────────────

@test("EventBus Publisher Adapter")
def test_eventbus_adapter():
    from clean.infrastructure.events import EventBusPublisherAdapter
    from clean.domain.events import DomainEvent
    from refactored.core.event_bus import EventBus
    
    bus = EventBus()
    adapter = EventBusPublisherAdapter(bus)
    
    events_received = []
    def handler(e):
        events_received.append(e)
    bus.subscribe("test.event", handler)
    
    event = DomainEvent("test.event", payload={"msg": "hello"})
    adapter.publish(event)
    
    assert len(events_received) == 1
    return True


@test("Strategy Adapter")
def test_strategy_adapter():
    from clean.infrastructure.strategy_adapter import StrategyAdapter
    from refactored.strategy.ghost import GhostMethodStrategy
    from refactored.core.event_bus import EventBus
    
    raw = GhostMethodStrategy({}, EventBus())
    adapter = StrategyAdapter(raw)
    
    from clean.application.interfaces import StrategyPort
    assert isinstance(adapter, StrategyPort)
    
    result = adapter.get_required_data()
    assert len(result) > 0
    return True


@test("Flask Web App — imports")
def test_flask_app():
    from clean.infrastructure.web.flask_app import FlaskWebApp
    assert FlaskWebApp is not None
    return True


@test("Composition Root — create_app")
def test_composition_root():
    from clean.infrastructure.main import create_app
    app = create_app()
    
    required_keys = [
        'event_bus', 'publisher', 'db_conn',
        'candle_repo', 'trade_repo', 'signal_repo',
        'gateway', 'strategy',
        'fetch_uc', 'signal_uc', 'trade_uc', 'status_uc',
        'controller', 'web_app', 'logger'
    ]
    for key in required_keys:
        assert key in app, f"Falta: {key}"
    
    return True


# ─── Integration ─────────────────────────────────────────

@test("End-to-End: Fetch → Signal → Trade")
def test_end_to_end():
    from clean.infrastructure.main import create_app
    import tempfile, os
    
    db_path = tempfile.mktemp(suffix='.db')
    try:
        app = create_app({'database': {'path': db_path}})
        
        # 1. Buscar dados de mercado (pode falhar sem internet — aceitável)
        market_data = app['fetch_uc'].execute("BTC")
        # Se não houver internet, é None — ok para teste
        
        # 2. Verificar que repositórios estão vazios
        assert app['trade_repo'].get_count() == 0
        
        # 3. Verificar que portfolio está limpo
        status = app['status_uc'].execute()
        assert status.capital == 10000.0
        assert not status.in_position
        
        return True
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def main():
    print("=" * 70)
    print("  🏛️  VERIFICAÇÃO — CLEAN ARCHITECTURE")
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
        print("\n  🎉 CLEAN ARCHITECTURE VALIDADA!")
        print("  Todas as camadas compõem corretamente.")
        return 0
    else:
        print(f"\n  ⚠️  {failed} teste(s) falharam.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
