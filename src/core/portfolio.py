"""Portfolio state tracker for the Hyperliquid trading bot.

Tracks cash balance, open positions, total portfolio value, and maximum
drawdown from peak capital.  All mutations are guarded by an asyncio lock
so the state remains consistent when accessed from multiple coroutines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from src.strategies.base import Position
from src.utils.helpers import safe_float, safe_divide, utc_now

logger = logging.getLogger(__name__)


def _utc_midnight_ms() -> int:
    """UTC midnight for the current calendar day (ms)."""
    from datetime import datetime, timezone

    now = utc_now()
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int(midnight.timestamp() * 1000)


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
            current_price=self.current_price,
            metadata=meta,
        )


@dataclass(frozen=True)
class PortfolioSnapshotView:
    """Atomic, read-only snapshot of portfolio state for dashboards.

    Produced by :meth:`PortfolioState.snapshot_sync` under a single
    internal lock acquisition, then frozen so the dashboard / status
    endpoint can read every counter without racing the writer.
    """
    cash: float
    peak_capital: float
    total_equity: float
    unrealized_pnl: float
    daily_pnl: float
    daily_realized_pnl: float
    daily_trades: int
    total_trades: int
    max_drawdown_pct: float
    daily_max_drawdown_pct: float
    day_start_equity: float
    day_start_unrealized: float
    position_count: int
    positions: Dict[str, Position]
    last_reset_date: str
    timestamp_ms: int


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
        self._total_trades_closed: int = 0
        self._trade_history: List[Dict[str, Any]] = []
        self._last_reset_date: str = utc_now().strftime("%Y-%m-%d")
        # Daily peak capital for drawdown tracking (resets at 00:00 UTC)
        self._daily_peak_capital: float = self._cash
        self._day_start_equity: float = self._cash
        self._day_start_unrealized: float = 0.0

        self._lock = asyncio.Lock()
        self._cache_lock = threading.Lock()
        self._dashboard_cache: Optional[PortfolioSnapshotView] = None
        self._day_start_from_meta: bool = False
        self._refresh_dashboard_cache_unlocked()

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    async def current_capital(self) -> float:
        """Return current total equity (cash + mark-to-market positions)."""
        async with self._lock:
            return self._total_equity()

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
        snap = self.get_dashboard_snapshot_sync()
        return dict(snap.positions)



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
            self._refresh_dashboard_cache_unlocked()

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
            self._refresh_dashboard_cache_unlocked()

    async def update_stop_loss(
        self,
        symbol: str,
        stop_loss_price: float,
        side: str,
    ) -> None:
        """Ratchet stop-loss for trailing stop (long: higher, short: lower)."""
        async with self._lock:
            pos = self._positions.get(symbol)
            if pos is None:
                return
            if side == "long":
                if pos.stop_loss_price is None or stop_loss_price > pos.stop_loss_price:
                    pos.stop_loss_price = stop_loss_price
            elif pos.stop_loss_price is None or stop_loss_price < pos.stop_loss_price:
                pos.stop_loss_price = stop_loss_price

    def _calc_unrealized_pnl(self, pos: _PositionSnapshot, price: float) -> float:
        """Compute unrealized PnL in USD for a single position."""
        if pos.side == "long":
            return (price - pos.entry_price) * pos.size
        else:
            return (pos.entry_price - price) * pos.size

    def _position_equity_at_mark(self, pos: _PositionSnapshot, mark: float) -> float:
        """Mark-to-market equity for one position at *mark*."""
        mark_f = safe_float(mark, 0.0)
        if mark_f <= 0.0:
            return self._position_equity(pos)
        if pos.side == "long":
            return mark_f * pos.size
        unrealized = (pos.entry_price - mark_f) * pos.size
        return pos.entry_price * pos.size + unrealized

    def _unrealized_at_mark(self, pos: _PositionSnapshot, mark: float) -> float:
        mark_f = safe_float(mark, 0.0)
        if mark_f <= 0.0:
            return pos.unrealized_pnl
        if pos.side == "long":
            return (mark_f - pos.entry_price) * pos.size
        return (pos.entry_price - mark_f) * pos.size

    def compute_equity_with_marks(self, mark_prices: Dict[str, float]) -> float:
        """Total equity using live mark prices (dashboard thread-safe)."""
        snap = self.get_dashboard_snapshot_sync()
        total = snap.cash
        for sym, pos in snap.positions.items():
            mark = safe_float(mark_prices.get(sym), 0.0)
            if mark <= 0.0:
                mark = safe_float(pos.current_price, 0.0) or safe_float(pos.entry_price, 0.0)
            total += self._position_equity_at_mark(pos, mark)
        return total

    def build_live_dashboard_metrics(
        self,
        mark_prices: Dict[str, float],
    ) -> Dict[str, float]:
        """Single source of truth for dashboard KPI math (live marks)."""
        snap = self.get_dashboard_snapshot_sync()
        live_unrealized = 0.0
        for sym, pos in snap.positions.items():
            mark = safe_float(mark_prices.get(sym), 0.0)
            if mark <= 0.0:
                mark = safe_float(pos.current_price, 0.0) or safe_float(pos.entry_price, 0.0)
            live_unrealized += self._unrealized_at_mark(pos, mark)

        live_equity = snap.cash + sum(
            self._position_equity_at_mark(
                snap.positions[sym],
                safe_float(mark_prices.get(sym), 0.0)
                or safe_float(snap.positions[sym].current_price, 0.0)
                or safe_float(snap.positions[sym].entry_price, 0.0),
            )
            for sym in snap.positions
        )

        day_start = snap.day_start_equity
        daily_pnl = live_equity - day_start
        day_base = day_start if day_start > 0 else live_equity
        daily_pnl_pct = safe_divide(daily_pnl, day_base, 0.0) * 100.0

        max_dd_pct = 0.0
        if snap.peak_capital > 0.0:
            max_dd_pct = max(
                (snap.peak_capital - live_equity) / snap.peak_capital, 0.0,
            ) * 100.0

        daily_max_dd_pct = 0.0
        if self._daily_peak_capital > 0.0:
            daily_max_dd_pct = max(
                (self._daily_peak_capital - live_equity) / self._daily_peak_capital, 0.0,
            ) * 100.0

        return {
            "capital": live_equity,
            "unrealized_pnl": live_unrealized,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl_pct,
            "daily_realized_pnl": snap.daily_realized_pnl,
            "day_start_equity": day_start,
            "max_drawdown_pct": max_dd_pct,
            "daily_max_drawdown_pct": daily_max_dd_pct,
            "open_positions": float(snap.position_count),
            "daily_trades": float(snap.daily_trades),
            "total_trades": float(snap.total_trades),
            "timestamp_ms": float(time.time() * 1000),
        }

    def _position_equity(self, pos: _PositionSnapshot) -> float:
        """Mark-to-market equity contribution of an open position.

        Paper mode debits notional from cash on entry; equity must add back
        position value (long: mark * size, short: entry notional + unrealized).
        Using cash + unrealized_pnl alone falsely shows ~20% drawdown on open.
        """
        if pos.side == "long":
            return pos.current_price * pos.size
        return pos.entry_price * pos.size + pos.unrealized_pnl

    def _total_equity(self) -> float:
        """Total portfolio equity (cash + mark-to-market position value)."""
        return self._cash + sum(
            self._position_equity(p) for p in self._positions.values()
        )

    def _update_peak_and_drawdown(self) -> None:
        """Update peak capital (global) and daily peak capital."""
        total = self._total_equity()
        if total > self._peak_capital:
            self._peak_capital = total
        if total > self._daily_peak_capital:
            self._daily_peak_capital = total

    def _fix_day_start_baseline_unlocked(
        self,
        total: float,
        unrealized: float,
    ) -> None:
        """Correct a stale ``day_start_equity`` after an equity-accounting fix.

        Daily equity change must equal realised PnL plus the change in open
        unrealised PnL since day start.  A persisted baseline from the era
        when position MTM was omitted (cash-only snapshots) drifts by roughly
        open notional and inflates Daily PnL on the dashboard.
        """
        expected_daily = (
            self._daily_pnl + unrealized - self._day_start_unrealized
        )
        actual_daily = total - self._day_start_equity
        drift = abs(actual_daily - expected_daily)
        tol = max(1.0, total * 1e-4)
        if drift <= tol:
            return
        implied_start = total - expected_daily
        logger.warning(
            "Correcting day_start_equity %.2f -> %.2f "
            "(drift=%.2f expected_daily=%.2f)",
            self._day_start_equity,
            implied_start,
            drift,
            expected_daily,
        )
        self._day_start_equity = implied_start
        self._day_start_from_meta = False

    async def reconcile_peaks(self) -> None:
        """Clamp peak capital after restore or phantom drawdown trips."""
        async with self._lock:
            total = self._total_equity()
            if total > self._peak_capital:
                self._peak_capital = total
            if total > self._daily_peak_capital:
                self._daily_peak_capital = total
            # Peak left from bad accounting: equity stable but peak at initial
            if self._peak_capital > total and total >= self._initial_capital * 0.5:
                implied_dd = (self._peak_capital - total) / self._peak_capital
                if implied_dd > 0.15 and len(self._positions) == 0:
                    logger.warning(
                        "Reconciling inflated peak_capital %.2f -> %.2f (phantom drawdown)",
                        self._peak_capital,
                        total,
                    )
                    self._peak_capital = max(total, self._initial_capital)
                    self._daily_peak_capital = max(
                        self._daily_peak_capital, total,
                    )
            self._refresh_dashboard_cache_unlocked()

    def sync_daily_max_drawdown_pct(self) -> float:
        """Synchronous read of daily max drawdown % (for dashboard only)."""
        total = self._total_equity()
        if self._daily_peak_capital <= 0.0:
            return 0.0
        dd = (self._daily_peak_capital - total) / self._daily_peak_capital
        return max(dd, 0.0) * 100.0

    # ------------------------------------------------------------------
    # Position lifecycle
    # ------------------------------------------------------------------

    async def apply_funding(
        self,
        symbol: str,
        funding_rate: float,
        position_size: float,
        side: str,
        mark_price: float,
    ) -> float:
        """Apply a funding payment to the open position's cash balance.

        Hyperliquid settles funding hourly. When ``funding_rate > 0``,
        longs pay shorts; when ``funding_rate < 0`` shorts pay longs.

        Args:
            symbol:        Position symbol (logged only).
            funding_rate:  Hourly funding rate as a fraction of notional
                           (e.g. 0.0001 = 0.01% per hour).
            position_size: Position size in base units (e.g. BTC).
            side:          ``"long"`` or ``"short"``.
            mark_price:    Mark price in USD for notional conversion.

        Returns:
            The cashflow applied (negative = paid, positive = received).
        """
        rate_f = safe_float(funding_rate, 0.0)
        size_f = safe_float(position_size, 0.0)
        if rate_f == 0.0 or size_f == 0.0:
            return 0.0

        async with self._lock:
            await self._check_daily_reset()
            price_f = safe_float(mark_price, 0.0)
            if price_f <= 0.0:
                pos = self._positions.get(symbol)
                if pos is not None:
                    price_f = safe_float(pos.current_price, 0.0) or safe_float(pos.entry_price, 0.0)
            if price_f <= 0.0:
                return 0.0

            notional_usd = size_f * price_f
            cashflow_magnitude = notional_usd * rate_f
            side_norm = str(side).lower()
            if side_norm == "long":
                cashflow = -cashflow_magnitude
            elif side_norm == "short":
                cashflow = cashflow_magnitude
            else:
                return 0.0

            self._cash += cashflow
            self._daily_pnl += cashflow
            pos = self._positions.get(symbol)
            if pos is not None:
                pos.metadata["funding_total"] = safe_float(
                    pos.metadata.get("funding_total", 0.0), 0.0,
                ) + cashflow
            self._update_peak_and_drawdown()
            self._refresh_dashboard_cache_unlocked()

        logger.debug(
            "Funding applied: %s %s rate=%s notional_usd=%.2f cashflow=%.4f",
            symbol, side, rate_f, notional_usd, cashflow,
        )
        return cashflow

    async def add_position(
        self,
        position: Position,
        cost: float,
    ) -> None:
        """Record a new open position and debit cash."""
        async with self._lock:
            await self._check_daily_reset()
            self._cash -= safe_float(cost)
            snap = _PositionSnapshot(
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
            snap.current_price = position.entry_price
            self._positions[position.symbol] = snap
            self._daily_trades += 1
            self._update_peak_and_drawdown()
            self._refresh_dashboard_cache_unlocked()

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
            self._total_trades_closed += 1
            self._update_peak_and_drawdown()
            self._refresh_dashboard_cache_unlocked()

    async def cancel_position(self, symbol: str) -> None:
        """Rollback a freshly-opened position (e.g. after a failed live order).

        v3.1.17 C9: removes the position from the book and credits the
        cost back to cash, so the engine is not left holding a phantom
        position. The corresponding DB trade row is updated to status
        ``'cancelled'`` by the caller.
        """
        async with self._lock:
            pos = self._positions.pop(symbol, None)
            if pos is None:
                return
            # Refund the cost we debited on add_position.
            cost = pos.entry_price * pos.size
            self._cash += safe_float(cost)
            # Roll back the daily_trades counter so a cancelled entry
            # doesn't poison the daily limit.
            if self._daily_trades > 0:
                self._daily_trades -= 1
            logger.info(
                "Portfolio cancel_position %s: refunded cost=%.2f cash=%.2f",
                symbol, cost, self._cash,
            )
            self._refresh_dashboard_cache_unlocked()

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    async def get_unrealized_pnl(self) -> float:
        """Return the sum of unrealized PnL across all open positions."""
        async with self._lock:
            return sum(p.unrealized_pnl for p in self._positions.values())

    async def get_total_value(self) -> float:
        """Return total equity (cash + mark-to-market positions)."""
        async with self._lock:
            return self._total_equity()

    async def get_max_drawdown(self) -> float:
        """Return the maximum drawdown from peak as a percentage (0.0–1.0).

        A value of 0.05 means a 5% drawdown from the all-time high.
        """
        async with self._lock:
            total = self._total_equity()
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
            total = self._total_equity()
            self._daily_peak_capital = total
            self._day_start_equity = total
            self._day_start_unrealized = sum(
                p.unrealized_pnl for p in self._positions.values()
            )
            self._last_reset_date = today
            logger.info("Daily portfolio reset - date=%s, daily_peak=%.2f", today, self._daily_peak_capital)

    async def force_daily_reset(self) -> None:
        """Manually trigger a daily reset (useful for testing)."""
        async with self._lock:
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._last_reset_date = utc_now().strftime("%Y-%m-%d")
            total = self._total_equity()
            self._daily_peak_capital = total
            self._day_start_equity = total
            self._day_start_unrealized = sum(
                p.unrealized_pnl for p in self._positions.values()
            )
            self._refresh_dashboard_cache_unlocked()

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    async def to_dict(self) -> Dict[str, Any]:
        """Return a snapshot of the entire portfolio state."""
        async with self._lock:
            unrealized = sum(p.unrealized_pnl for p in self._positions.values())
            total = self._total_equity()
            return {
                "cash": self._cash,
                "initial_capital": self._initial_capital,
                "peak_capital": self._peak_capital,
                "current_capital": total,
                "unrealized_pnl": unrealized,
                "daily_realized_pnl": self._daily_pnl,
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
                        "stop_loss_price": p.stop_loss_price,
                        "take_profit_price": p.take_profit_price,
                        "metadata": p.metadata,
                    }
                    for sym, p in self._positions.items()
                },
                "trade_history_count": len(self._trade_history),
                "total_trades_closed": self._total_trades_closed,
                "day_start_equity": self._day_start_equity,
                "day_start_unrealized": self._day_start_unrealized,
                "last_reset_date": self._last_reset_date,
            }

    async def from_dict(self, data: Dict[str, Any]) -> None:
        """Restore portfolio state from a previously saved snapshot.

        Used on engine startup to resume from DB state.
        """
        async with self._lock:
            positions_raw = dict(data.get("positions", {}) or {})
            meta = positions_raw.pop("_meta", {}) or {}
            if not isinstance(meta, dict):
                meta = {}

            self._peak_capital = safe_float(
                data.get("peak_capital"),
                self._peak_capital,
            )
            self._daily_peak_capital = safe_float(
                data.get("daily_peak_capital", meta.get("daily_peak_capital")),
                self._cash,
            )
            self._initial_capital = safe_float(
                data.get("initial_capital"),
                self._initial_capital,
            )
            self._daily_pnl = safe_float(
                data.get("daily_realized_pnl", data.get("daily_pnl")),
                0.0,
            )
            self._daily_trades = int(
                safe_float(
                    data.get("daily_trades", meta.get("daily_trades")),
                    0.0,
                )
            )
            self._total_trades_closed = int(
                safe_float(
                    data.get(
                        "total_trades_closed",
                        meta.get("total_trades_closed", data.get("trade_history_count", 0)),
                    ),
                    0.0,
                )
            )
            self._last_reset_date = str(
                data.get("last_reset_date", meta.get("last_reset_date", utc_now().strftime("%Y-%m-%d")))
            )

            # Restore positions before cash / day-start (order matters for equity math).
            self._positions.clear()
            for sym, p_data in positions_raw.items():
                snap = _PositionSnapshot(
                    symbol=p_data["symbol"],
                    side=p_data["side"],
                    entry_price=safe_float(p_data["entry_price"]),
                    size=safe_float(p_data["size"]),
                    entry_time_ms=int(p_data.get("entry_time_ms", 0)),
                    unrealized_pnl=safe_float(p_data.get("unrealized_pnl", 0.0)),
                    stop_loss_price=safe_float(p_data.get("stop_loss_price"), default=0.0) or None,
                    take_profit_price=safe_float(p_data.get("take_profit_price"), default=0.0) or None,
                    metadata=dict(p_data.get("metadata", {})),
                )
                snap.current_price = safe_float(p_data.get("current_price", snap.entry_price))
                self._positions[sym] = snap

            reported_capital = safe_float(
                data.get("current_capital", data.get("capital")),
                self._initial_capital,
            )
            cash_raw = data.get("cash", meta.get("cash"))
            if cash_raw is not None:
                self._cash = safe_float(cash_raw, self._initial_capital)
            else:
                # Legacy snapshots stored total equity as "cash" — derive real cash.
                pos_equity = sum(
                    self._position_equity(p) for p in self._positions.values()
                )
                self._cash = reported_capital - pos_equity

            total = self._total_equity()
            day_start_raw = data.get("day_start_equity", meta.get("day_start_equity"))
            today = utc_now().strftime("%Y-%m-%d")
            if day_start_raw is not None and self._last_reset_date == today:
                self._day_start_equity = safe_float(day_start_raw, total)
                self._day_start_unrealized = safe_float(
                    data.get("day_start_unrealized", meta.get("day_start_unrealized")),
                    0.0,
                )
                self._day_start_from_meta = True
            else:
                self._day_start_equity = total
                self._day_start_unrealized = sum(
                    p.unrealized_pnl for p in self._positions.values()
                )
                self._day_start_from_meta = False

            unrealized = sum(p.unrealized_pnl for p in self._positions.values())
            self._fix_day_start_baseline_unlocked(total, unrealized)

            logger.info(
                "Portfolio state restored: cash=%.2f equity=%.2f positions=%d day_start=%.2f",
                self._cash,
                total,
                len(self._positions),
                self._day_start_equity,
            )
            self._refresh_dashboard_cache_unlocked()

    async def reconcile_daily_from_db(
        self,
        db: Any,
        snap_daily_pnl: Optional[float] = None,
    ) -> None:
        """Rebuild today's counters from SQLite after a restart.

        Uses closed trades since UTC midnight for realised PnL and
        reconstructs ``day_start_equity`` when it was not persisted.
        """
        if db is None:
            return
        async with self._lock:
            await self._check_daily_reset()
            since_ms = _utc_midnight_ms()

            db_day = db.get_daily_realized_since(since_ms)
            db_realized = safe_float(db_day.get("pnl_usd"), 0.0) + safe_float(
                db_day.get("funding_paid"), 0.0,
            )
            snap_d = safe_float(snap_daily_pnl, 0.0)
            # Snap may include open-position funding not yet on closed rows.
            if snap_d > db_realized:
                self._daily_pnl = snap_d
            else:
                self._daily_pnl = db_realized

            entries = db.count_trade_entries_since(since_ms)
            if entries > 0:
                self._daily_trades = entries

            total = self._total_equity()
            unrealized = sum(p.unrealized_pnl for p in self._positions.values())

            if not self._day_start_from_meta:
                first_snap = db.get_first_portfolio_snapshot_since(since_ms)
                if first_snap is not None:
                    self._day_start_equity = safe_float(
                        first_snap.get("capital"),
                        total,
                    )
                    try:
                        import json as _json
                        pos_blob = _json.loads(first_snap.get("positions_json") or "{}")
                        meta0 = pos_blob.get("_meta", {}) if isinstance(pos_blob, dict) else {}
                        if isinstance(meta0, dict):
                            self._day_start_unrealized = safe_float(
                                meta0.get("day_start_unrealized"), 0.0,
                            )
                    except Exception:
                        self._day_start_unrealized = 0.0
                else:
                    # total = day_start + realized + (unrealized_now - unrealized_at_day_start)
                    self._day_start_equity = (
                        total - self._daily_pnl - unrealized + self._day_start_unrealized
                    )

            self._fix_day_start_baseline_unlocked(total, unrealized)

            logger.info(
                "Daily state reconciled from DB: realized=%.2f entries=%d "
                "day_start=%.2f equity=%.2f daily_equity_pnl=%.2f",
                self._daily_pnl,
                self._daily_trades,
                self._day_start_equity,
                total,
                total - self._day_start_equity,
            )
            self._refresh_dashboard_cache_unlocked()

    # ------------------------------------------------------------------
    # Synchronous accessors (for dashboard — race-safe snapshots)
    # ------------------------------------------------------------------

    def sync_capital(self) -> float:
        """Return current total equity (cash + mark-to-market positions)."""
        return self.get_dashboard_snapshot_sync().total_equity

    def sync_initial_capital(self) -> float:
        """Return the initial capital."""
        return self._initial_capital

    def sync_total_pnl(self) -> float:
        """Return total realized + unrealized PnL."""
        snap = self.get_dashboard_snapshot_sync()
        realized = sum(t.get("pnl_usd", 0.0) for t in self._trade_history)
        return realized + snap.unrealized_pnl

    def sync_max_drawdown_pct(self) -> float:
        """Return max drawdown as percentage."""
        return self.get_dashboard_snapshot_sync().max_drawdown_pct

    def sync_daily_trades(self) -> int:
        """Return number of trades today."""
        return self.get_dashboard_snapshot_sync().daily_trades

    def sync_daily_pnl(self) -> float:
        """Return today's equity change (mark-to-market, incl. open positions)."""
        return self.get_dashboard_snapshot_sync().daily_pnl

    def sync_daily_realized_pnl(self) -> float:
        """Return today's realized PnL + funding cashflows."""
        return self.get_dashboard_snapshot_sync().daily_realized_pnl

    def sync_total_trades(self) -> int:
        """Return lifetime closed-trade count."""
        return self.get_dashboard_snapshot_sync().total_trades

    def sync_trade_history(self) -> List[Dict[str, Any]]:
        """Return a copy of closed-trade history."""
        return list(self._trade_history)

    def _build_snapshot_view(self) -> PortfolioSnapshotView:
        """Build snapshot from current in-memory state (caller holds asyncio lock)."""
        positions = {
            sym: p.to_position() for sym, p in self._positions.items()
        }
        total_equity = self._total_equity()
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        max_dd_pct = 0.0
        if self._peak_capital > 0.0:
            max_dd_pct = max(
                (self._peak_capital - total_equity) / self._peak_capital, 0.0
            ) * 100.0
        daily_max_dd_pct = 0.0
        if self._daily_peak_capital > 0.0:
            daily_max_dd_pct = max(
                (self._daily_peak_capital - total_equity) / self._daily_peak_capital,
                0.0,
            ) * 100.0
        daily_equity_pnl = total_equity - self._day_start_equity
        return PortfolioSnapshotView(
            cash=self._cash,
            peak_capital=self._peak_capital,
            total_equity=total_equity,
            unrealized_pnl=unrealized,
            daily_pnl=daily_equity_pnl,
            daily_realized_pnl=self._daily_pnl,
            daily_trades=self._daily_trades,
            total_trades=self._total_trades_closed,
            max_drawdown_pct=max_dd_pct,
            daily_max_drawdown_pct=daily_max_dd_pct,
            day_start_equity=self._day_start_equity,
            day_start_unrealized=self._day_start_unrealized,
            position_count=len(self._positions),
            positions=positions,
            last_reset_date=self._last_reset_date,
            timestamp_ms=int(time.time() * 1000),
        )

    def _refresh_dashboard_cache_unlocked(self) -> None:
        """Refresh thread-safe dashboard cache (call with asyncio lock held)."""
        total = self._total_equity()
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        self._fix_day_start_baseline_unlocked(total, unrealized)
        snap = self._build_snapshot_view()
        with self._cache_lock:
            self._dashboard_cache = snap

    def get_dashboard_snapshot_sync(self) -> PortfolioSnapshotView:
        """Thread-safe frozen portfolio view for dashboard / REST."""
        with self._cache_lock:
            if self._dashboard_cache is not None:
                return self._dashboard_cache
        return self._build_snapshot_view()

    def snapshot_sync(self) -> PortfolioSnapshotView:
        """Alias for :meth:`get_dashboard_snapshot_sync`."""
        return self.get_dashboard_snapshot_sync()
