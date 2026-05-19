"""SQLite database layer for the Hyperliquid trading bot.

All tables use INTEGER millisecond timestamps for consistency with WebSocket data.
Context managers ensure safe connection handling. Batch inserts are used
for performance when backfilling historical candles.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple


@dataclass(frozen=True)
class Candle:
    """OHLCV candle with optional funding and open-interest fields."""
    symbol: str
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    funding_rate: Optional[float] = None
    oi_total: Optional[float] = None
    oi_delta: Optional[float] = None


@dataclass(frozen=True)
class TradeEntry:
    """Parameters required to open a new trade row."""
    symbol: str
    side: str
    entry_price: float
    entry_time: int
    size: float
    strategy: str
    status: str = "open"


@dataclass(frozen=True)
class TradeExit:
    """Parameters required to close an existing trade."""
    trade_id: int
    exit_price: float
    exit_time: int
    pnl_usd: float
    pnl_pct: float
    exit_reason: str
    status: str = "closed"


@dataclass(frozen=True)
class SignalRecord:
    """Record of a strategy signal."""
    symbol: str
    side: str
    confidence: float
    strategy: str
    price: float
    timestamp: int
    reason: str


@dataclass(frozen=True)
class FundingRecord:
    """Funding rate snapshot."""
    symbol: str
    current: float
    predicted: float
    timestamp: int


@dataclass(frozen=True)
class OIRecord:
    """Open-interest snapshot."""
    symbol: str
    oi_total: float
    oi_delta: float
    timestamp: int


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Portfolio state at a point in time."""
    timestamp: int
    capital: float
    peak_capital: float
    daily_pnl: float
    positions_json: str


CANDLE_COLUMNS: Tuple[str, ...] = (
    "symbol", "timestamp_ms", "open", "high", "low", "close",
    "volume", "funding_rate", "oi_total", "oi_delta",
)

