"""Rebuild orchestrator: official node_trades -> 1m OHLCV -> ResearchDatabase.

This implements the ``node_trades_reconstruction`` plan embedded in the
GoldRush support package (see
``src/data/candle_providers/parity_secondary.py::NODE_TRADES_RECONSTRUCTION_PROPOSAL``
and ``src/data/candle_providers/support_package.py``):

    1. Download node_trades archives for the divergent windows.
    2. Aggregate trades into 1m buckets using HL bucket boundaries (t/T).
    3. Apply format_hl_price / dynamic quantum when emitting OHLC.
    4. Upsert research DB with source=hl_node_trades_rebuild; never overwrite
       protected hl_candleSnapshot rows.
    5. Re-run secondary validation on rebuilt windows only.

Nothing in this module performs a real network call. Fetching is delegated
to an injectable :class:`~src.data.candle_providers.node_trades_fetcher.NodeTradesFetcher`
— tests and dry-run planning use
:class:`~src.data.candle_providers.node_trades_fetcher.FakeNodeTradesFetcher`;
real downloads require explicitly constructing
:class:`~src.data.candle_providers.node_trades_fetcher.S3NodeTradesFetcher`
(optional ``boto3`` dependency, AWS credentials, requester-pays cost).

The GoldRush parity gate itself (``parity.py`` tolerance, thresholds) is
untouched by this module — rebuilt data is a *new* source
(``hl_node_trades_rebuild``) that must still pass its own secondary
validation before being used for OOS/tuning. This module never marks that
gate satisfied; it only produces data for a human/pipeline to re-validate.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.data.candle_providers.base import INTERVAL_MS
from src.data.candle_providers.node_trades_aggregator import aggregate_trades_to_ohlcv
from src.data.candle_providers.node_trades_fetcher import (
    DEFAULT_BUCKET,
    DEFAULT_KEY_TEMPLATE,
    ArchiveObjectKey,
    NodeTradesFetcher,
    archive_keys_for_window,
)
from src.data.candle_providers.node_trades_parser import parse_archive_object
from src.data.database import Candle
from src.data.research_database import ResearchDatabase
from src.data.series_metadata import PROTECTED_OFFICIAL_SOURCES, SeriesMetadata

logger = logging.getLogger(__name__)

SOURCE_NODE_TRADES_REBUILD = "hl_node_trades_rebuild"
API_VERSION_NODE_TRADES_REBUILD = "node-trades-archive-v1"

DEFAULT_SUPPORT_PACKAGE_PATH = Path("data") / "research" / "goldrush_support_package_latest.json"


class NodeTradesRebuildError(RuntimeError):
    """Rebuild planning/execution failure."""


@dataclass(frozen=True)
class RebuildWindow:
    """One symbol's priority window to rebuild from official trades."""

    symbol: str
    start_ms: int
    end_ms: int
    interval: str = "1m"


@dataclass
class RebuildResult:
    symbol: str
    candles_built: int = 0
    candles_inserted: int = 0
    candles_skipped_protected: int = 0
    objects_fetched: List[str] = field(default_factory=list)
    objects_failed: List[Dict[str, str]] = field(default_factory=list)
    revalidation: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "candles_built": self.candles_built,
            "candles_inserted": self.candles_inserted,
            "candles_skipped_protected": self.candles_skipped_protected,
            "objects_fetched": self.objects_fetched,
            "objects_failed": self.objects_failed,
            "revalidation": self.revalidation,
        }


