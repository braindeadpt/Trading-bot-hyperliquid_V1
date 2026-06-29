"""Funding rate normalization and resolution for cross-venue strategies.

v3.1.21: the funding interval is *per-symbol*, not per-exchange.
Binance USDS-M perps use 4h funding for many pairs (not 8h), so
hardcoding 8h would either double-count or under-count depending on
the symbol. We expose:

  * ``EXCHANGE_FUNDING_INTERVAL_HOURS`` — default per-exchange fallback
  * ``SYMBOL_FUNDING_INTERVAL_HOURS`` — explicit per-(exchange, symbol)
    overrides; consulted *first* by ``interval_for()``
  * ``interval_for(exchange, symbol)`` — returns the effective
    interval for a (venue, symbol) pair, falling back to
    ``EXCHANGE_FUNDING_INTERVAL_HOURS[exchange]`` and finally to
    ``DEFAULT_CEX_FUNDING_INTERVAL_HOURS``.
  * ``fetch_binance_funding_interval_hours`` — live fetch of the
    actual interval from Binance USD-M's ``/fapi/v1/fundingInfo``
    endpoint (aiohttp). Caches the result so a single HTTP call
    covers all subsequent calls.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

from src.strategies.base import MarketEvent

# Hyperliquid perps use 1h funding intervals; most CEX perps use 8h.
DEFAULT_CEX_FUNDING_INTERVAL_HOURS = 8.0
DEFAULT_HL_FUNDING_INTERVAL_HOURS = 1.0
TARGET_FUNDING_INTERVAL_HOURS = 8.0

# Venue labels from HL predictedFundings API
HL_VENUE_HL = "HlPerp"
HL_VENUE_BINANCE = "BinPerp"
HL_VENUE_BYBIT = "BybitPerp"

EXCHANGE_FUNDING_INTERVAL_HOURS: Dict[str, float] = {
    "hyperliquid": DEFAULT_HL_FUNDING_INTERVAL_HOURS,
    "hl": DEFAULT_HL_FUNDING_INTERVAL_HOURS,
    "binance": DEFAULT_CEX_FUNDING_INTERVAL_HOURS,
    "bybit": DEFAULT_CEX_FUNDING_INTERVAL_HOURS,
    "okx": DEFAULT_CEX_FUNDING_INTERVAL_HOURS,
    "coinalyze": DEFAULT_CEX_FUNDING_INTERVAL_HOURS,
}

# v3.1.21: per-(exchange, symbol) overrides. Each entry is the
# funding interval in hours that the venue publishes for that
# specific symbol. The default is to consult
# EXCHANGE_FUNDING_INTERVAL_HOURS and then the CEX default.
#
# Binance USDS-M perps that pay funding every 4h instead of 8h (as
# of 2024-2025 — see /fapi/v1/fundingInfo). Operators can extend
# this map at startup.
SYMBOL_FUNDING_INTERVAL_HOURS: Dict[Tuple[str, str], float] = {
    # Binance USD-M: 4h funding for many major pairs
    ("binance", "BTC"): 4.0,
    ("binance", "ETH"): 4.0,
    ("binance", "SOL"): 4.0,
    ("binance", "HYPE"): 4.0,
    # Bybit / OKX are still 8h for the major pairs as of 2024-2025.
    # OKX publishes a ``fundingInterval`` field in its instrument
    # metadata; we default to 8h and let the live fetcher override
    # per-symbol if needed.
}


def interval_for(exchange: str, symbol: Optional[str] = None) -> float:
    """Return the funding interval (in hours) for *exchange* + *symbol*.

    Lookup order:
      1.  ``SYMBOL_FUNDING_INTERVAL_HOURS[(exchange, symbol)]`` — explicit
          per-symbol override.
      2.  ``EXCHANGE_FUNDING_INTERVAL_HOURS[exchange]`` — per-exchange
          default (8h for most CEX, 1h for HL).
      3.  ``DEFAULT_CEX_FUNDING_INTERVAL_HOURS`` (8h) — last-resort
          fallback for unknown exchanges.
    """
    if symbol:
        sym = symbol.upper()
        key = (exchange.lower(), sym)
        if key in SYMBOL_FUNDING_INTERVAL_HOURS:
            return float(SYMBOL_FUNDING_INTERVAL_HOURS[key])
    ex = exchange.lower()
    if ex in EXCHANGE_FUNDING_INTERVAL_HOURS:
        return float(EXCHANGE_FUNDING_INTERVAL_HOURS[ex])
    return DEFAULT_CEX_FUNDING_INTERVAL_HOURS


def register_symbol_interval(
    exchange: str,
    symbol: str,
    interval_hours: float,
) -> None:
    """Register a per-symbol funding interval (used by the live fetchers)."""
    if interval_hours <= 0:
        return
    SYMBOL_FUNDING_INTERVAL_HOURS[(exchange.lower(), symbol.upper())] = float(interval_hours)


# ── Live Binance fetcher ───────────────────────────────────────────


_BINANCE_FUNDING_INFO_URL = "https://fapi.binance.com/fapi/v1/fundingInfo"


async def fetch_binance_funding_interval_hours(
    session,  # aiohttp.ClientSession — typed loosely to avoid hard import
    symbol: str,
) -> Optional[float]:
    """Live fetch of the funding interval for *symbol* from Binance USD-M.

    Hits ``/fapi/v1/fundingInfo`` and reads the per-symbol
    ``fundingIntervalHours`` field. The result is automatically
    registered in ``SYMBOL_FUNDING_INTERVAL_HOURS`` so subsequent
    calls don't re-hit the API. Returns ``None`` on any failure.
    """
    sym = symbol.upper()
    cache_key = ("binance", sym)
    if cache_key in SYMBOL_FUNDING_INTERVAL_HOURS:
        return float(SYMBOL_FUNDING_INTERVAL_HOURS[cache_key])
    try:
        async with session.get(
            _BINANCE_FUNDING_INFO_URL, params={"symbol": f"{sym}USDT"},
            timeout=5.0,
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, list) or not data:
        return None
    row = data[0]
    if not isinstance(row, dict):
        return None
    # The fundingInfo field is ``fundingIntervalHours`` (Binance
    # 2024+). Older docs return ``fundingInterval`` in hours.
    hours_raw = row.get("fundingIntervalHours")
    if hours_raw is None:
        hours_raw = row.get("fundingInterval")
    try:
        hours = float(hours_raw)
    except (TypeError, ValueError):
        return None
    if hours <= 0 or not math.isfinite(hours):
        return None
    register_symbol_interval("binance", sym, hours)
    return hours


# ── Existing API (preserved for back-compat) ────────────────────────


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


def normalize_funding_for(
    rate: float,
    exchange: str,
    symbol: Optional[str] = None,
    *,
    target_hours: float = TARGET_FUNDING_INTERVAL_HOURS,
) -> float:
    """Normalize a per-interval funding rate for a specific (venue, symbol).

    Looks up the actual funding interval via :func:`interval_for` so
    that Binance 4h funding is treated correctly (1h rate × 8 = 8h
    rate, not 1h rate × 1 = 1h rate which the old global default
    would have produced).
    """
    hours = interval_for(exchange, symbol)
    return normalize_funding_to_8h(rate, hours, target_hours=target_hours)


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
