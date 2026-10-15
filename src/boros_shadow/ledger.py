"""SQLite evidence store — boros_* tables only, append-only by design.

boros_snapshots      — hourly raw payloads (markets + perp books),
                       zlib-compressed, sha256'd: tamper-evident + PIT.
boros_opportunities  — every evaluated candidate per snapshot with its
                       math (fixed, costs, capital, net_apy) + verdict.
boros_trades         — paper entries (open -> matured -> closed in
                       Phase 2). One open per (asset, venue_b, maturity).
boros_marks          — per-pass marks of every open trade (perp mids +
                       exit VWAPs, YU mids, cumulative funding/floating,
                       staleness, liquidation inputs) — Phase 2.
boros_funding        — per-leg funding events applied to a trade,
                       incremental since entry, with source tags.
boros_close          — realized-PnL components per closed trade
                       (fixed/floating/basis/costs/realized/ratio/
                       cluster_id). Sealed until the read gate.
boros_flags          — kill flags (liquidation, n5_ratio) — booleans.
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

CREATE TABLE IF NOT EXISTS boros_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    ts_ms INTEGER NOT NULL,
    on_schedule INTEGER NOT NULL,
    legs_json TEXT NOT NULL,       -- mids/exit-vwap per leg + stale flags
    yu_json TEXT NOT NULL,         -- per-Boros-leg mid rates + sources
    funding_usd REAL NOT NULL DEFAULT 0,   -- cumulative net perp funding
    floating_usd REAL NOT NULL DEFAULT 0,  -- cumulative signed Boros float
    equity_pooled REAL,
    data_gap INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_bmarks_tr ON boros_marks(trade_id, ts_ms);

CREATE TABLE IF NOT EXISTS boros_funding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    leg TEXT NOT NULL,             -- hl | b | spread
    venue TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,        -- funding event timestamp
    rate REAL NOT NULL,
    cash_usd REAL NOT NULL,        -- signed: + received / - paid
    source TEXT NOT NULL,          -- venue | unavailable
    UNIQUE(trade_id, leg, ts_ms)
);
CREATE INDEX IF NOT EXISTS ix_bfund_tr ON boros_funding(trade_id, leg, ts_ms);

CREATE TABLE IF NOT EXISTS boros_close (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL UNIQUE,
    ts_ms INTEGER NOT NULL,        -- close pass timestamp
    reason TEXT NOT NULL,          -- maturity|liquidated_pooled|
                                   -- liquidated_boros|delisted
    flags_json TEXT,               -- close_fallback / anomaly / data_gap
    fixed_usd REAL, floating_usd REAL, basis_usd REAL, costs_usd REAL,
    realized_usd REAL, realized_apy_cap REAL,
    locked_usd REAL, ratio REAL,
    cluster_id INTEGER
);

CREATE TABLE IF NOT EXISTS boros_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    asset TEXT NOT NULL,
    rate REAL NOT NULL,            -- sampled venue rate (Lighter, /hour)
    UNIQUE(ts_ms, asset)
);
CREATE INDEX IF NOT EXISTS ix_brates ON boros_rates(asset, ts_ms);

CREATE TABLE IF NOT EXISTS boros_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag TEXT NOT NULL,
    trade_id INTEGER,
    ts_ms INTEGER NOT NULL,
    data_json TEXT                 -- counts/ids only, never PnL
);
CREATE INDEX IF NOT EXISTS ix_bflags ON boros_flags(flag);

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

    # ─── Phase 2: marks / funding / close / flags ────────────────────────

    def mark_insert(self, row: Dict[str, Any]) -> int:
        cols = ",".join(row.keys())
        cur = self._con.execute(
            f"INSERT INTO boros_marks ({cols})"
            f" VALUES ({','.join('?' * len(row))})",
            tuple(row.values()))
        self._con.commit()
        return int(cur.lastrowid)

    def last_mark(self, trade_id: int) -> Optional[Dict[str, Any]]:
        r = self._con.execute(
            "SELECT * FROM boros_marks WHERE trade_id=?"
            " ORDER BY ts_ms DESC LIMIT 1", (trade_id,)).fetchone()
        if not r:
            return None
        cols = [d[0] for d in self._con.execute(
            "SELECT * FROM boros_marks LIMIT 0").description]
        return dict(zip(cols, r))

    def funding_insert(self, row: Dict[str, Any]) -> None:
        cols = ",".join(row.keys())
        self._con.execute(
            f"INSERT OR IGNORE INTO boros_funding ({cols})"
            f" VALUES ({','.join('?' * len(row))})",
            tuple(row.values()))
        self._con.commit()

    def funding_cursor(self, trade_id: int, leg: str) -> Optional[int]:
        r = self._con.execute(
            "SELECT MAX(ts_ms) FROM boros_funding"
            " WHERE trade_id=? AND leg=?", (trade_id, leg)).fetchone()
        return r[0]

    def funding_sum(self, trade_id: int) -> float:
        r = self._con.execute(
            "SELECT COALESCE(SUM(cash_usd),0) FROM boros_funding"
            " WHERE trade_id=?", (trade_id,)).fetchone()
        return float(r[0])

    def close_insert(self, row: Dict[str, Any]) -> int:
        cols = ",".join(row.keys())
        cur = self._con.execute(
            f"INSERT INTO boros_close ({cols})"
            f" VALUES ({','.join('?' * len(row))})",
            tuple(row.values()))
        self._con.commit()
        return int(cur.lastrowid)

    def closes(self) -> List[Dict[str, Any]]:
        cur = self._con.execute(
            "SELECT c.*, t.asset, t.entry_ms, t.maturity_ms, t.direction"
            " FROM boros_close c JOIN boros_trades t ON t.id=c.trade_id"
            " ORDER BY c.trade_id")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def flag_set(self, flag: str, trade_id: Optional[int],
                 ts_ms: int, data: Optional[Dict] = None) -> None:
        self._con.execute(
            "INSERT INTO boros_flags (flag, trade_id, ts_ms, data_json)"
            " VALUES (?,?,?,?)",
            (flag, trade_id, ts_ms, json.dumps(data or {})))
        self._con.commit()

    def rate_insert(self, ts_ms: int, asset: str, rate: float) -> None:
        self._con.execute(
            "INSERT OR IGNORE INTO boros_rates (ts_ms, asset, rate)"
            " VALUES (?,?,?)", (ts_ms, asset, rate))
        self._con.commit()

    def rates_at(self, ts_ms: int) -> Dict[str, float]:
        """Latest sampled rate per asset at-or-before ts (backfill)."""
        out: Dict[str, float] = {}
        for asset, rate in self._con.execute(
                "SELECT asset, rate FROM boros_rates r"
                " WHERE ts_ms = (SELECT MAX(ts_ms) FROM boros_rates"
                "               WHERE asset=r.asset AND ts_ms<=?)"
                " ORDER BY asset", (ts_ms,)):
            out[asset] = rate
        return out

    def flag_get(self, flag: str) -> List[Dict[str, Any]]:
        cur = self._con.execute(
            "SELECT * FROM boros_flags WHERE flag=? ORDER BY id", (flag,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def snapshot_pairs(self) -> List[Dict[str, Any]]:
        """(markets, books) rows grouped by ts, ascending — the backfill
        replay stream."""
        cur = self._con.execute(
            "SELECT id, ts_ms, kind FROM boros_snapshots ORDER BY ts_ms, id")
        rows = cur.fetchall()
        pairs: Dict[int, Dict[str, Any]] = {}
        order: List[int] = []
        for sid, ts, kind in rows:
            if ts not in pairs:
                pairs[ts] = {"ts_ms": ts}
                order.append(ts)
            pairs[ts][kind] = sid
        return [pairs[ts] for ts in order
                if "markets" in pairs[ts] and "books" in pairs[ts]]

    def close(self) -> None:
        self._con.close()
