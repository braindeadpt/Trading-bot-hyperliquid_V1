"""
Main — Composition Root.
Monta todas as dependências (Dependency Injection).
Este é o ÚNICO ficheiro que conhece TODAS as camadas.
"""
import sys
import logging
from pathlib import Path

# Adicionar paths
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from clean.domain.repositories import CandleRepository, TradeRepository, SignalRepository
from clean.domain.services import MarketDataProvider, ExchangeGateway
from clean.application.interfaces import EventPublisher, Logger, StrategyPort
from clean.application.use_cases import (
    FetchMarketDataUseCase,
    GenerateSignalUseCase,
    ExecuteTradeUseCase,
    GetPortfolioStatusUseCase,
)
from clean.interface_adapters.repositories.sqlite_candle_repository import SQLiteCandleRepository
from clean.interface_adapters.repositories.sqlite_trade_repository import SQLiteTradeRepository
from clean.interface_adapters.repositories.sqlite_signal_repository import SQLiteSignalRepository
from clean.interface_adapters.gateways.hyperliquid_api_gateway import HyperliquidAPIGateway
from clean.interface_adapters.controllers.web_api_controller import WebAPIController
from clean.interface_adapters.database import SQLiteConnection
from clean.infrastructure.events import EventBusPublisherAdapter
from clean.infrastructure.strategy_adapter import StrategyAdapter
from clean.infrastructure.web.flask_app import FlaskWebApp

# Importar EventBus e strategy do sistema refactored
from refactored.core.event_bus import EventBus
from refactored.strategy.ghost import GhostMethodStrategy


class StandardLogger(Logger):
    """Adaptador para logging padrão do Python."""
    
    def __init__(self, name: str = "clean_arch"):
        self._logger = logging.getLogger(name)
    
    def debug(self, msg: str) -> None:
        self._logger.debug(msg)
    
    def info(self, msg: str) -> None:
        self._logger.info(msg)
    
    def warning(self, msg: str) -> None:
        self._logger.warning(msg)
    
    def error(self, msg: str) -> None:
        self._logger.error(msg)


def create_app(config: dict = None) -> dict:
    """
    Composition Root — monta toda a aplicação.
    
    Retorna dict com todos os componentes wired.
    """
    config = config or {}
    
    # Logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = StandardLogger()
    
    # Infrastructure: Event Bus
    event_bus = EventBus()
    publisher = EventBusPublisherAdapter(event_bus)
    
    # Infrastructure: Database
    db_path = config.get('database', {}).get('path', 'data/trading_bot_clean.db')
    db_conn = SQLiteConnection(db_path)
    
    # Interface Adapters: Repositories
    candle_repo: CandleRepository = SQLiteCandleRepository(db_conn)
    trade_repo: TradeRepository = SQLiteTradeRepository(db_conn)
    signal_repo: SignalRepository = SQLiteSignalRepository(db_conn)
    
    # Interface Adapters: Gateway
    gateway = HyperliquidAPIGateway(config, paper_trading=True)
    
    # Infrastructure: Strategy Adapter
    raw_strategy = GhostMethodStrategy(config, event_bus=event_bus)
    strategy: StrategyPort = StrategyAdapter(raw_strategy)
    
    # Application: Use Cases
    fetch_uc = FetchMarketDataUseCase(gateway, publisher, logger)
    signal_uc = GenerateSignalUseCase(strategy, signal_repo, publisher, logger, fetch_uc)
    trade_uc = ExecuteTradeUseCase(gateway, trade_repo, publisher, logger, config)
    status_uc = GetPortfolioStatusUseCase(trade_repo, logger)
    
    # Interface Adapters: Controller
    controller = WebAPIController(fetch_uc, signal_uc, trade_uc, status_uc)
    
    # Infrastructure: Web App
    web_app = FlaskWebApp(controller, port=config.get('web', {}).get('port', 5000))
    
    return {
        'event_bus': event_bus,
        'publisher': publisher,
        'db_conn': db_conn,
        'candle_repo': candle_repo,
        'trade_repo': trade_repo,
        'signal_repo': signal_repo,
        'gateway': gateway,
        'strategy': strategy,
        'fetch_uc': fetch_uc,
        'signal_uc': signal_uc,
        'trade_uc': trade_uc,
        'status_uc': status_uc,
        'controller': controller,
        'web_app': web_app,
        'logger': logger,
    }


def main():
    """Entry point."""
    app = create_app()
    print("=" * 60)
    print("  🏛️  CLEAN ARCHITECTURE — Hyperliquid Bot")
    print("=" * 60)
    print("  Componentes inicializados:")
    for name in app:
        print(f"    ✓ {name}")
    print("=" * 60)
    print("  A iniciar servidor web...")
    app['web_app'].run()


if __name__ == "__main__":
    main()
