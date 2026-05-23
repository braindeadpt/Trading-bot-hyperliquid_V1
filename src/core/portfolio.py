"""Portfolio state tracker for the Hyperliquid trading bot.

Tracks cash balance, open positions, total portfolio value, and maximum
drawdown from peak capital.  All mutations are guarded by an asyncio lock
so the state remains consistent when accessed from multiple coroutines.
"""

from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from src.strategies.base import Position
from src.utils.helpers import safe_float, safe_divide, utc_now

logger = logging.getLogger(__name__)


@dataclass
class _PositionSnapshot:
    """Internal mutable copy of a Position used by PortfolioState."""

    symbol: str
    side: str
    entry_price: float
    size: float
    entry_time_ms: int
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def current_price(self) -> float:
        """Return the last known market price for this position."""
        return self.metadata.get("_last_price", self.entry_price)

    @current_price.setter
    def current_price(self, value: float) -> None:
        self.metadata["_last_price"] = value

    def to_position(self) -> Position:
        """Convert back to the immutable Position dataclass."""
        meta = {k: v for k, v in self.metadata.items() if not k.startswith("_")}
        return Position(
            symbol=self.symbol,
            side=self.side,
            entry_price=self.entry_price,
            size=self.size,
            entry_time_ms=self.entry_time_ms,
            stop_loss_price=self.stop_loss_price,
            take_profit_price=self.take_profit_price,
            unrealized_pnl=self.unrealized_pnl,
            metadata=meta,
        )


