"""SQLite evidence store — boros_* tables only, append-only by design.

boros_snapshots      — hourly raw payloads (markets + perp books),
                       zlib-compressed, sha256'd: tamper-evident + PIT.
boros_opportunities  — every evaluated candidate per snapshot with its
                       math (fixed, costs, capital, net_apy) + verdict.
boros_trades         — paper entries (open -> matured -> closed in
                       Phase 2). One open per (asset, venue_b, maturity).
boros_log            — timestamped event log.
boros_meta           — heartbeat / small key-value state.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS boros_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- markets|books
    sha256 TEXT NOT NULL,          -- of the UNcompressed canonical json
    payload_zlib BLOB NOT NULL,
    n_rows INTEGER,
    meta_json TEXT                 -- e.g. venues_unavailable
);
CREATE INDEX IF NOT EXISTS ix_bsnap_ts ON boros_snapshots(ts_ms);

CREATE TABLE IF NOT EXISTS boros_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    asset TEXT, venue_b TEXT, maturity_ms INTEGER,
    structure TEXT, direction TEXT,
    fixed_bruto REAL, net_apy REAL,
    capital_usd REAL, costs_usd REAL, ttm_days REAL,
    eligible INTEGER NOT NULL DEFAULT 0,
    entered INTEGER NOT NULL DEFAULT 0,
    block_reason TEXT,
    legs_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_bopp_ts ON boros_opportunities(ts_ms);

CREATE TABLE IF NOT EXISTS boros_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL, venue_b TEXT NOT NULL, maturity_ms INTEGER NOT NULL,
    structure TEXT NOT NULL, direction TEXT NOT NULL,
    state TEXT NOT NULL,           -- open|matured|closed(Phase2)
    entry_ms INTEGER NOT NULL,
    fixed_bruto REAL, net_apy REAL,
    capital_usd REAL, costs_usd REAL,
    prices_json TEXT               -- every entry price/leg
);
CREATE INDEX IF NOT EXISTS ix_btrades_state ON boros_trades(state);
CREATE UNIQUE INDEX IF NOT EXISTS ix_btrades_open
    ON boros_trades(asset, venue_b, maturity_ms) WHERE state='open';

CREATE TABLE IF NOT EXISTS boros_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref_id INTEGER,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_blog_ts ON boros_log(ts_ms);

CREATE TABLE IF NOT EXISTS boros_meta (key TEXT PRIMARY KEY, value TEXT);
"""


class BorosLedger:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.path))
        self._con.executescript(_SCHEMA)
        self._con.commit()

    # ─── snapshots ───────────────────────────────────────────────────────

    def snapshot_write(self, ts_ms: int, kind: str, payload: Any,
                       n_rows: int = 0,
                       meta: Optional[Dict] = None) -> int:
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        sha = hashlib.sha256(blob).hexdigest()
        cur = self._con.execute(
            "INSERT INTO boros_snapshots"
            " (ts_ms, kind, sha256, payload_zlib, n_rows, meta_json)"
            " VALUES (?,?,?,?,?,?)",
            (ts_ms, kind, sha, zlib.compress(blob), n_rows,
             json.dumps(meta or {})))
        self._con.commit()
        return int(cur.lastrowid)

    def snapshot_read(self, snapshot_id: int) -> Optional[Dict]:
        r = self._con.execute(
            "SELECT payload_zlib FROM boros_snapshots WHERE id=?",
            (snapshot_id,)).fetchone()
        return json.loads(zlib.decompress(r[0])) if r else None

    # ─── opportunities / trades ──────────────────────────────────────────

    def opportunity_insert(self, row: Dict[str, Any]) -> int:
        cols = ",".join(row.keys())
        cur = self._con.execute(
            f"INSERT INTO boros_opportunities ({cols})"
            f" VALUES ({','.join('?' * len(row))})",
            tuple(row.values()))
        self._con.commit()
        return int(cur.lastrowid)

    def trade_insert(self, row: Dict[str, Any]) -> Optional[int]:
        """INSERT only when no open trade exists for the triple —
        the partial unique index enforces it (IntegrityError -> None)."""
        cols = ",".join(row.keys())
        try:
            cur = self._con.execute(
                f"INSERT INTO boros_trades ({cols})"
                f" VALUES ({','.join('?' * len(row))})",
                tuple(row.values()))
        except sqlite3.IntegrityError:
            return None
        self._con.commit()
        return int(cur.lastrowid)

    def trades_by_state(self, *states: str) -> List[Dict[str, Any]]:
        q = ",".join("?" * len(states)) or "''"
        cur = self._con.execute(
            f"SELECT * FROM boros_trades WHERE state IN ({q})"
            " ORDER BY entry_ms", states)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def trade_update(self, trade_id: int, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self._con.execute(
            f"UPDATE boros_trades SET {sets} WHERE id=?",
            tuple(fields.values()) + (trade_id,))
        self._con.commit()

    def open_triples(self) -> set:
        return {(t["asset"], t["venue_b"], t["maturity_ms"])
                for t in self.trades_by_state("open")}

    def entries_today_for_asset(self, asset: str, day_ms: int) -> int:
        """New entries for `asset` on this UTC day — the 1/day cap."""
        return int(self._con.execute(
            "SELECT COUNT(*) FROM boros_trades"
            " WHERE asset=? AND entry_ms >= ? AND entry_ms < ?",
            (asset, day_ms, day_ms + 86_400_000)).fetchone()[0])

    # ─── log / meta / counts ─────────────────────────────────────────────

    def log(self, kind: str, data: Dict[str, Any],
            ref_id: Optional[int] = None, ts_ms: Optional[int] = None
            ) -> None:
        self._con.execute(
            "INSERT INTO boros_log (ts_ms, kind, ref_id, data)"
            " VALUES (?,?,?,?)",
            (ts_ms or int(time.time() * 1000), kind, ref_id,
             json.dumps(data, default=str)))
        self._con.commit()

    def meta_get(self, key: str) -> Optional[str]:
        r = self._con.execute(
            "SELECT value FROM boros_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def meta_set(self, key: str, value: str) -> None:
        self._con.execute(
            "INSERT OR REPLACE INTO boros_meta (key,value) VALUES (?,?)",
            (key, value))
        self._con.commit()

    def meta_incr(self, key: str) -> int:
        val = int(self.meta_get(key) or 0) + 1
        self.meta_set(key, str(val))
        return val

    def count(self, table: str, where: str = "", args: tuple = ()) -> int:
        return int(self._con.execute(
            f"SELECT COUNT(*) FROM {table} {where}", args).fetchone()[0])

    def close(self) -> None:
        self._con.close()
