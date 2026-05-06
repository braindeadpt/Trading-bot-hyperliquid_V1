"""Abstract strategy interface and shared data models."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from indicators import Candle


@dataclass(frozen=True)
class MarketEvent:
    """All market data a strategy might need, bundled into one event.
    
    Filled by the DataBus / CandleBuilder. Any field may be None if data
    is not yet available (e.g., waiting for enough candles).
    """
    symbol: str
    price: float
    timestamp_ms: int

    # Timeframe candles
    candle_1m: Optional[Candle] = None
    candle_5m: Optional[Candle] = None
    candle_15m: Optional[Candle] = None
    candle_1h: Optional[Candle] = None

    # Market microstructure
    funding: Optional[float] = None
    predicted_funding: Optional[float] = None
    oi_total: Optional[float] = None
    oi_delta: Optional[float] = None
    volume_1m: Optional[float] = None
    bid_ask_imbalance: Optional[float] = None
    vwap_15m: Optional[float] = None

    # Optional pre-computed indicator values from upstream
    ema_20: Optional[float] = None
    atr_14: Optional[float] = None
    rsi_14: Optional[float] = None


@dataclass(frozen=True)
class Signal:
    """Entry signal produced by a strategy."""
    strategy: str
    symbol: str
    side: str  # 'long' | 'short'
    confidence: float  # 0.0 - 1.0
    size_pct: float    # % of capital to risk
    entry_price: Optional[float] = None  # None = market order
    stop_loss_pct: float = 0.0
    take_profit_pct: Optional[float] = None
    reason: str = ""
    metadata: Dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExitSignal:
    """Exit signal produced when a strategy wants to close a position."""
    strategy: str
    symbol: str
    side: str  # opposite of position side, or 'close'
    confidence: float  # 0.0 - 1.0
    reason: str
    metadata: Dict = field(default_factory=dict)


@dataclass
class Position:
    """Current open position tracked by the risk manager."""
    symbol: str
    side: str  # 'long' | 'short'
    entry_price: float
    size: float  # base asset amount
    entry_time_ms: int
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    metadata: Dict = field(default_factory=dict)


class Strategy(ABC):
    """Base interface for all trading strategies.
    
    Every strategy receives MarketEvents and decides:
    - Should we enter a new position? → return Signal from on_data()
    - Should we exit an existing position? → return ExitSignal from on_position()
    """

    @abstractmethod
    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        """Called on every market tick / candle update.
        
        Returns a Signal if the strategy wants to enter a position,
        or None if no action should be taken.
        """
        ...

    @abstractmethod
    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Called when a position is open and a new market event arrives.
        
        Returns an ExitSignal if the strategy wants to close the position,
        or None to hold.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy name."""
        ...
