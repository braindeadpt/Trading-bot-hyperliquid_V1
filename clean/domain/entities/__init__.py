"""Domain entities — no external dependencies."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any


@dataclass
class Candle:
    """Vela OHLCV — entidade de domínio pura."""
    symbol: str
    interval: str
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    def is_bullish(self) -> bool:
        return self.close >= self.open

    def is_bearish(self) -> bool:
        return self.close < self.open

    def range_pct(self) -> float:
        if self.close == 0:
            return 0.0
        return abs(self.close - self.open) / self.close


@dataclass
class Signal:
    """Sinal de trading — entidade de domínio."""
    asset: str
    direction: str  # long / short
    confidence: float
    entry_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    timestamp: int = field(default_factory=lambda: int(datetime.utcnow().timestamp()))
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_long(self) -> bool:
        return self.direction == "long"

    @property
    def is_short(self) -> bool:
        return self.direction == "short"

    @property
    def risk_reward(self) -> float:
        if not self.stop_loss or not self.take_profit:
            return 0.0
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.take_profit - self.entry_price)
        return reward / risk if risk > 0 else 0.0


@dataclass
class Position:
    """Posição aberta — entidade de domínio."""
    id: Optional[int] = None
    asset: str = ""
    direction: str = ""  # long / short
    entry_price: float = 0.0
    size_usd: float = 0.0
    leverage: float = 1.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    entry_time: int = 0
    trailing_stop: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = field(default_factory=lambda: float("inf"))

    def current_pnl(self, current_price: float) -> float:
        if self.is_long:
            return (current_price - self.entry_price) / self.entry_price * self.size_usd
        return (self.entry_price - current_price) / self.entry_price * self.size_usd

    @property
    def is_long(self) -> bool:
        return self.direction == "long"

    @property
    def is_short(self) -> bool:
        return self.direction == "short"

    def update_trailing_stop(self, current_price: float, activation_pct: float, trailing_pct: float) -> bool:
        """Atualiza trailing stop. Retorna True se foi ajustado."""
        if self.is_long:
            if current_price > self.highest_price:
                self.highest_price = current_price
                activation_price = self.entry_price * (1 + activation_pct)
                if current_price >= activation_price:
                    new_stop = current_price * (1 - trailing_pct)
                    if new_stop > self.trailing_stop:
                        self.trailing_stop = new_stop
                        return True
        else:
            if current_price < self.lowest_price:
                self.lowest_price = current_price
                activation_price = self.entry_price * (1 - activation_pct)
                if current_price <= activation_price:
                    new_stop = current_price * (1 + trailing_pct)
                    if new_stop < self.trailing_stop or self.trailing_stop == 0:
                        self.trailing_stop = new_stop
                        return True
        return False


@dataclass
class Trade:
    """Trade fechado — entidade de domínio."""
    id: Optional[int] = None
    symbol: str = ""
    direction: str = ""
    entry_price: float = 0.0
    exit_price: Optional[float] = None
    entry_time: int = 0
    exit_time: Optional[int] = None
    size_usd: float = 0.0
    leverage: float = 1.0
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None
    strategy_params: Dict[str, Any] = field(default_factory=dict)

    def calculate_pnl(self, exit_price: float) -> float:
        if self.direction == "long":
            return (exit_price - self.entry_price) / self.entry_price * self.size_usd
        return (self.entry_price - exit_price) / self.entry_price * self.size_usd

    def is_winner(self) -> bool:
        return (self.pnl_usd or 0) > 0


@dataclass
class MarketSnapshot:
    """Snapshot de mercado — entidade de domínio."""
    asset: str = ""
    price: float = 0.0
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

    @property
    def spread(self) -> float:
        return self.ask - self.bid if self.ask > self.bid else 0.0

    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.bid > 0 and self.ask > 0 else self.price
