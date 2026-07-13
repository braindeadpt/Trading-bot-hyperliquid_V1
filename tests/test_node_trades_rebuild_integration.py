"""Integration (offline): full node_trades rebuild pipeline against a FakeFetcher.

Exercises: package parsing -> priority window extraction -> S3 key planning ->
FakeNodeTradesFetcher -> trade parsing -> 1m aggregation -> non-destructive
upsert into a tmp ResearchDatabase, asserting:
  * rebuilt candles land with source=hl_node_trades_rebuild
  * pre-existing protected hl_candleSnapshot rows are never overwritten
  * a re-validation hook is invoked with the rebuilt rows

No real network/S3 access occurs anywhere in this test.
"""
from __future__ import annotations

import json
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from src.data.candle_providers.node_trades_fetcher import (
    DEFAULT_BUCKET,
    DEFAULT_KEY_TEMPLATE,
    FakeNodeTradesFetcher,
    archive_keys_for_window,
)
from src.data.candle_providers.node_trades_rebuild import (
    SOURCE_NODE_TRADES_REBUILD,
    extract_priority_windows,
    load_support_package,
    plan_object_keys,
    rebuild_from_support_package,
    rebuild_window,
)
from src.data.database import Candle
from src.data.research_database import ResearchDatabase
from src.data.series_metadata import SeriesMetadata

pytestmark = pytest.mark.integration_offline


def _tmp_db_path() -> Path:
    return Path(tempfile.gettempdir()) / f"node_trades_rebuild_{uuid.uuid4().hex}.db"


_next_tid = [1000]


def _fill_event(coin, px, sz, time_ms, side="B", address=None, crossed=None):
    """Build one [address, fill] event for a node_fills_by_block trade leg."""
    _next_tid[0] += 1
    return [
        address or f"0xaddr{_next_tid[0]}",
        {
            "coin": coin,
            "px": str(px),
            "sz": str(sz),
            "side": side,
            "time": time_ms,
            "tid": _next_tid[0],
            "oid": _next_tid[0] * 10,
            "crossed": True if crossed is None else crossed,
            "hash": f"0xhash{_next_tid[0]}",
        },
    ]


def _fills_payload(*trades):
    """Wrap simple (coin, px, sz, time_ms[, side]) trades into one
    node_fills_by_block-shaped block dict (as a single-element list, the
    FakeNodeTradesFetcher payload format parse_node_fills_by_block_ndjson
    accepts directly). Each trade becomes exactly one leg here (a single
    event is enough for the parser to keep it — real archives have two legs
    per trade, but the parser doesn't require exactly two)."""
    events = []
    for t in trades:
        coin, px, sz, time_ms = t[0], t[1], t[2], t[3]
        side = t[4] if len(t) > 4 else "B"
        events.append(_fill_event(coin, px, sz, time_ms, side=side))
    return [
        {
            "local_time": "2026-07-10T23:00:00.119184915",
            "block_time": "2026-07-10T22:59:59.921821211",
            "block_number": 1068001525,
            "events": events,
        }
    ]


