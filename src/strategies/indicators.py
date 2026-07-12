"""Shared technical indicator calculations — pure functions, no side effects.

All functions accept minimal data and return None when insufficient.
Every indicator is implemented from first principles (no pandas)."""

from dataclasses import dataclass
import math
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Candle:
    """OHLCV candle with optional OI, volume split, and timestamp.

    v3.1.24: added buy_volume, sell_volume, trade_count for CVDOrderFlow
    and bid_ask_imbalance computation.
    """
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp_ms: int
    open_interest: Optional[float] = None
    buy_volume: Optional[float] = None
    sell_volume: Optional[float] = None
    trade_count: int = 0


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


def detect_support_resistance(
    candles: List[Candle],
    lookback: int = 50,
) -> Tuple[Optional[float], Optional[float]]:
    """Detect support and resistance via causal Williams Fractals.

    A pivot high at bar ``i-2`` is confirmed when its high is strictly
    greater than the highs of bars ``i-3`` and ``i-1`` (and bar ``i``
    must not exceed it). This means the pivot is *known* 2 bars after
    it occurs, so the indicator is fully causal — safe for live and
    backtest use.

    Returns ``(support, resistance)`` as the median of the last few
    confirmed pivot lows / highs, or a min/max fallback over the
    lookback window when no pivots are present.
    """
    if len(candles) < lookback + 3:
        return None, None

    pivot_highs: List[float] = []
    pivot_lows: List[float] = []

    # Causal scan: a pivot at index `i - 2` is decided using bars
    # `i - 3`, `i - 2`, `i - 1`, `i`. Loop runs while those indices
    # are all inside the array, so the result for bar `i` only
    # references data that is already available at decision time.
    for i in range(3, len(candles)):
        prev3 = candles[i - 3]
        pivot = candles[i - 2]
        prev1 = candles[i - 1]
        curr = candles[i]

        if (
            pivot.high > prev3.high
            and pivot.high > prev1.high
            and pivot.high >= curr.high
        ):
            pivot_highs.append(pivot.high)
        if (
            pivot.low < prev3.low
            and pivot.low < prev1.low
            and pivot.low <= curr.low
        ):
            pivot_lows.append(pivot.low)

    if not pivot_highs or not pivot_lows:
        # Fallback: min/max of the lookback window (causal — only past data)
        window = candles[-lookback:]
        return min(c.low for c in window), max(c.high for c in window)

    recent_highs = pivot_highs[-5:] if len(pivot_highs) >= 5 else pivot_highs
    recent_lows = pivot_lows[-5:] if len(pivot_lows) >= 5 else pivot_lows
    recent_highs.sort()
    recent_lows.sort()

    def _median(values: List[float]) -> float:
        n = len(values)
        mid = n // 2
        return (values[mid] + values[mid - 1]) / 2.0 if n % 2 == 0 else values[mid]

    return _median(recent_lows), _median(recent_highs)


