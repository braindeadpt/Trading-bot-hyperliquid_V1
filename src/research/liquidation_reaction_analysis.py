"""Liquidation-map Phase 2 — reaction analysis (research-only).

Two complementary tracks:

**Approach A (retrospective evidence):** Hyperliquid ``node_fills_by_block``
archives attach an optional ``liquidation`` object to fills that participate
in a forced liquidation. When present, we can measure post-event price
behaviour on trusted research candles (never ``goldrush``). This yields
real — but typically small-N — historical evidence.

**Approach B (prospective scaffold):** Phase-1 ``liquidation_map_snapshots``
capture *open* positions as of the snapshot time. They cannot be back-tested
against earlier price. The forward tracker correlates accumulated snapshots
with *subsequent* candles; until many zone-approach events exist, this path
reports **no evidence yet**.

Neither track is a trading strategy. Nothing here is wired into the live bot.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from src.data.candle_providers.node_trades_parser import (
    iter_fills_by_block_records,
    maybe_decompress_lz4,
)
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

# Prefer HL-official / local tape sources; never use GoldRush for Phase-2 math
# (known OHLC parity divergence — see AGENTS.md / GoldRush diagnostics).
PREFERRED_CANDLE_SOURCES = (
    "hl_candleSnapshot",
    "hl_node_trades_rebuild",
    "hl_ws_1m_tape_agg",
)

FillsPayload = Union[bytes, str, Path, List[Dict[str, Any]]]


@dataclass(frozen=True)
class LiquidationEvent:
    """One forced-liquidation fill (liquidated user's leg)."""

    coin: str
    time_ms: int
    price: float
    size: float
    liquidated_side: str  # long | short — side of the position that was liquidated
    liquidated_user: str
    mark_px: float
    method: str
    dir: str
    tid: Optional[Any] = None
    notional_usd: float = 0.0


@dataclass(frozen=True)
class ReactionResult:
    """Price reaction around a single liquidation event."""

    event: LiquidationEvent
    entry_close: float
    flush_return_pct: float
    reverse_return_pct: float
    flushed: bool
    reversed: bool
    candle_source: str
    flush_minutes: int
    reverse_minutes: int
    nearest_opposite_cluster_mid: Optional[float] = None
    distance_to_opposite_pct: Optional[float] = None


@dataclass
class ReactionReport:
    """Aggregate Approach A results — honest small-N statistics."""

    n_events: int = 0
    n_with_candles: int = 0
    n_flushed: int = 0
    n_reversed: int = 0
    mean_flush_return_pct: float = 0.0
    mean_reverse_return_pct: float = 0.0
    by_coin: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    results: List[ReactionResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_events": self.n_events,
            "n_with_candles": self.n_with_candles,
            "n_flushed": self.n_flushed,
            "n_reversed": self.n_reversed,
            "flush_rate": (
                self.n_flushed / self.n_with_candles if self.n_with_candles else None
            ),
            "reverse_rate": (
                self.n_reversed / self.n_with_candles if self.n_with_candles else None
            ),
            "mean_flush_return_pct": self.mean_flush_return_pct,
            "mean_reverse_return_pct": self.mean_reverse_return_pct,
            "by_coin": self.by_coin,
            "notes": self.notes,
            "evidence_class": "retrospective_small_n",
        }


@dataclass
class ZoneApproachEvent:
    """One Approach-B observation: price entered a persisted zone's neighbourhood."""

    snapshot_id: str
    fetched_at_ms: int
    coin: str
    side: str
    price_low: float
    price_high: float
    approach_time_ms: int
    approach_price: float
    forward_return_pct: float
    reaction: str  # reverse | accelerate | none


@dataclass
class ForwardTrackReport:
    """Approach B — prospective only; empty until enough snapshots accumulate."""

    n_snapshots: int = 0
    n_zones: int = 0
    n_approaches: int = 0
    reactions: Dict[str, int] = field(default_factory=dict)
    events: List[ZoneApproachEvent] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_snapshots": self.n_snapshots,
            "n_zones": self.n_zones,
            "n_approaches": self.n_approaches,
            "reactions": self.reactions,
            "notes": self.notes,
            "evidence_class": "prospective_scaffold",
            "evidence_ready": False,
        }


