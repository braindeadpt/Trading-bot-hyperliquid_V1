"""
Strategy Adapter — adapta a estratégia concreta (GhostMethod) para a porta StrategyPort.
"""
from clean.application.interfaces import StrategyPort


class StrategyAdapter(StrategyPort):
    """Adapta BaseStrategy concreta para StrategyPort."""
    
    def __init__(self, strategy_instance):
        self._strategy = strategy_instance
    
    def analyze(self, market_data: dict, price: float) -> dict:
        signal = self._strategy.analyze(market_data, price)
        if not signal:
            return {"type": "HOLD", "confidence": 0}
        return {
            "type": signal.type,
            "confidence": signal.confidence,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "reason": signal.reason,
            "metadata": signal.metadata
        }
    
    def get_required_data(self) -> list:
        return self._strategy.get_required_data()
