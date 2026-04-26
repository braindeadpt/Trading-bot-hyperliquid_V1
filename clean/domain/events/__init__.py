"""Domain events — entities representam mudanças no domínio."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any


@dataclass
class DomainEvent:
    """Evento de domínio base."""
    event_type: str
    timestamp: int = field(default_factory=lambda: int(datetime.utcnow().timestamp()))
    source: str = "domain"
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SignalGenerated(DomainEvent):
    """Evento: novo sinal gerado pela estratégia."""
    def __init__(self, asset: str, direction: str, confidence: float,
                 entry_price: float, reason: str, metadata: Dict = None):
        super().__init__(
            event_type="signal.generated",
            payload={
                "asset": asset,
                "direction": direction,
                "confidence": confidence,
                "entry_price": entry_price,
                "reason": reason,
                "metadata": metadata or {}
            }
        )


@dataclass
class TradeExecuted(DomainEvent):
    """Evento: trade executado."""
    def __init__(self, symbol: str, direction: str, size: float,
                 price: float, pnl: float = None, reason: str = ""):
        super().__init__(
            event_type="trade.executed",
            payload={
                "symbol": symbol,
                "direction": direction,
                "size": size,
                "price": price,
                "pnl": pnl,
                "reason": reason
            }
        )


@dataclass
class PositionOpened(DomainEvent):
    """Evento: posição aberta."""
    def __init__(self, asset: str, direction: str, entry_price: float,
                 size: float, stop_loss: float, take_profit: float):
        super().__init__(
            event_type="position.opened",
            payload={
                "asset": asset,
                "direction": direction,
                "entry_price": entry_price,
                "size": size,
                "stop_loss": stop_loss,
                "take_profit": take_profit
            }
        )


@dataclass
class PositionClosed(DomainEvent):
    """Evento: posição fechada."""
    def __init__(self, asset: str, exit_price: float, pnl_usd: float,
                 pnl_pct: float, reason: str, holding_time: int = None):
        super().__init__(
            event_type="position.closed",
            payload={
                "asset": asset,
                "exit_price": exit_price,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "reason": reason,
                "holding_time": holding_time
            }
        )


@dataclass
class MarketDataUpdated(DomainEvent):
    """Evento: dados de mercado atualizados."""
    def __init__(self, asset: str, price: float, oi: float, funding: float,
                 volume: float, source: str = ""):
        super().__init__(
            event_type="market.data_updated",
            payload={
                "asset": asset,
                "price": price,
                "oi": oi,
                "funding": funding,
                "volume": volume,
                "source": source
            }
        )


@dataclass
class RiskLimitBreached(DomainEvent):
    """Evento: limite de risco violado."""
    def __init__(self, limit_type: str, current_value: float, threshold: float):
        super().__init__(
            event_type="risk.limit_breached",
            payload={
                "limit_type": limit_type,
                "current_value": current_value,
                "threshold": threshold
            }
        )
