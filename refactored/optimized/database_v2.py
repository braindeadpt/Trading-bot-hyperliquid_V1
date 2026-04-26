"""
BotDatabase v2 — Otimizado: conexão persistente + batch queries + prepared statements.
Resolve problema de 4 conexões por get_stats() e N queries separadas.
"""
import sqlite3
import json
import logging
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
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

CREATE TABLE IF NOT EXISTS open_interest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    oi_usd REAL NOT NULL,
    UNIQUE(symbol, timestamp)
);

CREATE TABLE IF NOT EXISTS funding_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    funding_rate REAL NOT NULL,
    UNIQUE(symbol, timestamp)
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

CREATE TABLE IF NOT EXISTS performance_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    asset TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    win_rate REAL,
    profit_factor REAL,
    total_pnl REAL,
    max_drawdown REAL,
    UNIQUE(date, asset)
);

CREATE INDEX IF NOT EXISTS idx_candles_sym_int_ts ON candles(symbol, interval, timestamp);
CREATE INDEX IF NOT EXISTS idx_oi_sym_ts ON open_interest(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_funding_sym_ts ON funding_rates(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_sym_entry ON trades(symbol, entry_time);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp, asset);
"""


class BotDatabase:
    """Base de dados SQLite v2 — CONEXÃO PERSISTENTE por thread."""
    
    def __init__(self, db_path: str = "data/trading_bot.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # ✅ Thread-local connections (SQLite não é thread-safe com 1 conn)
        self._local = threading.local()
        self._init_db()
    
    def _connect(self) -> sqlite3.Connection:
        """Conexão persistente por thread."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            if str(self.db_path) != ':memory:':
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
            conn.execute("PRAGMA temp_store=memory")
            self._local.conn = conn
        return self._local.conn
    
    def _init_db(self):
        """Inicializa schema — com conexão efémera (boot only)."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        logger.info(f"[Database] Inicializada: {self.db_path}")
    
    def close(self):
        """Fecha conexão da thread atual."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
    
    # ─── Candles ─────────────────────────────────────────────
    
    def save_candles(self, symbol: str, interval: str, candles: List[Dict]):
        if not candles:
            return
        conn = self._connect()
        conn.executemany(
            """INSERT OR REPLACE INTO candles 
               (symbol, interval, timestamp, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(symbol, interval, c['timestamp'], c['open'], c['high'], 
              c['low'], c['close'], c['volume']) for c in candles]
        )
        conn.commit()
    
    def get_candles(self, symbol: str, interval: str, limit: int = 500) -> List[Dict]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM candles WHERE symbol = ? AND interval = ? ORDER BY timestamp DESC LIMIT ?",
            (symbol, interval, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    
    # ─── Trades ──────────────────────────────────────────────
    
    def save_trade(self, trade: Dict):
        conn = self._connect()
        conn.execute("""
            INSERT INTO trades 
            (symbol, direction, entry_price, exit_price, entry_time, exit_time,
             size_usd, leverage, pnl_usd, pnl_pct, exit_reason, strategy_params)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade['symbol'], trade['direction'], trade['entry_price'],
            trade.get('exit_price'), trade['entry_time'], trade.get('exit_time'),
            trade['size_usd'], trade.get('leverage', 1),
            trade.get('pnl_usd'), trade.get('pnl_pct'),
            trade.get('exit_reason'), json.dumps(trade.get('strategy_params', {}))
        ))
        conn.commit()
    
    def get_open_trade(self) -> Optional[Dict]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM trades WHERE exit_time IS NULL ORDER BY entry_time DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    
    def get_trades(self, symbol: str = None, limit: int = 100) -> List[Dict]:
        conn = self._connect()
        query = "SELECT * FROM trades WHERE 1=1"
        params = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY entry_time DESC LIMIT ?"
        params.append(limit)
        
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    
    def update_trade_exit(self, trade_id: int, exit_price: float, exit_time: int,
                          pnl_usd: float, pnl_pct: float, reason: str):
        conn = self._connect()
        conn.execute(
            "UPDATE trades SET exit_price = ?, exit_time = ?, pnl_usd = ?, pnl_pct = ?, exit_reason = ? WHERE id = ?",
            (exit_price, exit_time, pnl_usd, pnl_pct, reason, trade_id)
        )
        conn.commit()
    
    # ─── Signals ─────────────────────────────────────────────
    
    def save_signal(self, signal: Dict):
        conn = self._connect()
        conn.execute("""
            INSERT INTO signals 
            (timestamp, asset, signal_type, confidence, executed, execution_time,
             entry_price, stop_loss, take_profit, reason, market_regime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal.get('timestamp', int(time.time() * 1000)),
            signal['asset'], signal['signal_type'],
            signal.get('confidence', 1.0),
            signal.get('executed', False),
            signal.get('execution_time'),
            signal.get('entry_price'), signal.get('stop_loss'),
            signal.get('take_profit'), signal.get('reason', ''),
            signal.get('market_regime', '')
        ))
        conn.commit()
    
    def get_signals(self, asset: str = None, limit: int = 100) -> List[Dict]:
        conn = self._connect()
        query = "SELECT * FROM signals WHERE 1=1"
        params = []
        if asset:
            query += " AND asset = ?"
            params.append(asset)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    
    # ─── Performance ─────────────────────────────────────────
    
    def save_daily_performance(self, perf: Dict):
        conn = self._connect()
        conn.execute("""
            INSERT OR REPLACE INTO performance_daily
            (date, asset, total_trades, winning_trades, losing_trades,
             win_rate, profit_factor, total_pnl, max_drawdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            perf['date'], perf['asset'], perf.get('total_trades', 0),
            perf.get('winning_trades', 0), perf.get('losing_trades', 0),
            perf.get('win_rate'), perf.get('profit_factor'),
            perf.get('total_pnl'), perf.get('max_drawdown')
        ))
        conn.commit()
    
    # ─── Stats ───────────────────────────────────────────────
    
    def get_stats(self) -> Dict[str, Any]:
        """Estatísticas em UMA query (batch)."""
        conn = self._connect()
        cursor = conn.execute("""
            SELECT 'candles' as name, COUNT(*) as cnt FROM candles
            UNION ALL SELECT 'trades', COUNT(*) FROM trades
            UNION ALL SELECT 'signals', COUNT(*) FROM signals
            UNION ALL SELECT 'open_trades', COUNT(*) FROM trades WHERE exit_time IS NULL
            UNION ALL SELECT 'performance_days', COUNT(DISTINCT date) FROM performance_daily
        """)
        return {row['name']: row['cnt'] for row in cursor.fetchall()}
    
    def get_daily_stats(self, days: int = 7) -> List[Dict]:
        """Performance dos últimos N dias."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM performance_daily WHERE date >= date('now', ?) ORDER BY date DESC",
            (f'-{days} days',)
        ).fetchall()
        return [dict(r) for r in rows]
