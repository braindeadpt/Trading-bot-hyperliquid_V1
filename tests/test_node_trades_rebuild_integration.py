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


def _write_package(tmp_path: Path, entries) -> Path:
    package = {
        "package_type": "goldrush_parity_support",
        "divergent_entries": entries,
        "node_trades_reconstruction": {"trigger": "secondary_validation_inconclusive"},
    }
    p = tmp_path / "support_package.json"
    p.write_text(json.dumps(package), encoding="utf-8")
    return p


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
        fetcher.put(obj, [
            {"coin": "BTC", "px": "64361.0", "sz": "0.5", "time": open_ms},
            {"coin": "BTC", "px": "64370.0", "sz": "0.3", "time": open_ms + 30_000},
            {"coin": "BTC", "px": "64355.0", "sz": "0.2", "time": open_ms + 59_999},
        ])

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
        fetcher.put(obj, [
            {"coin": "BTC", "px": "999.0", "sz": "1.0", "time": open_ms},
        ])

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
    for w in windows:
        for obj in archive_keys_for_window(w.symbol, w.start_ms, w.end_ms):
            fetcher.put(obj, [
                {"coin": w.symbol, "px": "100.0", "sz": "1.0", "time": open_ms},
            ])

    db = ResearchDatabase(_tmp_db_path())
    summary = rebuild_from_support_package(package_path, fetcher, db)

    assert summary["candles_inserted_total"] == 2
    assert {w["symbol"] for w in summary["windows"]} == {"BTC", "ETH"}
    for r in summary["results"]:
        assert r["candles_inserted"] == 1


def test_extract_priority_windows_filters_by_symbol(tmp_path):
    entries = [
        {"symbol": "BTC", "interval": "1m", "timestamp_ms": 1_800_000_059_999},
        {"symbol": "ETH", "interval": "1m", "timestamp_ms": 1_800_000_059_999},
    ]
    package_path = _write_package(tmp_path, entries)
    windows = extract_priority_windows(load_support_package(package_path), symbols=["BTC"])
    assert len(windows) == 1
    assert windows[0].symbol == "BTC"
