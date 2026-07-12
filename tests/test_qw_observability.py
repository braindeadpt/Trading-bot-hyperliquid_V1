"""Unit tests for QW1 (decision_audit table + engine._persist_decision)
and QW2 (trade journal enrichment columns).

Run:  python tests/test_qw_observability.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.database import Database, TradeEntry, TradeExit  # noqa: E402
import pytest

pytestmark = pytest.mark.unit

FAILED = 0


def print_test(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    global FAILED
    if not ok:
        FAILED += 1


# ── DB schema tests ──────────────────────────────────────────────────────


def _fresh_db() -> Database:
    """Build a Database in a temp file so each test starts clean."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Database(Path(tmp.name))


def test_decision_audit_table_exists() -> None:
    db = _fresh_db()
    conn = sqlite3.connect(str(db.db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='decision_audit'"
        ).fetchone()
        ok = row is not None
    finally:
        conn.close()
    print_test("decision_audit_table_exists", ok)


def test_decision_audit_columns() -> None:
    db = _fresh_db()
    conn = sqlite3.connect(str(db.db_path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decision_audit)").fetchall()}
    finally:
        conn.close()
    expected = {
        "id", "timestamp", "decision_type", "symbol", "side",
        "strategy", "signal_confidence", "result", "reason", "metadata",
    }
    missing = expected - cols
    print_test("decision_audit_columns", not missing, f"missing={missing}" if missing else "all columns present")


def test_save_decision_basic() -> None:
    db = _fresh_db()
    now = int(time.time() * 1000)
    row_id = db.save_decision(
        timestamp=now,
        decision_type="risk",
        symbol="BTC",
        side="long",
        strategy="VWAPDeviation",
        signal_confidence=0.72,
        result="rejected",
        reason="ADX too high",
        metadata={"adx": 45.0},
    )
    rows = db.get_decisions(limit=10)
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    assert rows[0]["decision_type"] == "risk"
    assert rows[0]["metadata"] == {"adx": 45.0}
    assert row_id > 0
    print_test("save_decision_basic", True, f"id={row_id}")


def test_save_decision_minimal() -> None:
    """No side, no strategy, no confidence, no metadata — should still work."""
    db = _fresh_db()
    db.save_decision(
        timestamp=int(time.time() * 1000),
        decision_type="vol_circuit",
        symbol="ETH",
        result="rejected",
        reason="ATR 3x baseline",
    )
    rows = db.get_decisions(limit=10)
    assert len(rows) == 1
    assert rows[0]["side"] is None
    assert rows[0]["strategy"] is None
    assert rows[0]["signal_confidence"] is None
    assert rows[0]["metadata"] is None
    print_test("save_decision_minimal", True)


def test_get_decisions_filters() -> None:
    db = _fresh_db()
    now = int(time.time() * 1000)
    db.save_decision(timestamp=now, decision_type="risk", symbol="BTC", result="rejected", reason="x")
    db.save_decision(timestamp=now, decision_type="tca", symbol="ETH", result="rejected", reason="x")
    db.save_decision(timestamp=now, decision_type="risk", symbol="ETH", result="rejected", reason="x")
    db.save_decision(timestamp=now, decision_type="execution", symbol="BTC", result="executed", reason="y")

    only_risk = db.get_decisions(decision_type="risk")
    assert len(only_risk) == 2

    only_btc = db.get_decisions(symbol="BTC")
    assert len(only_btc) == 2

    only_rej = db.get_decisions(result="rejected")
    assert len(only_rej) == 3

    print_test("get_decisions_filters", True, "filters work independently")


def test_count_decisions() -> None:
    db = _fresh_db()
    now = int(time.time() * 1000)
    for _ in range(5):
        db.save_decision(timestamp=now, decision_type="risk", symbol="BTC", result="rejected", reason="x")
    for _ in range(3):
        db.save_decision(timestamp=now, decision_type="tca", symbol="BTC", result="rejected", reason="x")
    n_total = db.count_decisions()
    n_risk = db.count_decisions(decision_type="risk")
    n_rej = db.count_decisions(result="rejected")
    print_test(
        "count_decisions",
        n_total == 8 and n_risk == 5 and n_rej == 8,
        f"total={n_total} risk={n_risk} rej={n_rej}",
    )


def test_decision_audit_indexes() -> None:
    db = _fresh_db()
    conn = sqlite3.connect(str(db.db_path))
    try:
        idxs = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='decision_audit'"
            ).fetchall()
        }
    finally:
        conn.close()
    expected = {
        "idx_decision_ts", "idx_decision_strategy",
        "idx_decision_result", "idx_decision_type",
    }
    missing = expected - idxs
    print_test("decision_audit_indexes", not missing, f"missing={missing}" if missing else "all 4 indexes present")


# ── QW2: trade journal enrichment ────────────────────────────────────────


