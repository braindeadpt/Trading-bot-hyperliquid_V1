"""Hyperliquid-native liquidation map — research data infrastructure (Phase 1).

Harvest active whale addresses from ``node_fills_by_block`` archives, fetch
public ``clearinghouseState`` positions (including real ``liquidationPx``),
aggregate into price-band zones, and persist snapshots to the research DB.

No strategy logic. Not wired into the live bot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from src.data.candle_providers.node_trades_parser import (
    NodeFillLeg,
    iter_fill_legs_with_address,
    maybe_decompress_lz4,
)
from src.data.research_database import ResearchDatabase
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

FillsSource = Union[bytes, str, Path, List[Dict[str, Any]], Iterable[NodeFillLeg]]


@dataclass(frozen=True)
class HlOpenPosition:
    """One open perp position with a real Hyperliquid liquidation price."""

    address: str
    coin: str
    szi: float
    side: str  # long | short
    entry_px: float
    leverage: float
    liquidation_px: float
    margin_used: float
    notional_usd: float
    fetched_at_ms: int


@dataclass(frozen=True)
class LiquidationZone:
    """Aggregated liquidation liquidity in a price band for one coin/side."""

    coin: str
    side: str  # long | short — side of the *positions* that liquidate in this band
    price_low: float
    price_high: float
    total_notional_usd: float
    position_count: int
    distance_pct_from_mark: float
    mark_px: float


@dataclass
class FetchPositionsResult:
    positions: List[HlOpenPosition] = field(default_factory=list)
    errors: List[Dict[str, str]] = field(default_factory=list)
    addresses_queried: int = 0


def _load_fills_payload(source: Union[bytes, str, Path]) -> Union[bytes, str]:
    if isinstance(source, Path):
        data = source.read_bytes()
        # Decompress here so callers can pass .lz4 paths; iterator also
        # accepts already-decompressed text.
        try:
            data = maybe_decompress_lz4(data)
        except Exception:
            pass
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data
    return source


def _iter_legs(fills_source: FillsSource) -> Iterable[NodeFillLeg]:
    if isinstance(fills_source, Path):
        yield from iter_fill_legs_with_address(_load_fills_payload(fills_source))
        return
    if isinstance(fills_source, (bytes, bytearray, str)):
        yield from iter_fill_legs_with_address(fills_source)
        return
    if isinstance(fills_source, list) and fills_source and isinstance(fills_source[0], dict):
        yield from iter_fill_legs_with_address(fills_source)  # type: ignore[arg-type]
        return
    # Already an iterable of NodeFillLeg (or empty)
    for item in fills_source:  # type: ignore[union-attr]
        if isinstance(item, NodeFillLeg):
            yield item


def harvest_addresses_from_files(
    paths: Sequence[Path],
    *,
    top_n: int = 200,
    min_notional_usd: float = 50_000.0,
    coins: Optional[Sequence[str]] = None,
) -> List[str]:
    """Harvest across one or more local ``node_fills`` archive files (.lz4 or NDJSON)."""
    legs: List[NodeFillLeg] = []
    for path in paths:
        legs.extend(list(iter_fill_legs_with_address(_load_fills_payload(Path(path)))))
    return harvest_addresses(
        legs, top_n=top_n, min_notional_usd=min_notional_usd, coins=coins,
    )


def harvest_addresses(
    fills_source: FillsSource,
    *,
    top_n: int = 200,
    min_notional_usd: float = 50_000.0,
    coins: Optional[Sequence[str]] = None,
) -> List[str]:
    """Rank addresses **per coin** by traded notional; return the deduplicated union.

    For each coin independently: keep addresses with per-coin notional
    ``>= min_notional_usd``, take the top ``top_n``, then union across coins.
    This preserves coin-specialist whales that a global sum ranking would
    crowd out on smaller markets.

    Pure / offline. Both counterparty legs of each trade are counted (address
    harvest must see every active wallet).
    """
    coin_filter = {c.upper() for c in coins} if coins else None
    # coin -> address -> notional
    by_coin: Dict[str, Dict[str, float]] = {}

    for leg in _iter_legs(fills_source):
        if coin_filter is not None and leg.coin not in coin_filter:
            continue
        notional = abs(leg.size) * abs(leg.price)
        if notional <= 0:
            continue
        addr = leg.address.lower()
        bucket = by_coin.setdefault(leg.coin, {})
        bucket[addr] = bucket.get(addr, 0.0) + notional

    selected: List[str] = []
    seen: set[str] = set()
    threshold = float(min_notional_usd)
    n = max(0, int(top_n))
    for coin in sorted(by_coin):
        ranked = sorted(
            ((a, v) for a, v in by_coin[coin].items() if v >= threshold),
            key=lambda x: x[1],
            reverse=True,
        )
        for addr, _ in ranked[:n]:
            if addr not in seen:
                seen.add(addr)
                selected.append(addr)
    return selected


def parse_clearinghouse_positions(
    raw: Dict[str, Any],
    address: str,
    *,
    fetched_at_ms: Optional[int] = None,
    coins: Optional[Sequence[str]] = None,
) -> List[HlOpenPosition]:
    """Parse ``clearinghouseState`` JSON into positions with valid liquidationPx."""
    if not isinstance(raw, dict):
        return []
    coin_filter = {c.upper() for c in coins} if coins else None
    now_ms = int(fetched_at_ms if fetched_at_ms is not None else time.time() * 1000)
    out: List[HlOpenPosition] = []

    for item in raw.get("assetPositions", []) or []:
        if not isinstance(item, dict):
            continue
        pos = item.get("position")
        if not isinstance(pos, dict):
            continue
        coin = str(pos.get("coin", "")).upper()
        if not coin:
            continue
        if coin_filter is not None and coin not in coin_filter:
            continue
        szi = safe_float(pos.get("szi"))
        if abs(szi) < 1e-12:
            continue
        liq_raw = pos.get("liquidationPx")
        if liq_raw is None or liq_raw == "":
            continue
        liq_px = safe_float(liq_raw)
        if liq_px <= 0:
            continue
        entry_px = safe_float(pos.get("entryPx"))
        margin_used = safe_float(pos.get("marginUsed"))
        lev_obj = pos.get("leverage")
        if isinstance(lev_obj, dict):
            leverage = safe_float(lev_obj.get("value", lev_obj.get("leverage")))
        else:
            leverage = safe_float(lev_obj)
        # Prefer mark-based positionValue when the API provides it.
        position_value = abs(safe_float(pos.get("positionValue")))
        if position_value > 0:
            notional = position_value
        elif entry_px > 0:
            notional = abs(szi) * entry_px
        else:
            notional = abs(szi) * liq_px
        side = "long" if szi > 0 else "short"
        out.append(
            HlOpenPosition(
                address=address.lower(),
                coin=coin,
                szi=szi,
                side=side,
                entry_px=entry_px,
                leverage=leverage,
                liquidation_px=liq_px,
                margin_used=margin_used,
                notional_usd=notional,
                fetched_at_ms=now_ms,
            ),
        )
    return out


async def fetch_positions(
    addresses: Sequence[str],
    *,
    client: Any,
    delay_ms: int = 150,
    max_addresses: int = 300,
    coins: Optional[Sequence[str]] = None,
) -> FetchPositionsResult:
    """Fetch ``clearinghouseState`` per address; never raise on a single failure."""
    result = FetchPositionsResult()
    capped = list(addresses)[: max(0, int(max_addresses))]
    result.addresses_queried = len(capped)
    delay_s = max(0.0, float(delay_ms) / 1000.0)
    fetched_at = int(time.time() * 1000)

    for i, addr in enumerate(capped):
        try:
            raw = await client.clearinghouse_state(addr)
            result.positions.extend(
                parse_clearinghouse_positions(
                    raw, addr, fetched_at_ms=fetched_at, coins=coins,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — per-address isolation
            logger.warning("clearinghouseState failed for %s: %s", addr, exc)
            result.errors.append({"address": addr, "error": str(exc)})
        if i + 1 < len(capped) and delay_s > 0:
            await asyncio.sleep(delay_s)
    return result


def _bucket_bounds(liq_px: float, mark_px: float, bucket_pct: float) -> Tuple[float, float, int]:
    """Return (price_low, price_high, bucket_index) for a liquidation price.

    Bands are ``bucket_pct`` percent of *mark* wide, anchored at mark:
    band k covers ``mark * (1 + k*bp) .. mark * (1 + (k+1)*bp)`` where
    ``bp = bucket_pct / 100``.
    """
    if mark_px <= 0 or bucket_pct <= 0:
        raise ValueError("mark_px and bucket_pct must be positive")
    bp = float(bucket_pct) / 100.0
    rel = (liq_px / mark_px) - 1.0
    # Use floor; for exact boundary on the high edge, assign to previous bucket
    # except for the exact mark (rel==0 → bucket 0 lower edge).
    k = math.floor(rel / bp + 1e-12)
    low = mark_px * (1.0 + k * bp)
    high = mark_px * (1.0 + (k + 1) * bp)
    return low, high, k


def build_zones(
    positions: Sequence[HlOpenPosition],
    *,
    bucket_pct: float = 0.25,
    min_zone_notional_usd: float = 100_000.0,
    mark_prices: Optional[Dict[str, float]] = None,
) -> List[LiquidationZone]:
    """Aggregate liquidation prices into mark-relative percentage bands.

    *side* on a zone is the position side that liquidates there (longs below
    mark, shorts above — but we do not force-filter; we bucket whatever liq
    price the venue reports).
    """
    marks = {k.upper(): float(v) for k, v in (mark_prices or {}).items()}
    # Infer mark from entry px median-ish fallback: mean of |entry| per coin
    if not marks:
        scratch: Dict[str, List[float]] = {}
        for p in positions:
            if p.entry_px > 0:
                scratch.setdefault(p.coin, []).append(p.entry_px)
        for coin, vals in scratch.items():
            marks[coin] = sum(vals) / len(vals)

    # key: (coin, side, bucket_index)
    buckets: Dict[Tuple[str, str, int], Dict[str, Any]] = {}

    for p in positions:
        mark = marks.get(p.coin)
        if mark is None or mark <= 0:
            continue
        try:
            low, high, k = _bucket_bounds(p.liquidation_px, mark, bucket_pct)
        except ValueError:
            continue
        key = (p.coin, p.side, k)
        slot = buckets.get(key)
        if slot is None:
            slot = {
                "coin": p.coin,
                "side": p.side,
                "price_low": low,
                "price_high": high,
                "total_notional_usd": 0.0,
                "position_count": 0,
                "mark_px": mark,
                "bucket_index": k,
            }
            buckets[key] = slot
        slot["total_notional_usd"] += float(p.notional_usd)
        slot["position_count"] += 1

    zones: List[LiquidationZone] = []
    for slot in buckets.values():
        if slot["total_notional_usd"] < float(min_zone_notional_usd):
            continue
        mid = 0.5 * (slot["price_low"] + slot["price_high"])
        mark = float(slot["mark_px"])
        dist = ((mid / mark) - 1.0) * 100.0 if mark > 0 else 0.0
        zones.append(
            LiquidationZone(
                coin=slot["coin"],
                side=slot["side"],
                price_low=float(slot["price_low"]),
                price_high=float(slot["price_high"]),
                total_notional_usd=float(slot["total_notional_usd"]),
                position_count=int(slot["position_count"]),
                distance_pct_from_mark=dist,
                mark_px=mark,
            ),
        )

    zones.sort(key=lambda z: (z.coin, -z.total_notional_usd))
    return zones


def persist_snapshot(
    db: ResearchDatabase,
    zones: Sequence[LiquidationZone],
    positions_meta: Optional[Dict[str, Any]] = None,
    *,
    snapshot_id: Optional[str] = None,
    fetched_at_ms: Optional[int] = None,
) -> str:
    """Persist zones to ``liquidation_map_snapshots``. Returns snapshot_id."""
    sid = snapshot_id or str(uuid.uuid4())
    ts = int(fetched_at_ms if fetched_at_ms is not None else time.time() * 1000)
    meta = dict(positions_meta or {})
    meta_json = json.dumps(meta, sort_keys=True) if meta else None

    rows = [
        {
            "snapshot_id": sid,
            "fetched_at_ms": ts,
            "coin": z.coin,
            "side": z.side,
            "price_low": z.price_low,
            "price_high": z.price_high,
            "total_notional_usd": z.total_notional_usd,
            "position_count": z.position_count,
            "distance_pct_from_mark": z.distance_pct_from_mark,
            "mark_px": z.mark_px,
            "meta_json": meta_json,
        }
        for z in zones
    ]
    db.save_liquidation_map_zones(rows)
    return sid


def load_latest_snapshot(
    db: ResearchDatabase,
    coin: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load the most recent persisted liquidation-map zones."""
    return db.load_latest_liquidation_map(coin=coin)


def zone_to_dict(zone: LiquidationZone) -> Dict[str, Any]:
    return asdict(zone)
