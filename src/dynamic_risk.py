"""
Dynamic SL/TP System v1.0
- SL apertado na entrada (2-3%)
- Breakeven @ +1%
- Trailing dinâmico baseado em ATR (folga aumenta com lucro)
- Integrado com RiskManager

AUTOR: Risk Manager Agent (Auditoria 2026-04-27)
"""
import logging
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime, date

logger = logging.getLogger(__name__)


@dataclass
class DynamicPosition:
    """Representa uma posição com SL/TP dinâmico"""
    asset: str
    side: str  # 'long' ou 'short'
    entry_price: float
    entry_time: datetime
    position_size_usd: float
    leverage: float
    
    # Níveis configuráveis
    initial_sl_pct: float = 0.03  # SL inicial 3%
    breakeven_trigger_pct: float = 0.01  # Breakeven @ +1%
    take_profit_pct: float = 0.10  # TP 10%
    
    # Estado interno
    max_price: float = 0.0
    min_price: float = 0.0
    current_sl_price: float = 0.0
    current_tp_price: float = 0.0
    trailing_active: bool = False
    breakeven_active: bool = False
    
    # Níveis atingidos
    highest_gain_pct: float = 0.0
    exit_reason: Optional[str] = None
    
    def __post_init__(self):
        if self.side == 'long':
            self.max_price = self.entry_price
            self.current_sl_price = self.entry_price * (1 - self.initial_sl_pct)
            self.current_tp_price = self.entry_price * (1 + self.take_profit_pct)
        else:
            self.min_price = self.entry_price
            self.current_sl_price = self.entry_price * (1 + self.initial_sl_pct)
            self.current_tp_price = self.entry_price * (1 - self.take_profit_pct)
    
    def update_price(self, price: float):
        """Actualiza preço e recalcula níveis"""
        if self.side == 'long':
            if price > self.max_price:
                self.max_price = price
            self.highest_gain_pct = (self.max_price - self.entry_price) / self.entry_price
        else:
            if price < self.min_price:
                self.min_price = price
            self.highest_gain_pct = (self.entry_price - self.min_price) / self.entry_price
    
    def calculate_dynamic_trail(self, atr: float) -> float:
        """
        Calcula trailing stop dinâmico baseado em:
        - Nível de lucro actual
        - ATR (volatilidade)
        
        Folga aumenta com o lucro:
        - 0-1%: 3% fixo (sem trailing)
        - 1-3%: breakeven, folga = 0.5×ATR (mínimo 0.5%)
        - 3-5%: folga = 1.0×ATR (mínimo 0.8%)
        - 5-10%: folga = 1.5×ATR (mínimo 1.2%)
        - >10%: folga = 2.0×ATR (mínimo 1.5%)
        """
        gain = self.highest_gain_pct
        
        # Fase 1: SL fixo até breakeven trigger
        if gain < self.breakeven_trigger_pct:
            if self.side == 'long':
                return self.entry_price * (1 - self.initial_sl_pct)
            else:
                return self.entry_price * (1 + self.initial_sl_pct)
        
        # Fase 2: Breakeven com folga pequena
        if gain < 0.03:
            folga = max(atr * 0.5, self.entry_price * 0.005)  # Mínimo 0.5%
            if self.side == 'long':
                return max(self.entry_price, self.max_price - folga)
            else:
                return min(self.entry_price, self.min_price + folga)
        
        # Fase 3+: Trailing dinâmico com folga crescente
        if gain < 0.05:
            folga = max(atr * 1.0, self.entry_price * 0.008)  # Mínimo 0.8%
        elif gain < 0.10:
            folga = max(atr * 1.5, self.entry_price * 0.012)  # Mínimo 1.2%
        else:
            folga = max(atr * 2.0, self.entry_price * 0.015)  # Mínimo 1.5%
        
        if self.side == 'long':
            return self.max_price - folga
        else:
            return self.min_price + folga
    
    def check_exit(self, price: float, atr: float) -> Optional[Dict]:
        """
        Verifica se deve sair da posição.
        Retorna {'reason': str, 'pnl_pct': float, 'exit_price': float} ou None.
        """
        self.update_price(price)
        
        # Actualizar trailing stop dinâmico
        new_sl = self.calculate_dynamic_trail(atr)
        
        if self.side == 'long':
            # SL sobe, nunca desce
            if new_sl > self.current_sl_price:
                old_sl = self.current_sl_price
                self.current_sl_price = new_sl
                self.trailing_active = True
                if self.highest_gain_pct >= self.breakeven_trigger_pct:
                    self.breakeven_active = True
                logger.info(
                    f"📈 SL LONG: ${old_sl:,.2f} → ${self.current_sl_price:,.2f} "
                    f"(max: ${self.max_price:,.2f}, ATR: ${atr:,.2f}, gain: {self.highest_gain_pct*100:.1f}%)"
                )
            
            # Check SL
            if price <= self.current_sl_price:
                pnl = (price - self.entry_price) / self.entry_price
                if self.trailing_active and self.breakeven_active:
                    reason = 'DYNAMIC_TRAIL'
                elif self.trailing_active:
                    reason = 'TRAILING_STOP'
                else:
                    reason = 'STOP_LOSS'
                return {'reason': reason, 'pnl_pct': pnl, 'exit_price': price}
            
            # Check TP
            if price >= self.current_tp_price:
                pnl = (price - self.entry_price) / self.entry_price
                return {'reason': 'TAKE_PROFIT', 'pnl_pct': pnl, 'exit_price': price}
        
        else:  # short
            # SL desce, nunca sobe
            if new_sl < self.current_sl_price:
                old_sl = self.current_sl_price
                self.current_sl_price = new_sl
                self.trailing_active = True
                if self.highest_gain_pct >= self.breakeven_trigger_pct:
                    self.breakeven_active = True
                logger.info(
                    f"📉 SL SHORT: ${old_sl:,.2f} → ${self.current_sl_price:,.2f} "
                    f"(min: ${self.min_price:,.2f}, ATR: ${atr:,.2f}, gain: {self.highest_gain_pct*100:.1f}%)"
                )
            
            # Check SL
            if price >= self.current_sl_price:
                pnl = (self.entry_price - price) / self.entry_price
                if self.trailing_active and self.breakeven_active:
                    reason = 'DYNAMIC_TRAIL'
                elif self.trailing_active:
                    reason = 'TRAILING_STOP'
                else:
                    reason = 'STOP_LOSS'
                return {'reason': reason, 'pnl_pct': pnl, 'exit_price': price}
            
            # Check TP
            if price <= self.current_tp_price:
                pnl = (self.entry_price - price) / self.entry_price
                return {'reason': 'TAKE_PROFIT', 'pnl_pct': pnl, 'exit_price': price}
        
        return None
    
    def get_status(self) -> Dict:
        """Retorna estado actual da posição"""
        return {
            'side': self.side,
            'entry': self.entry_price,
            'current_sl': self.current_sl_price,
            'current_tp': self.current_tp_price,
            'max_price': self.max_price,
            'min_price': self.min_price,
            'highest_gain_pct': self.highest_gain_pct,
            'trailing_active': self.trailing_active,
            'breakeven_active': self.breakeven_active,
            'distance_to_sl_pct': abs(self.current_sl_price - self.entry_price) / self.entry_price,
        }


