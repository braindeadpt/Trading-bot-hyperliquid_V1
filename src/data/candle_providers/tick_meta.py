"""Hyperliquid tick / price formatting for parity comparisons."""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from src.utils.helpers import safe_float

PERP_MAX_DECIMALS = 6
SPOT_MAX_DECIMALS = 8

# Fallback szDecimals when meta unavailable (perps).
DEFAULT_SZ_DECIMALS: Dict[str, int] = {
    "BTC": 5,
    "ETH": 4,
    "SOL": 2,
    "HYPE": 2,
}


def sz_decimals_for(symbol: str, meta_cache: Optional[Dict[str, Dict[str, int]]] = None) -> int:
    sym = symbol.upper()
    if meta_cache and sym in meta_cache:
        return int(meta_cache[sym].get("sz_decimals", DEFAULT_SZ_DECIMALS.get(sym, 4)))
    return int(DEFAULT_SZ_DECIMALS.get(sym, 4))


def max_price_decimals(sz_decimals: int, *, is_spot: bool = False) -> int:
    """HL rule: max decimal places = MAX_DECIMALS - szDecimals."""
    base = SPOT_MAX_DECIMALS if is_spot else PERP_MAX_DECIMALS
    return max(0, base - int(sz_decimals))


def format_hl_price(
    value: Any,
    symbol: str,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
    *,
    is_spot: bool = False,
) -> float:
    """Round wire price to HL perp tick rules (5 sig figs + decimal cap)."""
    price = safe_float(value)
    if price > 100_000:
        return float(round(price))
    sz = sz_decimals_for(symbol, meta_cache)
    cap = max_price_decimals(sz, is_spot=is_spot)
    return round(float(f"{price:.5g}"), cap)


def dynamic_quantum_for_price(
    price: float,
    symbol: str,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
    *,
    is_spot: bool = False,
) -> float:
    """Minimum HL price increment at *price* (5 significant figures + decimal cap)."""
    formatted = format_hl_price(price, symbol, meta_cache, is_spot=is_spot)
    if formatted == 0.0:
        cap = max_price_decimals(sz_decimals_for(symbol, meta_cache), is_spot=is_spot)
        return 10.0 ** (-cap) if cap > 0 else 1.0
    if abs(formatted) > 100_000:
        return 1.0
    cap = max_price_decimals(sz_decimals_for(symbol, meta_cache), is_spot=is_spot)
    decimal_floor = 10.0 ** (-cap) if cap > 0 else 1.0
    exp = math.floor(math.log10(abs(formatted)))
    sig_fig_quantum = 10.0 ** (exp - 4)
    return max(sig_fig_quantum, decimal_floor)


def tick_size_for(
    symbol: str,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
    *,
    reference_price: Optional[float] = None,
) -> float:
    """HL tick at *reference_price* when provided; else decimal-cap floor."""
    if reference_price is not None and reference_price != 0.0:
        return dynamic_quantum_for_price(reference_price, symbol, meta_cache)
    sz = sz_decimals_for(symbol, meta_cache)
    cap = max_price_decimals(sz)
    return 10.0 ** (-cap) if cap > 0 else 1.0


def normalize_wire_price(
    symbol: str,
    value: Any,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
) -> float:
    return format_hl_price(value, symbol, meta_cache)


def price_match_ticks(
    symbol: str,
    a: Any,
    b: Any,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
) -> tuple[bool, bool, float]:
    """Return (identical, within_tick, delta_ticks) using dynamic HL quantum."""
    fa = format_hl_price(a, symbol, meta_cache)
    fb = format_hl_price(b, symbol, meta_cache)
    ref = max(abs(fa), abs(fb))
    tick = tick_size_for(symbol, meta_cache, reference_price=ref)
    delta_ticks = abs(fa - fb) / tick if tick > 0 else abs(fa - fb)
    if fa == fb:
        return True, True, delta_ticks
    if delta_ticks <= 1.0 + 1e-9:
        return False, True, delta_ticks
    return False, False, delta_ticks


def load_meta_cache_from_meta_response(raw: Any) -> Dict[str, Dict[str, int]]:
    """Parse HL ``metaAndAssetCtxs`` payload (list or dict) into szDecimals map."""
    meta: Dict[str, Any]
    if isinstance(raw, list) and raw:
        meta = raw[0] if isinstance(raw[0], dict) else {}
    elif isinstance(raw, dict):
        meta = raw
    else:
        return {}

    universe = meta.get("universe", [])
    cache: Dict[str, Dict[str, int]] = {}
    if not isinstance(universe, list):
        return cache
    for entry in universe:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).upper()
        if not name:
            continue
        sz = int(entry.get("szDecimals", 0))
        cache[name] = {
            "sz_decimals": sz,
            "px_decimals": max_price_decimals(sz),
        }
    return cache