def _load_text(payload: FillsPayload) -> Union[str, List[Dict[str, Any]]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Path):
        data = maybe_decompress_lz4(payload.read_bytes())
        return data.decode("utf-8")
    if isinstance(payload, (bytes, bytearray)):
        return maybe_decompress_lz4(bytes(payload)).decode("utf-8")
    return payload


def _infer_liquidated_side(dir_value: str) -> Optional[str]:
    """Map HL fill ``dir`` to the side of the position that was liquidated."""
    d = (dir_value or "").strip().lower()
    if d == "close long":
        return "long"
    if d == "close short":
        return "short"
    return None


def extract_liquidation_events(
    payload: FillsPayload,
    *,
    coins: Optional[Sequence[str]] = None,
    require_user_leg: bool = True,
) -> List[LiquidationEvent]:
    """Extract forced-liquidation events from ``node_fills_by_block`` content.

    A fill is a liquidation when it carries a ``liquidation`` object with
    ``liquidatedUser``. When *require_user_leg* is True (default), only the
    liquidated user's own fill leg is kept (address == liquidatedUser), and
    ``dir`` must be ``Close Long`` / ``Close Short`` so side is unambiguous.

    Deduplicates by ``(tid, liquidated_user)``.
    """
    coin_filter = {c.upper() for c in coins} if coins else None
    loaded = _load_text(payload)
    blocks: Iterable[Dict[str, Any]]
    if isinstance(loaded, list):
        blocks = loaded
    else:
        blocks = iter_fills_by_block_records(loaded)

    seen: set[Tuple[Any, str]] = set()
    out: List[LiquidationEvent] = []

    for block in blocks:
        if not isinstance(block, dict):
            continue
        for event in block.get("events") or []:
            if not (isinstance(event, (list, tuple)) and len(event) == 2):
                continue
            address, fill = event
            if not isinstance(fill, dict):
                continue
            liq = fill.get("liquidation")
            if not isinstance(liq, dict):
                continue
            user = str(liq.get("liquidatedUser") or "").strip().lower()
            if not user:
                continue
            addr = str(address or "").strip().lower()
            if require_user_leg and addr != user:
                continue
            coin = str(fill.get("coin", "")).upper()
            if not coin:
                continue
            if coin_filter is not None and coin not in coin_filter:
                continue
            side = _infer_liquidated_side(str(fill.get("dir", "")))
            if side is None:
                continue
            tid = fill.get("tid")
            key = (tid, user)
            if tid is not None and key in seen:
                continue
            if tid is not None:
                seen.add(key)
            px = safe_float(fill.get("px"))
            sz = abs(safe_float(fill.get("sz")))
            mark = safe_float(liq.get("markPx"), px)
            out.append(
                LiquidationEvent(
                    coin=coin,
                    time_ms=int(safe_float(fill.get("time"))),
                    price=px,
                    size=sz,
                    liquidated_side=side,
                    liquidated_user=user,
                    mark_px=mark,
                    method=str(liq.get("method") or ""),
                    dir=str(fill.get("dir") or ""),
                    tid=tid,
                    notional_usd=px * sz,
                ),
            )
    out.sort(key=lambda e: (e.time_ms, e.coin))
    return out