def detect_recent_pivots(
    candles: List[Candle],
    lookback: int = 100,
    max_pivots: int = 10,
) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Return (pivot_highs, pivot_lows) as lists of (timestamp_ms, price) tuples.

    Uses causal Williams Fractals: a pivot at bar ``i-2`` is confirmed when
    its high/low is strictly greater/less than bars ``i-3`` and ``i-1`` and
    bar ``i`` does not exceed it. Only pivots within the last ``lookback``
    bars are returned, most-recent-first. Used by SFP detection.

    Returns empty lists if not enough data.
    """
    if len(candles) < 5:
        return [], []

    pivot_highs: List[Tuple[int, float]] = []
    pivot_lows: List[Tuple[int, float]] = []

    start = max(3, len(candles) - lookback)
    for i in range(start, len(candles)):
        prev3 = candles[i - 3]
        pivot = candles[i - 2]
        prev1 = candles[i - 1]
        # curr is the bar that confirms the pivot (i); it must not exceed
        if (
            pivot.high > prev3.high
            and pivot.high > prev1.high
            and pivot.high >= candles[i].high
        ):
            pivot_highs.append((pivot.timestamp_ms, pivot.high))
        if (
            pivot.low < prev3.low
            and pivot.low < prev1.low
            and pivot.low <= candles[i].low
        ):
            pivot_lows.append((pivot.timestamp_ms, pivot.low))

    # Most-recent-first
    pivot_highs.reverse()
    pivot_lows.reverse()
    return pivot_highs[:max_pivots], pivot_lows[:max_pivots]


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


def calculate_adx(candles: List[Candle], period: int = 14) -> Optional[float]:
    """Average Directional Index — strength of trend (0-100).

    ADX > 25 = strong trend. ADX < 20 = weak/range-bound.
    Uses Wilder's smoothing. Requires at least 2*period + 1 candles.
    """
    if len(candles) < 2 * period + 1:
        return None

    # True Range, +DM, -DM
    tr_values: List[float] = []
    plus_dm: List[float] = []
    minus_dm: List[float] = []

    for i in range(1, len(candles)):
        prev = candles[i - 1]
        curr = candles[i]

        tr1 = curr.high - curr.low
        tr2 = abs(curr.high - prev.close)
        tr3 = abs(curr.low - prev.close)
        tr_values.append(max(tr1, tr2, tr3))

        up_move = curr.high - prev.high
        down_move = prev.low - curr.low

        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0.0)

        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0.0)

    # Need at least period values after the first diff
    if len(tr_values) < period:
        return None

    # Wilder's smoothing — first value = SMA, then iterative
    tr_smooth = sum(tr_values[:period]) / period
    plus_smooth = sum(plus_dm[:period]) / period
    minus_smooth = sum(minus_dm[:period]) / period

    multiplier = 1.0 / period

    dx_values: List[float] = []
    for i in range(period, len(tr_values)):
        tr_smooth = tr_smooth + multiplier * (tr_values[i] - tr_smooth)
        plus_smooth = plus_smooth + multiplier * (plus_dm[i] - plus_smooth)
        minus_smooth = minus_smooth + multiplier * (minus_dm[i] - minus_smooth)

        if tr_smooth == 0:
            dx_values.append(0.0)
            continue

        plus_di = 100.0 * plus_smooth / tr_smooth
        minus_di = 100.0 * minus_smooth / tr_smooth
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_values.append(0.0)
            continue
        dx = 100.0 * abs(plus_di - minus_di) / di_sum
        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    # Smooth DX values to get ADX
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = adx + multiplier * (dx - adx)

    return adx


def calculate_realized_volatility(
    candles: List[Candle],
    period: int = 480,  # 20 days of 1h candles
) -> Optional[float]:
    """Annualized realized volatility from log returns.

    Uses 1h candles (period=480 for 20 days). Annualized with sqrt(365 * 24).
    Returns None if insufficient data.
    """
    if len(candles) < period + 1:
        return None

    # Use the most recent `period` candles
    recent = candles[-period:]

    # Calculate log returns: ln(close_t / close_t-1)
    returns: List[float] = []
    for i in range(1, len(recent)):
        prev_close = recent[i - 1].close
        curr_close = recent[i].close
        if prev_close <= 0 or curr_close <= 0:
            continue
        log_return = math.log(curr_close / prev_close)
        returns.append(log_return)

    if len(returns) < 2:
        return None

    # Sample standard deviation of returns
    mean_return = sum(returns) / len(returns)
    variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
    std = variance ** 0.5

    # Annualize: std * sqrt(periods_per_year)
    # For 1h candles: 365 days * 24 hours = 8760 periods per year
    periods_per_year = 365.0 * 24.0
    annualized_vol = std * (periods_per_year ** 0.5)

    return annualized_vol


def calculate_vwap_zscore(
    candles: List[Candle],
    current_price: float,
    lookback: int = 24,  # 24h of 1h candles
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """VWAP + Z-score of current price deviation from VWAP.

    Returns (vwap, stddev, zscore) where:
      zscore = (current_price - vwap) / stddev

    Positive zscore = price above VWAP (potentially overbought)
    Negative zscore = price below VWAP (potentially oversold)

    Returns (None, None, None) if insufficient data or stddev=0.
    """
    if len(candles) < lookback:
        return None, None, None

    recent = candles[-lookback:]

    # Calculate VWAP
    total_pv = 0.0
    total_vol = 0.0
    typical_prices: List[float] = []
    for c in recent:
        tp = (c.high + c.low + c.close) / 3.0
        typical_prices.append(tp)
        total_pv += tp * c.volume
        total_vol += c.volume

    if total_vol == 0.0:
        return None, None, None

    vwap = total_pv / total_vol

    # Calculate stddev of typical prices
    mean_tp = sum(typical_prices) / len(typical_prices)
    variance = sum((tp - mean_tp) ** 2 for tp in typical_prices) / len(typical_prices)
    stddev = variance ** 0.5

    if stddev == 0.0:
        return vwap, 0.0, None

    zscore = (current_price - vwap) / stddev
    return vwap, stddev, zscore


def calculate_volume_ratio(
    candles: List[Candle],
    lookback: int = 24,
) -> Tuple[Optional[float], Optional[float]]:
    """Current volume vs average volume over lookback.

    Returns (current_volume, ratio) where ratio = current / average.
    """
    if len(candles) < 2:
        return None, None

    recent = candles[-lookback:] if len(candles) >= lookback else candles
    avg_volume = sum(c.volume for c in recent) / len(recent)
    current_volume = candles[-1].volume

    if avg_volume == 0.0:
        return current_volume, None

    ratio = current_volume / avg_volume
    return current_volume, ratio


def calculate_obv(candles: List[Candle]) -> Optional[float]:
    """On-Balance Volume (OBV).

    Cumulative volume signed by candle direction:
        - close > prev_close:  OBV += volume
        - close < prev_close:  OBV -= volume
        - close == prev_close: OBV unchanged

    Returns the running OBV value, or None if fewer than 2 candles.

    Note: OBV is unbounded (can be huge), so it is best used for
    divergence (price rising + OBV falling = bearish divergence)
    rather than absolute level. Use the helper `calculate_obv_slope`
    to normalize it to a rate.
    """
    if not candles or len(candles) < 2:
        return None

    obv = 0.0
    for i in range(1, len(candles)):
        c = candles[i]
        p = candles[i - 1]
        if c.close > p.close:
            obv += c.volume
        elif c.close < p.close:
            obv -= c.volume
    return obv


def calculate_obv_slope(candles: List[Candle], lookback: int = 14) -> Optional[float]:
    """OBV normalized to a slope (OBV delta over `lookback` bars).

    Returns the change in OBV over the last `lookback` candles, or
    None if insufficient data.

    Useful for divergence detection without exposing the raw unbounded
    OBV value to the UI.
    """
    if not candles or len(candles) < lookback + 1:
        return None

    obv_now = calculate_obv(candles[-lookback:])
    obv_prev = calculate_obv(candles[-(lookback + 1):-1])
    if obv_now is None or obv_prev is None:
        return None
    return obv_now - obv_prev


def calculate_mfi(candles: List[Candle], period: int = 14) -> Optional[float]:
    """Money Flow Index (MFI), 0..100.

    Typical Price (TP) = (high + low + close) / 3
    Raw Money Flow    = TP * volume
    Money Flow Ratio  = sum(PMF, last `period`) / sum(NMF, last `period`)
    MFI               = 100 - 100 / (1 + MFR)

    Returns the MFI value, or None if fewer than `period + 1` candles.
    """
    if not candles or len(candles) < period + 1:
        return None

    pos_flow = 0.0
    neg_flow = 0.0
    for i in range(1, period + 1):
        c = candles[-i]
        p = candles[-(i + 1)]
        tp = (c.high + c.low + c.close) / 3.0
        prev_tp = (p.high + p.low + p.close) / 3.0
        rmf = tp * c.volume
        if tp > prev_tp:
            pos_flow += rmf
        elif tp < prev_tp:
            neg_flow += rmf

    if neg_flow == 0.0:
        return 100.0 if pos_flow > 0.0 else 50.0
    mfr = pos_flow / neg_flow
    return 100.0 - (100.0 / (1.0 + mfr))


def calculate_vwap_multi_tf(
    candles_by_tf: Dict[str, List[Candle]],
) -> Dict[str, Optional[float]]:
    """VWAP computed for multiple timeframes in a single call.

    Args:
        candles_by_tf: dict mapping timeframe name (e.g. "1m", "5m",
            "15m", "1h") to a list of Candle objects. The list is
            typically the last N bars of that timeframe.

    Returns:
        dict with the same keys; each value is VWAP (typical price
        weighted by volume) for that timeframe, or None if the list
        is empty / all volumes are zero.

    Note: this is a "rolling session VWAP" per timeframe, not a
    true daily anchored VWAP. Sufficient for divergence detection
    and dashboard observability.
    """
    out: Dict[str, Optional[float]] = {}
    for tf, candles in candles_by_tf.items():
        if not candles:
            out[tf] = None
            continue
        pv_sum = 0.0
        vol_sum = 0.0
        for c in candles:
            tp = (c.high + c.low + c.close) / 3.0
            pv_sum += tp * c.volume
            vol_sum += c.volume
        out[tf] = (pv_sum / vol_sum) if vol_sum > 0.0 else None
    return out


def volatility_target_size(
    base_size_pct: float,
    realized_vol_annual: float,
    target_vol_annual: float = 0.20,
    min_size_mult: float = 0.25,
    max_size_mult: float = 3.0,
) -> float:
    """Calculate position size using volatility targeting.

    Formula: size = base_size * (target_vol / realized_vol)

    Args:
        base_size_pct: Base position size as % of capital (e.g., 0.02 = 2%)
        realized_vol_annual: Annualized realized volatility (e.g., 0.60 for 60%)
        target_vol_annual: Target portfolio volatility (default 20%)
        min_size_mult: Minimum size multiplier (default 0.25x = 25% of base)
        max_size_mult: Maximum size multiplier (default 3.0x = 300% of base)

    Returns:
        Adjusted position size as % of capital.
    """
    if realized_vol_annual <= 0 or target_vol_annual <= 0:
        return base_size_pct

    multiplier = target_vol_annual / realized_vol_annual

    # Clamp to prevent extreme sizing
    clamped = max(min_size_mult, min(multiplier, max_size_mult))

    return base_size_pct * clamped


@dataclass(frozen=True)
class VolumeProfileVA:
    """Volume Profile with Value Area metrics.

    - poc: Point of Control (price level with the highest volume)
    - vah: Value Area High (top of the 70%-volume region)
    - val: Value Area Low (bottom of the 70%-volume region)
    - is_balanced: True when VA spans > balanced_threshold of total range (D-shape)
    - total_volume: Sum of all bin volumes
    - n_bins: Number of bins used
    - bin_size: Price width of each bin
    """
    poc: float
    vah: float
    val: float
    is_balanced: bool
    total_volume: float
    n_bins: int
    bin_size: float


def calculate_volume_profile_va(
    candles: List[Candle],
    bins: int = 50,
    va_pct: float = 0.70,
    balanced_threshold: float = 0.60,
) -> Optional[VolumeProfileVA]:
    """Compute Volume Profile + Value Area for a candle window.

    Algorithm:
      1. Determine price range [min(low), max(high)] over the window
      2. Bin the range into ``bins`` equal-width bins
      3. For each candle, distribute its volume across the bins its
         [low, high] range overlaps (proportional to bin span covered)
      4. POC = bin with the highest total volume
      5. Value Area: expand up/down from POC, adding the side with more
         volume each step, until ``va_pct`` of total volume is included

    A "D-shape" (balanced) profile is flagged when the VA width exceeds
    ``balanced_threshold`` of the total range — directional trades are
    lower probability in this regime.

    Returns None if not enough data or zero volume.
    """
    if len(candles) < 10 or bins < 5:
        return None

    pmin = min(c.low for c in candles)
    pmax = max(c.high for c in candles)
    if pmax <= pmin:
        return None
    bin_size = (pmax - pmin) / bins
    if bin_size <= 0:
        return None

    bin_vol = [0.0] * bins
    for c in candles:
        if c.volume <= 0:
            continue
        lo_bin = int((c.low - pmin) / bin_size)
        hi_bin = int((c.high - pmin) / bin_size)
        lo_bin = max(0, min(bins - 1, lo_bin))
        hi_bin = max(0, min(bins - 1, hi_bin))
        span = hi_bin - lo_bin + 1
        if span <= 0:
            continue
        vol_per_bin = c.volume / span
        for i in range(lo_bin, hi_bin + 1):
            bin_vol[i] += vol_per_bin

    total_vol = sum(bin_vol)
    if total_vol <= 0:
        return None

    poc_bin = max(range(bins), key=lambda i: bin_vol[i])
    poc_price = pmin + (poc_bin + 0.5) * bin_size

    # Value Area: expand from POC until va_pct of total volume is included
    target = va_pct * total_vol
    va_vol = bin_vol[poc_bin]
    lo, hi = poc_bin, poc_bin
    while va_vol < target and (lo > 0 or hi < bins - 1):
        below = bin_vol[lo - 1] if lo > 0 else -1.0
        above = bin_vol[hi + 1] if hi < bins - 1 else -1.0
        if below < 0 and above < 0:
            break
        if below >= above and lo > 0:
            lo -= 1
            va_vol += bin_vol[lo]
        elif hi < bins - 1:
            hi += 1
            va_vol += bin_vol[hi]
        else:
            lo -= 1
            va_vol += bin_vol[lo]

    vah = pmin + (hi + 1) * bin_size  # top edge of highest VA bin
    val = pmin + lo * bin_size         # bottom edge of lowest VA bin
    va_width = vah - val
    total_range = pmax - pmin
    is_balanced = (va_width / total_range) >= balanced_threshold if total_range > 0 else False

    return VolumeProfileVA(
        poc=poc_price,
        vah=vah,
        val=val,
        is_balanced=is_balanced,
        total_volume=total_vol,
        n_bins=bins,
        bin_size=bin_size,
    )