CANDLE_INSERT_SQL = (
    "INSERT OR REPLACE INTO {table} "
    "(symbol, timestamp_ms, open, high, low, close, volume, funding_rate, oi_total, oi_delta) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class Database:
    """Thread-safe SQLite wrapper for all trading data."""

    # Timeframe → table name mapping
    TIMEFRAME_TABLES: Dict[str, str] = {
        "1m": "candles_1m",
        "5m": "candles_5m",
        "15m": "candles_15m",
        "1h": "candles_1h",
    }

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # CRIT-005 FIX: Serialize all DB write operations to prevent
        # interleaved coroutine access from corrupting WAL transactions.
        # SQLite WAL allows one writer + many readers; the lock ensures
        # a single writer at a time from the async event loop.
        self._write_lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return a thread-local connection (auto-creates on first use)."""
        if not hasattr(self._local, "connection"):
            self._local.connection = sqlite3.connect(
                str(self.db_path),
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,  # MEDIUM-001: disabled for threading flexibility; ensure all DB access is from the same thread or use proper locking
            )
            self._local.connection.row_factory = sqlite3.Row
            # Speed-ups for batch inserts
            self._local.connection.execute("PRAGMA journal_mode=WAL;")
            self._local.connection.execute("PRAGMA synchronous=NORMAL;")
        return self._local.connection

    def _cursor(self) -> sqlite3.Cursor:
        return self._conn().cursor()

    def _commit(self) -> None:
        self._conn().commit()

    def close(self) -> None:
        """Close the thread-local connection if open."""
        if hasattr(self._local, "connection"):
            self._local.connection.close()
            del self._local.connection

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._conn():
            self._create_candle_tables()
            self._create_trades_table()
            self._create_signals_table()
            self._create_funding_table()
            self._create_oi_table()
            self._create_portfolio_table()
            self._migrate_portfolio_table()
            self._create_indexes()

    def _create_candle_tables(self) -> None:
        for table in self.TIMEFRAME_TABLES.values():
            sql = f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    symbol        TEXT    NOT NULL,
                    timestamp_ms  INTEGER NOT NULL,
                    open          REAL    NOT NULL,
                    high          REAL    NOT NULL,
                    low           REAL    NOT NULL,
                    close         REAL    NOT NULL,
                    volume        REAL    NOT NULL,
                    funding_rate  REAL,
                    oi_total      REAL,
                    oi_delta      REAL,
                    PRIMARY KEY (symbol, timestamp_ms)
                ) WITHOUT ROWID;
            """
            self._conn().execute(sql)

    def _create_trades_table(self) -> None:
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS trades (
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

    def _create_signals_table(self) -> None:
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol    TEXT    NOT NULL,
                side      TEXT    NOT NULL,
                confidence REAL   NOT NULL,
                strategy  TEXT    NOT NULL,
                price     REAL    NOT NULL,
                timestamp INTEGER NOT NULL,
                reason    TEXT
            );
        """)

    def _create_funding_table(self) -> None:
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS funding_history (
                symbol    TEXT    NOT NULL,
                current   REAL    NOT NULL,
                predicted REAL    NOT NULL,
                timestamp INTEGER NOT NULL,
                PRIMARY KEY (symbol, timestamp)
            ) WITHOUT ROWID;
        """)

    def _create_oi_table(self) -> None:
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS oi_history (
                symbol    TEXT    NOT NULL,
                oi_total  REAL    NOT NULL,
                oi_delta  REAL    NOT NULL,
                timestamp INTEGER NOT NULL,
                PRIMARY KEY (symbol, timestamp)
            ) WITHOUT ROWID;
        """)

    def _create_portfolio_table(self) -> None:
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                timestamp      INTEGER PRIMARY KEY,
                capital        REAL    NOT NULL,
                peak_capital   REAL    NOT NULL,
                daily_pnl      REAL    NOT NULL,
                positions_json TEXT    NOT NULL
            );
        """)

    def _migrate_portfolio_table(self) -> None:
        """Add peak_capital column to existing portfolio_snapshots table."""
        try:
            self._conn().execute(
                "ALTER TABLE portfolio_snapshots ADD COLUMN peak_capital REAL NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            # Column already exists — ignore
            pass

    def _create_indexes(self) -> None:
        cur = self._cursor()
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_funding_symbol_ts ON funding_history(symbol, timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_oi_symbol_ts ON oi_history(symbol, timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_ts ON portfolio_snapshots(timestamp);")

    # ------------------------------------------------------------------
    # Candles
    # ------------------------------------------------------------------

    def save_candle(self, candle: Candle, timeframe: str) -> None:
        """Persist a single candle to the correct timeframe table."""
        table = self._resolve_table(timeframe)
        sql = CANDLE_INSERT_SQL.format(table=table)
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, self._candle_tuple(candle))
            conn.commit()

    def save_candles(self, candles: List[Candle], timeframe: str) -> None:
        """Batch insert candles for performance during backfills."""
        if not candles:
            return
        table = self._resolve_table(timeframe)
        sql = CANDLE_INSERT_SQL.format(table=table)
        rows = [self._candle_tuple(c) for c in candles]
        with self._write_lock:
            conn = self._conn()
            conn.executemany(sql, rows)
            conn.commit()

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> List[Candle]:
        """Retrieve candles ordered by time ascending."""
        table = self._resolve_table(timeframe)
        conditions: List[str] = ["symbol = ?"]
        params: List[Any] = [symbol]

        if start_ms is not None:
            conditions.append("timestamp_ms >= ?")
            params.append(start_ms)
        if end_ms is not None:
            conditions.append("timestamp_ms <= ?")
            params.append(end_ms)

        where_clause = " AND ".join(conditions)
        sql = (
            f"SELECT * FROM {table} "
            f"WHERE {where_clause} "
            f"ORDER BY timestamp_ms ASC LIMIT ?"
        )
        params.append(limit)

        with self._conn():
            cur = self._conn().execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_candle(row) for row in rows]

    def get_candles_df(self, symbol: str, timeframe: str, limit: int = 500):
        """Return candles as a pandas DataFrame (lazy import)."""
        import pandas as pd
        candles = self.get_candles(symbol, timeframe, limit)
        if not candles:
            return pd.DataFrame(columns=[
                "symbol", "timestamp_ms", "open", "high", "low",
                "close", "volume", "funding_rate", "oi_total", "oi_delta",
            ])
        data = [asdict(c) for c in candles]
        df = pd.DataFrame(data)
        df["timestamp_ms"] = pd.to_datetime(df["timestamp_ms"], unit="ms")
        return df

    @staticmethod
    def _candle_tuple(c: Candle) -> Tuple:
        return (
            c.symbol, c.timestamp_ms, c.open, c.high, c.low, c.close,
            c.volume, c.funding_rate, c.oi_total, c.oi_delta,
        )

    @staticmethod
    def _row_to_candle(row: sqlite3.Row) -> Candle:
        return Candle(
            symbol=row["symbol"],
            timestamp_ms=row["timestamp_ms"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            funding_rate=row["funding_rate"],
            oi_total=row["oi_total"],
            oi_delta=row["oi_delta"],
        )

    def _resolve_table(self, timeframe: str) -> str:
        if timeframe not in self.TIMEFRAME_TABLES:
            raise ValueError(f"Unknown timeframe '{timeframe}'. Supported: {list(self.TIMEFRAME_TABLES.keys())}")
        return self.TIMEFRAME_TABLES[timeframe]

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def save_trade_entry(self, entry: TradeEntry) -> int:
        """Insert a new open trade and return its auto-generated id."""
        sql = """
            INSERT INTO trades (symbol, side, entry_price, entry_time, size, strategy, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        with self._write_lock:
            conn = self._conn()
            cur = conn.execute(sql, (
                entry.symbol, entry.side, entry.entry_price, entry.entry_time,
                entry.size, entry.strategy, entry.status,
            ))
            trade_id = cur.lastrowid
            conn.commit()
        if trade_id is None:
            raise RuntimeError("Failed to retrieve lastrowid after trade insert")
        return trade_id

    def update_trade_exit(self, exit_update: TradeExit) -> None:
        """Close an existing trade with exit details."""
        sql = """
            UPDATE trades
            SET exit_price = ?,
                exit_time = ?,
                pnl_usd = ?,
                pnl_pct = ?,
                exit_reason = ?,
                status = ?
            WHERE id = ?
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, (
                exit_update.exit_price, exit_update.exit_time,
                exit_update.pnl_usd, exit_update.pnl_pct,
                exit_update.exit_reason, exit_update.status,
                exit_update.trade_id,
            ))
            conn.commit()

    def get_open_trades(self, strategy: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return all currently open trades as dicts."""
        if strategy:
            sql = "SELECT * FROM trades WHERE status = 'open' AND strategy = ? ORDER BY entry_time ASC"
            params = (strategy,)
        else:
            sql = "SELECT * FROM trades WHERE status = 'open' ORDER BY entry_time ASC"
            params = ()
        with self._conn():
            cur = self._conn().execute(sql, params)
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def get_trades(self, limit: int = 500, strategy: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return most recent trades (open + closed) as dicts."""
        if strategy:
            sql = "SELECT * FROM trades WHERE strategy = ? ORDER BY entry_time DESC LIMIT ?"
            params = (strategy, limit)
        else:
            sql = "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?"
            params = (limit,)
        with self._conn():
            cur = self._conn().execute(sql, params)
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def save_signal(self, signal: SignalRecord) -> None:
        sql = """
            INSERT INTO signals (symbol, side, confidence, strategy, price, timestamp, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, (
                signal.symbol, signal.side, signal.confidence, signal.strategy,
                signal.price, signal.timestamp, signal.reason,
            ))
            conn.commit()

    def get_signals(
        self,
        limit: int = 500,
        strategy: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conditions: List[str] = []
        params: List[Any] = []
        if strategy:
            conditions.append("strategy = ?")
            params.append(strategy)
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM signals {where_clause} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._conn():
            cur = self._conn().execute(sql, params)
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Funding
    # ------------------------------------------------------------------

    def save_funding(self, record: FundingRecord) -> None:
        sql = """
            INSERT OR REPLACE INTO funding_history (symbol, current, predicted, timestamp)
            VALUES (?, ?, ?, ?)
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, (record.symbol, record.current, record.predicted, record.timestamp))
            conn.commit()

    def get_funding_history(self, symbol: str, limit: int = 500) -> List[Dict[str, Any]]:
        sql = """
            SELECT * FROM funding_history
            WHERE symbol = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """
        with self._conn():
            cur = self._conn().execute(sql, (symbol, limit))
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Open Interest
    # ------------------------------------------------------------------

    def save_oi(self, record: OIRecord) -> None:
        sql = """
            INSERT OR REPLACE INTO oi_history (symbol, oi_total, oi_delta, timestamp)
            VALUES (?, ?, ?, ?)
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, (record.symbol, record.oi_total, record.oi_delta, record.timestamp))
            conn.commit()

    def get_oi_history(self, symbol: str, limit: int = 500) -> List[Dict[str, Any]]:
        sql = """
            SELECT * FROM oi_history
            WHERE symbol = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """
        with self._conn():
            cur = self._conn().execute(sql, (symbol, limit))
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Portfolio snapshots
    # ------------------------------------------------------------------

    def save_portfolio_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        sql = """
            INSERT OR REPLACE INTO portfolio_snapshots (timestamp, capital, peak_capital, daily_pnl, positions_json)
            VALUES (?, ?, ?, ?, ?)
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, (
                snapshot.timestamp, snapshot.capital, snapshot.peak_capital, snapshot.daily_pnl, snapshot.positions_json,
            ))
            conn.commit()

    def get_portfolio_history(self, limit: int = 500) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT ?"
        with self._conn():
            cur = self._conn().execute(sql, (limit,))
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def get_latest_portfolio_snapshot(self) -> Optional[Dict[str, Any]]:
        """Return the most recent portfolio snapshot, or None if none exists."""
        history = self.get_portfolio_history(limit=1)
        return history[0] if history else None

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune_old_data(self, days: int = 30) -> Dict[str, int]:
        """Delete data older than *days* to keep the DB lean. Returns row counts deleted per table."""
        import time
        cutoff_ms = int((time.time() - days * 86400) * 1000)
        deleted: Dict[str, int] = {}

        with self._conn():
            # Candles
            for table in self.TIMEFRAME_TABLES.values():
                cur = self._conn().execute(f"DELETE FROM {table} WHERE timestamp_ms < ?", (cutoff_ms,))
                deleted[table] = cur.rowcount

            # Funding, OI, signals, portfolio, trades
            for table in ("funding_history", "oi_history", "signals", "portfolio_snapshots"):
                cur = self._conn().execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff_ms,))
                deleted[table] = cur.rowcount

            # Trades: only delete closed trades older than cutoff
            cur = self._conn().execute(
                "DELETE FROM trades WHERE status = 'closed' AND exit_time < ?",
                (cutoff_ms,),
            )
            deleted["trades_closed"] = cur.rowcount

            # Vacuum to reclaim space
            self._conn().execute("VACUUM;")

        return deleted