def load_support_package(path: Path | str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise NodeTradesRebuildError(f"Support package not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise NodeTradesRebuildError(f"Invalid support package JSON at {p}: {exc}") from exc


def extract_priority_windows(
    package: Dict[str, Any],
    *,
    symbols: Optional[Sequence[str]] = None,
    interval: str = "1m",
) -> List[RebuildWindow]:
    """Merge ``divergent_entries`` timestamps into one window per symbol.

    Each divergent entry's ``timestamp_ms`` is the bar's close time (``T``).
    Entries for the same symbol are merged into a single
    ``[min_close - interval_ms + 1, max_close]`` span so a symbol with
    several nearby divergences only requires the archive objects covering
    that combined span rather than one fetch per bar.
    """
    if interval not in INTERVAL_MS:
        raise NodeTradesRebuildError(f"Unsupported interval: {interval}")
    gap = INTERVAL_MS[interval]
    wanted = {s.upper() for s in symbols} if symbols else None

    entries = package.get("divergent_entries") or []
    if not isinstance(entries, list):
        raise NodeTradesRebuildError("Support package 'divergent_entries' is not a list")

    by_symbol: Dict[str, List[int]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sym = str(entry.get("symbol", "")).upper()
        if not sym:
            continue
        if entry.get("interval") and entry["interval"] != interval:
            continue
        if wanted is not None and sym not in wanted:
            continue
        ts = entry.get("timestamp_ms")
        if ts is None:
            continue
        by_symbol.setdefault(sym, []).append(int(ts))

    windows: List[RebuildWindow] = []
    for sym, ts_list in sorted(by_symbol.items()):
        lo, hi = min(ts_list), max(ts_list)
        windows.append(
            RebuildWindow(symbol=sym, start_ms=lo - gap + 1, end_ms=hi, interval=interval)
        )
    return windows


def plan_object_keys(
    windows: Sequence[RebuildWindow],
    *,
    bucket: str = DEFAULT_BUCKET,
    key_template: str = DEFAULT_KEY_TEMPLATE,
) -> List[ArchiveObjectKey]:
    """Enumerate the deduplicated set of archive objects needed for *windows*."""
    seen: Dict[str, ArchiveObjectKey] = {}
    for w in windows:
        for obj in archive_keys_for_window(
            w.symbol, w.start_ms, w.end_ms, bucket=bucket, key_template=key_template,
        ):
            seen[obj.uri] = obj
    return [seen[k] for k in sorted(seen)]


def _row_to_candle(row: Dict[str, Any], symbol: str) -> Candle:
    close_time = int(row["T"])
    open_time = int(row["t"])
    c = Candle(
        symbol=symbol.upper(),
        timestamp_ms=close_time,
        open=float(row["o"]),
        high=float(row["h"]),
        low=float(row["l"]),
        close=float(row["c"]),
        volume=float(row.get("v", 0.0)),
        funding_rate=None,
        oi_total=None,
        oi_delta=None,
        buy_volume=None,
        sell_volume=None,
        trade_count=int(row.get("n", 0)),
    )
    object.__setattr__(c, "open_time_ms", open_time)  # type: ignore[misc]
    return c


def rebuild_window(
    window: RebuildWindow,
    fetcher: NodeTradesFetcher,
    db: ResearchDatabase,
    *,
    bucket: str = DEFAULT_BUCKET,
    key_template: str = DEFAULT_KEY_TEMPLATE,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
    is_spot: bool = False,
    revalidate_fn: Optional[Callable[[str, List[Dict[str, Any]]], Any]] = None,
) -> RebuildResult:
    """Fetch, parse, aggregate and upsert one symbol's priority window."""
    result = RebuildResult(symbol=window.symbol)
    objs = archive_keys_for_window(
        window.symbol, window.start_ms, window.end_ms, bucket=bucket, key_template=key_template,
    )

    all_trades = []
    for obj in objs:
        try:
            payload = fetcher.fetch_object(obj)
        except Exception as exc:  # noqa: BLE001 - surfaced per-object, rebuild continues
            logger.warning("Fetch failed for %s: %s", obj.uri, exc)
            result.objects_failed.append({"uri": obj.uri, "error": str(exc)})
            continue
        result.objects_fetched.append(obj.uri)
        try:
            all_trades.extend(parse_archive_object(payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Parse failed for %s: %s", obj.uri, exc)
            result.objects_failed.append({"uri": obj.uri, "error": f"parse: {exc}"})

    rows = aggregate_trades_to_ohlcv(
        all_trades,
        symbol=window.symbol,
        interval=window.interval,
        meta_cache=meta_cache,
        is_spot=is_spot,
    )
    # Keep only bars whose close time falls in the requested window — an
    # archive object covers a full hour and may contain trades outside the
    # exact priority span.
    rows = [r for r in rows if window.start_ms <= int(r["T"]) <= window.end_ms]
    result.candles_built = len(rows)

    if rows:
        candles = [_row_to_candle(r, window.symbol) for r in rows]
        meta = SeriesMetadata(
            source=SOURCE_NODE_TRADES_REBUILD,
            venue="hyperliquid",
            api_version=API_VERSION_NODE_TRADES_REBUILD,
            ingested_at_ms=int(time.time() * 1000),
            quality_flags={
                "taker_split": False,
                "volume_unit": "base",
                "close_time_key": "T",
                "provider": "hl_node_trades_rebuild",
                "reconstructed_from": "node_trades_archive",
            },
            volume_unit="base",
        )
        inserted, skipped = db.save_research_candles_non_destructive(
            candles, window.interval, meta, protect_sources=PROTECTED_OFFICIAL_SOURCES,
        )
        result.candles_inserted = inserted
        result.candles_skipped_protected = skipped

    if revalidate_fn is not None:
        result.revalidation = revalidate_fn(window.symbol, rows)

    return result


def rebuild_from_support_package(
    package_path: Path | str,
    fetcher: NodeTradesFetcher,
    db: ResearchDatabase,
    *,
    symbols: Optional[Sequence[str]] = None,
    interval: str = "1m",
    bucket: str = DEFAULT_BUCKET,
    key_template: str = DEFAULT_KEY_TEMPLATE,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
    revalidate_fn: Optional[Callable[[str, List[Dict[str, Any]]], Any]] = None,
) -> Dict[str, Any]:
    """End-to-end rebuild for every priority window in a support package."""
    package = load_support_package(package_path)
    windows = extract_priority_windows(package, symbols=symbols, interval=interval)
    results = [
        rebuild_window(
            w, fetcher, db,
            bucket=bucket, key_template=key_template,
            meta_cache=meta_cache, revalidate_fn=revalidate_fn,
        )
        for w in windows
    ]
    return {
        "package_path": str(package_path),
        "windows": [w.__dict__ for w in windows],
        "results": [r.to_dict() for r in results],
        "candles_inserted_total": sum(r.candles_inserted for r in results),
        "candles_skipped_protected_total": sum(r.candles_skipped_protected for r in results),
    }