class PortfolioState:
    """Thread-safe portfolio state tracker.

    Responsibilities:
      - Cash balance tracking
      - Open position book (unrealized PnL updated on every price tick)
      - Total portfolio value (cash + positions)
      - Maximum drawdown from peak capital
      - Daily reset at 00:00 UTC
    """

    def __init__(self, initial_capital: float) -> None:
        self._cash: float = safe_float(initial_capital)
        self._initial_capital: float = self._cash
        self._peak_capital: float = self._cash
        self._positions: Dict[str, _PositionSnapshot] = {}
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._trade_history: List[Dict[str, Any]] = []
        self._last_reset_date: str = utc_now().strftime("%Y-%m-%d")
        # Daily peak capital for drawdown tracking (resets at 00:00 UTC)
        self._daily_peak_capital: float = self._cash

        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    async def current_capital(self) -> float:
        """Return the current total capital (cash + unrealized PnL)."""
        async with self._lock:
            total = self._cash + sum(p.unrealized_pnl for p in self._positions.values())
            return total

    @property
    async def peak_capital(self) -> float:
        """Return the highest capital value observed."""
        async with self._lock:
            return self._peak_capital

    @property
    async def daily_pnl(self) -> float:
        """Return the unrealized + realized PnL since last daily reset."""
        async with self._lock:
            return self._daily_pnl

    @property
    async def daily_trades(self) -> int:
        """Return the number of trades executed today."""
        async with self._lock:
            return self._daily_trades

    @property
    async def positions(self) -> Dict[str, Position]:
        """Return a shallow copy of open positions (immutable)."""
        async with self._lock:
            return {sym: snap.to_position() for sym, snap in self._positions.items()}

    def get_positions_sync(self) -> Dict[str, Position]:
        """Synchronous read of open positions (for dashboard only)."""
        return {sym: snap.to_position() for sym, snap in self._positions.items()}



    @property
    async def trade_history(self) -> List[Dict[str, Any]]:
        """Return a copy of the closed-trade history."""
        async with self._lock:
            return list(self._trade_history)

    # ------------------------------------------------------------------
    # Price updates & PnL
    # ------------------------------------------------------------------

    async def update_price(self, symbol: str, price: float) -> None:
        """Update the market price for *symbol* and recalc unrealized PnL.

        Also triggers a daily-reset check and updates peak capital /
        drawdown tracking.
        """
        price_f = safe_float(price)
        if price_f <= 0.0:
            logger.warning("PortfolioState.update_price received non-positive price: %s", price)
            return

        async with self._lock:
            await self._check_daily_reset()

            pos = self._positions.get(symbol)
            if pos is not None:
                pos.current_price = price_f
                pos.unrealized_pnl = self._calc_unrealized_pnl(pos, price_f)

            self._update_peak_and_drawdown()

    async def update_prices(self, prices: Dict[str, float]) -> None:
        """Batch price update for multiple symbols."""
        async with self._lock:
            await self._check_daily_reset()
            for symbol, price in prices.items():
                price_f = safe_float(price)
                if price_f <= 0.0:
                    continue
                pos = self._positions.get(symbol)
                if pos is not None:
                    pos.current_price = price_f
                    pos.unrealized_pnl = self._calc_unrealized_pnl(pos, price_f)
            self._update_peak_and_drawdown()

    def _calc_unrealized_pnl(self, pos: _PositionSnapshot, price: float) -> float:
        """Compute unrealized PnL in USD for a single position."""
        if pos.side == "long":
            return (price - pos.entry_price) * pos.size
        else:
            return (pos.entry_price - price) * pos.size

    def _update_peak_and_drawdown(self) -> None:
        """Update peak capital (global) and daily peak capital."""
        total = self._cash + sum(p.unrealized_pnl for p in self._positions.values())
        if total > self._peak_capital:
            self._peak_capital = total
        # Update daily peak as well
        if total > self._daily_peak_capital:
            self._daily_peak_capital = total

    def sync_daily_max_drawdown_pct(self) -> float:
        """Synchronous read of daily max drawdown % (for dashboard only)."""
        total = self._cash + sum(p.unrealized_pnl for p in self._positions.values())
        if self._daily_peak_capital <= 0.0:
            return 0.0
        dd = (self._daily_peak_capital - total) / self._daily_peak_capital
        return max(dd, 0.0) * 100.0

    # ------------------------------------------------------------------
    # Position lifecycle
    # ------------------------------------------------------------------

    async def add_position(
        self,
        position: Position,
        cost: float,
    ) -> None:
        """Record a new open position and debit cash."""
        async with self._lock:
            await self._check_daily_reset()
            self._cash -= safe_float(cost)
            self._positions[position.symbol] = _PositionSnapshot(
                symbol=position.symbol,
                side=position.side,
                entry_price=position.entry_price,
                size=position.size,
                entry_time_ms=position.entry_time_ms,
                stop_loss_price=position.stop_loss_price,
                take_profit_price=position.take_profit_price,
                unrealized_pnl=0.0,
                metadata=deepcopy(position.metadata),
            )
            self._daily_trades += 1
            self._update_peak_and_drawdown()

    async def remove_position(
        self,
        symbol: str,
        exit_price: float,
        pnl_usd: float,
        pnl_pct: float,
        reason: str,
    ) -> None:
        """Close a position, credit cash, and record the trade."""
        async with self._lock:
            await self._check_daily_reset()
            pos = self._positions.pop(symbol, None)
            if pos is None:
                logger.warning("remove_position called for unknown symbol: %s", symbol)
                return

            # Credit cash with the notional value + realized PnL
            # When opening, we debited entry_price * size (cost).
            # When closing, we must credit back that cost plus the PnL.
            notional = pos.entry_price * pos.size
            self._cash += safe_float(notional) + safe_float(pnl_usd)
            self._daily_pnl += safe_float(pnl_usd)

            self._trade_history.append({
                "symbol": symbol,
                "side": pos.side,
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "size": pos.size,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "reason": reason,
                "entry_time_ms": pos.entry_time_ms,
                "exit_time_ms": int(utc_now().timestamp() * 1000),
            })
            self._update_peak_and_drawdown()

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    async def get_unrealized_pnl(self) -> float:
        """Return the sum of unrealized PnL across all open positions."""
        async with self._lock:
            return sum(p.unrealized_pnl for p in self._positions.values())

    async def get_total_value(self) -> float:
        """Return cash + open position market value."""
        async with self._lock:
            unrealized = sum(p.unrealized_pnl for p in self._positions.values())
            return self._cash + unrealized

    async def get_max_drawdown(self) -> float:
        """Return the maximum drawdown from peak as a percentage (0.0–1.0).

        A value of 0.05 means a 5% drawdown from the all-time high.
        """
        async with self._lock:
            total = self._cash + sum(p.unrealized_pnl for p in self._positions.values())
            if self._peak_capital <= 0.0:
                return 0.0
            dd = (self._peak_capital - total) / self._peak_capital
            return max(dd, 0.0)

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    async def _check_daily_reset(self) -> None:
        """Reset daily counters if the UTC date has rolled over."""
        today = utc_now().strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self._daily_pnl = 0.0
            self._daily_trades = 0
            total = self._cash + sum(p.unrealized_pnl for p in self._positions.values())
            self._daily_peak_capital = total  # Reset daily peak to total capital
            self._last_reset_date = today
            logger.info("Daily portfolio reset - date=%s, daily_peak=%.2f", today, self._daily_peak_capital)

    async def force_daily_reset(self) -> None:
        """Manually trigger a daily reset (useful for testing)."""
        async with self._lock:
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._last_reset_date = utc_now().strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    async def to_dict(self) -> Dict[str, Any]:
        """Return a snapshot of the entire portfolio state."""
        async with self._lock:
            unrealized = sum(p.unrealized_pnl for p in self._positions.values())
            total = self._cash + unrealized
            return {
                "cash": self._cash,
                "initial_capital": self._initial_capital,
                "peak_capital": self._peak_capital,
                "current_capital": total,
                "unrealized_pnl": unrealized,
                "daily_pnl": self._daily_pnl,
                "daily_trades": self._daily_trades,
                "max_drawdown": safe_divide(self._peak_capital - total, self._peak_capital, 0.0),
                "daily_peak_capital": self._daily_peak_capital,
                "initial_capital": self._initial_capital,
                "position_count": len(self._positions),
                "positions": {
                    sym: {
                        "symbol": p.symbol,
                        "side": p.side,
                        "entry_price": p.entry_price,
                        "size": p.size,
                        "entry_time_ms": p.entry_time_ms,
                        "unrealized_pnl": p.unrealized_pnl,
                        "current_price": p.current_price,
                        "metadata": p.metadata,
                    }
                    for sym, p in self._positions.items()
                },
                "trade_history_count": len(self._trade_history),
                "last_reset_date": self._last_reset_date,
            }

    async def from_dict(self, data: Dict[str, Any]) -> None:
        """Restore portfolio state from a previously saved snapshot.

        Used on engine startup to resume from DB state.
        """
        async with self._lock:
            self._cash = safe_float(data.get("cash"), self._initial_capital)
            self._peak_capital = safe_float(data.get("peak_capital"), self._peak_capital)
            self._daily_peak_capital = safe_float(data.get("daily_peak_capital"), self._cash)
            self._initial_capital = safe_float(data.get("initial_capital"), self._initial_capital)
            self._daily_pnl = safe_float(data.get("daily_pnl"), 0.0)
            self._daily_trades = safe_float(data.get("daily_trades"), 0.0)
            self._last_reset_date = data.get("last_reset_date", utc_now().strftime("%Y-%m-%d"))

            # Restore positions
            for sym, p_data in data.get("positions", {}).items():
                snap = _PositionSnapshot(
                    symbol=p_data["symbol"],
                    side=p_data["side"],
                    entry_price=safe_float(p_data["entry_price"]),
                    size=safe_float(p_data["size"]),
                    entry_time_ms=int(p_data.get("entry_time_ms", 0)),
                    unrealized_pnl=safe_float(p_data.get("unrealized_pnl", 0.0)),
                    metadata=dict(p_data.get("metadata", {})),
                )
                snap.current_price = safe_float(p_data.get("current_price", snap.entry_price))
                self._positions[sym] = snap

            logger.info(
                "Portfolio state restored: cash=%.2f positions=%d",
                self._cash,
                len(self._positions),
            )

    # ------------------------------------------------------------------
    # Synchronous accessors (for dashboard — race-safe snapshots)
    # ------------------------------------------------------------------

    def sync_capital(self) -> float:
        """Return current total capital (cash + unrealized)."""
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        return self._cash + unrealized

    def sync_initial_capital(self) -> float:
        """Return the initial capital."""
        return self._initial_capital

    def sync_total_pnl(self) -> float:
        """Return total realized + unrealized PnL."""
        realized = sum(t.get("pnl_usd", 0.0) for t in self._trade_history)
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        return realized + unrealized

    def sync_max_drawdown_pct(self) -> float:
        """Return max drawdown as percentage."""
        total = self._cash + sum(p.unrealized_pnl for p in self._positions.values())
        if self._peak_capital <= 0.0:
            return 0.0
        dd = (self._peak_capital - total) / self._peak_capital
        return max(dd, 0.0) * 100.0

    def get_positions_sync(self) -> Dict[str, Any]:
        """Return a shallow copy of positions for dashboard display."""
        return {
            sym: p for sym, p in self._positions.items()
        }

    def sync_daily_trades(self) -> int:
        """Return number of trades today."""
        return self._daily_trades

    def sync_daily_pnl(self) -> float:
        """Return today's PnL."""
        return self._daily_pnl

    def sync_trade_history(self) -> List[Dict[str, Any]]:
        """Return a copy of closed-trade history."""
        return list(self._trade_history)
