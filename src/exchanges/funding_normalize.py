"""Funding rate normalization and resolution for cross-venue strategies."""

from __future__ import annotations

import math
from typing import Dict, Optional

from src.strategies.base import MarketEvent

# Hyperliquid perps use 1h funding intervals; most CEX perps use 8h.
DEFAULT_CEX_FUNDING_INTERVAL_HOURS = 8.0
DEFAULT_HL_FUNDING_INTERVAL_HOURS = 1.0
TARGET_FUNDING_INTERVAL_HOURS = 8.0

# Venue labels from HL predictedFundings API
HL_VENUE_HL = "HlPerp"
HL_VENUE_BINANCE = "BinPerp"
HL_VENUE_BYBIT = "BybitPerp"

EXCHANGE_FUNDING_INTERVAL_HOURS: dict[str, float] = {
    "hyperliquid": DEFAULT_HL_FUNDING_INTERVAL_HOURS,
    "hl": DEFAULT_HL_FUNDING_INTERVAL_HOURS,
    "binance": DEFAULT_CEX_FUNDING_INTERVAL_HOURS,
    "bybit": DEFAULT_CEX_FUNDING_INTERVAL_HOURS,
    "okx": DEFAULT_CEX_FUNDING_INTERVAL_HOURS,
    "coinalyze": DEFAULT_CEX_FUNDING_INTERVAL_HOURS,
}


def parse_optional_rate(value: object) -> Optional[float]:
    """Parse API funding field; return None if missing or empty."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate):
        return None
    return rate


def normalize_funding_to_8h(
    rate: float,
    interval_hours: float,
    *,
    target_hours: float = TARGET_FUNDING_INTERVAL_HOURS,
) -> float:
    """Scale a per-interval funding rate to an equivalent *target_hours* rate."""
    if interval_hours <= 0:
        interval_hours = DEFAULT_CEX_FUNDING_INTERVAL_HOURS
    if target_hours <= 0:
        target_hours = TARGET_FUNDING_INTERVAL_HOURS
    return rate * (target_hours / interval_hours)


def is_valid_funding(rate: Optional[float]) -> bool:
    """True when rate is a finite number (zero is valid on HL)."""
    return rate is not None and math.isfinite(rate)


def resolve_effective_funding(
    event: MarketEvent,
    *,
    prefer_predicted: bool = True,
) -> Optional[float]:
    """Pick the best available funding rate for signal logic (all 8h-normalized when possible).

    Priority:
      1. Cross-exchange predicted average (aggregator, 8h-normalized)
      2. Engine-filled HL predicted (INFO predictedFundings, 8h-normalized)
      3. OI-weighted / simple cross-exchange average
      4. HL WebSocket current funding (8h-normalized in MarketEvent)
    """
    candidates: list[Optional[float]] = []
    if prefer_predicted:
        candidates.extend([
            event.predicted_funding_avg,
            event.predicted_funding,
        ])
    candidates.extend([
        event.funding_weighted,
        event.funding_avg,
        event.funding,
    ])
    for rate in candidates:
        if is_valid_funding(rate):
            return rate
    return None


def venue_funding_spread(venues: Optional[Dict[str, float]]) -> float:
    """Max minus min 8h funding across HL predictedFundings venues."""
    if not venues or len(venues) < 2:
        return 0.0
    vals = [v for v in venues.values() if is_valid_funding(v)]
    if len(vals) < 2:
        return 0.0
    return max(vals) - min(vals)


def funding_data_usable(
    event: MarketEvent,
    *,
    require_feed_health: bool = False,
    max_venue_spread: float = 0.001,
) -> bool:
    """False when feed is red, stale-only, or cross-venue funding disagrees."""
    if require_feed_health and event.market_data_health == "red":
        return False
    if not is_valid_funding(resolve_effective_funding(event, prefer_predicted=True)):
        return False
    spread = venue_funding_spread(event.predicted_funding_by_venue)
    if spread > max_venue_spread:
        return False
    return True
