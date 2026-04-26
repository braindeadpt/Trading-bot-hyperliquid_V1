"""
RiskManager — Gestão de risco adaptativa.
Valida trades antes de execução, gere limites diários.
"""
import logging
from typing import Dict, Optional

from ..data.database import BotDatabase
from ..strategy.base import Signal

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Gestor de risco com regras adaptativas.
    
    Regras:
    - Daily loss limit (soft + hard)
    - Adaptive leverage baseado em funding/volatilidade
    - Cooldown entre trades
    - Max trades por dia
    """
    
    def __init__(self, config: Dict, database: BotDatabase = None):
        self.config = config.get('risk', {})
        self.db = database
        
        self.max_daily_trades = self.config.get('max_daily_trades', 5)
        self.daily_loss_soft = self.config.get('daily_loss_limit_pct', 0.05)
        self.daily_loss_hard = self.config.get('daily_loss_hard_stop_pct', 0.10)
        
        # Adaptive leverage
        self.adaptive = self.config.get('adaptive_leverage', True)
        self.low_leverage_cond = self.config.get('adaptive_conditions', {}).get('low_leverage', {})
        self.high_leverage_cond = self.config.get('adaptive_conditions', {}).get('high_leverage', {})
    
    def allow_trade(self, signal: Signal, daily_pnl: float) -> bool:
        """
        Valida se trade deve ser executado.
        
        Returns:
            True se trade é permitido
        """
        # Circuit breaker
        capital = self.config.get('initial_capital', 10000)
        daily_return = daily_pnl / capital
        
        if daily_return <= -self.daily_loss_hard:
            logger.warning("[Risk] HARD STOP — trading bloqueado")
            return False
        
        if daily_return <= -self.daily_loss_soft:
            logger.warning("[Risk] SOFT STOP — trading bloqueado")
            return False
        
        # Confiança mínima
        if signal.confidence < 0.5:
            logger.debug("[Risk] Confiança insuficiente")
            return False
        
        return True
    
    def calculate_leverage(self, funding_rate: float, volatility: float = None,
                           streak_losses: int = 0) -> int:
        """Calcula leverage adaptativo."""
        if not self.adaptive:
            return self.config.get('max_leverage', 3)
        
        base = self.config.get('max_leverage', 3)
        
        # Condições de risco → reduzir
        low_funding = self.low_leverage_cond.get('funding_threshold', 0.0005)
        low_vol = self.low_leverage_cond.get('volatility_threshold', 0.06)
        
        if funding_rate > low_funding:
            return 2
        if volatility and volatility > low_vol:
            return 2
        if streak_losses >= self.low_leverage_cond.get('streak_threshold', 3):
            return 2
        
        # Condições favoráveis → aumentar
        high_funding = self.high_leverage_cond.get('funding_threshold', -0.0002)
        high_vol = self.high_leverage_cond.get('volatility_threshold', 0.03)
        
        if funding_rate < high_funding:
            return 5
        if volatility and volatility < high_vol:
            return 5
        
        return base
    
    def calculate_position_size(self, capital: float, leverage: int,
                                confidence: float) -> float:
        """Calcula tamanho da posição baseado em Kelly simplificado."""
        max_size = self.config.get('max_position_size_usd', 100)
        base_size = min(capital * 0.1 * confidence, max_size)
        return base_size
