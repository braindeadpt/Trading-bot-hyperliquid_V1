"""
Use Case: ExecuteTrade
Executa trade com validação de risco, persistência e publicação de eventos.
"""
from typing import Optional, Dict, Any
from ...domain.entities import Position, Trade
from ...domain.events import PositionOpened, PositionClosed, TradeExecuted
from ...domain.repositories import TradeRepository
from ...domain.services import ExchangeGateway
from ..interfaces import EventPublisher, Logger
from ..dto import MarketDataDTO, SignalDTO, TradeDTO


class ExecuteTradeUseCase:
    """
    Caso de uso: executar trade (entrada ou saída).
    
    - Valida risco
    - Coloca ordem na exchange (ou simula)
    - Persiste trade
    - Publica eventos
    """
    
    def __init__(self,
                 gateway: ExchangeGateway,
                 trade_repo: TradeRepository,
                 publisher: EventPublisher,
                 logger: Logger,
                 config: Dict[str, Any]):
        self.gateway = gateway
        self.trade_repo = trade_repo
        self.publisher = publisher
        self.logger = logger
        self.config = config
        
        self.initial_capital = config.get('risk', {}).get('initial_capital', 10000.0)
        self.max_position_usd = config.get('risk', {}).get('max_position_size_usd', 100)
        self.max_leverage = config.get('risk', {}).get('max_leverage', 3)
        self.stop_loss_pct = config.get('risk', {}).get('stop_loss_pct', 0.035)
        self.take_profit_pct = config.get('risk', {}).get('take_profit_pct', 0.06)
    
    def enter_position(self, signal: SignalDTO, 
                       current_price: float) -> Optional[TradeDTO]:
        """Abre posição baseada num sinal."""
        # Calcular tamanho
        size = min(self.max_position_usd, self.initial_capital * 0.01)
        leverage = 1  # Simplificado - pode ser adaptativo
        
        # Calcular SL/TP
        if signal.direction == 'long':
            stop_loss = signal.entry_price * (1 - self.stop_loss_pct)
            take_profit = signal.entry_price * (1 + self.take_profit_pct)
        else:
            stop_loss = signal.entry_price * (1 + self.stop_loss_pct)
            take_profit = signal.entry_price * (1 - self.take_profit_pct)
        
        # Simular execução (paper trading)
        entry_time = int(__import__('time').time())
        
        # Criar entidade
        trade = Trade(
            symbol=signal.asset,
            direction=signal.direction,
            entry_price=signal.entry_price,
            size_usd=size,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_time=entry_time,
            strategy_params=signal.__dict__ if hasattr(signal, '__dict__') else {}
        )
        
        # Persistir
        trade_id = self.trade_repo.save(trade)
        trade.id = trade_id
        
        # Publicar evento
        event = PositionOpened(
            asset=signal.asset,
            direction=signal.direction,
            entry_price=signal.entry_price,
            size=size,
            stop_loss=stop_loss,
            take_profit=take_profit
        )
        self.publisher.publish(event)
        
        self.logger.info(
            f"[ExecuteTrade] ENTER {signal.direction.upper()} {signal.asset} "
            f"@${signal.entry_price:,.2f} x{leverage}"
        )
        
        return TradeDTO(
            symbol=trade.symbol,
            direction=trade.direction,
            entry_price=trade.entry_price,
            size_usd=trade.size_usd,
            leverage=trade.leverage,
            stop_loss=trade.stop_loss,
            take_profit=trade.take_profit,
            entry_time=trade.entry_time
        )
    
    def exit_position(self, trade_id: int, exit_price: float,
                      reason: str = "manual") -> Optional[TradeDTO]:
        """Fecha posição existente."""
        # Buscar trade aberto
        trade = self.trade_repo.get_open()
        if not trade or trade.id != trade_id:
            self.logger.warning(f"[ExecuteTrade] Trade {trade_id} não encontrado ou já fechado")
            return None
        
        # Calcular PnL
        pnl = trade.calculate_pnl(exit_price)
        pnl_pct = pnl / trade.size_usd if trade.size_usd > 0 else 0
        exit_time = int(__import__('time').time())
        
        # Atualizar
        self.trade_repo.update_exit(
            trade_id, exit_price, exit_time, pnl, pnl_pct, reason
        )
        
        # Publicar eventos
        event = PositionClosed(
            asset=trade.symbol,
            exit_price=exit_price,
            pnl_usd=pnl,
            pnl_pct=pnl_pct,
            reason=reason,
            holding_time=exit_time - trade.entry_time
        )
        self.publisher.publish(event)
        
        trade_event = TradeExecuted(
            symbol=trade.symbol,
            direction=trade.direction,
            size=trade.size_usd,
            price=exit_price,
            pnl=pnl,
            reason=reason
        )
        self.publisher.publish(trade_event)
        
        self.logger.info(
            f"[ExecuteTrade] EXIT {trade.direction.upper()} {trade.symbol} "
            f"@${exit_price:,.2f} PnL: ${pnl:+.2f} ({reason})"
        )
        
        return TradeDTO(
            symbol=trade.symbol,
            direction=trade.direction,
            entry_price=trade.entry_price,
            size_usd=trade.size_usd,
            leverage=trade.leverage,
            stop_loss=trade.stop_loss,
            take_profit=trade.take_profit,
            entry_time=trade.entry_time,
            exit_price=exit_price,
            exit_time=exit_time,
            pnl_usd=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason
        )
