"""Controllers — recebem input do mundo exterior e chamam use cases."""
from typing import Dict, Any
from ...application.use_cases import (
    FetchMarketDataUseCase,
    GenerateSignalUseCase,
    ExecuteTradeUseCase,
    GetPortfolioStatusUseCase,
)
from ...application.dto import MarketDataDTO, SignalDTO


class WebAPIController:
    """Controller para API REST (Flask)."""
    
    def __init__(self,
                 fetch_uc: FetchMarketDataUseCase,
                 signal_uc: GenerateSignalUseCase,
                 trade_uc: ExecuteTradeUseCase,
                 status_uc: GetPortfolioStatusUseCase):
        self.fetch_uc = fetch_uc
        self.signal_uc = signal_uc
        self.trade_uc = trade_uc
        self.status_uc = status_uc
    
    def get_market_data(self, asset: str) -> Dict:
        result = self.fetch_uc.execute(asset)
        if not result:
            return {"error": "Dados não disponíveis"}
        return {
            "asset": result.asset,
            "price": result.price,
            "oi": result.oi_usd,
            "funding": result.funding_rate,
            "volume": result.volume_24h,
        }
    
    def generate_signal(self, asset: str) -> Dict:
        # Primeiro busca dados
        market_data = self.fetch_uc.execute(asset)
        if not market_data:
            return {"error": "Sem dados de mercado"}
        
        signal = self.signal_uc.execute(asset, market_data)
        if not signal:
            return {"signal": "HOLD", "reason": "Sem sinal"}
        
        return {
            "signal": signal.direction.upper(),
            "confidence": signal.confidence,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "reason": signal.reason,
        }
    
    def get_portfolio(self) -> Dict:
        status = self.status_uc.execute()
        return {
            "capital": status.capital,
            "initial_capital": status.initial_capital,
            "total_return_pct": status.total_return_pct,
            "trade_count": status.trade_count,
            "win_rate": status.win_rate,
            "profit_factor": status.profit_factor,
            "in_position": status.in_position,
            "position": status.position,
        }
    
    def emergency_close(self) -> Dict:
        open_trade = self.trade_uc.trade_repo.get_open()
        if open_trade:
            self.trade_uc.exit_position(open_trade.id, open_trade.entry_price, "emergency")
            return {"success": True, "message": "Posição fechada"}
        return {"success": False, "message": "Nenhuma posição aberta"}
