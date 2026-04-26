"""
SQLite Trade Repository — implementação da porta TradeRepository.
"""
import sqlite3
from typing import List, Optional
from ...domain.entities import Trade
from ...domain.repositories import TradeRepository
from ..database import SQLiteConnection
from ..mappers import TradeMapper


class SQLiteTradeRepository(TradeRepository):
    """Repositório SQLite para trades."""
    
    def __init__(self, db: SQLiteConnection):
        self.db = db
        self._mapper = TradeMapper()
    
    def save(self, trade: Trade) -> int:
        conn = self.db.connect()
        cursor = conn.execute(
            """INSERT INTO trades 
            (symbol, direction, entry_price, exit_price, entry_time, exit_time,
             size_usd, leverage, pnl_usd, pnl_pct, exit_reason, strategy_params)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade.symbol, trade.direction, trade.entry_price,
             trade.exit_price, trade.entry_time, trade.exit_time,
             trade.size_usd, trade.leverage, trade.pnl_usd,
             trade.pnl_pct, trade.exit_reason, str(trade.strategy_params))
        )
        conn.commit()
        return cursor.lastrowid
    
    def get_open(self, symbol: str = None) -> Optional[Trade]:
        conn = self.db.connect()
        query = "SELECT * FROM trades WHERE exit_time IS NULL"
        params = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY entry_time DESC LIMIT 1"
        row = conn.execute(query, params).fetchone()
        return self._mapper.to_entity(dict(row)) if row else None
    
    def get_recent(self, symbol: str = None, limit: int = 100) -> List[Trade]:
        conn = self.db.connect()
        query = "SELECT * FROM trades WHERE 1=1"
        params = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY entry_time DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [self._mapper.to_entity(dict(r)) for r in rows]
    
    def update_exit(self, trade_id: int, exit_price: float,
                    exit_time: int, pnl_usd: float, pnl_pct: float,
                    reason: str) -> None:
        conn = self.db.connect()
        conn.execute(
            "UPDATE trades SET exit_price = ?, exit_time = ?, pnl_usd = ?, pnl_pct = ?, exit_reason = ? WHERE id = ?",
            (exit_price, exit_time, pnl_usd, pnl_pct, reason, trade_id)
        )
        conn.commit()
    
    def get_count(self, symbol: str = None) -> int:
        conn = self.db.connect()
        if symbol:
            row = conn.execute("SELECT COUNT(*) FROM trades WHERE symbol = ?", (symbol,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        return row[0] if row else 0
