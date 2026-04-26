"""
Use Case: GetPortfolioStatus
Consulta estado atual do portfólio a partir do repositório.
"""
from typing import Dict, Any, List
from ...domain.repositories import TradeRepository
from ..interfaces import Logger
from ..dto import PortfolioStatusDTO


class GetPortfolioStatusUseCase:
    """
    Caso de uso: obter status do portfólio.
    
    Input:  (nenhum)
    Output: PortfolioStatusDTO
    """
    
    def __init__(self,
                 trade_repo: TradeRepository,
                 logger: Logger,
                 initial_capital: float = 10000.0):
        self.trade_repo = trade_repo
        self.logger = logger
        self.initial_capital = initial_capital
    
    def execute(self) -> PortfolioStatusDTO:
        """Retorna status atual do portfólio."""
        trades = self.trade_repo.get_recent(limit=1000)
        
        total_pnl = sum((t.pnl_usd or 0) for t in trades)
        current_capital = self.initial_capital + total_pnl
        
        winners = [t for t in trades if t.is_winner()]
        losers = [t for t in trades if not t.is_winner() and t.pnl_usd is not None]
        
        win_count = len(winners)
        loss_count = len(losers)
        total_trades = len(trades)
        
        win_rate = win_count / total_trades if total_trades > 0 else 0.0
        
        gross_profit = sum(t.pnl_usd for t in winners if t.pnl_usd)
        gross_loss = abs(sum(t.pnl_usd for t in losers if t.pnl_usd))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0
        
        # Drawdown
        peak = self.initial_capital
        max_dd = 0.0
        cumulative = self.initial_capital
        for t in trades:
            cumulative += (t.pnl_usd or 0)
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / peak
            if dd > max_dd:
                max_dd = dd
        
        # Posição aberta
        open_trade = self.trade_repo.get_open()
        in_position = open_trade is not None
        position = None
        if open_trade:
            position = {
                'asset': open_trade.symbol,
                'direction': open_trade.direction,
                'entry_price': open_trade.entry_price,
                'size': open_trade.size_usd,
                'leverage': open_trade.leverage
            }
        
        return PortfolioStatusDTO(
            capital=current_capital,
            initial_capital=self.initial_capital,
            peak_capital=peak,
            daily_pnl=0.0,  # Simplificado
            total_return_pct=(current_capital - self.initial_capital) / self.initial_capital * 100,
            max_drawdown_pct=max_dd * 100,
            trade_count=total_trades,
            win_count=win_count,
            loss_count=loss_count,
            win_rate=win_rate,
            profit_factor=profit_factor,
            in_position=in_position,
            position=position
        )
