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
    """Parameters required to open a new trade row.

    QW2 trade-journal enrichment:  the *entry_market_snapshot* and
    *signal_metadata* fields are JSON-encoded blobs that capture the
    full regime context at entry time, so post-mortem analysis can
    correlate trades with market conditions (ADX, OIR, funding, etc.).
    """
    symbol: str
    side: str
    entry_price: float
    entry_time: int
    size: float
    strategy: str
    status: str = "open"
    sub_strategy: Optional[str] = None
    # QW2 journal fields
    entry_adx: Optional[float] = None
    entry_oir: Optional[float] = None
    entry_funding: Optional[float] = None
    entry_predicted_funding: Optional[float] = None
    entry_bid_ask_imbalance: Optional[float] = None
    entry_volume_1m: Optional[float] = None
    entry_market_snapshot: Optional[str] = None
    signal_metadata: Optional[str] = None
    # v3.1.16 C4: persisted so PnL is correctly deducted on close-after-restart
    entry_fee: float = 0.0


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
    funding_paid: float = 0.0


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
            self._create_strategy_pnl_table()
            self._create_decision_audit_table()
            self._migrate_portfolio_table()
            self._migrate_trades_table()
            self._migrate_decision_audit_table()
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
                status      TEXT    NOT NULL DEFAULT 'open',
                entry_fee   REAL    DEFAULT 0.0
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

    def _create_strategy_pnl_table(self) -> None:
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS strategy_pnl (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy    TEXT    NOT NULL,
                symbol      TEXT    NOT NULL,
                side        TEXT    NOT NULL,
                pnl_usd     REAL    NOT NULL,
                pnl_pct     REAL    NOT NULL,
                size        REAL    NOT NULL,
                entry_time  INTEGER NOT NULL,
                exit_time   INTEGER NOT NULL,
                exit_reason TEXT,
                trade_id    INTEGER,
                is_win      INTEGER NOT NULL
            );
        """)

    def _create_decision_audit_table(self) -> None:
        """Persistent decision audit log (replaces in-memory _decision_history).

        Records every signal gate decision:  correlation, vol_circuit,
        funding_blackout, can_enter, execution.  Survives restarts, so
        post-mortem analysis of rejected signals is possible.
        """
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS decision_audit (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp          INTEGER NOT NULL,
                decision_type      TEXT    NOT NULL,
                symbol             TEXT    NOT NULL,
                side               TEXT,
                strategy           TEXT,
                signal_confidence  REAL,
                result             TEXT    NOT NULL,
                reason             TEXT,
                metadata           TEXT
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

    def _migrate_trades_table(self) -> None:
        """Add trade journal columns and QW2 enrichment fields.

        New columns (all nullable, safe migration):
          - sub_strategy       (existing; preserved)
          - entry_adx          ADX(14) at entry time
          - entry_oir          orderbook imbalance ratio at entry
          - entry_funding      funding rate at entry
          - entry_predicted_funding predicted funding at entry
          - entry_bid_ask_imbalance   15m buy/sell imbalance at entry
          - entry_volume_1m    1m volume at entry (USD)
          - entry_market_snapshot     full JSON snapshot of regime
          - signal_metadata    raw signal.metadata as JSON
        """
        cols = {
            row[1]
            for row in self._conn().execute("PRAGMA table_info(trades)").fetchall()
        }
        new_columns = [
            ("sub_strategy",            "TEXT"),
            ("entry_adx",               "REAL"),
            ("entry_oir",               "REAL"),
            ("entry_funding",           "REAL"),
            ("entry_predicted_funding", "REAL"),
            ("entry_bid_ask_imbalance", "REAL"),
            ("entry_volume_1m",         "REAL"),
            ("entry_market_snapshot",   "TEXT"),
            ("signal_metadata",         "TEXT"),
            ("entry_fee",               "REAL DEFAULT 0.0"),
            ("funding_paid",            "REAL DEFAULT 0.0"),
        ]
        for col_name, col_type in new_columns:
            if col_name not in cols:
                try:
                    self._conn().execute(
                        f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}"
                    )
                except sqlite3.OperationalError:
                    pass  # Race or already added — safe to ignore

    def _migrate_decision_audit_table(self) -> None:
        """No-op for now; placeholder for future decision_audit columns."""
        return

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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_strategy_pnl_strategy ON strategy_pnl(strategy);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_strategy_pnl_exit_time ON strategy_pnl(exit_time);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_decision_ts ON decision_audit(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_decision_strategy ON decision_audit(strategy);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_decision_result ON decision_audit(result);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_decision_type ON decision_audit(decision_type);")

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

    def count_candles(self, symbol: str, timeframe: str) -> int:
        """Return number of stored candles for a symbol/timeframe."""
        table = self._resolve_table(timeframe)
        with self._conn():
            row = self._conn().execute(
                f"SELECT COUNT(*) FROM {table} WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return int(row[0]) if row else 0

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
        """Insert a new open trade and return its auto-generated id.

        QW2: persists QW2 journal fields (entry_adx, entry_oir, etc.
        + entry_market_snapshot, signal_metadata) when provided.
        v3.1.16 C4: also persists entry_fee so close-after-restart can
        correctly deduct the entry commission from realized PnL.
        v3.1.23 dashboard: funding_paid column (default 0) tracked.
        """
        sql = """
            INSERT INTO trades (
                symbol, side, entry_price, entry_time, size,
                strategy, sub_strategy, status,
                entry_adx, entry_oir, entry_funding, entry_predicted_funding,
                entry_bid_ask_imbalance, entry_volume_1m,
                entry_market_snapshot, signal_metadata, entry_fee,
                funding_paid
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._write_lock:
            conn = self._conn()
            cur = conn.execute(sql, (
                entry.symbol, entry.side, entry.entry_price, entry.entry_time,
                entry.size, entry.strategy, entry.sub_strategy, entry.status,
                entry.entry_adx, entry.entry_oir, entry.entry_funding,
                entry.entry_predicted_funding, entry.entry_bid_ask_imbalance,
                entry.entry_volume_1m,
                entry.entry_market_snapshot, entry.signal_metadata,
                entry.entry_fee, 0.0,
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
                status = ?,
                funding_paid = COALESCE(?, funding_paid)
            WHERE id = ?
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, (
                exit_update.exit_price, exit_update.exit_time,
                exit_update.pnl_usd, exit_update.pnl_pct,
                exit_update.exit_reason, exit_update.status,
                getattr(exit_update, "funding_paid", None),
                exit_update.trade_id,
            ))
            conn.commit()

    def update_trade_status(
        self,
        trade_id: int,
        status: str,
        reason: Optional[str] = None,
    ) -> None:
        """Update a trade's status (e.g. to ``'cancelled'`` after a failed live order).

        v3.1.17 C9: lets the execution engine mark a DB row as cancelled
        so reconciliation loops don't re-open a phantom position.
        """
        if reason is not None:
            sql = "UPDATE trades SET status = ?, exit_reason = ? WHERE id = ?"
            params: tuple = (status, reason, trade_id)
        else:
            sql = "UPDATE trades SET status = ? WHERE id = ?"
            params = (status, trade_id)
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, params)
            conn.commit()

    def update_trade_funding(self, trade_id: int, funding_paid: float) -> None:
        """v3.1.23: write the running funding total for an open trade.

        Called by the engine's funding-settlement loop every hour that a
        position is open. The column is also written on close via
        ``update_trade_exit`` so the final value is preserved.
        """
        sql = "UPDATE trades SET funding_paid = ? WHERE id = ?"
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, (funding_paid, trade_id))
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
    # Strategy PnL (per-strategy breakdown)
    # ------------------------------------------------------------------

    def record_strategy_pnl(
        self,
        strategy: str,
        symbol: str,
        side: str,
        pnl_usd: float,
        pnl_pct: float,
        size: float,
        entry_time: int,
        exit_time: int,
        exit_reason: str,
        trade_id: Optional[int],
    ) -> None:
        """Insert a closed-trade row into the strategy_pnl table for drill-down."""
        sql = """
            INSERT INTO strategy_pnl (
                strategy, symbol, side, pnl_usd, pnl_pct, size,
                entry_time, exit_time, exit_reason, trade_id, is_win
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        is_win = 1 if pnl_usd > 0 else 0
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, (
                strategy, symbol, side, pnl_usd, pnl_pct, size,
                entry_time, exit_time, exit_reason, trade_id, is_win,
            ))
            conn.commit()

    def get_strategy_pnl(
        self,
        since_ms: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return per-strategy aggregate stats.

        When ``since_ms`` is provided only closed trades with
        ``exit_time >= since_ms`` are considered (default: all time).
        """
        where: List[str] = []
        params: List[Any] = []
        if since_ms is not None:
            where.append("exit_time >= ?")
            params.append(since_ms)
        if strategy is not None:
            where.append("strategy = ?")
            params.append(strategy)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT
                strategy,
                COUNT(*)              AS trades,
                SUM(is_win)           AS wins,
                SUM(pnl_usd)          AS total_pnl_usd,
                AVG(pnl_pct) * 100    AS avg_pnl_pct,
                AVG(CASE WHEN pnl_usd > 0 THEN pnl_pct ELSE 0 END) * 100 AS avg_win_pct,
                AVG(CASE WHEN pnl_usd < 0 THEN pnl_pct ELSE 0 END) * 100 AS avg_loss_pct,
                MAX(pnl_usd)          AS best_trade_usd,
                MIN(pnl_usd)          AS worst_trade_usd,
                MAX(exit_time)        AS last_exit_ms
            FROM strategy_pnl
            {where_sql}
            GROUP BY strategy
            ORDER BY total_pnl_usd DESC
        """
        with self._conn():
            cur = self._conn().execute(sql, params)
            rows = cur.fetchall()
        results: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            trades = d.get("trades") or 0
            wins = d.get("wins") or 0
            d["win_rate"] = round((wins / trades) if trades else 0.0, 4)
            results.append(d)
        return results

    # ------------------------------------------------------------------
    # Decision audit (QW1)
    # ------------------------------------------------------------------

    def save_decision(
        self,
        timestamp: int,
        decision_type: str,
        symbol: str,
        result: str,
        reason: str,
        side: Optional[str] = None,
        strategy: Optional[str] = None,
        signal_confidence: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Persist a single decision-audit row.

        Parameters
        ----------
        timestamp : int
            Unix millisecond timestamp of the decision.
        decision_type : str
            Gate name:  'risk_check', 'correlation', 'vol_circuit',
            'funding_blackout', 'execution', 'tca', 'ensemble'.
        symbol : str
            Trading symbol the decision relates to.
        result : str
            'accepted' | 'rejected' | 'executed'.
        reason : str
            Human-readable explanation.
        side : str, optional
            'long' | 'short' (when applicable).
        strategy : str, optional
            Strategy that produced the signal.
        signal_confidence : float, optional
            Confidence score of the underlying signal.
        metadata : dict, optional
            Extra structured context (e.g.  ADX, OIR, capital, correlation).
        """
        meta_json = json.dumps(metadata) if metadata else None
        sql = """
            INSERT INTO decision_audit (
                timestamp, decision_type, symbol, side, strategy,
                signal_confidence, result, reason, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._write_lock:
            conn = self._conn()
            cur = conn.execute(sql, (
                timestamp, decision_type, symbol, side, strategy,
                signal_confidence, result, reason, meta_json,
            ))
            conn.commit()
        return int(cur.lastrowid or 0)

    def get_decisions(
        self,
        limit: int = 200,
        decision_type: Optional[str] = None,
        result: Optional[str] = None,
        strategy: Optional[str] = None,
        symbol: Optional[str] = None,
        since_ms: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Query the decision audit log with optional filters.

        All filters are AND-combined.  Results sorted newest first.
        """
        conditions: List[str] = []
        params: List[Any] = []
        if decision_type:
            conditions.append("decision_type = ?")
            params.append(decision_type)
        if result:
            conditions.append("result = ?")
            params.append(result)
        if strategy:
            conditions.append("strategy = ?")
            params.append(strategy)
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if since_ms is not None:
            conditions.append("timestamp >= ?")
            params.append(since_ms)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM decision_audit {where_clause} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._conn():
            cur = self._conn().execute(sql, params)
            rows = cur.fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            # Decode metadata JSON for callers
            if d.get("metadata"):
                try:
                    d["metadata"] = json.loads(d["metadata"])
                except (TypeError, ValueError):
                    pass
            out.append(d)
        return out

    def count_decisions(
        self,
        decision_type: Optional[str] = None,
        result: Optional[str] = None,
        since_ms: Optional[int] = None,
    ) -> int:
        """Count decision-audit rows (useful for gate analytics)."""
        conditions: List[str] = []
        params: List[Any] = []
        if decision_type:
            conditions.append("decision_type = ?")
            params.append(decision_type)
        if result:
            conditions.append("result = ?")
            params.append(result)
        if since_ms is not None:
            conditions.append("timestamp >= ?")
            params.append(since_ms)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT COUNT(*) AS n FROM decision_audit {where_clause}"
        with self._conn():
            row = self._conn().execute(sql, params).fetchone()
        return int(row["n"] or 0) if row else 0

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

    def get_strategy_returns(
        self,
        since_ms: int,
        strategy: Optional[str] = None,
    ) -> Dict[str, List[float]]:
        """Return per-trade pnl_pct lists keyed by attribution strategy name."""
        conditions = ["status = 'closed'", "exit_time >= ?"]
        params: List[Any] = [since_ms]
        if strategy:
            conditions.append("(sub_strategy = ? OR (sub_strategy IS NULL AND strategy = ?))")
            params.extend([strategy, strategy])

        sql = f"""
            SELECT strategy, sub_strategy, pnl_pct
            FROM trades
            WHERE {' AND '.join(conditions)}
            ORDER BY exit_time ASC
        """
        out: Dict[str, List[float]] = {}
        with self._conn():
            cur = self._conn().execute(sql, tuple(params))
            for row in cur.fetchall():
                name = row["sub_strategy"] or row["strategy"]
                if name in ("StrategyEnsemble",):
                    continue
                out.setdefault(name, []).append(float(row["pnl_pct"] or 0.0))
        return out

    def get_metrics_by_strategy(self, since_ms: int) -> Dict[str, Dict[str, Any]]:
        """Aggregate closed-trade metrics per attribution strategy since *since_ms*."""
        import math

        returns_map = self.get_strategy_returns(since_ms)
        metrics: Dict[str, Dict[str, Any]] = {}

        def _trade_sharpe(returns_pct: List[float]) -> float:
            if len(returns_pct) < 2:
                return 0.0
            mean = sum(returns_pct) / len(returns_pct)
            var = sum((r - mean) ** 2 for r in returns_pct) / (len(returns_pct) - 1)
            stdev = math.sqrt(var) if var > 0 else 0.0
            if stdev <= 0:
                return 0.0
            return (mean / stdev) * math.sqrt(len(returns_pct))

        sql = """
            SELECT
                COALESCE(sub_strategy, strategy) AS attr_strategy,
                COUNT(*) AS total_trades,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(COALESCE(pnl_usd, 0)) AS total_pnl,
                AVG(COALESCE(pnl_pct, 0)) AS avg_pnl_pct
            FROM trades
            WHERE status = 'closed' AND exit_time >= ?
            GROUP BY attr_strategy
        """
        with self._conn():
            cur = self._conn().execute(sql, (since_ms,))
            for row in cur.fetchall():
                name = row["attr_strategy"]
                if name in ("StrategyEnsemble",):
                    continue
                total = int(row["total_trades"] or 0)
                wins = int(row["wins"] or 0)
                rets = returns_map.get(name, [])
                metrics[name] = {
                    "total_trades": total,
                    "wins": wins,
                    "losses": total - wins,
                    "win_rate": round((wins / total * 100.0) if total else 0.0, 2),
                    "total_pnl": round(float(row["total_pnl"] or 0.0), 4),
                    "avg_pnl_pct": round(float(row["avg_pnl_pct"] or 0.0), 4),
                    "sharpe_ratio": round(_trade_sharpe(rets), 4),
                }
        return metrics

    def get_daily_pnl_series(self, days: int = 30) -> List[Dict[str, Any]]:
        """Daily realised PnL from closed trades over the last *days*."""
        import time

        since_ms = int((time.time() - days * 86400) * 1000)
        sql = """
            SELECT
                date(exit_time / 1000, 'unixepoch') AS day,
                SUM(COALESCE(pnl_usd, 0)) AS pnl_usd,
                COUNT(*) AS trades
            FROM trades
            WHERE status = 'closed' AND exit_time >= ?
            GROUP BY day
            ORDER BY day ASC
        """
        with self._conn():
            cur = self._conn().execute(sql, (since_ms,))
            rows = cur.fetchall()
        return [
            {
                "day": row["day"],
                "pnl_usd": round(float(row["pnl_usd"] or 0.0), 2),
                "trades": int(row["trades"] or 0),
            }
            for row in rows
        ]

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
