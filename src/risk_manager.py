"""
Gestão de risco — Wrapper para DynamicRiskManager

Este ficheiro mantém compatibilidade com código existente que importa RiskManager,
mas delega toda a lógica para o DynamicRiskManager (novo sistema).

AUTOR: Risk Manager Agent (Auditoria 2026-04-27)
MIGRAÇÃO: RiskManager → DynamicRiskManager
"""
import logging
from typing import Dict, Optional
from datetime import datetime, date

from dynamic_risk import DynamicRiskManager, DynamicPosition, ATRCalculator

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Wrapper compatível com a API antiga do RiskManager.
    Delega toda a lógica para DynamicRiskManager.
    
    ⚠️ DEPRECATED: Usar DynamicRiskManager directamente no novo código.
    """
    
    def __init__(self, config: Dict):
        self._dynamic = DynamicRiskManager(config)
        
        # Compatibilidade: expor atributos antigos
        risk = config.get('risk', {})
        self.max_position = risk.get('max_position_size_usd', 100)
        self.max_leverage = risk.get('max_leverage', 2)
        self.stop_loss_pct = risk.get('stop_loss_pct', 0.05)
        self.max_daily_trades = risk.get('max_daily_trades', 5)
        self.daily_loss_limit_pct = risk.get('daily_loss_limit_pct', 0.05)
        self.daily_loss_hard_stop_pct = risk.get('daily_loss_hard_stop_pct', 0.10)
        
        # Estado antigo (mantido para compatibilidade)
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.daily_date = date.today()
        self.positions = {}
        self._circuit_tripped = False
        self._circuit_reason = None
    
    def can_trade(self) -> bool:
        """Delega para DynamicRiskManager"""
        return self._dynamic.can_trade()
    
    def calculate_position_size(self, price: float, confidence: float = 1.0) -> float:
        """Mantém API antiga — delega cálculo"""
        if not isinstance(confidence, (int, float)):
            confidence = 1.0
        confidence = max(0.0, min(1.0, float(confidence)))
        size = min(self.max_position * confidence, self.max_position)
        logger.info(f"Tamanho de posição calculado: ${size:.2f} (confiança: {confidence:.2f})")
        return size
    
    def check_stop_loss(self, entry_price: float, current_price: float, direction: str) -> bool:
        """
        API antiga — agora usa SL dinâmico.
        ⚠️ Não é mais usada pelo PaperTrader moderno.
        """
        if entry_price <= 0:
            logger.warning("Entry price inválido — não posso calcular stop loss")
            return False
        
        # Usar SL dinâmico se disponível
        if self._dynamic.active_position:
            result = self._dynamic.check_exit(current_price)
            if result:
                return True
        
        # Fallback: cálculo antigo
        if direction == 'long':
            loss_pct = (entry_price - current_price) / entry_price
        else:
            loss_pct = (current_price - entry_price) / entry_price
        
        return loss_pct >= self.stop_loss_pct
    
    def check_circuit_breaker(self, current_capital: float, initial_capital: float) -> bool:
        """
        API antiga — agora usa cálculo correcto de PnL diário.
        """
        # Reset diário
        today = date.today()
        if today != self.daily_date:
            self.daily_date = today
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self._circuit_tripped = False
            self._circuit_reason = None
            self._dynamic.daily_date = today
            self._dynamic.daily_pnl = 0.0
            self._dynamic.daily_trades = 0
            self._dynamic._circuit_tripped = False
            self._dynamic._circuit_reason = None
            self._dynamic._capital_at_start_of_day = current_capital
            logger.info("🔄 Novo dia — circuit breaker resetado")
        
        if self._circuit_tripped:
            return True
        
        # Usar cálculo correcto do DynamicRiskManager
        if self._dynamic._capital_at_start_of_day > 0:
            daily_loss = -self._dynamic.daily_pnl if self._dynamic.daily_pnl < 0 else 0
            daily_loss_pct = daily_loss / self._dynamic._capital_at_start_of_day
            
            if daily_loss_pct >= self.daily_loss_hard_stop_pct:
                self._circuit_tripped = True
                self._circuit_reason = f"HARD STOP: Perda diária {daily_loss_pct*100:.1f}%"
                logger.critical(f"🛑 {self._circuit_reason}")
                return True
            
            if daily_loss_pct >= self.daily_loss_limit_pct:
                self._circuit_tripped = True
                self._circuit_reason = f"SOFT STOP: Perda diária {daily_loss_pct*100:.1f}%"
                logger.critical(f"🛑 {self._circuit_reason}")
                return True
        
        return False
    
    def get_circuit_status(self) -> Dict:
        """Retorna estado do circuit breaker"""
        return {
            'tripped': self._circuit_tripped or self._dynamic._circuit_tripped,
            'reason': self._circuit_reason or self._dynamic._circuit_reason,
            'daily_loss_limit_pct': self.daily_loss_limit_pct,
            'daily_loss_hard_stop_pct': self.daily_loss_hard_stop_pct,
            'daily_pnl': self._dynamic.daily_pnl,
        }
    
    def record_trade(self):
        """Regista uma trade executada"""
        self.daily_trades += 1
        self._dynamic.daily_trades = self.daily_trades
        logger.info(f"Trades hoje: {self.daily_trades}/{self.max_daily_trades}")
    
    # ========== NOVOS MÉTODOS (DynamicRiskManager) ==========
    
    def get_dynamic_risk(self) -> DynamicRiskManager:
        """Retorna o DynamicRiskManager subjacente"""
        return self._dynamic
    
    def update_candles(self, candles):
        """Actualiza candles para cálculo de ATR"""
        self._dynamic.update_candles(candles)
    
    def enter_position(self, asset: str, side: str, price: float, 
                       size_usd: float, leverage: float) -> DynamicPosition:
        """Abre posição com SL/TP dinâmico"""
        return self._dynamic.enter_position(asset, side, price, size_usd, leverage)
    
    def check_exit(self, price: float) -> Optional[Dict]:
        """Verifica saída da posição activa"""
        return self._dynamic.check_exit(price)
    
    def get_position_status(self) -> Optional[Dict]:
        """Retorna estado da posição"""
        return self._dynamic.get_position_status()
    
    def get_risk_metrics(self) -> Dict:
        """Retorna métricas de risco"""
        return self._dynamic.get_risk_metrics()
    
    def calculate_adaptive_leverage(self, side: str, funding: float, 
                                    regime: str, recent_pnls: list) -> int:
        """Calcula leverage adaptativa"""
        return self._dynamic.calculate_adaptive_leverage(side, funding, regime, recent_pnls)
