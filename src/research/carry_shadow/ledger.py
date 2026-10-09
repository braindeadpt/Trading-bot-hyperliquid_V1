"""Append-only SQLite ledger for carry-shadow observations.

Everything auditable is a row: entry attempts, fills, rebalances,
liquidations, funding accruals, ADL observations, episode snapshots.
No UPDATEs on events — episodes get a mutable summary row for the live
state machine plus an immutable event stream.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL,
    perp TEXT NOT NULL,
    pm_branch INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,           -- pending_entry|open|closed|aborted|liquidated
    entry_decision_ms INTEGER NOT NULL,
    opened_ms INTEGER,
    closed_ms INTEGER,
    close_reason TEXT,             -- funding_exit|delev_floor|liquidated|gap_kill|abort_unfilled|forced_stop
    -- accounting on 1 unit notional at entry
    p0 REAL, s0 REAL,              -- entry mark prices (fill marks)
    q REAL NOT NULL DEFAULT 1.0,   -- remaining fraction of both legs
    margin REAL NOT NULL DEFAULT 0.60,
    cum_f REAL NOT NULL DEFAULT 0.0,
    realized_pnl REAL NOT NULL DEFAULT 0.0,
    cost_bps REAL NOT NULL DEFAULT 0.0,
    maint REAL, m_trigger REAL, m0 REAL,
    last_funding_ms INTEGER,
    gap_unverified INTEGER NOT NULL DEFAULT 0,  -- 1 = WS gap could not be replayed
    notes TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    episode_id INTEGER,
    kind TEXT NOT NULL,   -- entry_attempt|fill|unfilled|rebalance|liquidation|
                        -- gap_kill|adl_obs|funding|exit|unlegged_unwind
    data TEXT NOT NULL    -- json blob
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts_ms);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind);
CREATE TABLE IF NOT EXISTS fill_stats (
    leg TEXT NOT NULL,      -- spot|perp (entry_*|exit_* prefixed)
    ts_ms INTEGER NOT NULL,
    filled INTEGER NOT NULL,           -- strict-crossing model (THE GATE)
    time_to_fill_s REAL,
    proxy_filled INTEGER,              -- context only: aggressor vol >= size
    proxy_vol_usd REAL,
    PRIMARY KEY (leg, ts_ms)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Ledger:
    """Append-only writer + open-episode reloader."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.path))
        self._con.executescript(_SCHEMA)
        self._migrate()
        self._con.commit()

    def _migrate(self) -> None:
        """Column adds for ledgers created before these fields existed."""
        cols = {r[1] for r in self._con.execute("PRAGMA table_info(fill_stats)")}
        for col, ddl in (("proxy_filled", "ALTER TABLE fill_stats ADD COLUMN proxy_filled INTEGER"),
                         ("proxy_vol_usd", "ALTER TABLE fill_stats ADD COLUMN proxy_vol_usd REAL"),
                         # §12 sensitivity (2026-10-09, measurement only):
                         # crossed visible size + tape vol vs need, per leg.
                         # NULL on rows that predate the columns.
                         ("crossing_size_usd", "ALTER TABLE fill_stats ADD COLUMN crossing_size_usd REAL"),
                         ("fill_frac_est", "ALTER TABLE fill_stats ADD COLUMN fill_frac_est REAL")):
            if col not in cols:
                self._con.execute(ddl)
        ecols = {r[1] for r in self._con.execute("PRAGMA table_info(episodes)")}
        if "gap_unverified" not in ecols:
            self._con.execute(
                "ALTER TABLE episodes ADD COLUMN gap_unverified INTEGER DEFAULT 0")
        # unlegged-risk instrumentation (2026-10-09, measurement only):
        # NULL on episodes that predate the columns — never backfilled
        for col, ddl in (
            ("unlegged_s", "ALTER TABLE episodes ADD COLUMN unlegged_s REAL"),
            ("unlegged_max_adverse_bps",
             "ALTER TABLE episodes ADD COLUMN unlegged_max_adverse_bps REAL"),
            ("unlegged_basis_bps",
             "ALTER TABLE episodes ADD COLUMN unlegged_basis_bps REAL"),
            # §12: episode-level fill fraction = min(leg fracs) — only set
            # when both legs filled under the measured build; NULL else.
            ("fill_frac_est",
             "ALTER TABLE episodes ADD COLUMN fill_frac_est REAL"),
        ):
            if col not in ecols:
                self._con.execute(ddl)

    def event(self, kind: str, data: Dict[str, Any],
              episode_id: Optional[int] = None, ts_ms: Optional[int] = None) -> None:
        self._con.execute(
            "INSERT INTO events (ts_ms, episode_id, kind, data) VALUES (?,?,?,?)",
            (ts_ms or int(time.time() * 1000), episode_id, kind,
             json.dumps(data, default=str)))
        self._con.commit()

    def episode_open(self, row: Dict[str, Any]) -> int:
        cols = ",".join(row.keys())
        cur = self._con.execute(
            f"INSERT INTO episodes ({cols}) VALUES ({','.join('?' * len(row))})",
            tuple(row.values()))
        self._con.commit()
        return int(cur.lastrowid)

    def episode_update(self, ep_id: int, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self._con.execute(
            f"UPDATE episodes SET {sets} WHERE id=?",
            tuple(fields.values()) + (ep_id,))
        self._con.commit()

    def open_episodes(self) -> list[Dict[str, Any]]:
        cur = self._con.execute(
            "SELECT * FROM episodes WHERE state IN ('pending_entry','open')")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def record_fill(self, leg: str, filled: bool, ttf_s: Optional[float],
                    ts_ms: int, proxy_filled: Optional[bool] = None,
                    proxy_vol_usd: Optional[float] = None,
                    crossing_size_usd: Optional[float] = None,
                    fill_frac_est: Optional[float] = None) -> None:
        self._con.execute(
            "INSERT OR REPLACE INTO fill_stats"
            " (leg,ts_ms,filled,time_to_fill_s,proxy_filled,proxy_vol_usd,"
            "  crossing_size_usd,fill_frac_est)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (leg, ts_ms, int(filled), ttf_s,
             None if proxy_filled is None else int(proxy_filled),
             proxy_vol_usd, crossing_size_usd, fill_frac_est))
        self._con.commit()

    def fill_rate(self, leg: str) -> tuple[int, float]:
        n, f = self._con.execute(
            "SELECT COUNT(*), COALESCE(SUM(filled),0) FROM fill_stats WHERE leg=?",
            (leg,)).fetchone()
        return int(n), (float(f) / n if n else 0.0)

    def closed_episode_n(self) -> int:
        return int(self._con.execute(
            "SELECT COUNT(*) FROM episodes WHERE state='closed'").fetchone()[0])

    def closed_rows(self) -> list[Dict[str, Any]]:
        cur = self._con.execute(
            "SELECT * FROM episodes WHERE state='closed'"
            " ORDER BY entry_decision_ms")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def meta_get(self, key: str) -> Optional[str]:
        r = self._con.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def meta_set(self, key: str, value: str) -> None:
        self._con.execute(
            "INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (key, value))
        self._con.commit()

    def meta_incr(self, key: str) -> int:
        val = int(self.meta_get(key) or 0) + 1
        self.meta_set(key, str(val))
        return val

    def close(self) -> None:
        self._con.close()