class ATRCalculator:
    """Calcula Average True Range a partir de candles"""
    
    @staticmethod
    def calculate(candles: List[Dict], period: int = 14) -> float:
        """
        Calcula ATR a partir de uma lista de candles.
        Candles devem ter: open, high, low, close
        """
        if len(candles) < period + 1:
            return 0.0
        
        tr_values = []
        
        for i in range(1, len(candles)):
            curr = candles[i]
            prev = candles[i - 1]
            
            high = curr.get('high', curr.get('close', 0))
            low = curr.get('low', curr.get('close', 0))
            prev_close = prev.get('close', 0)
            
            if high <= 0 or low <= 0 or prev_close <= 0:
                continue
            
            # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)
            
            tr = max(tr1, tr2, tr3)
            tr_values.append(tr)
        
        if len(tr_values) < period:
            return sum(tr_values) / len(tr_values) if tr_values else 0.0
        
        # Wilder's smoothing
        atr = sum(tr_values[:period]) / period
        for tr in tr_values[period:]:
            atr = ((atr * (period - 1)) + tr) / period
        
        return atr
    
    @staticmethod
    def calculate_pct(candles: List[Dict], period: int = 14) -> float:
        """ATR como percentagem do preço actual"""
        atr = ATRCalculator.calculate(candles, period)
        if candles:
            price = candles[-1].get('close', 0)
            return atr / price if price > 0 else 0.0
        return 0.0
    
    @staticmethod
    def from_price_list(prices: List[float], period: int = 14) -> float:
        """ATR simplificado a partir de lista de preços (usa apenas close-to-close)"""
        if len(prices) < period + 1:
            return 0.0
        
        # ATR simplificado: média das diferenças absolutas
        diffs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
        if len(diffs) < period:
            return sum(diffs) / len(diffs) if diffs else 0.0
        
        # Wilder's smoothing
        atr = sum(diffs[:period]) / period
        for d in diffs[period:]:
            atr = ((atr * (period - 1)) + d) / period
        
        return atr


