"""The live trading DB must be openable read-only by research code.

Research and backtest sweeps read candles from ``data/live/bot.db`` while the
bot is running. A normal ``Database(...)`` open is read-WRITE: it runs
``_init_db()``'s DDL and sets ``PRAGMA journal_mode=WAL`` on a database the
bot is actively writing. That blocked on the writer's lock every night the
bot was up — the overnight sessions died on the wrapper's timeout with no
results — and it also let a research process mutate live trading data.

These tests pin the read-only contract in both directions: reads still work,
writes and schema creation cannot happen, and a missing file is an error
rather than a silently created empty database.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.data.database import Database

pytestmark = pytest.mark.unit


def _seed(path: Path) -> None:
    """A minimal real DB: Database() creates the schema the normal
    (read-write) way, then one row goes in via raw SQL — save_candles takes
    candle objects, and the row's shape is irrelevant to what is under test."""
    db = Database(path)
    db.close()
    con = sqlite3.connect(str(path))
    con.execute(
        "INSERT INTO candles_1m (symbol, timestamp_ms, open, high, low, close, volume) "
        "VALUES ('BTC', 1700000000000, 1.0, 2.0, 0.5, 1.5, 10.0)"
    )
    con.commit()
    con.close()


def test_read_only_can_still_read_candles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot.db"
        _seed(path)
        ro = Database(path, read_only=True)
        try:
            rows = ro.get_candles("BTC", "1m", limit=10)
            assert len(rows) == 1, rows
            assert rows[0].symbol == "BTC"  # get_candles returns Candle objects
        finally:
            ro.close()


def test_read_only_connection_refuses_writes() -> None:
    """mode=ro is enforced by SQLite itself, not by our own discipline."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot.db"
        _seed(path)
        ro = Database(path, read_only=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                ro._conn().execute(
                    "INSERT INTO candles_1m (symbol, timestamp_ms) VALUES ('ETH', 1)"
                )
        finally:
            ro.close()


def test_read_only_runs_no_schema_ddl_on_a_foreign_database() -> None:
    """_init_db() must not run: opening someone else's DB read-only may not
    add our tables to it (the live bot owns that schema, not the sweep)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "foreign.db"
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE unrelated (x INTEGER)")
        con.commit()
        con.close()

        Database(path, read_only=True).close()

        con = sqlite3.connect(str(path))
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        assert names == {"unrelated"}, names


def test_read_only_missing_file_raises_instead_of_creating() -> None:
    """A read-write open would create an empty DB and the sweep would silently
    backtest on zero candles. Read-only must fail loud."""
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope" / "bot.db"
        with pytest.raises(sqlite3.OperationalError):
            Database(missing, read_only=True)._conn()
        assert not missing.exists()


def test_default_open_is_still_read_write() -> None:
    """The bot's own path is untouched by the new keyword."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot.db"
        db = Database(path)
        try:
            db._conn().execute(
                "INSERT INTO candles_1m (symbol, timestamp_ms, open, high, low, "
                "close, volume) VALUES ('ETH', 1700000060000, 1.0, 2.0, 0.5, 1.5, 1.0)"
            )
            db._conn().commit()
            assert len(db.get_candles("ETH", "1m", limit=10)) == 1
        finally:
            db.close()
