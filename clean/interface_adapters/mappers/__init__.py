"""Mappers — convertem entre modelo de BD e entidade de domínio."""
from ...domain.entities import Candle, Trade, Signal


class CandleMapper:
    """Mapper Candle <-> SQLite row."""
    
    def to_entity(self, row: dict) -> Candle:
        return Candle(
            symbol=row['symbol'],
            interval=row['interval'],
            timestamp=row['timestamp'],
            open=row['open'],
            high=row['high'],
            low=row['low'],
            close=row['close'],
            volume=row['volume']
        )


class TradeMapper:
    """Mapper Trade <-> SQLite row."""
    
    def to_entity(self, row: dict) -> Trade:
        return Trade(
            id=row.get('id'),
            symbol=row['symbol'],
            direction=row['direction'],
            entry_price=row['entry_price'],
            exit_price=row.get('exit_price'),
            entry_time=row['entry_time'],
            exit_time=row.get('exit_time'),
            size_usd=row['size_usd'],
            leverage=row.get('leverage', 1.0),
            pnl_usd=row.get('pnl_usd'),
            pnl_pct=row.get('pnl_pct'),
            exit_reason=row.get('exit_reason')
        )


class SignalMapper:
    """Mapper Signal <-> SQLite row."""
    
    def to_entity(self, row: dict) -> Signal:
        return Signal(
            asset=row['asset'],
            direction=row['signal_type'].lower(),
            confidence=row.get('confidence', 1.0),
            entry_price=row.get('entry_price', 0.0),
            stop_loss=row.get('stop_loss'),
            take_profit=row.get('take_profit'),
            reason=row.get('reason', ''),
            timestamp=row['timestamp']
        )
