"""
SQLite Candle Repository — implementação da porta CandleRepository.
Converte entre entidade de domínio e modelo de base de dados.
"""
import sqlite3
from typing import List
from ...domain.entities import Candle
from ...domain.repositories import CandleRepository
from ..database import SQLiteConnection
from ..mappers import CandleMapper


class SQLiteCandleRepository(CandleRepository):
    """Repositório SQLite para candles."""
    
    def __init__(self, db: SQLiteConnection):
        self.db = db
        self._mapper = CandleMapper()
    
    def save(self, candles: List[Candle]) -> None:
        if not candles:
            return
        conn = self.db.connect()
        conn.executemany(
            """INSERT OR REPLACE INTO candles 
               (symbol, interval, timestamp, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(c.symbol, c.interval, c.timestamp, c.open, c.high,
              c.low, c.close, c.volume) for c in candles]
        )
        conn.commit()
    
    def get_recent(self, symbol: str, interval: str, limit: int) -> List[Candle]:
        conn = self.db.connect()
        rows = conn.execute(
            "SELECT * FROM candles WHERE symbol = ? AND interval = ? ORDER BY timestamp DESC LIMIT ?",
            (symbol, interval, limit)
        ).fetchall()
        return [self._mapper.to_entity(dict(r)) for r in rows]
    
    def get_count(self, symbol: str = None) -> int:
        conn = self.db.connect()
        if symbol:
            row = conn.execute("SELECT COUNT(*) FROM candles WHERE symbol = ?", (symbol,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM candles").fetchone()
        return row[0] if row else 0
