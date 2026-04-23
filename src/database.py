"""
Database Module - SQLite para armazenar dados históricos e trades
"""
import sqlite3
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class BotDatabase:
    """Base de dados SQLite para o bot de trading"""
    
    def __init__(self, db_path: str = "data/trading_bot.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _get_conn(self):
        """Obtém conexão com a base de dados"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_db(self):
        """Inicializa tabelas"""
        with self._get_conn() as conn:
            # Candles (OHLCV)
            conn.execute("""
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
                )
            """)
            
            # Open Interest
            conn.execute("""
                CREATE TABLE IF NOT EXISTS open_interest (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    oi_usd REAL NOT NULL,
                    exchange TEXT DEFAULT 'aggregated',
                    UNIQUE(symbol, timestamp, exchange)
                )
            """)
            
            # Funding Rate
            conn.execute("""
                CREATE TABLE IF NOT EXISTS funding_rates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    funding_rate REAL NOT NULL,
                    exchange TEXT DEFAULT 'aggregated',
                    UNIQUE(symbol, timestamp, exchange)
                )
            """)
            
            # Trades (backtest e live)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    entry_time INTEGER NOT NULL,
                    exit_time INTEGER,
                    size_usd REAL NOT NULL,
                    pnl_usd REAL,
                    pnl_pct REAL,
                    exit_reason TEXT,
                    is_backtest INTEGER DEFAULT 1,
                    strategy_params TEXT
                )
            """)
            
            # Price history (para análise rápida)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    price REAL NOT NULL,
                    source TEXT DEFAULT 'hyperliquid',
                    UNIQUE(symbol, timestamp)
                )
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_candles_symbol_interval_ts 
                ON candles(symbol, interval, timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_oi_symbol_ts 
                ON open_interest(symbol, timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_funding_symbol_ts 
                ON funding_rates(symbol, timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_symbol 
                ON trades(symbol)
            """)
            
            conn.commit()
            logger.info(f"Base de dados inicializada: {self.db_path}")
    
    def save_candles(self, symbol: str, interval: str, candles: List[Dict]):
        """Guarda candles em batch"""
        with self._get_conn() as conn:
            for c in candles:
                conn.execute("""
                    INSERT OR REPLACE INTO candles 
                    (symbol, interval, timestamp, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    symbol, interval, c['timestamp'],
                    c['open'], c['high'], c['low'], c['close'], c['volume']
                ))
            conn.commit()
            logger.info(f"Guardados {len(candles)} candles para {symbol} {interval}")
    
    def get_candles(self, symbol: str, interval: str, 
                     start_time: Optional[int] = None,
                     end_time: Optional[int] = None,
                     limit: int = 1000) -> List[Dict]:
        """Busca candles da base de dados"""
        query = """
            SELECT * FROM candles 
            WHERE symbol = ? AND interval = ?
        """
        params = [symbol, interval]
        
        if start_time:
            query += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            query += " AND timestamp <= ?"
            params.append(end_time)
        
        query += " ORDER BY timestamp"
        
        if limit:
            query += f" LIMIT {limit}"
        
        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
    
    def get_candles_for_backtest(self, symbol: str, interval: str = "15m",
                                  days: int = 30) -> List[Dict]:
        """Busca candles formatados para backtest com OI e funding"""
        # Buscar candles
        candles = self.get_candles(symbol, interval, limit=10000)
        
        if not candles:
            logger.warning(f"Sem candles em DB para {symbol} {interval}")
            return []
        
        # Enriquecer com OI e funding
        with self._get_conn() as conn:
            for candle in candles:
                ts = candle['timestamp']
                
                # Buscar OI mais próximo
                oi_row = conn.execute("""
                    SELECT oi_usd FROM open_interest 
                    WHERE symbol = ? AND ABS(timestamp - ?) < 300000
                    ORDER BY ABS(timestamp - ?) LIMIT 1
                """, (symbol, ts, ts)).fetchone()
                candle['oi'] = oi_row['oi_usd'] if oi_row else 0
                
                # Buscar funding mais próximo
                fund_row = conn.execute("""
                    SELECT funding_rate FROM funding_rates 
                    WHERE symbol = ? AND ABS(timestamp - ?) < 28800000
                    ORDER BY ABS(timestamp - ?) LIMIT 1
                """, (symbol, ts, ts)).fetchone()
                candle['funding_rate'] = fund_row['funding_rate'] if fund_row else 0
        
        logger.info(f"Backtest data: {len(candles)} candles para {symbol}")
        return candles
    
    def save_oi(self, symbol: str, timestamp: int, oi_usd: float, exchange: str = 'aggregated'):
        """Guarda Open Interest"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO open_interest (symbol, timestamp, oi_usd, exchange)
                VALUES (?, ?, ?, ?)
            """, (symbol, timestamp, oi_usd, exchange))
            conn.commit()
    
    def save_funding(self, symbol: str, timestamp: int, funding_rate: float, exchange: str = 'aggregated'):
        """Guarda Funding Rate"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO funding_rates (symbol, timestamp, funding_rate, exchange)
                VALUES (?, ?, ?, ?)
            """, (symbol, timestamp, funding_rate, exchange))
            conn.commit()
    
    def save_trade(self, trade: Dict):
        """Guarda um trade"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO trades 
                (symbol, direction, entry_price, exit_price, entry_time, exit_time,
                 size_usd, pnl_usd, pnl_pct, exit_reason, is_backtest, strategy_params)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade['symbol'], trade['direction'], trade['entry_price'],
                trade.get('exit_price'), trade['entry_time'], trade.get('exit_time'),
                trade['size_usd'], trade.get('pnl_usd'), trade.get('pnl_pct'),
                trade.get('exit_reason'), trade.get('is_backtest', 1),
                json.dumps(trade.get('strategy_params', {}))
            ))
            conn.commit()
    
    def get_trades(self, symbol: Optional[str] = None, is_backtest: int = 1) -> List[Dict]:
        """Busca trades"""
        query = "SELECT * FROM trades WHERE is_backtest = ?"
        params = [is_backtest]
        
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        
        query += " ORDER BY entry_time DESC"
        
        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
    
    def get_stats(self) -> Dict:
        """Estatísticas da base de dados"""
        with self._get_conn() as conn:
            stats = {}
            
            for table in ['candles', 'open_interest', 'funding_rates', 'trades']:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                stats[table] = count
            
            # Symbols disponíveis
            symbols = conn.execute(
                "SELECT DISTINCT symbol FROM candles"
            ).fetchall()
            stats['symbols'] = [row[0] for row in symbols]
            
            # Date range
            date_range = conn.execute(
                "SELECT MIN(timestamp), MAX(timestamp) FROM candles"
            ).fetchone()
            if date_range and date_range[0]:
                stats['date_from'] = datetime.fromtimestamp(date_range[0]/1000).isoformat()
                stats['date_to'] = datetime.fromtimestamp(date_range[1]/1000).isoformat()
            
            return stats
    
    def clear_old_data(self, days: int = 90):
        """Limpa dados antigos"""
        cutoff = int((datetime.now().timestamp() - days * 86400) * 1000)
        
        with self._get_conn() as conn:
            for table in ['candles', 'open_interest', 'funding_rates', 'price_history']:
                result = conn.execute(
                    f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,)
                )
                logger.info(f"Limpos {result.rowcount} registos antigos de {table}")
            conn.commit()

if __name__ == "__main__":
    # Test
    db = BotDatabase()
    print(f"Base de dados: {db.db_path}")
    print(f"Estatísticas: {db.get_stats()}")
