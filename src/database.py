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
        """Obtém conexão com a base de dados - com WAL mode para melhor performance"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        # ⚡ WAL mode - permite leitura durante escritas, evita "database is locked"
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")  # Esperar até 5s se DB estiver locked
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

            # =====================================================
            # NOVAS TABELAS v2.0 - Arquitetura Completa
            # =====================================================

            # Sinais gerados pela estratégia (inclui os que NÃO entraram)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    asset TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    confidence REAL DEFAULT 1.0,
                    strategy TEXT NOT NULL,
                    entry_price REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    executed BOOLEAN DEFAULT FALSE,
                    execution_time INTEGER,
                    reason TEXT,
                    market_regime TEXT,
                    volume_ratio REAL,
                    oi_change REAL,
                    funding_rate REAL,
                    price_above_sma BOOLEAN,
                    bullish_count INTEGER,
                    bearish_count INTEGER
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_signals_time
                ON signals(timestamp, asset)
            """)

            # Regime de mercado (volatilidade, tendência)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_regime (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    asset TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    volatility_24h REAL,
                    trend_strength REAL,
                    volume_profile TEXT,
                    sma_200 REAL,
                    price_vs_sma_pct REAL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_regime_time
                ON market_regime(timestamp, asset)
            """)

            # Performance diária (rollup automático)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS performance_log (
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
                    sharpe_ratio REAL,
                    avg_trade_duration REAL,
                    long_trades INTEGER DEFAULT 0,
                    short_trades INTEGER DEFAULT 0,
                    long_pnl REAL,
                    short_pnl REAL,
                    UNIQUE(date, asset)
                )
            """)

            # LLM Analysis (preparado para futuro)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_analysis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    score INTEGER,
                    summary TEXT,
                    raw_data TEXT,
                    applied_to_strategy BOOLEAN DEFAULT FALSE
                )
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
            # Validar que limit é inteiro para prevenir SQL injection
            limit = int(limit)
            if limit > 0:
                query += " LIMIT ?"
                params.append(limit)

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

        # ⚡ OTIMIZAÇÃO: Buscar OI e funding de UMA VEZ para todo o intervalo
        # Em vez de N+2 queries, fazemos 2 queries só
        with self._get_conn() as conn:
            # Timestamp range dos candles
            min_ts = candles[0]['timestamp']
            max_ts = candles[-1]['timestamp']

            # Buscar TODO o OI para este intervalo (±1 hora de margem)
            oi_rows = conn.execute("""
                SELECT timestamp, oi_usd FROM open_interest
                WHERE symbol = ? AND timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp
            """, (symbol, min_ts - 3600000, max_ts + 3600000)).fetchall()

            # Criar lookup dict: timestamp -> oi_usd
            oi_lookup = {}
            for row in oi_rows:
                oi_lookup[row['timestamp']] = row['oi_usd']

            # Buscar TODO o funding para este intervalo (±8 horas de margem)
            fund_rows = conn.execute("""
                SELECT timestamp, funding_rate FROM funding_rates
                WHERE symbol = ? AND timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp
            """, (symbol, min_ts - 28800000, max_ts + 28800000)).fetchall()

            # Criar lookup dict: timestamp -> funding_rate
            fund_lookup = {}
            for row in fund_rows:
                fund_lookup[row['timestamp']] = row['funding_rate']

            # Enriquecer candles usando lookups (O(1) em vez de O(N) queries)
            for candle in candles:
                ts = candle['timestamp']

                # OI: encontrar o mais próximo no lookup
                candle['oi'] = self._find_nearest(ts, oi_lookup) or 0
                candle['funding_rate'] = self._find_nearest(ts, fund_lookup) or 0

        logger.info(f"Backtest data: {len(candles)} candles para {symbol} (2 queries, N+2 eliminado)")
        return candles

    def _find_nearest(self, target_ts: int, lookup: Dict[int, float]) -> Optional[float]:
        """Encontra valor mais próximo no lookup dict"""
        if not lookup:
            return None

        nearest_ts = min(lookup.keys(), key=lambda ts: abs(ts - target_ts))
        # Só usar se estiver dentro de 1 hora
        if abs(nearest_ts - target_ts) < 3600000:
            return lookup[nearest_ts]
        return None

    def save_oi(self, symbol: str, timestamp: int, oi_usd: float, exchange: str = 'aggregated'):
        """Guarda Open Interest"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO open_interest (symbol, timestamp, oi_usd, exchange)
                VALUES (?, ?, ?, ?)
            """, (symbol, timestamp, oi_usd, exchange))
            conn.commit()

    # Alias para compatibilidade
    save_open_interest = save_oi

    def save_funding(self, symbol: str, timestamp: int, funding_rate: float, exchange: str = 'aggregated'):
        """Guarda Funding Rate"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO funding_rates (symbol, timestamp, funding_rate, exchange)
                VALUES (?, ?, ?, ?)
            """, (symbol, timestamp, funding_rate, exchange))
            conn.commit()

    # Alias para compatibilidade
    save_funding_rate = save_funding

    def save_price(self, symbol: str, price: float, source: str = 'hyperliquid'):
        """Guarda preço na tabela price_history"""
        import time
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO price_history (symbol, timestamp, price, source)
                VALUES (?, ?, ?, ?)
            """, (symbol, int(time.time()), price, source))
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

    def get_open_trade(self) -> Optional[Dict]:
        """
        ⚡ CRASH RECOVERY - Busca trade aberto (sem exit_time)
        Retorna o trade mais recente com exit_time IS NULL
        """
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM trades
                WHERE exit_time IS NULL
                ORDER BY entry_time DESC
                LIMIT 1
            """).fetchone()
            return dict(row) if row else None

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

    # =====================================================
    # MÉTODOS v2.0 - Signals, Market Regime, Performance
    # =====================================================

    def save_signal(self, signal: Dict):
        """Guarda um sinal gerado pela estratégia (executado ou não)"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO signals
                (timestamp, asset, signal_type, confidence, strategy,
                 entry_price, stop_loss, take_profit, executed, execution_time,
                 reason, market_regime, volume_ratio, oi_change, funding_rate,
                 price_above_sma, bullish_count, bearish_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal.get('timestamp', int(datetime.now().timestamp() * 1000)),
                signal.get('asset', 'BTC'),
                signal.get('signal_type', 'NEUTRAL'),
                signal.get('confidence', 1.0),
                signal.get('strategy', 'momentum'),
                signal.get('entry_price'),
                signal.get('stop_loss'),
                signal.get('take_profit'),
                signal.get('executed', False),
                signal.get('execution_time'),
                signal.get('reason', ''),
                signal.get('market_regime', ''),
                signal.get('volume_ratio'),
                signal.get('oi_change'),
                signal.get('funding_rate'),
                signal.get('price_above_sma'),
                signal.get('bullish_count'),
                signal.get('bearish_count')
            ))
            conn.commit()

    def get_signals(self, asset: Optional[str] = None,
                    start_time: Optional[int] = None,
                    end_time: Optional[int] = None,
                    executed_only: bool = False,
                    limit: int = 1000) -> List[Dict]:
        """Busca sinais da base de dados"""
        query = "SELECT * FROM signals WHERE 1=1"
        params = []

        if asset:
            query += " AND asset = ?"
            params.append(asset)
        if start_time:
            query += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            query += " AND timestamp <= ?"
            params.append(end_time)
        if executed_only:
            query += " AND executed = TRUE"

        query += " ORDER BY timestamp DESC"
        limit = int(limit)
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def save_market_regime(self, regime_data: Dict):
        """Guarda regime de mercado"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO market_regime
                (timestamp, asset, regime, volatility_24h, trend_strength,
                 volume_profile, sma_200, price_vs_sma_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                regime_data.get('timestamp', int(datetime.now().timestamp() * 1000)),
                regime_data.get('asset', 'BTC'),
                regime_data.get('regime', 'ranging'),
                regime_data.get('volatility_24h'),
                regime_data.get('trend_strength'),
                regime_data.get('volume_profile', 'NORMAL'),
                regime_data.get('sma_200'),
                regime_data.get('price_vs_sma_pct')
            ))
            conn.commit()

    def get_market_regime(self, asset: str, limit: int = 100) -> List[Dict]:
        """Busca regime de mercado recente"""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM market_regime
                WHERE asset = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (asset, int(limit))).fetchall()
            return [dict(row) for row in rows]

    def save_performance_log(self, perf: Dict):
        """Guarda log de performance diário"""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO performance_log
                (date, asset, total_trades, winning_trades, losing_trades,
                 win_rate, profit_factor, total_pnl, max_drawdown,
                 sharpe_ratio, avg_trade_duration, long_trades, short_trades,
                 long_pnl, short_pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                perf.get('date'),
                perf.get('asset', 'BTC'),
                perf.get('total_trades', 0),
                perf.get('winning_trades', 0),
                perf.get('losing_trades', 0),
                perf.get('win_rate'),
                perf.get('profit_factor'),
                perf.get('total_pnl'),
                perf.get('max_drawdown'),
                perf.get('sharpe_ratio'),
                perf.get('avg_trade_duration'),
                perf.get('long_trades', 0),
                perf.get('short_trades', 0),
                perf.get('long_pnl'),
                perf.get('short_pnl')
            ))
            conn.commit()

    def get_performance_log(self, asset: Optional[str] = None,
                            days: int = 30) -> List[Dict]:
        """Busca logs de performance"""
        query = "SELECT * FROM performance_log WHERE 1=1"
        params = []

        if asset:
            query += " AND asset = ?"
            params.append(asset)

        query += " ORDER BY date DESC LIMIT ?"
        params.append(int(days))

        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def get_stats_v2(self) -> Dict:
        """Estatísticas completas v2"""
        stats = self.get_stats()

        with self._get_conn() as conn:
            # Signals
            stats['signals_total'] = conn.execute(
                "SELECT COUNT(*) FROM signals"
            ).fetchone()[0]
            stats['signals_executed'] = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE executed = TRUE"
            ).fetchone()[0]

            # Market regime
            stats['regime_entries'] = conn.execute(
                "SELECT COUNT(*) FROM market_regime"
            ).fetchone()[0]

            # Performance logs
            stats['performance_days'] = conn.execute(
                "SELECT COUNT(*) FROM performance_log"
            ).fetchone()[0]

            # Paper trades
            stats['paper_trades'] = conn.execute(
                "SELECT COUNT(*) FROM paper_trades"
            ).fetchone()[0]

        return stats

if __name__ == "__main__":
    # Test
    db = BotDatabase()
    print(f"Base de dados: {db.db_path}")
    print(f"Estatísticas: {db.get_stats_v2()}")
