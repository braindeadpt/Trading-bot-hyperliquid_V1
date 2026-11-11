"""SQLite evidence store — resmom_* tables only, append-only by design.

resmom_rebalances — one row per (week, arm): universe, signals, target
                    weights, turnover/cost charged, input sha256.
resmom_positions  — paper legs opened/closed at each rebalance (qty,
                    entry/exit px, cumulative funding paid).
resmom_nav        — daily equity marks per arm (nav = 10k + cum net pnl,
                    gross, drawdown, per-leg contribution json).
resmom_nulls      — 200 random-signal portfolios per week: seed, weights,
                    week PnL, running NAV (Sharpe p75 reference).
resmom_marks      — daily mids per held symbol (audit trail).
resmom_funding    — funding events applied to open legs (rate, pnl).
resmom_meta       — heartbeat / small key-value state.
resmom_log        — timestamped event log.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resmom_meta (
    k TEXT PRIMARY KEY, v TEXT, ts_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS resmom_rebalances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_key_ms INTEGER NOT NULL,        -- Monday 00:00 UTC of the week
    arm TEXT NOT NULL,                   -- 'A' | 'B'
    created_at_ms INTEGER NOT NULL,
    universe_json TEXT NOT NULL,         -- ranked eligible symbols + vols
    signals_json TEXT NOT NULL,          -- per-symbol beta/resid15/vol30/s
    weights_json TEXT NOT NULL,          -- signed target gross weights
    turnover_usd REAL NOT NULL,
    cost_usd REAL NOT NULL,
    flags_json TEXT,                     -- e.g. beta_neutral_unsolvable
    inputs_sha256 TEXT NOT NULL,
    UNIQUE (week_key_ms, arm)
);

CREATE TABLE IF NOT EXISTS resmom_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rebalance_id INTEGER NOT NULL,
    arm TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                  -- 'long' | 'short'
    weight REAL NOT NULL,                -- signed gross weight (sum|w|=1)
    qty REAL NOT NULL,
    entry_px REAL NOT NULL,
    entry_ms INTEGER NOT NULL,
    exit_px REAL, exit_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'open', -- 'open' | 'closed'
    funding_usd REAL NOT NULL DEFAULT 0.0,
    pnl_usd REAL,                        -- price pnl at close (ex-funding)
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_rpos_arm ON resmom_positions(arm, status);
CREATE INDEX IF NOT EXISTS ix_rpos_reb ON resmom_positions(rebalance_id);

CREATE TABLE IF NOT EXISTS resmom_nav (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    arm TEXT NOT NULL,
    nav_usd REAL NOT NULL,
    gross_usd REAL NOT NULL,
    n_positions INTEGER NOT NULL,
    dd_pct REAL NOT NULL,                -- descriptive drawdown vs peak
    legs_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_rnav ON resmom_nav(arm, ts_ms);

CREATE TABLE IF NOT EXISTS resmom_nulls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_key_ms INTEGER NOT NULL,
    rebalance_id INTEGER NOT NULL,
    null_id INTEGER NOT NULL,
    seed TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    week_pnl_usd REAL,                   -- filled when the week closes
    nav_usd REAL NOT NULL,               -- running NAV after this week
    cost_usd REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS ix_rnull ON resmom_nulls(week_key_ms, null_id);

CREATE TABLE IF NOT EXISTS resmom_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    arm TEXT NOT NULL,
    symbol TEXT NOT NULL,
    mid REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rmarks ON resmom_marks(ts_ms);

CREATE TABLE IF NOT EXISTS resmom_funding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,              -- when applied (pass time)
    event_ms INTEGER NOT NULL,           -- funding event time
    arm TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rate REAL NOT NULL,
    notional_usd REAL NOT NULL,
    pnl_usd REAL NOT NULL                -- signed: + received, - paid
);
CREATE INDEX IF NOT EXISTS ix_rfund ON resmom_funding(symbol, event_ms);

CREATE TABLE IF NOT EXISTS resmom_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref_id INTEGER,
    data_json TEXT
);
"""


