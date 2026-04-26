"""
SQLite Signal Repository — implementação da porta SignalRepository.
"""
import sqlite3
import time
from typing import List
from ...domain.entities import Signal
from ...domain.repositories import SignalRepository
from ..database import SQLiteConnection
from ..mappers import SignalMapper


class SQLiteSignalRepository(SignalRepository):
    """Repositório SQLite para sinais."""
    
    def __init__(self, db: SQLiteConnection):
        self.db = db
        self._mapper = SignalMapper()
    
    def save(self, signal: Signal) -> None:
        conn = self.db.connect()
        conn.execute(
            """INSERT INTO signals 
            (timestamp, asset, signal_type, confidence, executed,
             entry_price, stop_loss, take_profit, reason, market_regime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal.timestamp, signal.asset, signal.direction.upper(),
             signal.confidence, False,
             signal.entry_price, signal.stop_loss,
             signal.take_profit, signal.reason, "")
        )
        conn.commit()
    
    def get_recent(self, asset: str = None, limit: int = 100) -> List[Signal]:
        conn = self.db.connect()
        query = "SELECT * FROM signals WHERE 1=1"
        params = []
        if asset:
            query += " AND asset = ?"
            params.append(asset)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [self._mapper.to_entity(dict(r)) for r in rows]
    
    def get_count(self, asset: str = None) -> int:
        conn = self.db.connect()
        if asset:
            row = conn.execute("SELECT COUNT(*) FROM signals WHERE asset = ?", (asset,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM signals").fetchone()
        return row[0] if row else 0
