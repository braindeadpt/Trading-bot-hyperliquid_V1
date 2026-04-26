"""
ServiceContainer — Injeção de Dependências + Singletons Thread-Safe.
Garante uma única instância de cada serviço, partilhada por todos os componentes.
"""
import threading
import logging
from typing import Any, Dict, Optional, Callable

logger = logging.getLogger(__name__)


class ServiceContainer:
    """
    Container de Injeção de Dependências.
    
    Resolve os problemas do legado:
    - DataAggregator instanciado 3x → 1x
    - BotDatabase instanciado 5x → 1x  
    - Sem estado global, tudo injetado
    """
    
    def __init__(self, config: Dict[str, Any], event_bus=None):
        self._config = config
        self._event_bus = event_bus
        self._services: Dict[str, Any] = {}
        self._factories: Dict[str, Callable] = {}
        self._lock = threading.RLock()
        self._booted = False
    
    def register(self, name: str, factory: Callable) -> 'ServiceContainer':
        """Regista factory lazy para um serviço."""
        self._factories[name] = factory
        return self
    
    def get(self, name: str) -> Any:
        """Obtém serviço (lazy initialization thread-safe)."""
        if name not in self._services:
            with self._lock:
                if name not in self._services:  # Double-check
                    if name not in self._factories:
                        raise KeyError(f"Serviço '{name}' não registado no container")
                    try:
                        self._services[name] = self._factories[name](self._config, self)
                        logger.info(f"[Container] Serviço inicializado: {name}")
                    except Exception as e:
                        logger.error(f"[Container] Erro a inicializar {name}: {e}")
                        raise
        return self._services[name]
    
    def has(self, name: str) -> bool:
        """Verifica se serviço existe."""
        return name in self._services or name in self._factories
    
    def boot(self) -> 'ServiceContainer':
        """
        Bootstrap: regista todas as factories padrão do bot.
        Chamado uma vez no arranque.
        """
        if self._booted:
            return self
        
        logger.info("[Container] Booting services...")
        
        # Registar factories (lazy — só criam quando get() é chamado)
        self.register('event_bus', lambda cfg, ctn: self._event_bus or self._create_event_bus())
        self.register('database', lambda cfg, ctn: self._create_database(cfg))
        self.register('cache', lambda cfg, ctn: self._create_cache(cfg))
        self.register('api_client', lambda cfg, ctn: self._create_api_client(cfg))
        self.register('aggregator', lambda cfg, ctn: self._create_aggregator(cfg, ctn))
        self.register('strategy', lambda cfg, ctn: self._create_strategy(cfg, ctn))
        self.register('risk_manager', lambda cfg, ctn: self._create_risk_manager(cfg, ctn))
        self.register('trader', lambda cfg, ctn: self._create_trader(cfg, ctn))
        
        self._booted = True
        logger.info("[Container] Boot completo. Serviços registados:")
        for name in self._factories:
            logger.info(f"  • {name}")
        
        return self
    
    # ─── Factories Privadas ──────────────────────────────────
    
    def _create_event_bus(self):
        from .event_bus import EventBus
        return EventBus()
    
    def _create_database(self, cfg):
        from ..data.database import BotDatabase
        db_path = cfg.get('database', {}).get('path', 'data/trading_bot.db')
        return BotDatabase(db_path)
    
    def _create_cache(self, cfg):
        from ..data.cache import DataCache
        return DataCache(ttl_seconds=cfg.get('cache', {}).get('ttl', 10))
    
    def _create_api_client(self, cfg):
        from ..api.hyperliquid_client import HyperliquidClient
        paper = cfg.get('bot', {}).get('paper_trading', True)
        return HyperliquidClient(cfg, paper_trading=paper)
    
    def _create_aggregator(self, cfg, ctn):
        from ..data.aggregator import DataAggregator
        return DataAggregator(cfg, cache=ctn.get('cache'), event_bus=ctn.get('event_bus'))
    
    def _create_strategy(self, cfg, ctn):
        from ..strategy.ghost import GhostMethodStrategy
        return GhostMethodStrategy(cfg, event_bus=ctn.get('event_bus'))
    
    def _create_risk_manager(self, cfg, ctn):
        from ..execution.risk import RiskManager
        return RiskManager(cfg, database=ctn.get('database'))
    
    def _create_trader(self, cfg, ctn):
        from ..execution.trader import PaperTrader
        return PaperTrader(
            config=cfg,
            api_client=ctn.get('api_client'),
            strategy=ctn.get('strategy'),
            risk_manager=ctn.get('risk_manager'),
            database=ctn.get('database'),
            event_bus=ctn.get('event_bus')
        )
    
    # ─── Acesso Direto (conveniência) ────────────────────────
    
    @property
    def event_bus(self):
        return self.get('event_bus')
    
    @property
    def database(self):
        return self.get('database')
    
    @property
    def cache(self):
        return self.get('cache')
    
    @property
    def aggregator(self):
        return self.get('aggregator')
    
    @property
    def strategy(self):
        return self.get('strategy')
    
    @property
    def risk_manager(self):
        return self.get('risk_manager')
    
    @property
    def trader(self):
        return self.get('trader')
    
    @property
    def api_client(self):
        return self.get('api_client')
