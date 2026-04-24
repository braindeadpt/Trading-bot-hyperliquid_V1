"""
Gestão de risco
"""
import logging
from typing import Dict
from datetime import datetime, date

logger = logging.getLogger(__name__)


class RiskManager:
    """Controla tamanho de posição, stops e limites diários"""
    
    def __init__(self, config: Dict):
        risk = config.get('risk', {})
        self.max_position = risk.get('max_position_size_usd', 100)
        self.max_leverage = risk.get('max_leverage', 2)
        self.stop_loss_pct = risk.get('stop_loss_pct', 0.02)
        self.max_daily_trades = risk.get('max_daily_trades', 5)
        
        # ⚡ CIRCUIT BREAKER — Perda diária máxima
        self.daily_loss_limit_pct = risk.get('daily_loss_limit_pct', 0.05)  # 5% default
        self.daily_loss_hard_stop_pct = risk.get('daily_loss_hard_stop_pct', 0.10)  # 10% hard stop
        
        self.daily_trades = 0
        self.daily_pnl = 0.0  # PnL acumulado do dia
        self.daily_date = date.today()
        self.positions = {}  # asset -> position info
        self._circuit_tripped = False  # True = parado por circuit breaker
        self._circuit_reason = None
    
    def can_trade(self) -> bool:
        """Verifica se podemos abrir nova posição"""
        if self.daily_trades >= self.max_daily_trades:
            logger.warning(f"Limite diário de trades atingido: {self.daily_trades}/{self.max_daily_trades}")
            return False
        return True
    
    def calculate_position_size(self, price: float, confidence: float = 1.0) -> float:
        """Calcula tamanho da posição em USD"""
        # Validar confidence
        if not isinstance(confidence, (int, float)):
            confidence = 1.0
        confidence = max(0.0, min(1.0, float(confidence)))
        
        # Tamanho base limitado
        size = min(self.max_position * confidence, self.max_position)
        
        # Em paper trading, podemos ser mais conservadores
        logger.info(f"Tamanho de posição calculado: ${size:.2f} (confiança: {confidence:.2f})")
        return size
    
    def check_stop_loss(self, entry_price: float, current_price: float, direction: str) -> bool:
        """Verifica se stop loss foi atingido"""
        if entry_price <= 0:
            logger.warning("Entry price inválido (<=0) — não posso calcular stop loss")
            return False
        
        if direction == 'long':
            loss_pct = (entry_price - current_price) / entry_price
        else:
            loss_pct = (current_price - entry_price) / entry_price
        
        if loss_pct >= self.stop_loss_pct:
            logger.warning(f"[STOP] STOP LOSS! Perda: {loss_pct*100:.2f}%")
            return True
        
        return False
    
    def check_circuit_breaker(self, current_capital: float, initial_capital: float) -> bool:
        """
        ⚡ CIRCUIT BREAKER — Verifica se atingimos o limite de perda diária.
        Retorna True se trading deve PARAR.
        """
        # Reset diário
        today = date.today()
        if today != self.daily_date:
            self.daily_date = today
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self._circuit_tripped = False
            self._circuit_reason = None
            logger.info("🔄 Novo dia — circuit breaker resetado")
        
        if self._circuit_tripped:
            return True
        
        # Calcular perda do dia em percentagem
        if initial_capital > 0:
            daily_loss_pct = (initial_capital - current_capital) / initial_capital
        else:
            daily_loss_pct = 0
        
        # Hard stop (10%) — para IMEDIATAMENTE
        if daily_loss_pct >= self.daily_loss_hard_stop_pct:
            self._circuit_tripped = True
            self._circuit_reason = f"HARD STOP: Perda diária {daily_loss_pct*100:.1f}% >= {self.daily_loss_hard_stop_pct*100:.1f}%"
            logger.critical(f"🛑 {self._circuit_reason}")
            logger.critical("🛑 BOT PARADO — Circuit breaker ativado!")
            return True
        
        # Soft stop (5%) — avisa mas permite recuperação
        if daily_loss_pct >= self.daily_loss_limit_pct:
            self._circuit_tripped = True
            self._circuit_reason = f"SOFT STOP: Perda diária {daily_loss_pct*100:.1f}% >= {self.daily_loss_limit_pct*100:.1f}%"
            logger.critical(f"🛑 {self._circuit_reason}")
            logger.critical("🛑 BOT PARADO — Circuit breaker ativado!")
            return True
        
        return False
    
    def get_circuit_status(self) -> Dict:
        """Retorna estado do circuit breaker"""
        return {
            'tripped': self._circuit_tripped,
            'reason': self._circuit_reason,
            'daily_loss_limit_pct': self.daily_loss_limit_pct,
            'daily_loss_hard_stop_pct': self.daily_loss_hard_stop_pct,
            'daily_pnl': self.daily_pnl,
        }
    
    def record_trade(self):
        """Regista uma trade executada"""
        self.daily_trades += 1
        logger.info(f"Trades hoje: {self.daily_trades}/{self.max_daily_trades}")
