"""Backtest engine for the Hyperliquid trading bot.

Loads historical data from the local SQLite DB, walks through it
chronologically, feeds events to a strategy, simulates fills with
configurable slippage, and tracks portfolio state.

Output:
    equity_curve : List[Tuple[int, float]]   # (timestamp_ms, capital)
    trades       : List[Dict[str, Any]]      # closed trades
    metrics      : Dict[str, float]          # via metrics.py
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# Local imports
from src.data.database import Candle, Database
from src.backtest.metrics import calculate_metrics


# ---------------------------------------------------------------------------
# Dataclasses used inside the backtest engine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketEvent:
    """Snapshot of market state delivered to a strategy each step."""
    symbol: str
    price: float
    timestamp_ms: int
    candle_1m: Optional[Candle] = None
    candle_5m: Optional[Candle] = None
    candle_15m: Optional[Candle] = None
    funding: Optional[float] = None
    predicted_funding: Optional[float] = None
    oi_total: Optional[float] = None
    oi_delta: Optional[float] = None
    volume_1m: Optional[float] = None
    bid_ask_imbalance: Optional[float] = None
    vwap_15m: Optional[float] = None


@dataclass(frozen=True)
class Signal:
    """Entry signal produced by a strategy."""
    strategy: str
    symbol: str
    side: str                 # 'long' | 'short'
    confidence: float         # 0.0 – 1.0
    size_pct: float           # % of capital to deploy
    entry_price: Optional[float] = None   # None = market order
    stop_loss_pct: float = 0.0
    take_profit_pct: Optional[float] = None
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    """Simulated open position tracked by the engine."""
    id: int
    strategy: str
    symbol: str
    side: str                  # 'long' | 'short'
    entry_price: float
    entry_time: int
    size: float                # absolute notional size
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    unrealised_pnl: float = 0.0
    max_price_seen: float = 0.0
    min_price_seen: float = 0.0

    def update(self, current_price: float) -> None:
        """Recalculate unrealised PnL and tracked extremes."""
        if self.side == "long":
            self.unrealised_pnl = (current_price - self.entry_price) * self.size
            if current_price > self.max_price_seen:
                self.max_price_seen = current_price
            if current_price < self.min_price_seen or self.min_price_seen == 0:
                self.min_price_seen = current_price
        else:
            self.unrealised_pnl = (self.entry_price - current_price) * self.size
            if current_price < self.min_price_seen or self.min_price_seen == 0:
                self.min_price_seen = current_price
            if current_price > self.max_price_seen:
                self.max_price_seen = current_price


@dataclass(frozen=True)
class ExitSignal:
    """Exit instruction produced by a strategy."""
    position_id: int
    reason: str
    price: Optional[float] = None   # None = market exit


# ---------------------------------------------------------------------------
# Strategy interface (mirrors the architecture contract)
# ---------------------------------------------------------------------------

class Strategy(ABC):
    """Abstract base class all back-testable strategies must implement."""

    @abstractmethod
    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        """Called on every new market event. Return a Signal to enter, or None."""
        ...

    @abstractmethod
    def on_position(self, position: Position) -> Optional[ExitSignal]:
        """Called on every event while *position* is open. Return ExitSignal to close, or None."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...


