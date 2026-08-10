"""Shared liquidation event model + provenance helpers.

Real venues publish individual forced-liquidation prints. ``source="real"`` is
the *engine provenance label* meaning "at least one genuine venue contributed
to the rolling window" — never a wire-format source string from a feed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Wire-format sources that may be summed into MarketEvent 5m aggregates.
REAL_LIQUIDATION_SOURCES = frozenset({"hl", "okx", "bybit", "binance"})

# Heuristic candle+OI synthesis — never treated as real.
PROXY_LIQUIDATION_SOURCES = frozenset({"proxy"})

# Aggregator APIs that already include other venues — verify / gap-check only.
# Never publish into ``liquidation:{symbol}`` for strategy scoring.
VERIFY_ONLY_LIQUIDATION_SOURCES = frozenset({"coinalyze"})

# Provenance labels accepted by LiquidationCatcher / ChecklistMeta when
# ``require_real_liquidation_data`` is True.
ACCEPTABLE_REAL_PROVENANCE = frozenset({"real"}) | REAL_LIQUIDATION_SOURCES


@dataclass(frozen=True, slots=True)
class LiquidationEvent:
    """Normalized forced-liquidation print for DataBus ``liquidation:{symbol}``."""

    symbol: str  # base asset, e.g. BTC
    timestamp_ms: int
    notional_usd: float
    side: str  # "long" = longs liquidated, "short" = shorts liquidated
    source: str = "unknown"  # hl | okx | bybit | binance (never proxy/coinalyze here)


def is_real_liquidation_source(source: Optional[str]) -> bool:
    """True when provenance is a genuine venue or the rollup label ``real``."""
    if source is None:
        return False
    return str(source).strip().lower() in ACCEPTABLE_REAL_PROVENANCE


def is_proxy_liquidation_source(source: Optional[str]) -> bool:
    if source is None:
        return False
    return str(source).strip().lower() in PROXY_LIQUIDATION_SOURCES
