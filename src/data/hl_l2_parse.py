"""Shared HL L2 book parsing for research sampling."""

from __future__ import annotations

from typing import Any, Optional, Tuple

from src.data.research_database import L2SnapshotRecord
from src.data.series_metadata import (
    HL_API_VERSION,
    SOURCE_HL_L2_SAMPLE,
    SOURCE_HL_L2_WS,
    VENUE_HYPERLIQUID,
)
from src.utils.helpers import safe_float


def _level_px_sz(level: Any) -> Tuple[float, float]:
    if isinstance(level, dict):
        return safe_float(level.get("px"), 0.0), safe_float(level.get("sz"), 0.0)
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        return safe_float(level[0], 0.0), safe_float(level[1], 0.0)
    return 0.0, 0.0


def l2_snapshot_from_book(
    symbol: str,
    book: dict,
    ts_ms: int,
    ingested_ms: int,
    *,
    quality_flags: str = '{"sampled":true}',
) -> Optional[L2SnapshotRecord]:
    levels = book.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        return None
    bids, asks = levels[0], levels[1]
    if not bids or not asks:
        return None
    best_bid_px, _ = _level_px_sz(bids[0])
    best_ask_px, _ = _level_px_sz(asks[0])
    if best_bid_px <= 0 or best_ask_px <= 0:
        return None
    mid = (best_bid_px + best_ask_px) / 2.0
    spread_bps = ((best_ask_px - best_bid_px) / mid) * 10_000.0 if mid > 0 else 0.0
    bid_depth = sum(_level_px_sz(lv)[0] * _level_px_sz(lv)[1] for lv in bids[:5])
    ask_depth = sum(_level_px_sz(lv)[0] * _level_px_sz(lv)[1] for lv in asks[:5])
    oir: Optional[float] = None
    total = bid_depth + ask_depth
    if total > 0:
        oir = (bid_depth - ask_depth) / total
    return L2SnapshotRecord(
        symbol=symbol.upper(),
        timestamp_ms=int(book.get("time", ts_ms)),
        mid_price=mid,
        spread_bps=spread_bps,
        bid_depth_usd=bid_depth,
        ask_depth_usd=ask_depth,
        oir=oir,
        source=SOURCE_HL_L2_SAMPLE,
        venue=VENUE_HYPERLIQUID,
        api_version=HL_API_VERSION,
        ingested_at_ms=ingested_ms,
        quality_flags=quality_flags,
    )


def l2_snapshot_from_hl_l2book(
    book: Any,
    ingested_ms: int,
    *,
    source: str = SOURCE_HL_L2_WS,
    quality_flags: str = '{"sampled":true,"ws":true}',
) -> Optional[L2SnapshotRecord]:
    """Build L2SnapshotRecord from a DataBus :class:`HlL2Book` event."""
    bids = getattr(book, "bids", None) or []
    asks = getattr(book, "asks", None) or []
    if not bids or not asks:
        return None
    best_bid = bids[0]
    best_ask = asks[0]
    best_bid_px = getattr(best_bid, "price", 0.0)
    best_ask_px = getattr(best_ask, "price", 0.0)
    if best_bid_px <= 0 or best_ask_px <= 0:
        return None
    mid = (best_bid_px + best_ask_px) / 2.0
    spread_bps = ((best_ask_px - best_bid_px) / mid) * 10_000.0 if mid > 0 else 0.0
    bid_depth = sum(getattr(lv, "price", 0.0) * getattr(lv, "size", 0.0) for lv in bids[:5])
    ask_depth = sum(getattr(lv, "price", 0.0) * getattr(lv, "size", 0.0) for lv in asks[:5])
    oir: Optional[float] = None
    total = bid_depth + ask_depth
    if total > 0:
        oir = (bid_depth - ask_depth) / total
    symbol = str(getattr(book, "symbol", "")).upper()
    ts_ms = int(getattr(book, "timestamp_ms", ingested_ms))
    return L2SnapshotRecord(
        symbol=symbol,
        timestamp_ms=ts_ms,
        mid_price=mid,
        spread_bps=spread_bps,
        bid_depth_usd=bid_depth,
        ask_depth_usd=ask_depth,
        oir=oir,
        source=source,
        venue=VENUE_HYPERLIQUID,
        api_version=HL_API_VERSION,
        ingested_at_ms=ingested_ms,
        quality_flags=quality_flags,
    )
