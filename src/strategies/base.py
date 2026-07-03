"""Abstract strategy interface and shared data models."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.strategies.indicators import Candle


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

    # Market microstructure — Hyperliquid specific
    funding: Optional[float] = None
    predicted_funding: Optional[float] = None
    oi_total: Optional[float] = None
    oi_delta: Optional[float] = None
    volume_1m: Optional[float] = None
    bid_ask_imbalance: Optional[float] = None
    vwap_15m: Optional[float] = None

    # Cross-exchange aggregated data (funding + OI from Binance, Bybit, OKX, etc.)
    funding_avg: Optional[float] = None          # Simple average across exchanges
    funding_weighted: Optional[float] = None     # Weighted by OI
    predicted_funding_avg: Optional[float] = None
    oi_total_aggregated: Optional[float] = None   # Sum of OI across exchanges
    oi_exchange_count: int = 0

    # Orderbook microstructure (L2 Hyperliquid)
    orderbook_spread_pct: Optional[float] = None   # Bid-ask spread as % of mid
    orderbook_oir: Optional[float] = None        # Orderbook Imbalance Ratio (-1 to +1)
    orderbook_depth_quality: Optional[float] = None  # 0-1, bid depth / total depth
    orderbook_bid_ask_ratio: Optional[float] = None  # bid_depth / ask_depth
    orderbook_largest_bid_wall: Optional[float] = None  # Price of largest bid wall
    orderbook_largest_ask_wall: Optional[float] = None  # Price of largest ask wall

    # Optional pre-computed indicator values from upstream
    ema_20: Optional[float] = None
    atr_14: Optional[float] = None
    rsi_14: Optional[float] = None
    adx_14: Optional[float] = None

    # Liquidation cluster data (Task 3.3)
    liquidation_notional_5m: Optional[float] = None  # USD notional liquidated in last 5min
    liquidation_side_5m: Optional[str] = None  # "long" (longs liquidated = price down) or "short"
    liquidation_count_5m: Optional[int] = None  # number of liquidation events
    liquidation_data_source: Optional[str] = None  # "binance" | "proxy" | None

    # Feed health (engine sets when Binance liquidation WS is validated)
    liquidation_feed_ready: bool = False

    # Cross-exchange positioning (Binance global long/short account ratio)
    oi_long_ratio: Optional[float] = None   # 0–1 fraction of accounts long
    oi_short_ratio: Optional[float] = None  # 0–1 fraction of accounts short

    # HL INFO predictedFundings — per-venue rates (8h-normalized)
    predicted_funding_by_venue: Optional[Dict[str, float]] = None

    # Market data feed quality (engine)
    market_data_health: Optional[str] = None  # green | yellow | red
    market_data_stale: bool = False

    # Binance spot lead price (engine injects from binance_price:{symbol} DataBus topic)
    binance_mid: Optional[float] = None
    binance_timestamp_ms: Optional[int] = None

    # Binance USD-M perp mark price (basis-corrected reference for LeadLag)
    binance_perp_mid: Optional[float] = None
    binance_perp_timestamp_ms: Optional[int] = None


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
    current_price: float = 0.0  # last mark; 0 = fall back to entry_price in UI
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