def cluster_liquidation_prices(
    events: Sequence[LiquidationEvent],
    *,
    bucket_pct: float = 0.25,
    min_events: int = 2,
    mark_px: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Same-hour proxy zones: bucket liquidation *event* prices by side.

    Unlike Phase-1 ``build_zones`` (open-position ``liquidationPx``), this
    clusters prices at which forced liquidations actually printed.
    """
    if not events:
        return []
    by_coin: Dict[str, List[LiquidationEvent]] = {}
    for e in events:
        by_coin.setdefault(e.coin, []).append(e)

    clusters: List[Dict[str, Any]] = []
    bp = float(bucket_pct) / 100.0
    if bp <= 0:
        return []

    for coin, evs in by_coin.items():
        mark = float(mark_px) if mark_px and mark_px > 0 else (
            sum(e.mark_px or e.price for e in evs) / len(evs)
        )
        if mark <= 0:
            continue
        buckets: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for e in evs:
            rel = (e.price / mark) - 1.0
            k = math.floor(rel / bp + 1e-12)
            key = (e.liquidated_side, k)
            slot = buckets.get(key)
            if slot is None:
                low = mark * (1.0 + k * bp)
                high = mark * (1.0 + (k + 1) * bp)
                slot = {
                    "coin": coin,
                    "side": e.liquidated_side,
                    "price_low": low,
                    "price_high": high,
                    "event_count": 0,
                    "total_notional_usd": 0.0,
                    "mark_px": mark,
                }
                buckets[key] = slot
            slot["event_count"] += 1
            slot["total_notional_usd"] += e.notional_usd
        for slot in buckets.values():
            if slot["event_count"] >= int(min_events):
                mid = 0.5 * (slot["price_low"] + slot["price_high"])
                slot["mid"] = mid
                slot["distance_pct_from_mark"] = ((mid / mark) - 1.0) * 100.0
                clusters.append(slot)
    clusters.sort(key=lambda c: (c["coin"], -c["total_notional_usd"]))
    return clusters


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_trusted_candles_1m(
    db_path: Path,
    symbol: str,
    *,
    start_ms: int,
    end_ms: int,
    preferred_sources: Sequence[str] = PREFERRED_CANDLE_SOURCES,
) -> Tuple[List[Dict[str, Any]], str]:
    """Load 1m OHLCV from research DB, preferring trusted sources.

    Returns ``(rows, source_used)``. Never returns GoldRush rows.
    """
    if not db_path.exists():
        return [], ""
    conn = _open_readonly(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(candles_1m)").fetchall()}
        has_source = "source" in cols
        if has_source:
            placeholders = ",".join("?" for _ in preferred_sources)
            sql = (
                f"SELECT timestamp_ms, open, high, low, close, volume, source "
                f"FROM candles_1m WHERE symbol = ? AND timestamp_ms >= ? AND timestamp_ms <= ? "
                f"AND source IN ({placeholders}) ORDER BY timestamp_ms ASC"
            )
            params: List[Any] = [symbol.upper(), int(start_ms), int(end_ms), *preferred_sources]
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            if rows:
                # Prefer the source with most bars in-window among preferred set
                counts: Dict[str, int] = {}
                for r in rows:
                    counts[str(r["source"])] = counts.get(str(r["source"]), 0) + 1
                best = max(counts, key=lambda s: counts[s])
                filtered = [r for r in rows if r["source"] == best]
                return filtered, best
            return [], ""
        # Live-schema DB without source column (read-only OK for Approach B)
        sql = (
            "SELECT timestamp_ms, open, high, low, close, volume "
            "FROM candles_1m WHERE symbol = ? AND timestamp_ms >= ? AND timestamp_ms <= ? "
            "ORDER BY timestamp_ms ASC"
        )
        rows = [dict(r) for r in conn.execute(sql, (symbol.upper(), int(start_ms), int(end_ms))).fetchall()]
        return rows, "unlabeled"
    finally:
        conn.close()


def _close_at_or_before(candles: Sequence[Dict[str, Any]], ts_ms: int) -> Optional[float]:
    chosen: Optional[float] = None
    for c in candles:
        if int(c["timestamp_ms"]) <= ts_ms:
            chosen = float(c["close"])
        else:
            break
    return chosen


def measure_reaction(
    event: LiquidationEvent,
    candles: Sequence[Dict[str, Any]],
    *,
    flush_minutes: int = 5,
    reverse_minutes: int = 30,
    flush_threshold_pct: float = 0.05,
    reverse_threshold_pct: float = 0.05,
    candle_source: str = "",
    opposite_clusters: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[ReactionResult]:
    """Measure flush-then-reverse around a liquidation using 1m closes.

    Conventions (signed return from entry close):
    - Long liquidated (price dumped into longs): flush = further **down** (neg);
      reverse = bounce **up**.
    - Short liquidated (price pumped into shorts): flush = further **up** (pos);
      reverse = dump **down**.
    """
    if not candles:
        return None
    entry = _close_at_or_before(candles, event.time_ms)
    if entry is None or entry <= 0:
        return None
    flush_ts = event.time_ms + int(flush_minutes) * 60_000
    reverse_ts = event.time_ms + int(reverse_minutes) * 60_000
    flush_px = _close_at_or_before(candles, flush_ts)
    reverse_px = _close_at_or_before(candles, reverse_ts)
    if flush_px is None or reverse_px is None:
        return None

    flush_ret = (flush_px / entry - 1.0) * 100.0
    reverse_ret = (reverse_px / entry - 1.0) * 100.0
    thr_f = float(flush_threshold_pct)
    thr_r = float(reverse_threshold_pct)

    if event.liquidated_side == "long":
        flushed = flush_ret <= -thr_f
        # Reverse = recovered toward/above entry after a dump
        reversed_ = reverse_ret >= thr_r
    else:
        flushed = flush_ret >= thr_f
        reversed_ = reverse_ret <= -thr_r

    opp_mid: Optional[float] = None
    dist_opp: Optional[float] = None
    if opposite_clusters:
        want = "short" if event.liquidated_side == "long" else "long"
        cands = [
            c for c in opposite_clusters
            if c.get("coin") == event.coin and c.get("side") == want
        ]
        if cands:
            best = min(cands, key=lambda c: abs(float(c["mid"]) - event.price))
            opp_mid = float(best["mid"])
            dist_opp = ((opp_mid / event.price) - 1.0) * 100.0 if event.price else None

    return ReactionResult(
        event=event,
        entry_close=entry,
        flush_return_pct=flush_ret,
        reverse_return_pct=reverse_ret,
        flushed=flushed,
        reversed=reversed_,
        candle_source=candle_source,
        flush_minutes=int(flush_minutes),
        reverse_minutes=int(reverse_minutes),
        nearest_opposite_cluster_mid=opp_mid,
        distance_to_opposite_pct=dist_opp,
    )


def run_retrospective_analysis(
    fills_paths: Sequence[Path],
    research_db: Path,
    *,
    coins: Sequence[str] = ("BTC", "ETH", "SOL", "HYPE"),
    flush_minutes: int = 5,
    reverse_minutes: int = 30,
    flush_threshold_pct: float = 0.05,
    reverse_threshold_pct: float = 0.05,
    pad_ms: int = 3_600_000,
) -> ReactionReport:
    """Approach A: scan fills archives for liquidations and measure reactions."""
    report = ReactionReport()
    report.notes.append(
        "Approach A uses fills with a real `liquidation` object; "
        "candles prefer hl_candleSnapshot / hl_node_trades_rebuild / hl_ws_1m_tape_agg "
        "(never goldrush)."
    )

    all_events: List[LiquidationEvent] = []
    for path in fills_paths:
        if not path.exists():
            report.notes.append(f"missing fills file: {path}")
            continue
        all_events.extend(extract_liquidation_events(path, coins=coins))

    # Dedupe across files
    uniq: Dict[Tuple[Any, str], LiquidationEvent] = {}
    for e in all_events:
        key = (e.tid, e.liquidated_user)
        if e.tid is None:
            uniq[(id(e), e.liquidated_user)] = e
        else:
            uniq[key] = e
    events = sorted(uniq.values(), key=lambda e: e.time_ms)
    report.n_events = len(events)

    clusters = cluster_liquidation_prices(events, bucket_pct=0.25, min_events=2)

    results: List[ReactionResult] = []
    by_coin: Dict[str, List[ReactionResult]] = {}
    for ev in events:
        start = ev.time_ms - 60_000
        end = ev.time_ms + int(reverse_minutes) * 60_000 + pad_ms
        candles, src = load_trusted_candles_1m(
            research_db, ev.coin, start_ms=start, end_ms=end,
        )
        if not candles:
            continue
        rr = measure_reaction(
            ev,
            candles,
            flush_minutes=flush_minutes,
            reverse_minutes=reverse_minutes,
            flush_threshold_pct=flush_threshold_pct,
            reverse_threshold_pct=reverse_threshold_pct,
            candle_source=src,
            opposite_clusters=clusters,
        )
        if rr is None:
            continue
        results.append(rr)
        by_coin.setdefault(ev.coin, []).append(rr)

    report.results = results
    report.n_with_candles = len(results)
    report.n_flushed = sum(1 for r in results if r.flushed)
    report.n_reversed = sum(1 for r in results if r.reversed)
    if results:
        report.mean_flush_return_pct = sum(r.flush_return_pct for r in results) / len(results)
        report.mean_reverse_return_pct = sum(r.reverse_return_pct for r in results) / len(results)

    for coin, rows in by_coin.items():
        report.by_coin[coin] = {
            "n": len(rows),
            "flushed": sum(1 for r in rows if r.flushed),
            "reversed": sum(1 for r in rows if r.reversed),
            "mean_flush_return_pct": sum(r.flush_return_pct for r in rows) / len(rows),
            "mean_reverse_return_pct": sum(r.reverse_return_pct for r in rows) / len(rows),
            "candle_sources": sorted({r.candle_source for r in rows}),
        }

    if report.n_with_candles < 30:
        report.notes.append(
            f"Only {report.n_with_candles} events have trusted candles — "
            "not statistically significant; treat rates as descriptive only."
        )
    return report


def load_liquidation_snapshots(
    research_db: Path,
    *,
    coins: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Read Phase-1 zones from research DB (read-only)."""
    if not research_db.exists():
        return []
    conn = _open_readonly(research_db)
    try:
        if coins:
            placeholders = ",".join("?" for _ in coins)
            sql = (
                f"SELECT * FROM liquidation_map_snapshots WHERE coin IN ({placeholders}) "
                f"ORDER BY fetched_at_ms ASC"
            )
            rows = conn.execute(sql, [c.upper() for c in coins]).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM liquidation_map_snapshots ORDER BY fetched_at_ms ASC"
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as exc:
        logger.warning("liquidation_map_snapshots unavailable: %s", exc)
        return []
    finally:
        conn.close()


def classify_forward_reaction(
    zone_side: str,
    approach_price: float,
    forward_price: float,
    *,
    reverse_threshold_pct: float = 0.1,
    accelerate_threshold_pct: float = 0.1,
) -> str:
    """Classify price path after approaching a Phase-1 zone.

    Long-liq zones sit below price (support-ish if bounce); short-liq above.
    - reverse: price moves away from the zone (bounce for long zones / reject for short)
    - accelerate: price continues through the zone
    - none: small move
    """
    if approach_price <= 0:
        return "none"
    ret = (forward_price / approach_price - 1.0) * 100.0
    if zone_side == "long":
        # Approaching long-liq from above: reverse = bounce up; accelerate = continue down
        if ret >= reverse_threshold_pct:
            return "reverse"
        if ret <= -accelerate_threshold_pct:
            return "accelerate"
    else:
        if ret <= -reverse_threshold_pct:
            return "reverse"
        if ret >= accelerate_threshold_pct:
            return "accelerate"
    return "none"


def run_forward_track_analysis(
    research_db: Path,
    candle_db: Path,
    *,
    coins: Sequence[str] = ("BTC", "ETH", "SOL", "HYPE"),
    approach_pct: float = 0.25,
    forward_minutes: int = 60,
    min_snapshots: int = 20,
) -> ForwardTrackReport:
    """Approach B: correlate persisted zones with subsequent price (prospective).

    Yields **zero evidential claims** until enough snapshots and approach
    events accumulate. Safe to run today; expect ``evidence_ready=False``.
    """
    report = ForwardTrackReport()
    report.notes.append(
        "Approach B is prospective only. Open-position snapshots cannot validate "
        "past price. This scaffold measures zone approaches AFTER each snapshot."
    )
    rows = load_liquidation_snapshots(research_db, coins=coins)
    report.n_zones = len(rows)
    snap_ids = sorted({str(r["snapshot_id"]) for r in rows})
    report.n_snapshots = len(snap_ids)

    if report.n_snapshots < min_snapshots:
        days_hourly = max(1, (min_snapshots + 23) // 24)
        days_daily = min_snapshots
        report.notes.append(
            f"Have {report.n_snapshots} distinct snapshots; need ~{min_snapshots}+ "
            f"spread over time (recommend hourly Task Scheduler / cron) before "
            f"interpreting rates. Roughly {min_snapshots} wall-clock hours at "
            f"1 snapshot/hour (~{days_hourly} day(s)), or ~{days_daily} days at "
            f"daily cadence — plus enough price paths that actually approach "
            f"zones (target dozens of approach events; see estimate_sample_need)."
        )

    # Group zones by snapshot
    by_snap: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_snap.setdefault(str(r["snapshot_id"]), []).append(r)

    reactions: Dict[str, int] = {"reverse": 0, "accelerate": 0, "none": 0}
    events: List[ZoneApproachEvent] = []

    for sid, zones in by_snap.items():
        fetched = int(zones[0]["fetched_at_ms"])
        end_ms = fetched + int(forward_minutes) * 60_000 + 3_600_000
        for z in zones:
            coin = str(z["coin"]).upper()
            low = float(z["price_low"])
            high = float(z["price_high"])
            mid = 0.5 * (low + high)
            # Expand band by approach_pct of mid for "near zone"
            pad = mid * (float(approach_pct) / 100.0)
            band_lo, band_hi = low - pad, high + pad
            candles, _src = load_trusted_candles_1m(
                candle_db if candle_db.exists() else research_db,
                coin,
                start_ms=fetched,
                end_ms=end_ms,
            )
            if not candles:
                # try research db if candle_db was live without coverage
                if candle_db != research_db:
                    candles, _src = load_trusted_candles_1m(
                        research_db, coin, start_ms=fetched, end_ms=end_ms,
                    )
            if not candles:
                continue
            approach_c = None
            for c in candles:
                px = float(c["close"])
                if band_lo <= px <= band_hi and int(c["timestamp_ms"]) >= fetched:
                    approach_c = c
                    break
            if approach_c is None:
                continue
            fwd_ts = int(approach_c["timestamp_ms"]) + int(forward_minutes) * 60_000
            fwd_px = _close_at_or_before(candles, fwd_ts)
            if fwd_px is None:
                continue
            ap = float(approach_c["close"])
            reaction = classify_forward_reaction(
                str(z["side"]), ap, fwd_px,
            )
            reactions[reaction] = reactions.get(reaction, 0) + 1
            events.append(
                ZoneApproachEvent(
                    snapshot_id=sid,
                    fetched_at_ms=fetched,
                    coin=coin,
                    side=str(z["side"]),
                    price_low=low,
                    price_high=high,
                    approach_time_ms=int(approach_c["timestamp_ms"]),
                    approach_price=ap,
                    forward_return_pct=(fwd_px / ap - 1.0) * 100.0,
                    reaction=reaction,
                ),
            )

    report.events = events
    report.n_approaches = len(events)
    report.reactions = reactions
    if report.n_approaches < 30:
        report.notes.append(
            f"Only {report.n_approaches} zone-approach events so far — "
            "insufficient for any strategy go/no-go decision."
        )
    return report


def estimate_sample_need(
    *,
    snapshots_per_day: int = 24,
    approaches_per_snapshot: float = 0.5,
    target_approaches: int = 50,
) -> Dict[str, Any]:
    """Rough wall-clock estimate for a minimally meaningful Approach B sample."""
    if approaches_per_snapshot <= 0 or snapshots_per_day <= 0:
        days = None
    else:
        days = math.ceil(target_approaches / (snapshots_per_day * approaches_per_snapshot))
    return {
        "target_approach_events": target_approaches,
        "assumed_snapshots_per_day": snapshots_per_day,
        "assumed_approaches_per_snapshot": approaches_per_snapshot,
        "estimated_days": days,
        "caveat": (
            "Order-of-magnitude only; volatile regimes produce more approaches. "
            "Even at N=50, treat as exploratory — not strategy-grade."
        ),
    }
