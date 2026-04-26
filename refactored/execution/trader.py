"""
PaperTrader — Execução de trades (paper + real).
Integra com API, risk manager, database e event bus.
"""
import time
import logging
from typing import Dict, Optional, Any
from dataclasses import dataclass, field

from ..api.hyperliquid_client import HyperliquidClient
from ..strategy.base import Signal
from .risk import RiskManager
from ..data.database import BotDatabase
from ..core.event_bus import EventBus

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Posição aberta."""
    asset: str
    direction: str  # long / short
    entry_price: float
    size_usd: float
    leverage: float
    stop_loss: float
    take_profit: float
    entry_time: float
    trailing_stop: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = float('inf')


class PaperTrader:
    """
    Executor de trades com gestão de posição.
    
    Features:
    - Paper trading completo
    - Trailing stop adaptativo
    - Circuit breaker diário
    - Persistência em SQLite
    """
    
    def __init__(self, 
                 config: Dict,
                 api_client: HyperliquidClient,
                 strategy,
                 risk_manager: RiskManager,
                 database: BotDatabase,
                 event_bus: EventBus = None):
        self.config = config
        self.api = api_client
        self.strategy = strategy
        self.risk = risk_manager
        self.db = database
        self.event_bus = event_bus
        
        # Capital
        self.initial_capital = config.get('risk', {}).get('initial_capital', 10000.0)
        self.capital = self.initial_capital
        self.peak_capital = self.initial_capital
        
        # Posição
        self.position: Optional[Position] = None
        
        # Config
        risk_cfg = config.get('risk', {})
        self.max_position_usd = risk_cfg.get('max_position_size_usd', 100)
        self.max_leverage = risk_cfg.get('max_leverage', 3)
        self.stop_loss_pct = risk_cfg.get('stop_loss_pct', 0.035)
        self.trailing_activation = risk_cfg.get('trailing_activation_pct', 0.02)
        self.trailing_pct = risk_cfg.get('trailing_stop_pct', 0.02)
        self.daily_loss_limit = risk_cfg.get('daily_loss_limit_pct', 0.05)
        self.daily_loss_hard = risk_cfg.get('daily_loss_hard_stop_pct', 0.10)
        
        # Estado
        self.trade_count = 0
        self.daily_pnl = 0.0
        self.last_trade_day = None
        self._shutdown = False
    
    def run_cycle(self, asset: str) -> Dict[str, Any]:
        """
        Ciclo completo de trading — compatível com BotEngine v1.
        
        Busca dados, analisa, e executa se houver sinal.
        Retorna dict com status da operação.
        """
        if self._shutdown:
            return {'status': 'shutdown', 'reason': 'Trader parado'}
        
        # Buscar dados (usa o aggregator via API)
        try:
            data = self.api.get_market_data(asset)
            if not data or data.get('price', 0) <= 0:
                return {'status': 'no_data', 'asset': asset}
            
            # Adicionar asset ao dict
            data['asset'] = asset
            
            # Chamar o fluxo normal
            self.on_market_data(data)
            
            # Retornar status
            return {
                'status': 'ok',
                'asset': asset,
                'price': data.get('price'),
                'in_position': self.position is not None,
                'position_direction': self.position.direction if self.position else None,
                'capital': self.capital,
                'daily_pnl': self.daily_pnl
            }
            
        except Exception as e:
            logger.error(f"[Trader] Erro no run_cycle para {asset}: {e}")
            return {'status': 'error', 'asset': asset, 'error': str(e)}
    
    def on_market_data(self, data: Dict):
        """Chamado quando chegam novos dados de mercado."""
        if self._shutdown:
            return
        
        asset = data.get('asset', 'BTC')
        price = data.get('price', 0)
        
        if price <= 0:
            return
        
        # Reset diário
        self._check_daily_reset()
        
        # Circuit breaker
        if self._check_circuit_breaker():
            return
        
        # Se tem posição, gerir
        if self.position:
            self._manage_position(asset, price, data)
        else:
            # Procurar entrada
            self._check_entry(asset, price, data)
    
    def _check_entry(self, asset: str, price: float, data: Dict):
        """Verifica sinal de entrada."""
        signal = self.strategy.analyze(data, price)
        if not signal or signal.type not in ('LONG', 'SHORT'):
            return
        
        # Risk check
        if not self.risk.allow_trade(signal, self.daily_pnl):
            logger.info(f"[Trader] Trade bloqueado pelo risk manager: {signal.reason}")
            return
        
        # Calcular tamanho
        size = min(self.max_position_usd, self.capital * 0.1)
        leverage = min(self.max_leverage, 3)
        
        # Executar
        self._enter_position(asset, signal, price, size, leverage)
    
    def _enter_position(self, asset: str, signal: Signal, price: float, size: float, leverage: float):
        """Abre posição."""
        side = 'BUY' if signal.type == 'LONG' else 'SELL'
        
        # Ordem paper
        result = self.api.place_order(asset, side, size, market_price=price)
        if result['status'] != 'PAPER_FILLED':
            logger.warning(f"[Trader] Ordem rejeitada: {result.get('reason')}")
            return
        
        # Criar posição
        sl = signal.stop_loss or (price * 0.965 if signal.type == 'LONG' else price * 1.035)
        tp = signal.take_profit or (price * 1.06 if signal.type == 'LONG' else price * 0.94)
        
        self.position = Position(
            asset=asset,
            direction=signal.type.lower(),
            entry_price=price,
            size_usd=size,
            leverage=leverage,
            stop_loss=sl,
            take_profit=tp,
            entry_time=time.time(),
            trailing_stop=sl,
            highest_price=price,
            lowest_price=price
        )
        
        # Guardar na DB
        self.db.save_trade({
            'symbol': asset,
            'direction': self.position.direction,
            'entry_price': price,
            'entry_time': int(time.time() * 1000),
            'size_usd': size,
            'leverage': leverage,
            'strategy_params': signal.to_dict()
        })
        
        # Evento
        if self.event_bus:
            self.event_bus.publish('trade.entered', {
                'asset': asset, 'direction': signal.type,
                'price': price, 'size': size, 'reason': signal.reason
            })
        
        self.trade_count += 1
        logger.info(f"[Trader] ⬆️ ENTER {signal.type} {asset} @ ${price:,.2f} | Size: ${size:.2f}")
    
    def _manage_position(self, asset: str, price: float, data: Dict):
        """Gere posição aberta (SL, TP, trailing)."""
        pos = self.position
        if not pos:
            return
        
        # Atualizar high/low
        if price > pos.highest_price:
            pos.highest_price = price
        if price < pos.lowest_price:
            pos.lowest_price = price
        
        # ─── Stop Loss ─────────────────────────────────────
        
        if pos.direction == 'long':
            if price <= pos.stop_loss:
                self._exit_position(asset, price, 'STOP_LOSS')
                return
        else:
            if price >= pos.stop_loss:
                self._exit_position(asset, price, 'STOP_LOSS')
                return
        
        # ─── Take Profit ───────────────────────────────────
        
        if pos.direction == 'long':
            if price >= pos.take_profit:
                self._exit_position(asset, price, 'TAKE_PROFIT')
                return
        else:
            if price <= pos.take_profit:
                self._exit_position(asset, price, 'TAKE_PROFIT')
                return
        
        # ─── Trailing Stop ─────────────────────────────────
        
        if pos.direction == 'long':
            gain_pct = (price - pos.entry_price) / pos.entry_price
            if gain_pct >= self.trailing_activation:
                new_trailing = pos.highest_price * (1 - self.trailing_pct)
                if new_trailing > pos.trailing_stop:
                    pos.trailing_stop = new_trailing
                    logger.info(f"[Trader] Trailing ajustado: ${pos.trailing_stop:,.2f}")
                if price <= pos.trailing_stop:
                    self._exit_position(asset, price, 'TRAILING_STOP')
                    return
        else:
            gain_pct = (pos.entry_price - price) / pos.entry_price
            if gain_pct >= self.trailing_activation:
                new_trailing = pos.lowest_price * (1 + self.trailing_pct)
                if new_trailing < pos.trailing_stop or pos.trailing_stop == 0:
                    pos.trailing_stop = new_trailing
                    logger.info(f"[Trader] Trailing ajustado: ${pos.trailing_stop:,.2f}")
                if price >= pos.trailing_stop:
                    self._exit_position(asset, price, 'TRAILING_STOP')
                    return
        
        # ─── Sinal de saída da estratégia ──────────────────
        
        signal = self.strategy.analyze(data, price)
        if signal and signal.type == 'EXIT':
            self._exit_position(asset, price, f'SIGNAL_EXIT:{signal.reason}')
            return
    
    def _exit_position(self, asset: str, price: float, reason: str):
        """Fecha posição e calcula PnL."""
        pos = self.position
        if not pos:
            return
        
        # Calcular PnL
        if pos.direction == 'long':
            pnl_pct = (price - pos.entry_price) / pos.entry_price
        else:
            pnl_pct = (pos.entry_price - price) / pos.entry_price
        
        pnl_usd = pos.size_usd * pnl_pct
        self.capital += pnl_usd
        self.daily_pnl += pnl_usd
        
        # Update peak
        if self.capital > self.peak_capital:
            self.peak_capital = self.capital
        
        # Fechar na API
        self.api.close_position(asset)
        
        # Atualizar DB
        open_trade = self.db.get_open_trade()
        if open_trade:
            self.db.update_trade_exit(
                open_trade['id'], price, int(time.time() * 1000),
                pnl_usd, pnl_pct, reason
            )
        
        # Evento
        if self.event_bus:
            self.event_bus.publish('trade.exited', {
                'asset': asset, 'direction': pos.direction,
                'entry': pos.entry_price, 'exit': price,
                'pnl_usd': pnl_usd, 'pnl_pct': pnl_pct,
                'reason': reason, 'duration_min': (time.time() - pos.entry_time) / 60
            })
        
        emoji = "🟢" if pnl_usd > 0 else "🔴"
        logger.info(
            f"[Trader] {emoji} EXIT {pos.direction.upper()} {asset} @ ${price:,.2f} | "
            f"PnL: ${pnl_usd:+.2f} ({pnl_pct*100:+.2f}%) | {reason} | "
            f"Capital: ${self.capital:,.2f}"
        )
        
        self.position = None
    
    def _check_daily_reset(self):
        """Reset do contador diário."""
        today = time.strftime('%Y-%m-%d')
        if today != self.last_trade_day:
            if self.last_trade_day:
                logger.info(f"[Trader] Novo dia. PnL anterior: ${self.daily_pnl:+.2f}")
            self.daily_pnl = 0.0
            self.last_trade_day = today
    
    def _check_circuit_breaker(self) -> bool:
        """Verifica se circuit breaker ativou."""
        daily_return = self.daily_pnl / self.initial_capital
        
        if daily_return <= -self.daily_loss_hard:
            logger.error(f"[Trader] 🔴 HARD STOP ativado: {daily_return*100:.2f}%")
            self._shutdown = True
            return True
        
        if daily_return <= -self.daily_loss_limit:
            logger.warning(f"[Trader] 🟡 SOFT STOP ativado: {daily_return*100:.2f}%")
            return True
        
        return False
    
    @property
    def is_in_position(self) -> bool:
        return self.position is not None
    
    @property
    def current_pnl(self) -> float:
        """PnL não realizado da posição aberta."""
        if not self.position:
            return 0.0
        # Simplificado — precisa de preço atual
        return 0.0
    
    def get_status(self) -> Dict[str, Any]:
        """Status completo para dashboard."""
        return {
            'capital': self.capital,
            'initial_capital': self.initial_capital,
            'total_pnl': self.capital - self.initial_capital,
            'trade_count': self.trade_count,
            'daily_pnl': self.daily_pnl,
            'in_position': self.is_in_position,
            'position': {
                'asset': self.position.asset,
                'direction': self.position.direction,
                'entry_price': self.position.entry_price,
                'size': self.position.size_usd,
                'stop_loss': self.position.stop_loss,
                'take_profit': self.position.take_profit,
            } if self.position else None
        }
