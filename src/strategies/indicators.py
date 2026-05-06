"""Shared technical indicator calculations — pure functions, no side effects.

All functions accept minimal data and return None when insufficient.
Every indicator is implemented from first principles (no pandas)."""

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Candle:
    """OHLCV candle with optional OI and timestamp."""
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp_ms: int
    open_interest: Optional[float] = None


def calculate_vwap(candles: List[Candle]) -> Optional[float]:
    """Volume Weighted Average Price over the given candles.
    
    VWAP = Σ(price * volume) / Σ(volume)
    Where price = (high + low + close) / 3 (typical price).
    """
    if not candles:
        return None

    total_pv = 0.0
    total_vol = 0.0
    for c in candles:
        typical_price = (c.high + c.low + c.close) / 3.0
        total_pv += typical_price * c.volume
        total_vol += c.volume

    if total_vol == 0.0:
        return None
    return total_pv / total_vol


def calculate_ema(prices: List[float], period: int) -> Optional[float]:
    """Exponential Moving Average.
    
    Uses standard multiplier: 2 / (period + 1).
    Requires at least `period` data points.
    """
    if len(prices) < period:
        return None

    # Seed with SMA of first `period` values
    multiplier = 2.0 / (period + 1.0)
    ema = sum(prices[:period]) / period

    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema

    return ema


def calculate_atr(candles: List[Candle], period: int = 14) -> Optional[float]:
    """Average True Range — volatility measure.
    
    TR = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR = EMA of TR (smoothed, not simple average, per Wilder).
    """
    if len(candles) < period + 1:
        return None

    tr_values: List[float] = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].close
        high = candles[i].high
        low = candles[i].low

        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        tr_values.append(max(tr1, tr2, tr3))

    # Wilder's smoothing: first value = SMA, then EMA-style
    atr = sum(tr_values[:period]) / period
    multiplier = 1.0 / period
    for tr in tr_values[period:]:
        atr = atr + multiplier * (tr - atr)

    return atr


def calculate_volume_profile(candles: List[Candle]) -> Tuple[Optional[float], Optional[float]]:
    """Volume delta estimate: bullish vs bearish volume.
    
    Returns (buy_volume_estimate, sell_volume_estimate).
    Uses close position in the candle to split volume:
    - Close near high → more buy volume
    - Close near low → more sell volume
    This is a standard approximation when tick-level data isn't available.
    """
    if not candles:
        return None, None

    total_buy = 0.0
    total_sell = 0.0
    for c in candles:
        if c.high == c.low:
            # Neutral candle — split evenly
            total_buy += c.volume * 0.5
            total_sell += c.volume * 0.5
        else:
            # Position of close within the candle range: 0 = low, 1 = high
            close_position = (c.close - c.low) / (c.high - c.low)
            # Bias toward buy volume if close is near high
            buy_frac = 0.2 + 0.6 * close_position  # range 0.2 to 0.8
            total_buy += c.volume * buy_frac
            total_sell += c.volume * (1.0 - buy_frac)

    return total_buy, total_sell


