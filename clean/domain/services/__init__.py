"""Domain service interfaces (ports) — define what the domain needs from external systems."""
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from ..entities import MarketSnapshot


class MarketDataProvider(ABC):
    """Porta para fornecedor de dados de mercado."""
    
    @abstractmethod
    def get_snapshot(self, asset: str) -> Optional[MarketSnapshot]:
        """Retorna snapshot atual do mercado."""
        pass
    
    @abstractmethod
    def get_candles(self, asset: str, interval: str, limit: int) -> List[Dict]:
        """Retorna candles OHLCV."""
        pass


class ExchangeGateway(ABC):
    """Porta para execução de ordens na exchange."""
    
    @abstractmethod
    def place_order(self, asset: str, direction: str, size: float,
                    price: float = None, order_type: str = "market") -> Dict:
        """Coloca ordem. Retorna resultado da execução."""
        pass
    
    @abstractmethod
    def get_balance(self) -> float:
        """Retorna saldo disponível."""
        pass
    
    @abstractmethod
    def is_healthy(self) -> bool:
        """Verifica se a exchange está acessível."""
        pass
