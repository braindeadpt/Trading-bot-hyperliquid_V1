"""
Gestão de risco
"""
import logging
from typing import Dict

logger = logging.getLogger(__name__)


class RiskManager:
    """Controla tamanho de posição, stops e limites diários"""
    
    def __init__(self, config: Dict):
        self.max_position = config['risk']['max_position_size_usd']
        self.max_leverage = config['risk']['max_leverage']
        self.stop_loss_pct = config['risk']['stop_loss_pct']
        self.max_daily_trades = config['risk']['max_daily_trades']
        
        self.daily_trades = 0
        self.positions = {}  # asset -> position info
    
    def can_trade(self) -> bool:
        """Verifica se podemos abrir nova posição"""
        if self.daily_trades >= self.max_daily_trades:
            logger.warning(f"Limite diário de trades atingido: {self.daily_trades}/{self.max_daily_trades}")
            return False
        return True
    
    def calculate_position_size(self, price: float, confidence: float = 1.0) -> float:
        """Calcula tamanho da posição em USD"""
        # Tamanho base limitado
        size = min(self.max_position * confidence, self.max_position)
        
        # Em paper trading, podemos ser mais conservadores
        logger.info(f"Tamanho de posição calculado: ${size:.2f} (confiança: {confidence:.2f})")
        return size
    
    def check_stop_loss(self, entry_price: float, current_price: float, direction: str) -> bool:
        """Verifica se stop loss foi atingido"""
        if direction == 'long':
            loss_pct = (entry_price - current_price) / entry_price
        else:
            loss_pct = (current_price - entry_price) / entry_price
        
        if loss_pct >= self.stop_loss_pct:
            logger.warning(f"[STOP] STOP LOSS! Perda: {loss_pct*100:.2f}%")
            return True
        
        return False
    
    def record_trade(self):
        """Regista uma trade executada"""
        self.daily_trades += 1
        logger.info(f"Trades hoje: {self.daily_trades}/{self.max_daily_trades}")
