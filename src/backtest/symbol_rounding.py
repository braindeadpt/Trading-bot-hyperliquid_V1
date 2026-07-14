"""Hyperliquid-style size rounding for backtest parity."""

from __future__ import annotations

import math
from typing import Dict, Optional, Union

from src.utils.config import Config, coerce_config

# Fallback when exchange meta is unavailable (matches typical HL perps).
DEFAULT_SZ_DECIMALS: Dict[str, int] = {
    "BTC": 5,
    "ETH": 4,
    "SOL": 2,
    "HYPE": 1,
}


def get_sz_decimals(symbol: str, config: Optional[Union[Config, dict]] = None) -> int:
    """Return size decimal places for *symbol*."""
    if config is not None:
        if isinstance(config, dict):
            raw = (config.get("execution") or {}).get("symbol_sz_decimals", {}) or {}
        else:
            # Config-like (possibly a cross-module-identity instance from
            # main.py's bare-import path) — duck-type through coerce_config
            # rather than a nominal isinstance check, which would silently
            # fail across the utils.config/src.utils.config module split.
            raw = coerce_config(config).get("execution.symbol_sz_decimals", {}) or {}
        if isinstance(raw, dict) and symbol in raw:
            return int(raw[symbol])
    return DEFAULT_SZ_DECIMALS.get(symbol, 4)


def round_position_size(
    symbol: str,
    size: float,
    config: Optional[Union[Config, dict]] = None,
) -> float:
    """Floor size to venue lot step (same semantics as hyperliquid_live.normalize_size)."""
    if size <= 0.0:
        return 0.0
    decimals = get_sz_decimals(symbol, config)
    factor = 10 ** decimals
    return math.floor(size * factor) / factor