def calculate_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Relative Strength Index — momentum oscillator (0-100).
    
    RSI = 100 - 100 / (1 + RS)
    RS = avg_gain / avg_loss (smoothed, Wilder's method).
    """
    if len(prices) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    if len(gains) < period:
        return None

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    multiplier = 1.0 / period
    for i in range(period, len(gains)):
        avg_gain = avg_gain + multiplier * (gains[i] - avg_gain)
        avg_loss = avg_loss + multiplier * (losses[i] - avg_loss)

    if avg_loss == 0.0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_bollinger_bands(
    prices: List[float], period: int = 20, std: float = 2.0
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Bollinger Bands: (lower, middle, upper).
    
    Middle = SMA(period)
    Upper = Middle + std * σ
    Lower = Middle - std * σ
    """
    if len(prices) < period:
        return None, None, None

    window = prices[-period:]
    middle = sum(window) / period
    variance = sum((p - middle) ** 2 for p in window) / period
    sigma = variance ** 0.5

    upper = middle + std * sigma
    lower = middle - std * sigma
    return lower, middle, upper


def detect_support_resistance(candles: List[Candle], lookback: int = 50) -> Tuple[Optional[float], Optional[float]]:
    """Detect support and resistance levels via pivot highs/lows.
    
    A pivot high: high > neighbors on both sides (3-bar lookaround).
    A pivot low:  low  < neighbors on both sides.
    Support = median of recent pivot lows.
    Resistance = median of recent pivot highs.
    """
    if len(candles) < lookback + 2:
        return None, None

    pivot_highs: List[float] = []
    pivot_lows: List[float] = []

    for i in range(2, len(candles) - 2):
        prev2 = candles[i - 2]
        prev1 = candles[i - 1]
        curr = candles[i]
        next1 = candles[i + 1]
        next2 = candles[i + 2]

        if curr.high > prev2.high and curr.high > prev1.high and curr.high > next1.high and curr.high > next2.high:
            pivot_highs.append(curr.high)
        if curr.low < prev2.low and curr.low < prev1.low and curr.low < next1.low and curr.low < next2.low:
            pivot_lows.append(curr.low)

    if not pivot_highs or not pivot_lows:
        # Fallback: use min/max of lookback window
        window = candles[-lookback:]
        return min(c.low for c in window), max(c.high for c in window)

    # Use median of last few pivots
    recent_highs = pivot_highs[-5:] if len(pivot_highs) >= 5 else pivot_highs
    recent_lows = pivot_lows[-5:] if len(pivot_lows) >= 5 else pivot_lows

    recent_highs.sort()
    recent_lows.sort()

    def _median(values: List[float]) -> float:
        n = len(values)
        mid = n // 2
        return (values[mid] + values[mid - 1]) / 2.0 if n % 2 == 0 else values[mid]

    return _median(recent_lows), _median(recent_highs)


def calculate_oi_concentration(oi_history: List[float]) -> Optional[float]:
    """Estimate long/short concentration ratio from OI history.
    
    Returns: long_ratio (0.0-1.0), where > 0.65 means overcrowded longs,
    < 0.35 means overcrowded shorts.
    
    Hyperliquid doesn't expose raw long/short split directly, so we use
    price direction + OI change as a proxy:
    - If price up AND OI up → longs entering (longs likely > 50%)
    - If price down AND OI up → shorts entering (shorts likely > 50%)
    - If OI flat → mixed, return 0.5
    
    This is an approximation; the exchange API may provide better data.
    """
    if len(oi_history) < 3:
        return None

    oi_now = oi_history[-1]
    oi_prev = oi_history[-2]
    oi_delta = oi_now - oi_prev

    if abs(oi_delta) < 1e-6:
        return 0.5

    # Look at last 3 OI changes for direction confirmation
    deltas = [oi_history[i] - oi_history[i - 1] for i in range(1, len(oi_history))]
    avg_delta = sum(deltas) / len(deltas)

    # If OI is growing, someone is entering. We bias toward the dominant side.
    # Without price data, we assume 50/50. With price data, the strategy layer
    # combines price direction + OI for a better estimate.
    # Here we just return a mild directional bias based on OI trend.
    if avg_delta > 0:
        return 0.55  # Slight long bias (arbitrary, overridden by strategy logic)
    elif avg_delta < 0:
        return 0.45  # Slight short bias
    return 0.5


def calculate_overcrowded_score(funding: Optional[float], oi_ratio: Optional[float]) -> float:
    """Combined overcrowding score (0.0 = balanced, 1.0 = extremely crowded).
    
    Combines funding extremity and OI concentration into a single score.
    """
    if funding is None or oi_ratio is None:
        return 0.0

    # Funding extremity: how far from 0
    funding_abs = abs(funding)
    funding_score = min(funding_abs / 0.01, 1.0)  # 1% funding = max score

    # OI concentration: distance from 0.5
    oi_score = min(abs(oi_ratio - 0.5) / 0.2, 1.0)  # 70/30 = max score

    # Weighted combination — funding is stronger signal on Hyperliquid
    return min(0.6 * funding_score + 0.4 * oi_score, 1.0)