def _write_package(tmp_path: Path, entries) -> Path:
    package = {
        "package_type": "goldrush_parity_support",
        "divergent_entries": entries,
        "node_trades_reconstruction": {"trigger": "secondary_validation_inconclusive"},
    }
    p = tmp_path / "support_package.json"
    p.write_text(json.dumps(package), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# S3 key layout — matches the real hl-mainnet-node-data/node_fills_by_block/
# shape confirmed via a real read-only list_objects_v2
# (scripts/check_hl_s3_access.py): node_fills_by_block/hourly/{YYYYMMDD}/{hour}.lz4,
# hour unpadded, no {coin} segment. node_fills_by_block replaced node_trades
# as the default prefix because node_trades is stale (2025-03-22..2025-06-21
# only) while node_fills_by_block reaches all the way to today.
# ---------------------------------------------------------------------------


def test_default_key_template_matches_real_observed_layout():
    assert DEFAULT_KEY_TEMPLATE == "node_fills_by_block/hourly/{date}/{hour}.lz4"
    assert DEFAULT_BUCKET == "hl-mainnet-node-data"


def test_archive_keys_for_window_hour_is_not_zero_padded():
    import datetime as dt

    # 2026-07-10 hour 0 and hour 1 (single-digit hours) must render as
    # ".../0.lz4" and ".../1.lz4", never ".../00.lz4"/".../01.lz4".
    start = dt.datetime(2026, 7, 10, 0, 30, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 7, 10, 1, 30, tzinfo=dt.timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    keys = archive_keys_for_window("BTC", start_ms, end_ms)
    uris = sorted(obj.uri for obj in keys)
    assert uris == [
        "s3://hl-mainnet-node-data/node_fills_by_block/hourly/20260710/0.lz4",
        "s3://hl-mainnet-node-data/node_fills_by_block/hourly/20260710/1.lz4",
    ]


def test_archive_keys_for_window_no_per_coin_segment():
    # Same date/hour window requested for two different symbols must
    # resolve to the identical object key — there is no per-coin path
    # segment in the real archive layout.
    import datetime as dt

    ts = dt.datetime(2026, 7, 10, 10, 15, tzinfo=dt.timezone.utc)
    ms = int(ts.timestamp() * 1000)

    btc_keys = archive_keys_for_window("BTC", ms, ms)
    eth_keys = archive_keys_for_window("ETH", ms, ms)
    assert len(btc_keys) == 1 and len(eth_keys) == 1
    assert btc_keys[0].uri == eth_keys[0].uri
    assert btc_keys[0].uri == "s3://hl-mainnet-node-data/node_fills_by_block/hourly/20260710/10.lz4"


def test_full_rebuild_pipeline_inserts_with_correct_source(tmp_path):
    open_ms = 1_800_000_000_000
    open_ms = (open_ms // 60_000) * 60_000
    close_ms = open_ms + 59_999

    entries = [
        {"symbol": "BTC", "interval": "1m", "timestamp_ms": close_ms, "field": "o"},
    ]
    package_path = _write_package(tmp_path, entries)

    windows = extract_priority_windows(load_support_package(package_path))
    assert len(windows) == 1
    window = windows[0]
    assert window.symbol == "BTC"

    keys = plan_object_keys(windows)
    assert len(keys) >= 1

    fetcher = FakeNodeTradesFetcher()
    for obj in archive_keys_for_window(window.symbol, window.start_ms, window.end_ms):
        fetcher.put(obj, _fills_payload(
            ("BTC", "64361.0", "0.5", open_ms),
            ("BTC", "64370.0", "0.3", open_ms + 30_000),
            ("BTC", "64355.0", "0.2", open_ms + 59_999),
        ))

    db = ResearchDatabase(_tmp_db_path())

    revalidation_calls = []

    def _revalidate(symbol, rows):
        revalidation_calls.append((symbol, list(rows)))
        return {"symbol": symbol, "rows_checked": len(rows)}

    result = rebuild_window(window, fetcher, db, revalidate_fn=_revalidate)

    assert result.candles_built == 1
    assert result.candles_inserted == 1
    assert result.candles_skipped_protected == 0
    assert fetcher.fetch_log  # objects were actually fetched via the fake

    # Re-validation hook was invoked with the rebuilt rows for this symbol.
    assert len(revalidation_calls) == 1
    assert revalidation_calls[0][0] == "BTC"
    assert revalidation_calls[0][1][0]["T"] == close_ms

    meta = db.get_candle_metadata_sample("BTC", "1m", limit=1)
    assert meta is not None
    assert meta["source"] == SOURCE_NODE_TRADES_REBUILD


def test_rebuild_never_overwrites_protected_hl_candlesnapshot_rows(tmp_path):
    open_ms = 1_800_000_000_000
    open_ms = (open_ms // 60_000) * 60_000
    close_ms = open_ms + 59_999

    db = ResearchDatabase(_tmp_db_path())
    protected = Candle(
        symbol="BTC",
        timestamp_ms=close_ms,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10.0,
    )
    db.save_research_candles([protected], "1m", SeriesMetadata.hl_candles())

    entries = [{"symbol": "BTC", "interval": "1m", "timestamp_ms": close_ms, "field": "c"}]
    package_path = _write_package(tmp_path, entries)
    windows = extract_priority_windows(load_support_package(package_path))
    window = windows[0]

    fetcher = FakeNodeTradesFetcher()
    for obj in archive_keys_for_window(window.symbol, window.start_ms, window.end_ms):
        fetcher.put(obj, _fills_payload(("BTC", "999.0", "1.0", open_ms)))

    result = rebuild_window(window, fetcher, db)

    assert result.candles_built == 1
    assert result.candles_inserted == 0
    assert result.candles_skipped_protected == 1

    # The original protected row must be untouched.
    sources = db.get_candle_sources_in_range("BTC", "1m", close_ms, close_ms)
    assert sources[close_ms] == "hl_candleSnapshot"
    row = db._conn().execute(
        "SELECT open, close, source FROM candles_1m WHERE symbol='BTC' AND timestamp_ms=?",
        (close_ms,),
    ).fetchone()
    assert row["source"] == "hl_candleSnapshot"
    assert float(row["open"]) == 1.0


def test_rebuild_from_support_package_end_to_end(tmp_path):
    open_ms = 1_800_000_000_000
    open_ms = (open_ms // 60_000) * 60_000
    close_ms = open_ms + 59_999

    entries = [
        {"symbol": "BTC", "interval": "1m", "timestamp_ms": close_ms, "field": "o"},
        {"symbol": "ETH", "interval": "1m", "timestamp_ms": close_ms, "field": "c"},
    ]
    package_path = _write_package(tmp_path, entries)

    windows = extract_priority_windows(load_support_package(package_path))
    fetcher = FakeNodeTradesFetcher()
    # Real node_trades archive objects are multi-coin: BTC's and ETH's windows
    # fall in the same date/hour, so they resolve to the *same* object URI
    # (no {coin} path segment). Populate that one shared object with trades
    # for both coins mixed together, mirroring the real file shape.
    seen_uris = set()
    for w in windows:
        for obj in archive_keys_for_window(w.symbol, w.start_ms, w.end_ms):
            seen_uris.add(obj.uri)
            fetcher.put(obj, _fills_payload(
                ("BTC", "100.0", "1.0", open_ms),
                ("ETH", "200.0", "1.0", open_ms),
            ))
    assert len(seen_uris) == 1  # BTC and ETH share the same hourly archive object

    db = ResearchDatabase(_tmp_db_path())
    summary = rebuild_from_support_package(package_path, fetcher, db)

    assert summary["candles_inserted_total"] == 2
    assert {w["symbol"] for w in summary["windows"]} == {"BTC", "ETH"}
    for r in summary["results"]:
        assert r["candles_inserted"] == 1

    # The shared object was fetched exactly once across both symbols' windows
    # (the trade_cache in rebuild_from_support_package dedupes the download).
    assert len(fetcher.fetch_log) == 1


def test_rebuild_from_support_package_dedupes_shared_hourly_object_fetch(tmp_path):
    """Two symbols whose windows land in the same date/hour must not trigger
    two separate fetches of the same underlying multi-coin archive object."""
    open_ms = 1_800_000_000_000
    open_ms = (open_ms // 60_000) * 60_000
    close_ms = open_ms + 59_999

    entries = [
        {"symbol": "BTC", "interval": "1m", "timestamp_ms": close_ms, "field": "o"},
        {"symbol": "ETH", "interval": "1m", "timestamp_ms": close_ms, "field": "c"},
        {"symbol": "SOL", "interval": "1m", "timestamp_ms": close_ms, "field": "h"},
    ]
    package_path = _write_package(tmp_path, entries)
    windows = extract_priority_windows(load_support_package(package_path))

    keys = plan_object_keys(windows)
    # plan_object_keys dedupes by object URI across all symbols sharing a
    # date/hour — three symbols in the same hour must plan exactly one key.
    assert len(keys) == 1

    fetcher = FakeNodeTradesFetcher()
    obj = keys[0]
    fetcher.put(obj, _fills_payload(
        ("BTC", "100.0", "1.0", open_ms),
        ("ETH", "200.0", "1.0", open_ms),
        ("SOL", "30.0", "1.0", open_ms),
    ))

    db = ResearchDatabase(_tmp_db_path())
    summary = rebuild_from_support_package(package_path, fetcher, db)

    assert summary["candles_inserted_total"] == 3
    assert len(fetcher.fetch_log) == 1  # one shared download served all 3 symbols


def test_multi_coin_archive_object_yields_only_target_coin_candles(tmp_path):
    """A single (real-shaped) multi-coin hourly archive object must produce
    candles for only the requested symbol when rebuilding one symbol's window,
    even though the fetched payload also contains other coins' trades."""
    open_ms = 1_800_000_000_000
    open_ms = (open_ms // 60_000) * 60_000
    close_ms = open_ms + 59_999

    entries = [{"symbol": "BTC", "interval": "1m", "timestamp_ms": close_ms, "field": "o"}]
    package_path = _write_package(tmp_path, entries)
    windows = extract_priority_windows(load_support_package(package_path))
    window = windows[0]

    fetcher = FakeNodeTradesFetcher()
    for obj in archive_keys_for_window(window.symbol, window.start_ms, window.end_ms):
        fetcher.put(obj, _fills_payload(
            ("BTC", "64361.0", "0.5", open_ms),
            ("ETH", "3000.0", "5.0", open_ms),
            ("SOL", "150.0", "10.0", open_ms),
        ))

    db = ResearchDatabase(_tmp_db_path())
    result = rebuild_window(window, fetcher, db)

    assert result.candles_built == 1
    assert result.candles_inserted == 1
    meta = db.get_candle_metadata_sample("BTC", "1m", limit=1)
    assert meta is not None
    row = db._conn().execute(
        "SELECT open, symbol FROM candles_1m WHERE symbol='BTC' AND timestamp_ms=?",
        (close_ms,),
    ).fetchone()
    assert row["symbol"] == "BTC"
    assert float(row["open"]) == 64361.0
    # No ETH/SOL rows were created from the shared multi-coin object.
    other = db._conn().execute(
        "SELECT COUNT(*) as n FROM candles_1m WHERE symbol IN ('ETH', 'SOL')"
    ).fetchone()
    assert other["n"] == 0


def test_extract_priority_windows_filters_by_symbol(tmp_path):
    entries = [
        {"symbol": "BTC", "interval": "1m", "timestamp_ms": 1_800_000_059_999},
        {"symbol": "ETH", "interval": "1m", "timestamp_ms": 1_800_000_059_999},
    ]
    package_path = _write_package(tmp_path, entries)
    windows = extract_priority_windows(load_support_package(package_path), symbols=["BTC"])
    assert len(windows) == 1
    assert windows[0].symbol == "BTC"