def test_trades_table_new_columns() -> None:
    db = _fresh_db()
    conn = sqlite3.connect(str(db.db_path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    finally:
        conn.close()
    expected = {
        "entry_adx", "entry_oir", "entry_funding", "entry_predicted_funding",
        "entry_bid_ask_imbalance", "entry_volume_1m",
        "entry_market_snapshot", "signal_metadata",
    }
    missing = expected - cols
    print_test("trades_table_new_columns", not missing, f"missing={missing}" if missing else "all 8 columns present")


def test_save_trade_entry_with_snapshot() -> None:
    db = _fresh_db()
    snapshot = {
        "adx_14": 22.5,
        "funding": -0.0001,
        "oir": 0.3,
        "price": 81000.0,
    }
    entry = TradeEntry(
        symbol="BTC",
        side="long",
        entry_price=81000.0,
        entry_time=int(time.time() * 1000),
        size=0.01,
        strategy="VWAPDeviation",
        sub_strategy="VWAPDeviation",
        status="open",
        entry_adx=22.5,
        entry_oir=0.3,
        entry_funding=-0.0001,
        entry_bid_ask_imbalance=0.15,
        entry_volume_1m=1_500_000.0,
        entry_market_snapshot=json.dumps(snapshot),
        signal_metadata=json.dumps({"zscore": -2.8, "vol_ratio": 1.4}),
    )
    trade_id = db.save_trade_entry(entry)
    assert trade_id > 0

    # Read back
    row = db.get_trade_by_id(trade_id) if hasattr(db, "get_trade_by_id") else None
    if row is None:
        # Use get_open_trades fallback
        rows = db.get_open_trades()
        assert len(rows) == 1, f"expected 1 open trade, got {len(rows)}"
        row = rows[0]

    assert row["entry_adx"] == 22.5
    assert row["entry_oir"] == 0.3
    assert row["entry_funding"] == -0.0001
    assert row["entry_bid_ask_imbalance"] == 0.15
    assert row["entry_volume_1m"] == 1_500_000.0

    # JSON columns
    snap_back = json.loads(row["entry_market_snapshot"])
    assert snap_back["adx_14"] == 22.5
    assert snap_back["price"] == 81000.0
    meta_back = json.loads(row["signal_metadata"])
    assert meta_back["zscore"] == -2.8
    assert meta_back["vol_ratio"] == 1.4
    print_test("save_trade_entry_with_snapshot", True, f"id={trade_id}")


def test_migration_adds_columns_to_existing_trades_table() -> None:
    """Simulate a bot.db that has an old trades table; verify migration."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)

    # First, create a v1 schema without the new columns
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""
            CREATE TABLE trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                side        TEXT    NOT NULL,
                entry_price REAL    NOT NULL,
                exit_price  REAL,
                entry_time  INTEGER NOT NULL,
                exit_time   INTEGER,
                size        REAL    NOT NULL,
                pnl_usd     REAL,
                pnl_pct     REAL,
                strategy    TEXT    NOT NULL,
                exit_reason TEXT,
                status      TEXT    NOT NULL DEFAULT 'open'
            );
        """)
        conn.execute("INSERT INTO trades (symbol, side, entry_price, entry_time, size, strategy) "
                     "VALUES ('BTC', 'long', 80000, 1700000000000, 0.01, 'TestStrat')")
        conn.commit()
    finally:
        conn.close()

    # Now open via Database — should trigger migration
    db = Database(db_path)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
        finally:
            conn.close()
        expected = {
            "entry_adx", "entry_oir", "entry_funding", "entry_predicted_funding",
            "entry_bid_ask_imbalance", "entry_volume_1m",
            "entry_market_snapshot", "signal_metadata", "sub_strategy",
        }
        missing = expected - cols
        # Old row should still be there
        rows = db.get_open_trades()
        assert len(rows) == 1, f"old row missing after migration: {rows}"
        print_test("migration_adds_columns_to_existing_trades_table",
                   not missing, f"missing={missing}" if missing else "all new columns + sub_strategy added")
    finally:
        pass


def test_decision_audit_persists_metadata_with_special_chars() -> None:
    db = _fresh_db()
    metadata = {"key": 'value with "quotes" and \\backslashes', "n": 1}
    db.save_decision(
        timestamp=int(time.time() * 1000),
        decision_type="risk",
        symbol="BTC",
        result="rejected",
        reason="x",
        metadata=metadata,
    )
    rows = db.get_decisions(limit=1)
    assert rows[0]["metadata"] == metadata, f"metadata round-trip failed: {rows[0]['metadata']}"
    print_test("decision_audit_persists_metadata_with_special_chars", True)


# ── Runner ───────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 70)
    print("QW1 (decision_audit) + QW2 (trade journal enrichment) tests")
    print("=" * 70)

    tests = [
        test_decision_audit_table_exists,
        test_decision_audit_columns,
        test_save_decision_basic,
        test_save_decision_minimal,
        test_get_decisions_filters,
        test_count_decisions,
        test_decision_audit_indexes,
        test_trades_table_new_columns,
        test_save_trade_entry_with_snapshot,
        test_migration_adds_columns_to_existing_trades_table,
        test_decision_audit_persists_metadata_with_special_chars,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print_test(t.__name__, False, f"AssertionError: {e}")
        except Exception as e:  # noqa: BLE001
            print_test(t.__name__, False, f"{type(e).__name__}: {e}")

    print("=" * 70)
    if FAILED == 0:
        print(f"ALL TESTS PASSED ({len(tests)}/{len(tests)})")
        return 0
    print(f"FAILED: {FAILED}/{len(tests)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
