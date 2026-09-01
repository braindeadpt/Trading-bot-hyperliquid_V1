"""Research-DB persistence for TopTrader bias samples and virtual trades.

Best-effort writes — never raise into the live engine path.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from src.data.research_database import ResearchDatabase
from src.exchanges.top_trader_tracker import TopTraderSymbolSnapshot

logger = logging.getLogger(__name__)

EXIT_BIAS_FLIP = "bias_flip"
EXIT_TIMEOUT = "timeout"
EXIT_SL = "stop_loss"
EXIT_TP = "take_profit"


class TopTraderStore:
    """Append-only bias samples + virtual trade ledger in the research DB."""

    def __init__(self, db: Optional[ResearchDatabase] = None) -> None:
        self._db = db or ResearchDatabase.open()
        self._lock = threading.Lock()
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self._lock:
            try:
                with self._db._write_lock:
                    conn = self._db._conn()
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS top_trader_bias_samples (
                            id              INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp_ms    INTEGER NOT NULL,
                            symbol          TEXT    NOT NULL,
                            n_long          INTEGER NOT NULL,
                            n_short         INTEGER NOT NULL,
                            long_notional   REAL    NOT NULL,
                            short_notional  REAL    NOT NULL,
                            net_bias        REAL    NOT NULL,
                            long_frac       REAL    NOT NULL,
                            ingested_at_ms  INTEGER NOT NULL
                        );
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_tt_bias_sym_ts "
                        "ON top_trader_bias_samples(symbol, timestamp_ms);"
                    )
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS top_trader_virtual_trades (
                            id              INTEGER PRIMARY KEY AUTOINCREMENT,
                            symbol          TEXT    NOT NULL,
                            side            TEXT    NOT NULL,
                            entry_price     REAL    NOT NULL,
                            entry_ts_ms     INTEGER NOT NULL,
                            exit_price      REAL,
                            exit_ts_ms      INTEGER,
                            exit_reason     TEXT,
                            stop_loss_pct   REAL    NOT NULL,
                            take_profit_pct REAL    NOT NULL,
                            size_pct        REAL    NOT NULL,
                            entry_bias      REAL    NOT NULL,
                            exit_bias       REAL,
                            pnl_pct         REAL,
                            status          TEXT    NOT NULL,
                            ingested_at_ms  INTEGER NOT NULL
                        );
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_tt_virt_status "
                        "ON top_trader_virtual_trades(status, entry_ts_ms);"
                    )
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS top_trader_collapse_events (
                            id              INTEGER PRIMARY KEY AUTOINCREMENT,
                            ts_ms           INTEGER NOT NULL,
                            wallet          TEXT    NOT NULL,
                            symbol          TEXT    NOT NULL,
                            side            TEXT,
                            from_notional   REAL    NOT NULL,
                            to_notional     REAL    NOT NULL,
                            drop_pct        REAL    NOT NULL,
                            ingested_at_ms  INTEGER NOT NULL
                        );
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_tt_collapse_sym_ts "
                        "ON top_trader_collapse_events(symbol, ts_ms);"
                    )
                    conn.commit()
            except sqlite3.Error as exc:
                logger.warning("TopTraderStore schema init failed: %s", exc)

    def persist_bias_samples(
        self,
        snapshots: Sequence[TopTraderSymbolSnapshot] | Dict[str, TopTraderSymbolSnapshot],
    ) -> int:
        """Insert one row per snapshot. Returns rows written (0 on failure)."""
        if isinstance(snapshots, dict):
            rows = list(snapshots.values())
        else:
            rows = list(snapshots)
        if not rows:
            return 0
        ingested = int(time.time() * 1000)
        sql = """
            INSERT INTO top_trader_bias_samples
            (timestamp_ms, symbol, n_long, n_short, long_notional, short_notional,
             net_bias, long_frac, ingested_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = [
            (
                int(s.updated_ms),
                str(s.symbol).upper(),
                int(s.n_long),
                int(s.n_short),
                float(s.long_notional_usd),
                float(s.short_notional_usd),
                float(s.net_bias),
                float(s.long_frac),
                ingested,
            )
            for s in rows
        ]
        with self._lock:
            try:
                with self._db._write_lock:
                    conn = self._db._conn()
                    conn.executemany(sql, params)
                    conn.commit()
                return len(params)
            except sqlite3.Error as exc:
                logger.debug("top_trader_bias_samples persist failed: %s", exc)
                return 0

    def load_bias_samples(
        self,
        symbol: str,
        *,
        start_ms: int,
        end_ms: int,
    ) -> List[Dict[str, Any]]:
        """Load bias samples for *symbol* in ``[start_ms, end_ms]`` ascending."""
        sql = """
            SELECT timestamp_ms, symbol, n_long, n_short, long_notional,
                   short_notional, net_bias, long_frac
            FROM top_trader_bias_samples
            WHERE symbol = ? AND timestamp_ms >= ? AND timestamp_ms <= ?
            ORDER BY timestamp_ms ASC
        """
        with self._lock:
            try:
                conn = self._db._conn()
                cur = conn.execute(sql, (symbol.upper(), int(start_ms), int(end_ms)))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except sqlite3.Error as exc:
                logger.debug("load_bias_samples failed: %s", exc)
                return []

    def persist_collapse_event(
        self,
        *,
        ts_ms: int,
        wallet: str,
        symbol: str,
        side: Optional[str],
        from_notional: float,
        to_notional: float,
        drop_pct: float,
    ) -> bool:
        """Record a suspected forced-liquidation (position collapse) event."""
        sql = """
            INSERT INTO top_trader_collapse_events
            (ts_ms, wallet, symbol, side, from_notional, to_notional,
             drop_pct, ingested_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        ingested = int(time.time() * 1000)
        with self._lock:
            try:
                with self._db._write_lock:
                    conn = self._db._conn()
                    conn.execute(
                        sql,
                        (
                            int(ts_ms),
                            str(wallet).lower(),
                            str(symbol).upper(),
                            str(side) if side else None,
                            float(from_notional),
                            float(to_notional),
                            float(drop_pct),
                            ingested,
                        ),
                    )
                    conn.commit()
                    return True
            except sqlite3.Error as exc:
                logger.debug("persist_collapse_event failed: %s", exc)
                return False

    def collapse_events(
        self,
        *,
        symbol: Optional[str] = None,
        since_ms: Optional[int] = None,
        limit: int = 10_000,
    ) -> List[Dict[str, Any]]:
        """Recent collapse events (ascending), optionally filtered."""
        clauses: List[str] = []
        params: List[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(str(symbol).upper())
        if since_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(int(since_ms))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT ts_ms, wallet, symbol, side, from_notional, to_notional, drop_pct
            FROM top_trader_collapse_events
            {where}
            ORDER BY ts_ms ASC
            LIMIT ?
        """
        params.append(int(limit))
        with self._lock:
            try:
                conn = self._db._conn()
                cur = conn.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except sqlite3.Error as exc:
                logger.debug("collapse_events failed: %s", exc)
                return []

    def insert_virtual_trade_open(self, trade: Dict[str, Any]) -> Optional[int]:
        sql = """
            INSERT INTO top_trader_virtual_trades
            (symbol, side, entry_price, entry_ts_ms, exit_price, exit_ts_ms,
             exit_reason, stop_loss_pct, take_profit_pct, size_pct,
             entry_bias, exit_bias, pnl_pct, status, ingested_at_ms)
            VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, NULL, NULL, 'open', ?)
        """
        ingested = int(time.time() * 1000)
        with self._lock:
            try:
                with self._db._write_lock:
                    conn = self._db._conn()
                    cur = conn.execute(
                        sql,
                        (
                            str(trade["symbol"]).upper(),
                            str(trade["side"]),
                            float(trade["entry_price"]),
                            int(trade["entry_ts_ms"]),
                            float(trade["stop_loss_pct"]),
                            float(trade["take_profit_pct"]),
                            float(trade["size_pct"]),
                            float(trade["entry_bias"]),
                            ingested,
                        ),
                    )
                    conn.commit()
                    return int(cur.lastrowid)
            except sqlite3.Error as exc:
                logger.debug("insert_virtual_trade_open failed: %s", exc)
                return None

    def close_virtual_trade(
        self,
        row_id: int,
        *,
        exit_price: float,
        exit_ts_ms: int,
        exit_reason: str,
        exit_bias: Optional[float],
        pnl_pct: float,
    ) -> bool:
        sql = """
            UPDATE top_trader_virtual_trades
            SET exit_price=?, exit_ts_ms=?, exit_reason=?, exit_bias=?,
                pnl_pct=?, status='closed'
            WHERE id=? AND status='open'
        """
        with self._lock:
            try:
                with self._db._write_lock:
                    conn = self._db._conn()
                    cur = conn.execute(
                        sql,
                        (
                            float(exit_price),
                            int(exit_ts_ms),
                            str(exit_reason),
                            float(exit_bias) if exit_bias is not None else None,
                            float(pnl_pct),
                            int(row_id),
                        ),
                    )
                    conn.commit()
                    return cur.rowcount > 0
            except sqlite3.Error as exc:
                logger.debug("close_virtual_trade failed: %s", exc)
                return False

    def list_closed_trades(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        sql = """
            SELECT id, symbol, side, entry_price, entry_ts_ms, exit_price,
                   exit_ts_ms, exit_reason, stop_loss_pct, take_profit_pct,
                   size_pct, entry_bias, exit_bias, pnl_pct, status
            FROM top_trader_virtual_trades
            WHERE status='closed'
            ORDER BY exit_ts_ms DESC
            LIMIT ?
        """
        with self._lock:
            try:
                conn = self._db._conn()
                cur = conn.execute(sql, (int(limit),))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except sqlite3.Error as exc:
                logger.debug("list_closed_trades failed: %s", exc)
                return []

    def latest_bias_by_symbol(self) -> Dict[str, Dict[str, Any]]:
        """Most recent sample per symbol (for cold dashboard without live tracker)."""
        sql = """
            SELECT t.timestamp_ms, t.symbol, t.n_long, t.n_short, t.long_notional,
                   t.short_notional, t.net_bias, t.long_frac
            FROM top_trader_bias_samples t
            INNER JOIN (
                SELECT symbol, MAX(timestamp_ms) AS mx
                FROM top_trader_bias_samples
                GROUP BY symbol
            ) m ON t.symbol = m.symbol AND t.timestamp_ms = m.mx
        """
        with self._lock:
            try:
                conn = self._db._conn()
                cur = conn.execute(sql)
                cols = [d[0] for d in cur.description]
                out: Dict[str, Dict[str, Any]] = {}
                for row in cur.fetchall():
                    d = dict(zip(cols, row))
                    out[str(d["symbol"]).upper()] = d
                return out
            except sqlite3.Error as exc:
                logger.debug("latest_bias_by_symbol failed: %s", exc)
                return {}