# ---------------------------------------------------------------------------
# Configuration dataclass for the engine
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    """Hyperparameters that govern simulation fidelity."""
    initial_capital: float = 100_000.0
    commission_pct: float = 0.04         # 4 bps per side (Hyperliquid approx)
    slippage_bps: float = 2.0            # 2 bps slippage on fill
    max_positions: int = 5               # max concurrent open trades
    per_trade_risk_pct: float = 1.0      # default % of capital at risk per trade
    use_funding: bool = True             # whether to load funding from DB
    use_oi: bool = True                  # whether to load OI from DB


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Walks chronologically through DB data and simulates strategy execution."""

    def __init__(
        self,
        database: Database,
        strategy: Strategy,
        config: Optional[BacktestConfig] = None,
    ) -> None:
        self.db = database
        self.strategy = strategy
        self.cfg = config or BacktestConfig()
        self.positions: Dict[int, Position] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.equity_curve: List[Tuple[int, float]] = []
        self._next_position_id = 1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        symbol: str,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute the backtest over the requested date range.

        Returns a dict with keys:
            equity_curve, trades, metrics, capital, total_return
        """
        # 1. Load 1m candles (base timeline)
        candles = self.db.get_candles(symbol, "1m", limit=100_000, start_ms=start_ms, end_ms=end_ms)
        if not candles:
            raise ValueError(f"No 1m candles found for {symbol} in the requested range")

        # 2. Load funding + OI aligned to the same range
        funding_map = self._load_funding(symbol, start_ms, end_ms) if self.cfg.use_funding else {}
        oi_map = self._load_oi(symbol, start_ms, end_ms) if self.cfg.use_oi else {}

        # 3. Pre-build higher-timeframe candle lookup dicts for speed
        candles_5m = self._dict_candles(self.db.get_candles(symbol, "5m", limit=100_000, start_ms=start_ms, end_ms=end_ms))
        candles_15m = self._dict_candles(self.db.get_candles(symbol, "15m", limit=100_000, start_ms=start_ms, end_ms=end_ms))
        candles_1h = self._dict_candles(self.db.get_candles(symbol, "1h", limit=100_000, start_ms=start_ms, end_ms=end_ms))

        capital = self.cfg.initial_capital
        daily_pnl = 0.0
        last_snapshot_ts = 0

        # 4. Main event loop
        for i, c1m in enumerate(candles):
            ts = c1m.timestamp_ms
            price = c1m.close

            # -- build MarketEvent --
            event = MarketEvent(
                symbol=symbol,
                price=price,
                timestamp_ms=ts,
                candle_1m=c1m,
                candle_5m=candles_5m.get(ts),
                candle_15m=candles_15m.get(ts),
                funding=funding_map.get(ts),
                predicted_funding=None,   # not stored per-tick yet; could extend schema
                oi_total=oi_map.get(ts, (None, None))[0],
                oi_delta=oi_map.get(ts, (None, None))[1],
                volume_1m=c1m.volume,
                bid_ask_imbalance=None,
                vwap_15m=None,
            )

            # -- update open positions --
            self._update_positions(price)

            # -- check exits first (FIFO) --
            exits = self._check_exits(event)
            for pos_id, exit_price, reason in exits:
                capital = self._close_position(pos_id, exit_price, ts, reason, capital)
                daily_pnl += self.closed_trades[-1]["pnl_usd"]

            # -- check entry signals (respect max_positions) --
            if len(self.positions) < self.cfg.max_positions:
                signal = self.strategy.on_data(event)
                if signal is not None:
                    capital = self._open_position(signal, price, ts, capital)

            # -- equity snapshot (once per hour to save memory) --
            if ts - last_snapshot_ts >= 3_600_000 or i == len(candles) - 1:
                open_pnl = sum(p.unrealised_pnl for p in self.positions.values())
                self.equity_curve.append((ts, capital + open_pnl))
                last_snapshot_ts = ts

        # 5. Force-close any remaining open positions at last price
        if self.positions:
            last_price = candles[-1].close
            last_ts = candles[-1].timestamp_ms
            for pos_id in list(self.positions.keys()):
                capital = self._close_position(pos_id, last_price, last_ts, "force_close_eod", capital)

        # 6. Metrics
        metrics = calculate_metrics(self.equity_curve, self.closed_trades)
        metrics["total_return"] = (capital - self.cfg.initial_capital) / self.cfg.initial_capital

        return {
            "equity_curve": self.equity_curve,
            "trades": self.closed_trades,
            "metrics": metrics,
            "capital": capital,
            "total_return": metrics["total_return"],
        }

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    def _load_funding(
        self,
        symbol: str,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> Dict[int, float]:
        """Return {timestamp_ms: current_funding} from DB."""
        rows = self.db.get_funding_history(symbol, limit=100_000)
        out: Dict[int, float] = {}
        for r in rows:
            ts = int(r["timestamp"])
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts > end_ms:
                continue
            out[ts] = float(r["current"])
        return out

    def _load_oi(
        self,
        symbol: str,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> Dict[int, Tuple[float, float]]:
        """Return {timestamp_ms: (oi_total, oi_delta)} from DB."""
        rows = self.db.get_oi_history(symbol, limit=100_000)
        out: Dict[int, Tuple[float, float]] = {}
        for r in rows:
            ts = int(r["timestamp"])
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts > end_ms:
                continue
            out[ts] = (float(r["oi_total"]), float(r["oi_delta"]))
        return out

    @staticmethod
    def _dict_candles(candles: List[Candle]) -> Dict[int, Candle]:
        """Build a timestamp→Candle lookup dict."""
        return {c.timestamp_ms: c for c in candles}

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _open_position(
        self,
        signal: Signal,
        price: float,
        ts: int,
        capital: float,
    ) -> float:
        """Simulate opening a new position and deduct capital."""
        entry_price = self._apply_slippage(price, signal.side, "entry")
        notional = capital * (signal.size_pct / 100.0)
        if notional <= 0 or notional > capital:
            return capital

        # commission on entry
        commission = notional * (self.cfg.commission_pct / 100.0)
        capital -= commission

        stop = None
        if signal.stop_loss_pct > 0:
            if signal.side == "long":
                stop = entry_price * (1 - signal.stop_loss_pct / 100.0)
            else:
                stop = entry_price * (1 + signal.stop_loss_pct / 100.0)

        tp = None
        if signal.take_profit_pct is not None and signal.take_profit_pct > 0:
            if signal.side == "long":
                tp = entry_price * (1 + signal.take_profit_pct / 100.0)
            else:
                tp = entry_price * (1 - signal.take_profit_pct / 100.0)

        pos = Position(
            id=self._next_position_id,
            strategy=signal.strategy,
            symbol=signal.symbol,
            side=signal.side,
            entry_price=entry_price,
            entry_time=ts,
            size=notional / entry_price,   # size in base asset units
            stop_loss_price=stop,
            take_profit_price=tp,
            max_price_seen=entry_price,
            min_price_seen=entry_price,
        )
        self.positions[pos.id] = pos
        self._next_position_id += 1
        return capital

    def _close_position(
        self,
        pos_id: int,
        price: float,
        ts: int,
        reason: str,
        capital: float,
    ) -> float:
        """Simulate closing a position, realise PnL, return updated capital."""
        pos = self.positions.pop(pos_id, None)
        if pos is None:
            return capital

        exit_price = self._apply_slippage(price, pos.side, "exit")

        if pos.side == "long":
            gross_pnl = (exit_price - pos.entry_price) * pos.size
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.size

        notional = pos.size * exit_price
        commission = notional * (self.cfg.commission_pct / 100.0)
        net_pnl = gross_pnl - commission
        capital += (pos.size * exit_price) + net_pnl - (pos.size * pos.entry_price)
        # Actually simpler: capital = old capital + entry_notional + pnl
        # But we already deducted entry notional from capital on open,
        # so we add back: exit_notional + pnl
        # Re-calculate properly:
        entry_notional = pos.size * pos.entry_price
        exit_notional = pos.size * exit_price
        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price if pos.side == "long" else (pos.entry_price - exit_price) / pos.entry_price
        net_pnl = (exit_notional - entry_notional) if pos.side == "long" else (entry_notional - exit_notional)
        net_pnl -= commission
        capital = capital + entry_notional + net_pnl

        self.closed_trades.append({
            "id": pos.id,
            "strategy": pos.strategy,
            "symbol": pos.symbol,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "entry_time": pos.entry_time,
            "exit_time": ts,
            "size": pos.size,
            "pnl_usd": round(net_pnl, 4),
            "pnl_pct": round(pnl_pct * 100, 4),
            "exit_reason": reason,
            "max_price_seen": pos.max_price_seen,
            "min_price_seen": pos.min_price_seen,
        })
        return capital

    def _update_positions(self, current_price: float) -> None:
        for pos in self.positions.values():
            pos.update(current_price)

    def _check_exits(self, event: MarketEvent) -> List[Tuple[int, float, str]]:
        """Return list of (position_id, exit_price, reason) for positions that should close now."""
        exits: List[Tuple[int, float, str]] = []
        price = event.price
        for pos in list(self.positions.values()):
            # 1. Strategy discretionary exit
            exit_sig = self.strategy.on_position(pos)
            if exit_sig is not None:
                exits.append((pos.id, price, exit_sig.reason))
                continue

            # 2. Stop loss
            if pos.stop_loss_price is not None:
                if pos.side == "long" and price <= pos.stop_loss_price:
                    exits.append((pos.id, price, "stop_loss"))
                    continue
                if pos.side == "short" and price >= pos.stop_loss_price:
                    exits.append((pos.id, price, "stop_loss"))
                    continue

            # 3. Take profit
            if pos.take_profit_price is not None:
                if pos.side == "long" and price >= pos.take_profit_price:
                    exits.append((pos.id, price, "take_profit"))
                    continue
                if pos.side == "short" and price <= pos.take_profit_price:
                    exits.append((pos.id, price, "take_profit"))
                    continue

        return exits

    def _apply_slippage(self, price: float, side: str, direction: str) -> float:
        """Apply configurable slippage to a fill price.

        *direction* is 'entry' or 'exit'.  Slippage always works against the
        trader: worse fill price.
        """
        bps = self.cfg.slippage_bps / 10_000.0
        if side == "long":
            if direction == "entry":
                return price * (1 + bps)
            else:
                return price * (1 - bps)
        else:  # short
            if direction == "entry":
                return price * (1 - bps)
            else:
                return price * (1 + bps)
