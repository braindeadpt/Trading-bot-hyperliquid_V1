"""Domain repository interfaces (ports) — define what the domain needs, not how."""
from abc import ABC, abstractmethod
from typing import List, Optional
from ..entities import Candle, Trade, Signal


class CandleRepository(ABC):
    """Porta para persistência de candles."""
    
    @abstractmethod
    def save(self, candles: List[Candle]) -> None:
        """Guarda candles (batch)."""
        pass
    
    @abstractmethod
    def get_recent(self, symbol: str, interval: str, limit: int) -> List[Candle]:
        """Retorna candles mais recentes."""
        pass
    
    @abstractmethod
    def get_count(self, symbol: str = None) -> int:
        """Contagem total."""
        pass


class TradeRepository(ABC):
    """Porta para persistência de trades."""
    
    @abstractmethod
    def save(self, trade: Trade) -> int:
        """Guarda trade, retorna ID."""
        pass
    
    @abstractmethod
    def get_open(self, symbol: str = None) -> Optional[Trade]:
        """Retorna trade aberto."""
        pass
    
    @abstractmethod
    def get_recent(self, symbol: str = None, limit: int = 100) -> List[Trade]:
        """Retorna trades recentes."""
        pass
    
    @abstractmethod
    def update_exit(self, trade_id: int, exit_price: float,
                    exit_time: int, pnl_usd: float, pnl_pct: float,
                    reason: str) -> None:
        """Atualiza saída de trade."""
        pass
    
    @abstractmethod
    def get_count(self, symbol: str = None) -> int:
        """Contagem total."""
        pass


class SignalRepository(ABC):
    """Porta para persistência de sinais."""
    
    @abstractmethod
    def save(self, signal: Signal) -> None:
        """Guarda sinal."""
        pass
    
    @abstractmethod
    def get_recent(self, asset: str = None, limit: int = 100) -> List[Signal]:
        """Retorna sinais recentes."""
        pass
    
    @abstractmethod
    def get_count(self, asset: str = None) -> int:
        """Contagem total."""
        pass
