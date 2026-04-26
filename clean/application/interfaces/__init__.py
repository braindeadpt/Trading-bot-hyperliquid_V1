"""Application layer interfaces — define how application talks to outside world."""
from abc import ABC, abstractmethod
from typing import Callable
from ...domain.events import DomainEvent


class EventPublisher(ABC):
    """Porta para publicação de eventos."""
    
    @abstractmethod
    def publish(self, event: DomainEvent) -> None:
        """Publica evento de domínio."""
        pass
    
    @abstractmethod
    def subscribe(self, event_type: str, handler: Callable) -> None:
        """Subscreve handler para tipo de evento."""
        pass


class Logger(ABC):
    """Porta para logging — permite mock em tests."""
    
    @abstractmethod
    def debug(self, msg: str) -> None:
        pass
    
    @abstractmethod
    def info(self, msg: str) -> None:
        pass
    
    @abstractmethod
    def warning(self, msg: str) -> None:
        pass
    
    @abstractmethod
    def error(self, msg: str) -> None:
        pass


class StrategyPort(ABC):
    """Porta para estratégia — permite trocar estratégias sem alterar use cases."""
    
    @abstractmethod
    def analyze(self, market_data: dict, price: float) -> dict:
        """Analisa dados de mercado e retorna sinal dict."""
        pass
    
    @abstractmethod
    def get_required_data(self) -> list:
        """Lista de campos necessários."""
        pass
