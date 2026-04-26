"""Application DTOs — Data Transfer Objects for use case boundaries."""
from dataclasses import dataclass
from typing import Optional, Dict, Any, List


@dataclass
class MarketDataDTO:
    """DTO para dados de mercado — entrada do use case."""
    asset: str
    price: float
    mark_price: float = 0.0
    oracle_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    volume_24h: float = 0.0
    oi_usd: float = 0.0
    oi_change_pct: float = 0.0
    funding_rate: float = 0.0
    funding_avg: float = 0.0
    volume_ratio: float = 1.0
    timestamp: int = 0
    source: str = ""


@dataclass
class SignalDTO:
    """DTO para sinal gerado — saída do use case."""
    asset: str
    direction: str
    confidence: float
    entry_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    timestamp: int = 0


@dataclass
class TradeDTO:
    """DTO para trade executado."""
    symbol: str
    direction: str
    entry_price: float
    size_usd: float
    leverage: float
    stop_loss: float
    take_profit: float
    entry_time: int
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_price: Optional[float] = None
    exit_time: Optional[int] = None
    exit_reason: Optional[str] = None


@dataclass
class PortfolioStatusDTO:
    """DTO para status do portfólio."""
    capital: float
    initial_capital: float
    peak_capital: float
    daily_pnl: float
    total_return_pct: float
    max_drawdown_pct: float
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    profit_factor: float
    in_position: bool
    position: Optional[Dict[str, Any]] = None