class DynamicRiskManager:
    """
    Gestor de risco com SL/TP dinâmicos.
    Substitui a lógica fixa do risk_manager.py original.
    
    Regras:
    - SL inicial: 3% (mais apertado que 5% anterior)
    - Breakeven: Quando lucro atinge +1%, SL move para preço de entrada
    - Trailing dinâmico: Folga baseada em ATR, aumenta com lucro
    """
    
    def __init__(self, config: Dict):
        risk = config.get('risk', {})
        self.initial_capital = risk.get('initial_capital', 10000.0)
        self.current_capital = self.initial_capital
        self.max_position = risk.get('max_position_size_usd', 100)
        self.max_leverage = risk.get('max_leverage', 3)
        self.max_daily_trades = risk.get('max_daily_trades', 5)
        
        # Circuit breaker
        self.daily_loss_limit_pct = risk.get('daily_loss_limit_pct', 0.05)
        self.daily_loss_hard_stop_pct = risk.get('daily_loss_hard_stop_pct', 0.10)
        
        # Configurações dinâmicas
        self.initial_sl_pct = risk.get('dynamic_sl_pct', 0.03)  # 3% default
        self.breakeven_pct = risk.get('breakeven_pct', 0.01)  # 1% default
        self.take_profit_pct = risk.get('take_profit_pct', 0.10)  # 10% default
        
        # Estado
        self.active_position: Optional[DynamicPosition] = None
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.daily_date = date.today()
        self._circuit_tripped = False
        self._circuit_reason = None
        self._capital_at_start_of_day = self.initial_capital
        
        # ATR
        self.atr_period = 14
        self._candles_buffer: List[Dict] = []
        self._prices_buffer: List[float] = []
    
    def update_candles(self, candles: List[Dict]):
        """Actualiza buffer de candles para cálculo de ATR"""
        self._candles_buffer = list(candles)[-50:] if len(candles) > 50 else list(candles)
        # Também guardar preços
        self._prices_buffer = [c.get('close', 0) for c in self._candles_buffer if c.get('close', 0) > 0]
    
    def get_atr(self) -> float:
        """Retorna ATR actual em preço absoluto"""
        if self._candles_buffer:
            return ATRCalculator.calculate(self._candles_buffer, self.atr_period)
        elif len(self._prices_buffer) >= self.atr_period + 1:
            return ATRCalculator.from_price_list(self._prices_buffer, self.atr_period)
        return 0.0
    
    def get_atr_pct(self) -> float:
        """Retorna ATR como percentagem do preço"""
        atr = self.get_atr()
        if self._prices_buffer:
            price = self._prices_buffer[-1]
            return atr / price if price > 0 else 0.0
        return 0.0
    
    def can_trade(self) -> bool:
        """Verifica se podemos abrir nova posição"""
        # Reset diário
        today = date.today()
        if today != self.daily_date:
            self.daily_date = today
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self._circuit_tripped = False
            self._circuit_reason = None
            self._capital_at_start_of_day = self.current_capital
            logger.info("🔄 Novo dia — contadores resetados")
        
        if self._circuit_tripped:
            return False
        
        if self.daily_trades >= self.max_daily_trades:
            logger.warning(f"Limite diário: {self.daily_trades}/{self.max_daily_trades}")
            return False
        
        if self.active_position is not None:
            return False  # Já em posição
        
        return True
    
    def enter_position(self, asset: str, side: str, price: float, 
                       size_usd: float, leverage: float) -> DynamicPosition:
        """Abre nova posição com SL/TP dinâmico"""
        atr = self.get_atr()
        atr_pct = self.get_atr_pct()
        
        pos = DynamicPosition(
            asset=asset,
            side=side,
            entry_price=price,
            entry_time=datetime.now(),
            position_size_usd=size_usd,
            leverage=leverage,
            initial_sl_pct=self.initial_sl_pct,
            breakeven_trigger_pct=self.breakeven_pct,
            take_profit_pct=self.take_profit_pct,
        )
        
        self.active_position = pos
        self.daily_trades += 1
        
        sl_distance = pos.initial_sl_pct * 100
        tp_distance = pos.take_profit_pct * 100
        
        logger.info(
            f"🎯 ENTER {side.upper()} {asset} @ ${price:,.2f} | "
            f"Size: ${size_usd:.2f} | Lev: {leverage}x | "
            f"SL: ${pos.current_sl_price:,.2f} (-{sl_distance:.1f}%) | "
            f"TP: ${pos.current_tp_price:,.2f} (+{tp_distance:.1f}%) | "
            f"ATR: ${atr:,.2f} ({atr_pct*100:.2f}%) | "
            f"Breakeven @ +{pos.breakeven_trigger_pct*100:.1f}%"
        )
        
        return pos
    
    def check_exit(self, price: float) -> Optional[Dict]:
        """Verifica se a posição activa deve sair"""
        if self.active_position is None:
            return None
        
        atr = self.get_atr()
        result = self.active_position.check_exit(price, atr)
        
        if result:
            # Actualizar capital e PnL
            pnl_usd = self.active_position.position_size_usd * result['pnl_pct']
            self.current_capital += pnl_usd
            self.daily_pnl += pnl_usd
            
            result['pnl_usd'] = pnl_usd
            result['capital'] = self.current_capital
            
            emoji = "✅" if pnl_usd > 0 else "❌"
            logger.info(
                f"{emoji} EXIT {self.active_position.side.upper()} {self.active_position.asset} | "
                f"Reason: {result['reason']} | "
                f"PnL: ${pnl_usd:+.2f} ({result['pnl_pct']*100:+.2f}%) | "
                f"Capital: ${self.current_capital:,.2f}"
            )
            
            self.active_position = None
            
            # Verificar circuit breaker após saída
            self._check_circuit_breaker()
        
        return result
    
    def _check_circuit_breaker(self) -> bool:
        """Verifica se atingimos limite de perda diária (baseado no capital do início do dia)"""
        if self._capital_at_start_of_day <= 0:
            return False
        
        # Perda = negative PnL (daily_pnl negativo = perda)
        daily_loss = -self.daily_pnl if self.daily_pnl < 0 else 0
        daily_loss_pct = daily_loss / self._capital_at_start_of_day
        
        if daily_loss_pct >= self.daily_loss_hard_stop_pct:
            self._circuit_tripped = True
            self._circuit_reason = f"HARD STOP: Perda diária {daily_loss_pct*100:.1f}% >= {self.daily_loss_hard_stop_pct*100:.1f}%"
            logger.critical(f"🛑 {self._circuit_reason} — BOT PARADO")
            return True
        
        if daily_loss_pct >= self.daily_loss_limit_pct:
            self._circuit_tripped = True
            self._circuit_reason = f"SOFT STOP: Perda diária {daily_loss_pct*100:.1f}% >= {self.daily_loss_limit_pct*100:.1f}%"
            logger.critical(f"🛑 {self._circuit_reason} — BOT PARADO")
            return True
        
        return False
    
    def get_position_status(self) -> Optional[Dict]:
        """Retorna estado da posição activa"""
        if self.active_position:
            return self.active_position.get_status()
        return None
    
    def get_risk_metrics(self) -> Dict:
        """Retorna métricas de risco actuais"""
        atr_pct = self.get_atr_pct()
        pos_status = self.get_position_status()
        
        return {
            'daily_trades': self.daily_trades,
            'max_daily_trades': self.max_daily_trades,
            'daily_pnl': self.daily_pnl,
            'daily_loss_limit_pct': self.daily_loss_limit_pct,
            'circuit_tripped': self._circuit_tripped,
            'circuit_reason': self._circuit_reason,
            'capital': self.current_capital,
            'capital_at_day_start': self._capital_at_start_of_day,
            'atr_pct': atr_pct,
            'atr_14': self.get_atr(),
            'position_status': pos_status,
        }
    
    def calculate_adaptive_leverage(self, side: str, funding: float, 
                                    regime: str, recent_pnls: List[float]) -> int:
        """
        Calcula leverage adaptativa baseada em:
        - ATR (volatilidade)
        - Funding rate
        - Regime de mercado
        - Streak de perdas
        """
        atr_pct = self.get_atr_pct()
        base = self.max_leverage  # Default 3x
        
        low_factors = 0
        high_factors = 0
        
        # === CONDIÇÕES PARA LEVERAGE BAIXA (2x) ===
        
        # Funding caro (positivo alto)
        if funding > 0.0005:  # 0.05%
            low_factors += 1
        
        # Volatilidade alta (ATR > 5%)
        if atr_pct > 0.05:
            low_factors += 1
        
        # Streak de perdas (últimos 5 trades)
        loss_streak = 0
        for pnl in reversed(recent_pnls[-5:]):
            if pnl < 0:
                loss_streak += 1
            else:
                break
        if loss_streak >= 3:
            low_factors += 1
        
        # Regime contra a posição
        if (regime == 'bear' and side == 'long') or (regime == 'bull' and side == 'short'):
            low_factors += 1
        
        if low_factors >= 2:
            return 2
        
        # === CONDIÇÕES PARA LEVERAGE ALTA (5x) ===
        
        # Funding negativo (pagam para segurar)
        if funding < -0.0002:  # -0.02%
            high_factors += 1
        
        # Volatilidade baixa (ATR < 3%)
        if 0 < atr_pct < 0.03:
            high_factors += 1
        
        # Regime a favor
        if (regime == 'bull' and side == 'long') or (regime == 'bear' and side == 'short'):
            high_factors += 1
        
        if high_factors >= 2:
            return 5
        
        return base  # 3x default
