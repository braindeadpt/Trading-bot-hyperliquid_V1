"""Database connection — gerencia conexão SQLite persistente."""
import sqlite3
import threading
from pathlib import Path


class SQLiteConnection:
    """
    Conexão SQLite persistente por thread.
    Resolve problema de N conexões por query do legado.
    """
    
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS candles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        interval TEXT NOT NULL,
        timestamp INTEGER NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume REAL NOT NULL,
        UNIQUE(symbol, interval, timestamp)
    );
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL,
        entry_price REAL NOT NULL,
        exit_price REAL,
        entry_time INTEGER NOT NULL,
        exit_time INTEGER,
        size_usd REAL NOT NULL,
        leverage REAL DEFAULT 1,
        pnl_usd REAL,
        pnl_pct REAL,
        exit_reason TEXT,
        is_backtest INTEGER DEFAULT 0,
        strategy_params TEXT
    );
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER NOT NULL,
        asset TEXT NOT NULL,
        signal_type TEXT NOT NULL,
        confidence REAL DEFAULT 1.0,
        executed BOOLEAN DEFAULT FALSE,
        execution_time INTEGER,
        entry_price REAL,
        stop_loss REAL,
        take_profit REAL,
        reason TEXT,
        market_regime TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_candles_sym_int_ts ON candles(symbol, interval, timestamp);
    CREATE INDEX IF NOT EXISTS idx_trades_sym_entry ON trades(symbol, entry_time);
    CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp, asset);
    """
    
    def __init__(self, db_path: str = "data/trading_bot_clean.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()
    
    def connect(self) -> sqlite3.Connection:
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn
    
    def _init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()
    
    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