class ResmomLedger:
    def __init__(self, db_path: str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ── meta ──────────────────────────────────────────────────────────
    def meta_get(self, k: str) -> Optional[str]:
        r = self.conn.execute(
            "SELECT v FROM resmom_meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def meta_set(self, k: str, v: str) -> None:
        self.conn.execute(
            "INSERT INTO resmom_meta(k,v,ts_ms) VALUES(?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts_ms=excluded.ts_ms",
            (k, v, int(time.time() * 1000)))
        self.conn.commit()

    def meta_incr(self, k: str) -> int:
        cur = int(self.meta_get(k) or 0) + 1
        self.meta_set(k, str(cur))
        return cur

    # ── rebalances / positions / nav / nulls ──────────────────────────
    def rebalance_insert(self, row: Dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO resmom_rebalances(week_key_ms, arm, created_at_ms,"
            " universe_json, signals_json, weights_json, turnover_usd,"
            " cost_usd, flags_json, inputs_sha256)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (row["week_key_ms"], row["arm"], row["created_at_ms"],
             row["universe_json"], row["signals_json"], row["weights_json"],
             row["turnover_usd"], row["cost_usd"], row.get("flags_json"),
             row["inputs_sha256"]))
        self.conn.commit()
        return int(cur.lastrowid)

    def rebalance_get(self, week_key_ms: int, arm: str) -> Optional[Dict]:
        r = self.conn.execute(
            "SELECT * FROM resmom_rebalances WHERE week_key_ms=? AND arm=?",
            (week_key_ms, arm)).fetchone()
        return dict(r) if r else None

    def rebalances(self, arm: str = "A") -> List[Dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM resmom_rebalances WHERE arm=? "
            "ORDER BY week_key_ms", (arm,))]

    def position_insert(self, row: Dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO resmom_positions(rebalance_id, arm, symbol, side,"
            " weight, qty, entry_px, entry_ms, status)"
            " VALUES(?,?,?,?,?,?,?,?, 'open')",
            (row["rebalance_id"], row["arm"], row["symbol"], row["side"],
             row["weight"], row["qty"], row["entry_px"], row["entry_ms"]))
        self.conn.commit()
        return int(cur.lastrowid)

    def positions_open(self, arm: str) -> List[Dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM resmom_positions WHERE arm=? AND status='open'",
            (arm,))]

    def position_close(self, pid: int, exit_px: float, exit_ms: int,
                       pnl_usd: float, reason: str) -> None:
        self.conn.execute(
            "UPDATE resmom_positions SET exit_px=?, exit_ms=?, status='closed',"
            " pnl_usd=?, close_reason=? WHERE id=?",
            (exit_px, exit_ms, pnl_usd, reason, pid))
        self.conn.commit()

    def position_add_funding(self, pid: int, usd: float) -> None:
        self.conn.execute(
            "UPDATE resmom_positions SET funding_usd = funding_usd + ?"
            " WHERE id=?", (usd, pid))
        self.conn.commit()

    def nav_insert(self, ts_ms: int, arm: str, nav: float, gross: float,
                   n_pos: int, dd: float, legs: Optional[Dict] = None) -> None:
        self.conn.execute(
            "INSERT INTO resmom_nav(ts_ms, arm, nav_usd, gross_usd,"
            " n_positions, dd_pct, legs_json) VALUES(?,?,?,?,?,?,?)",
            (ts_ms, arm, nav, gross, n_pos, dd,
             json.dumps(legs or {}, sort_keys=True)))
        self.conn.commit()

    def nav_series(self, arm: str = "A") -> List[Dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM resmom_nav WHERE arm=? ORDER BY ts_ms", (arm,))]

    def nav_peak(self, arm: str) -> float:
        r = self.conn.execute(
            "SELECT MAX(nav_usd) m FROM resmom_nav WHERE arm=?",
            (arm,)).fetchone()
        return float(r["m"]) if r and r["m"] is not None else 0.0

    def null_insert(self, row: Dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO resmom_nulls(week_key_ms, rebalance_id, null_id,"
            " seed, weights_json, week_pnl_usd, nav_usd, cost_usd)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (row["week_key_ms"], row["rebalance_id"], row["null_id"],
             row["seed"], row["weights_json"], row.get("week_pnl_usd"),
             row["nav_usd"], row.get("cost_usd", 0.0)))
        self.conn.commit()
        return int(cur.lastrowid)

    def nulls_open_week(self, week_key_ms: int) -> List[Dict]:
        """Null rows whose week_pnl hasn't been scored yet for that week."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM resmom_nulls WHERE week_key_ms=?",
            (week_key_ms,))]

    def null_score(self, row_id: int, week_pnl: float, nav: float) -> None:
        self.conn.execute(
            "UPDATE resmom_nulls SET week_pnl_usd=?, nav_usd=? WHERE id=?",
            (week_pnl, nav, row_id))
        self.conn.commit()

    def nulls_all(self) -> List[Dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM resmom_nulls ORDER BY null_id, week_key_ms")]

    def nulls_latest_navs(self) -> Dict[int, float]:
        """null_id -> last recorded nav (for chaining weeks)."""
        out: Dict[int, float] = {}
        for r in self.conn.execute(
                "SELECT null_id, nav_usd FROM resmom_nulls"
                " ORDER BY week_key_ms"):
            out[int(r["null_id"])] = float(r["nav_usd"])
        return out

    def mark_insert(self, ts_ms: int, arm: str, symbol: str, mid: float) -> None:
        self.conn.execute(
            "INSERT INTO resmom_marks(ts_ms, arm, symbol, mid)"
            " VALUES(?,?,?,?)", (ts_ms, arm, symbol, mid))
        self.conn.commit()

    def funding_insert(self, ts_ms: int, event_ms: int, arm: str,
                       symbol: str, rate: float, notional: float,
                       pnl: float) -> None:
        self.conn.execute(
            "INSERT INTO resmom_funding(ts_ms, event_ms, arm, symbol, rate,"
            " notional_usd, pnl_usd) VALUES(?,?,?,?,?,?,?)",
            (ts_ms, event_ms, arm, symbol, rate, notional, pnl))
        self.conn.commit()

    def funding_applied_events(self, arm: str, symbol: str) -> set:
        return {int(r["event_ms"]) for r in self.conn.execute(
            "SELECT event_ms FROM resmom_funding WHERE arm=? AND symbol=?",
            (arm, symbol))}

    def log(self, kind: str, data: Dict[str, Any], ref_id: Optional[int] = None,
            ts_ms: Optional[int] = None) -> None:
        self.conn.execute(
            "INSERT INTO resmom_log(ts_ms, kind, ref_id, data_json)"
            " VALUES(?,?,?,?)",
            (ts_ms or int(time.time() * 1000), kind, ref_id,
             json.dumps(data, sort_keys=True, default=str)))
        self.conn.commit()

    def count(self, table: str, where: str = "") -> int:
        r = self.conn.execute(
            f"SELECT COUNT(*) c FROM {table} {where}").fetchone()
        return int(r["c"]) if r else 0

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
