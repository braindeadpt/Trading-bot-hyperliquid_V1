"""Append-only SQLite ledger for unlock-shadow observations.

Own file (data/research/unlock_shadow.db), own unlock_* tables — same
isolation shape as carry_shadow. Raw snapshots are stored verbatim with
sha256 for byte-exact replay; the event stream is append-only; event and
sim rows are mutable ONLY on their state/lifecycle fields.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS unlock_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    source TEXT NOT NULL,          -- defillama|cryptorank|tokenomist
    token TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    n_raw INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_usnap_ts ON unlock_snapshots(ts_ms);

CREATE TABLE IF NOT EXISTS unlock_calendar (
    -- one row per (token, event_ms, amount, source): the point-in-time
    -- first_seen/last_seen book that eligibility (§2) reads
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL,
    event_ms INTEGER NOT NULL,
    amount_tokens REAL NOT NULL,
    pct_unlocked REAL,
    category TEXT,
    kind TEXT NOT NULL,            -- cliff|linear|excluded
    source TEXT NOT NULL,          -- defillama|cryptorank|tokenomist
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    UNIQUE (token, event_ms, amount_tokens, source)
);
CREATE INDEX IF NOT EXISTS ix_ucal_tok ON unlock_calendar(token, event_ms);

CREATE TABLE IF NOT EXISTS unlock_events (
    -- resolved cross-source verdicts; UNIQUE per (token, canonical date)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL,
    event_ms INTEGER NOT NULL,     -- canonical = source-A date
    amount_tokens REAL NOT NULL,
    pct_unlocked REAL,
    state TEXT NOT NULL,           -- pending|eligible|not_eligible (Emenda 1)
    seen_a_ms INTEGER, seen_b_ms INTEGER,
    amt_b REAL, dt_b_ms INTEGER,
    b_confirmed INTEGER NOT NULL DEFAULT 0,  -- descriptive only (Emenda 1)
    cluster_head_ms INTEGER,       -- self when head; head's date when merged
    adv7d_usd REAL,                -- measured at T-5 (None = not measured)
    adv_gated INTEGER NOT NULL DEFAULT 0,  -- 1 = filtered out by ADV < $10M
    registered_ms INTEGER,
    UNIQUE (token, event_ms)
);

CREATE TABLE IF NOT EXISTS unlock_sims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- event|placebo
    parent_event_id INTEGER,       -- placebo -> owning unlock_events.id
    token TEXT NOT NULL,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    state TEXT NOT NULL,           -- scheduled|open|closed|invalidated|gated_out
    beta REAL, hedge_notional_usd REAL,
    tok_px_in REAL, tok_px_out REAL, btc_px_in REAL, btc_px_out REAL,
    tok_slip_in_bps REAL, tok_slip_out_bps REAL,
    btc_slip_in_bps REAL, btc_slip_out_bps REAL,
    slip_unverified INTEGER NOT NULL DEFAULT 0,
    funding_tok_usd REAL, funding_btc_usd REAL,
    gross_usd REAL, fees_usd REAL, net_usd REAL, net_bps REAL,
    priced_in_ms INTEGER, priced_out_ms INTEGER,
    review_flag INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    UNIQUE (kind, parent_event_id, window_start_ms)
);
CREATE INDEX IF NOT EXISTS ix_usims_state ON unlock_sims(state);

CREATE TABLE IF NOT EXISTS unlock_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- snapshot|eligible|single|merged|open|
                                   -- close|adv_gate|placebo_draw|invalidate|
                                   -- manual_review|error
    ref_id INTEGER,
    data TEXT NOT NULL             -- json blob
);
CREATE INDEX IF NOT EXISTS ix_ulog_ts ON unlock_log(ts_ms);

CREATE TABLE IF NOT EXISTS unlock_meta (key TEXT PRIMARY KEY, value TEXT);
"""


