"""SQLite store — isolated ledger, own tables only."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_meta (
    k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS probe_sessions (
    session_id TEXT PRIMARY KEY,
    window TEXT NOT NULL,           -- 'us-close' | 'control'
    ny_date TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER,
    n_events INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS probe_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    t0_ms INTEGER NOT NULL,          -- Binance move detected (local clock)
    bin_symbol TEXT NOT NULL,
    xyz_symbol TEXT NOT NULL,
    direction INTEGER NOT NULL,      -- +1 up, -1 down
    bin_move_bps REAL NOT NULL,
    delay_ms INTEGER,                -- xyz half-move latency; NULL=no move
    entry_px REAL, exit_px REAL,     -- taker ask/bid at t0+500ms, mid at t0+5s
    gain_bps REAL,                   -- sign-adj move minus 11bps; NULL=no_fill
    status TEXT NOT NULL,            -- 'closed' | 'no_move' | 'no_fill'
    detail_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_pev_sess ON probe_events(session_id);
"""


class ProbeStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def meta_set(self, k: str, v: str) -> None:
        self.conn.execute(
            "INSERT INTO probe_meta(k,v) VALUES(?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.conn.commit()

    def meta_get(self, k: str) -> Optional[str]:
        r = self.conn.execute(
            "SELECT v FROM probe_meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def session_open(self, session_id: str, window: str,
                     ny_date: str, start_ms: int) -> None:
        self.conn.execute(
            "INSERT INTO probe_sessions(session_id, window, ny_date, start_ms)"
            " VALUES(?,?,?,?) ON CONFLICT(session_id) DO NOTHING",
            (session_id, window, ny_date, start_ms))
        self.conn.commit()

    def session_close(self, session_id: str, end_ms: int) -> None:
        self.conn.execute(
            "UPDATE probe_sessions SET end_ms=? WHERE session_id=?",
            (end_ms, session_id))
        self.conn.commit()

    def event_insert(self, row: Dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO probe_events(session_id, t0_ms, bin_symbol,"
            " xyz_symbol, direction, bin_move_bps, delay_ms, entry_px,"
            " exit_px, gain_bps, status, detail_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["session_id"], row["t0_ms"], row["bin_symbol"],
             row["xyz_symbol"], row["direction"], row["bin_move_bps"],
             row.get("delay_ms"), row.get("entry_px"), row.get("exit_px"),
             row.get("gain_bps"), row["status"],
             json.dumps(row.get("detail") or {}, sort_keys=True)))
        self.conn.execute(
            "UPDATE probe_sessions SET n_events = n_events + 1"
            " WHERE session_id=?", (row["session_id"],))
        self.conn.commit()
        return int(cur.lastrowid)

    def close(self) -> None:
        self.conn.close()
