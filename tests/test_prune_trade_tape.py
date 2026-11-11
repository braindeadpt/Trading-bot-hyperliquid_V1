"""trade_tape retention — opt-in pruning (post-cutover cleanup 2026-10-28).

The research store is append-only by contract; ``prune_trade_tape`` must
refuse silently at 0/negative days and only delete when a positive
retention is passed explicitly.
"""
from __future__ import annotations

import pytest

from src.data.research_database import ResearchDatabase


def _db(tmp_path):
    return ResearchDatabase(str(tmp_path / "r.db"))


def _insert_tape(db, ts_ms, symbol="BTC"):
    from src.data.research_database import TradeTapeRecord
    from src.data.series_metadata import (
        SOURCE_HL_TRADE_TAPE, HL_API_VERSION, VENUE_HYPERLIQUID)
    db.save_trade_tape([TradeTapeRecord(
        symbol=symbol, timestamp_ms=ts_ms, price=100.0,
        size=1.0, side="buy", trade_id=f"t{ts_ms}",
        source=SOURCE_HL_TRADE_TAPE, venue=VENUE_HYPERLIQUID,
        api_version=HL_API_VERSION, ingested_at_ms=ts_ms)])


@pytest.mark.unit
def test_prune_disabled_by_default(tmp_path):
    db = _db(tmp_path)
    _insert_tape(db, 1_000)
    assert db.prune_trade_tape(0) == 0
    assert db.prune_trade_tape(-30) == 0
    n = db._conn().execute("SELECT COUNT(*) FROM trade_tape").fetchone()[0]
    assert n == 1
    db.close()


@pytest.mark.unit
def test_prune_deletes_only_older(tmp_path):
    db = _db(tmp_path)
    now = 10 * 86_400_000                    # arbitrary epoch
    _insert_tape(db, now - 40 * 86_400_000)  # 40d old -> purged
    _insert_tape(db, now - 10 * 86_400_000)  # 10d old -> kept
    deleted = db.prune_trade_tape(30, now_ms=now)
    assert deleted == 1
    rows = db._conn().execute(
        "SELECT timestamp_ms FROM trade_tape").fetchall()
    assert [r[0] for r in rows] == [now - 10 * 86_400_000]
    db.close()
