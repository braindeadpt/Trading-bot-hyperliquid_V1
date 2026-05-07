"""
Orderbook metrics for real-time microstructure analysis.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class PriceLevel:
    price: float
    size: float


@dataclass
class OrderbookMetrics:
    """Real-time L2 orderbook metrics."""

    symbol: str
    timestamp_ms: int

    # Basic
    best_bid: float
    best_ask: float
    spread: float
    spread_pct: float
    mid_price: float
    weighted_mid: float

    # Depth
    bid_depth_1pct: float  # Total bid volume within 1% of mid
    ask_depth_1pct: float
    bid_depth_5pct: float
    ask_depth_5pct: float

    # Imbalance
    oir_10levels: float  # Orderbook Imbalance Ratio (top 10 levels)
    oir_1pct: float  # OIR within 1% depth

    # Walls
    largest_bid_wall_price: Optional[float]
    largest_bid_wall_size: float
    largest_ask_wall_price: Optional[float]
    largest_ask_wall_size: float

    # Quality
    depth_quality: float  # 0-1 score (bid_depth / total_depth)
    bid_ask_ratio: float  # bid_depth / ask_depth


def calculate_metrics(
    bids: List[PriceLevel],
    asks: List[PriceLevel],
    symbol: str,
    timestamp_ms: int,
) -> OrderbookMetrics:
    """Calculate all orderbook metrics from raw L2 data."""
    if not bids or not asks:
        return _empty_metrics(symbol, timestamp_ms)

    best_bid = bids[0].price
    best_ask = asks[0].price
    mid = (best_bid + best_ask) / 2
    spread = best_ask - best_bid
    spread_pct = spread / mid if mid else 0

    # Weighted mid (volume-weighted average of top 5 levels)
    weighted_mid = _calc_weighted_mid(bids[:5], asks[:5])

    # Depth calculations
    bid_depth_1pct = _cumulative_volume_within_pct(bids, mid, 1.0)
    ask_depth_1pct = _cumulative_volume_within_pct(asks, mid, 1.0)
    bid_depth_5pct = _cumulative_volume_within_pct(bids, mid, 5.0)
    ask_depth_5pct = _cumulative_volume_within_pct(asks, mid, 5.0)

    # OIR (Orderbook Imbalance Ratio)
    oir_10levels = _calc_oir(bids[:10], asks[:10])
    oir_1pct = _calc_oir(
        _levels_within_pct(bids, mid, 1.0),
        _levels_within_pct(asks, mid, 1.0),
    )

    # Wall detection
    largest_bid = max(bids, key=lambda x: x.size) if bids else None
    largest_ask = max(asks, key=lambda x: x.size) if asks else None

    # Depth quality
    total_depth = bid_depth_1pct + ask_depth_1pct
    depth_quality = bid_depth_1pct / total_depth if total_depth > 0 else 0.5

    bid_ask_ratio = bid_depth_1pct / ask_depth_1pct if ask_depth_1pct > 0 else 1.0

    return OrderbookMetrics(
        symbol=symbol,
        timestamp_ms=timestamp_ms,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        spread_pct=spread_pct,
        mid_price=mid,
        weighted_mid=weighted_mid,
        bid_depth_1pct=bid_depth_1pct,
        ask_depth_1pct=ask_depth_1pct,
        bid_depth_5pct=bid_depth_5pct,
        ask_depth_5pct=ask_depth_5pct,
        oir_10levels=oir_10levels,
        oir_1pct=oir_1pct,
        largest_bid_wall_price=largest_bid.price if largest_bid else None,
        largest_bid_wall_size=largest_bid.size if largest_bid else 0,
        largest_ask_wall_price=largest_ask.price if largest_ask else None,
        largest_ask_wall_size=largest_ask.size if largest_ask else 0,
        depth_quality=depth_quality,
        bid_ask_ratio=bid_ask_ratio,
    )


def _empty_metrics(symbol: str, timestamp_ms: int) -> OrderbookMetrics:
    return OrderbookMetrics(
        symbol=symbol,
        timestamp_ms=timestamp_ms,
        best_bid=0,
        best_ask=0,
        spread=0,
        spread_pct=0,
        mid_price=0,
        weighted_mid=0,
        bid_depth_1pct=0,
        ask_depth_1pct=0,
        bid_depth_5pct=0,
        ask_depth_5pct=0,
        oir_10levels=0,
        oir_1pct=0,
        largest_bid_wall_price=None,
        largest_bid_wall_size=0,
        largest_ask_wall_price=None,
        largest_ask_wall_size=0,
        depth_quality=0.5,
        bid_ask_ratio=1.0,
    )


def _calc_weighted_mid(bids: List[PriceLevel], asks: List[PriceLevel]) -> float:
    """Volume-weighted mid price."""
    total_vol = sum(b.size for b in bids) + sum(a.size for a in asks)
    if total_vol == 0:
        return 0
    weighted_sum = sum(b.price * b.size for b in bids) + sum(a.price * a.size for a in asks)
    return weighted_sum / total_vol


def _cumulative_volume_within_pct(levels: List[PriceLevel], mid: float, pct: float) -> float:
    """Sum of volumes within pct% of mid price."""
    threshold = mid * pct / 100
    total = 0.0
    for level in levels:
        if abs(level.price - mid) <= threshold:
            total += level.size
        else:
            break  # Levels are sorted, so we can stop early
    return total


def _levels_within_pct(levels: List[PriceLevel], mid: float, pct: float) -> List[PriceLevel]:
    """Filter levels within pct% of mid."""
    threshold = mid * pct / 100
    return [level for level in levels if abs(level.price - mid) <= threshold]


def _calc_oir(bids: List[PriceLevel], asks: List[PriceLevel]) -> float:
    """Orderbook Imbalance Ratio: (bid_vol - ask_vol) / (bid_vol + ask_vol).

    Range: -1.0 (all asks) to +1.0 (all bids).
    """
    bid_vol = sum(b.size for b in bids)
    ask_vol = sum(a.size for a in asks)
    total = bid_vol + ask_vol
    if total == 0:
        return 0
    return (bid_vol - ask_vol) / total


def estimate_slippage(
    levels: List[PriceLevel],
    target_size: float,
    side: str,  # "buy" = asks, "sell" = bids
) -> float:
    """Estimate slippage for a market order of target_size.

    Returns estimated average fill price relative to best price (as pct).
    """
    if not levels or target_size <= 0:
        return 0

    remaining = target_size
    total_cost = 0.0
    total_filled = 0.0
    best_price = levels[0].price

    for level in levels:
        if remaining <= 0:
            break
        fill = min(level.size, remaining)
        total_cost += fill * level.price
        total_filled += fill
        remaining -= fill

    if total_filled == 0:
        return 0

    avg_price = total_cost / total_filled
    slippage_pct = abs(avg_price - best_price) / best_price if best_price else 0
    return slippage_pct


def calculate_fill_ratio(
    levels: List[PriceLevel],
    target_size: float,
) -> float:
    """Return the fraction of *target_size* that can be filled from *levels*.

    Example: if the book has 0.3 BTC and target is 0.5 BTC,
    returns 0.6 (60%).
    """
    if not levels or target_size <= 0:
        return 0.0

    total_available = sum(level.size for level in levels)
    return min(total_available / target_size, 1.0)


def detect_wall(
    levels: List[PriceLevel],
    avg_size: float,
    threshold_mult: float = 3.0,
) -> Optional[Tuple[float, float]]:
    """Detect a price wall (cluster of volume > threshold_mult × average).

    Returns (price, size) of the largest wall, or None.
    """
    if not levels or avg_size <= 0:
        return None
    walls = [level for level in levels if level.size > avg_size * threshold_mult]
    if not walls:
        return None
    biggest = max(walls, key=lambda x: x.size)
    return (biggest.price, biggest.size)