class UnlockLedger:
    """Writer + reader for the unlock-shadow evidence store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.path))
        self._con.executescript(_SCHEMA)
        self._migrate()
        self._con.commit()

    def _migrate(self) -> None:
        """Additive migrations for DBs created before Emenda 1."""
        cols = {r[1] for r in self._con.execute(
            "PRAGMA table_info(unlock_events)")}
        if "b_confirmed" not in cols:
            self._con.execute(
                "ALTER TABLE unlock_events ADD COLUMN b_confirmed"
                " INTEGER NOT NULL DEFAULT 0")

    # ─── primitives ──────────────────────────────────────────────────────

    def log(self, kind: str, data: Dict[str, Any],
            ref_id: Optional[int] = None, ts_ms: Optional[int] = None
            ) -> None:
        self._con.execute(
            "INSERT INTO unlock_log (ts_ms, kind, ref_id, data)"
            " VALUES (?,?,?,?)",
            (ts_ms or int(time.time() * 1000), kind, ref_id,
             json.dumps(data, default=str)))
        self._con.commit()

    def snapshot_write(self, ts_ms: int, source: str, token: str,
                       sha256: str, payload_json: str, n_raw: int) -> int:
        cur = self._con.execute(
            "INSERT INTO unlock_snapshots"
            " (ts_ms, source, token, sha256, payload_json, n_raw)"
            " VALUES (?,?,?,?,?,?)",
            (ts_ms, source, token, sha256, payload_json, n_raw))
        self._con.commit()
        return int(cur.lastrowid)

    def calendar_seen(self, token: str, event_ms: int, amount: float,
                      pct: Optional[float], category: str, kind: str,
                      source: str, seen_ms: int) -> None:
        """INSERT OR keep first_seen; always bumps last_seen — the PIT
        visibility book §2 reads."""
        self._con.execute(
            "INSERT INTO unlock_calendar (token,event_ms,amount_tokens,"
            " pct_unlocked,category,kind,source,first_seen_ms,last_seen_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(token,event_ms,amount_tokens,source)"
            " DO UPDATE SET last_seen_ms=excluded.last_seen_ms,"
            " pct_unlocked=COALESCE(unlock_calendar.pct_unlocked,"
            "   excluded.pct_unlocked)",
            (token, event_ms, amount, pct, category, kind, source,
             seen_ms, seen_ms))
        self._con.commit()

    def calendar_rows(self) -> List[Dict[str, Any]]:
        cur = self._con.execute(
            "SELECT * FROM unlock_calendar ORDER BY event_ms")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ─── resolved events ─────────────────────────────────────────────────

    def event_upsert(self, row: Dict[str, Any]) -> int:
        """Insert or update-by-key (token,event_ms). Returns row id."""
        cur = self._con.execute(
            "SELECT id FROM unlock_events WHERE token=? AND event_ms=?",
            (row["token"], row["event_ms"]))
        found = cur.fetchone()
        if found is None:
            cols = ",".join(row.keys())
            c = self._con.execute(
                f"INSERT INTO unlock_events ({cols})"
                f" VALUES ({','.join('?' * len(row))})",
                tuple(row.values()))
            self._con.commit()
            return int(c.lastrowid)
        sets = ",".join(f"{k}=?" for k in row if k not in ("token", "event_ms"))
        vals = [v for k, v in row.items() if k not in ("token", "event_ms")]
        if sets:
            self._con.execute(
                f"UPDATE unlock_events SET {sets} WHERE id=?",
                tuple(vals) + (found[0],))
            self._con.commit()
        return int(found[0])

    def event_row(self, event_id: int) -> Optional[Dict[str, Any]]:
        cur = self._con.execute(
            "SELECT * FROM unlock_events WHERE id=?", (event_id,))
        r = cur.fetchone()
        if r is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, r))

    def events_by_state(self, *states: str) -> List[Dict[str, Any]]:
        q = ",".join("?" * len(states)) or "''"
        cur = self._con.execute(
            f"SELECT * FROM unlock_events WHERE state IN ({q})"
            " ORDER BY event_ms", states)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def all_events(self) -> List[Dict[str, Any]]:
        cur = self._con.execute("SELECT * FROM unlock_events ORDER BY event_ms")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ─── sims ────────────────────────────────────────────────────────────

    def sim_insert(self, row: Dict[str, Any]) -> Optional[int]:
        """INSERT OR IGNORE on (kind,parent_event_id,window_start_ms)."""
        cols = ",".join(row.keys())
        cur = self._con.execute(
            f"INSERT OR IGNORE INTO unlock_sims ({cols})"
            f" VALUES ({','.join('?' * len(row))})",
            tuple(row.values()))
        self._con.commit()
        return int(cur.lastrowid) if cur.rowcount else None

    def sim_update(self, sim_id: int, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self._con.execute(
            f"UPDATE unlock_sims SET {sets} WHERE id=?",
            tuple(fields.values()) + (sim_id,))
        self._con.commit()

    def sims_by_state(self, *states: str) -> List[Dict[str, Any]]:
        q = ",".join("?" * len(states)) or "''"
        cur = self._con.execute(
            f"SELECT * FROM unlock_sims WHERE state IN ({q})"
            " ORDER BY window_start_ms", states)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ─── meta / counts ───────────────────────────────────────────────────

    def meta_get(self, key: str) -> Optional[str]:
        r = self._con.execute(
            "SELECT value FROM unlock_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def meta_set(self, key: str, value: str) -> None:
        self._con.execute(
            "INSERT OR REPLACE INTO unlock_meta (key,value) VALUES (?,?)",
            (key, value))
        self._con.commit()

    def meta_incr(self, key: str) -> int:
        val = int(self.meta_get(key) or 0) + 1
        self.meta_set(key, str(val))
        return val

    def count(self, table: str, where: str = "",
              args: tuple = ()) -> int:
        return int(self._con.execute(
            f"SELECT COUNT(*) FROM {table} {where}", args).fetchone()[0])

    def close(self) -> None:
        self._con.close()
