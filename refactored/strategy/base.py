"""
BaseStrategy — Classe base abstrata para todas as estratégias.
Permite trocar estratégias sem alterar o PaperTrader.
"""
from abc import ABC, abstractmethod
from typing import Dict, Optional, Any, List


class Signal:
    """Sinal de trading tipado."""
    
    def __init__(self, 
                 signal_type: str,  # LONG, SHORT, EXIT, HOLD
                 confidence: float = 1.0,
                 entry_price: float = None,
                 stop_loss: float = None,
                 take_profit: float = None,
                 reason: str = "",
                 metadata: Dict[str, Any] = None):
        self.type = signal_type
        self.confidence = confidence
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.reason = reason
        self.metadata = metadata or {}
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'type': self.type,
            'confidence': self.confidence,
            'entry_price': self.entry_price,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'reason': self.reason,
            'metadata': self.metadata
        }
    
    @classmethod
    def hold(cls, reason: str = ""):
        return cls("HOLD", confidence=0, reason=reason)
    
    @classmethod
    def exit(cls, reason: str = ""):
        return cls("EXIT", confidence=1.0, reason=reason)


class BaseStrategy(ABC):
    """
    Interface abstrata para estratégias de trading.
    
    Todas as estratégias devem implementar:
    - analyze(): recebe dados de mercado, retorna Signal
    - get_required_data(): lista de dados necessários
    """
    
    def __init__(self, config: Dict, event_bus=None):
        self.config = config
        self.event_bus = event_bus
        self.strategy_config = config.get('strategy', {})
    
    @abstractmethod
    def analyze(self, market_data: Dict, price: float) -> Optional[Signal]:
        """
        Analisa dados de mercado e retorna sinal.
        
        Args:
            market_data: Dict com dados agregados (OI, funding, volume, etc)
            price: Preço atual do asset
        
        Returns:
            Signal com direção e metadados, ou None se sem sinal
        """
        pass
    
    def get_required_data(self) -> List[str]:
        """Lista de campos necessários em market_data."""
        return ['price', 'oi_total', 'funding_avg']
    
    def validate_data(self, market_data: Dict) -> bool:
        """Valida se dados têm todos os campos necessários."""
        required = self.get_required_data()
        return all(k in market_data for k in required)
    
    def _notify(self, event_type: str, payload: Dict):
        """Publica evento se event_bus disponível."""
        if self.event_bus:
            self.event_bus.publish(event_type, payload, source=self.__class__.__name__)
