"""Backtest engine for the Hyperliquid trading bot.

Uses the same strategy types as live trading (src.strategies.base).
Walks merged multi-symbol 1m candles chronologically, feeds MarketEvents
to a strategy (typically StrategyEnsemble), and simulates fills with fees
and slippage.
"""

from __future__ import annotations

import bisect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.backtest.metrics import calculate_metrics
from src.core.kelly_sizer import KellySizer
from src.core.regime import apply_regime_weights, regime_strategy_name
from src.core.tca import passes_tca_check
from src.data.database import Candle as DBCandle
from src.data.database import Database
from src.strategies.base import MarketEvent, Position, Signal, Strategy
from src.strategies.indicators import Candle, calculate_adx
from src.utils.helpers import safe_divide, safe_float

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Hyperparameters that govern simulation fidelity."""
    initial_capital: float = 100_000.0
    commission_pct: float = 0.035       # taker fee per side (%)
    slippage_bps: float = 2.0           # slippage in basis points per fill
    max_positions: int = 5
    per_trade_risk_pct: float = 1.0
    use_funding: bool = True
    use_oi: bool = True
    tca_enabled: bool = True
    min_edge_buffer_pct: float = 0.05   # percent
    paper_slippage_pct: float = 0.05    # percent per side (for TCA when no L2)
    use_regime_weights: bool = True
    use_cooldown: bool = True
    use_kelly: bool = True
    use_microstructure_proxy: bool = True
    regime_weights: Dict[str, Dict[str, float]] = field(default_factory=dict)
    adx_trend_threshold: float = 25.0
    adx_range_threshold: float = 20.0
    cooldown_base_ms: int = 3_600_000
    max_daily_trades: int = 5


@dataclass
class _OpenPosition:
    """Internal backtest position tracker."""
    id: int
    strategy: str
    symbol: str
    side: str
    entry_price: float
    entry_time_ms: int
    size: float
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class BacktestEngine:
    """Walks chronologically through DB data and simulates strategy execution."""

    def __init__(
        self,
        database: Database,
        strategy: Strategy,
        config: Optional[BacktestConfig] = None,
        symbols: Optional[List[str]] = None,
    ) -> None:
        self.db = database
        self.strategy = strategy
        self.cfg = config or BacktestConfig()
        self.symbols = list(symbols or ["BTC", "ETH", "SOL"])
        self.positions: Dict[int, _OpenPosition] = {}
        self.positions_by_symbol: Dict[str, int] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.equity_curve: List[Tuple[int, float]] = []
        self._next_position_id = 1
        self._cooldown_state: Dict[str, int] = {}
        self._daily_trade_count: Dict[str, int] = {}
        self._current_day: Optional[str] = None
        self._kelly = KellySizer(min_trades=20, half_kelly=True)

    def run(
        self,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute backtest over all configured symbols.

        Returns dict with keys: equity_curve, trades, metrics, capital, total_return.
        """
        timeline = self._build_timeline(start_ms, end_ms)
        if not timeline:
            raise ValueError(
                f"No 1m candles found for {self.symbols} in the requested range"
            )

        symbol_data = {
            sym: self._load_symbol_data(sym, start_ms, end_ms)
            for sym in self.symbols
        }

        capital = self.cfg.initial_capital
        last_snapshot_ts = 0

        for idx, (ts, symbol, c1m) in enumerate(timeline):
            data = symbol_data[symbol]
            event = self._build_market_event(symbol, ts, c1m, data)

            capital = self._process_exits(event, capital)

            if (
                symbol not in self.positions_by_symbol
                and len(self.positions) < self.cfg.max_positions
            ):
                signal = self.strategy.on_data(event)
                if signal is not None:
                    signal = self._apply_live_parity(signal, event, ts)
                    if signal is None:
                        continue
                    if self.cfg.tca_enabled:
                        fee_frac = self.cfg.commission_pct / 100.0
                        slip_frac = self.cfg.paper_slippage_pct / 100.0
                        buffer_frac = self.cfg.min_edge_buffer_pct / 100.0
                        ok, _ = passes_tca_check(
                            signal, fee_frac, slip_frac, buffer_frac,
                        )
                        if not ok:
                            continue
                    capital = self._open_position(signal, event.price, ts, capital)
                    day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
                    self._daily_trade_count[day] = self._daily_trade_count.get(day, 0) + 1

            if ts - last_snapshot_ts >= 3_600_000 or idx == len(timeline) - 1:
                open_pnl = self._unrealised_pnl(event.price, symbol)
                self.equity_curve.append((ts, capital + open_pnl))
                last_snapshot_ts = ts

        if timeline:
            last_ts, last_sym, last_c1m = timeline[-1]
            last_price = last_c1m.close
            for pos_id in list(self.positions.keys()):
                capital = self._close_position(
                    pos_id, last_price, last_ts, "force_close_eod", capital
                )

        metrics = calculate_metrics(self.equity_curve, self.closed_trades)
        metrics["total_return"] = safe_divide(
            capital - self.cfg.initial_capital,
            self.cfg.initial_capital,
            0.0,
        )

        return {
            "equity_curve": self.equity_curve,
            "trades": self.closed_trades,
            "metrics": metrics,
            "capital": capital,
            "total_return": metrics["total_return"],
        }

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _build_timeline(
        self,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> List[Tuple[int, str, DBCandle]]:
        timeline: List[Tuple[int, str, DBCandle]] = []
        for symbol in self.symbols:
            candles = self.db.get_candles(
                symbol, "1m", limit=500_000, start_ms=start_ms, end_ms=end_ms
            )
            for c in candles:
                timeline.append((c.timestamp_ms, symbol, c))
        timeline.sort(key=lambda row: row[0])
        return timeline

    def _load_symbol_data(
        self,
        symbol: str,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> Dict[str, Any]:
        return {
            "candles_5m": self.db.get_candles(
                symbol, "5m", limit=500_000, start_ms=start_ms, end_ms=end_ms
            ),
            "candles_15m": self.db.get_candles(
                symbol, "15m", limit=500_000, start_ms=start_ms, end_ms=end_ms
            ),
            "candles_1h": self.db.get_candles(
                symbol, "1h", limit=500_000, start_ms=start_ms, end_ms=end_ms
            ),
            "funding_ts": self._load_funding_series(symbol, start_ms, end_ms),
            "oi_ts": self._load_oi_series(symbol, start_ms, end_ms),
            "candles_15m_ind": [],
            "hist_15m": [],
        }

    def _load_funding_series(
        self,
        symbol: str,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> List[Tuple[int, float]]:
        if not self.cfg.use_funding:
            return []
        rows = self.db.get_funding_history(symbol, limit=500_000)
        out: List[Tuple[int, float]] = []
        for r in rows:
            ts = int(r["timestamp"])
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts > end_ms:
                continue
            out.append((ts, float(r["current"])))
        out.sort(key=lambda x: x[0])
        return out

    def _load_oi_series(
        self,
        symbol: str,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> List[Tuple[int, float, float]]:
        if not self.cfg.use_oi:
            return []
        rows = self.db.get_oi_history(symbol, limit=500_000)
        out: List[Tuple[int, float, float]] = []
        for r in rows:
            ts = int(r["timestamp"])
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts > end_ms:
                continue
            out.append((ts, float(r["oi_total"]), float(r["oi_delta"])))
        out.sort(key=lambda x: x[0])
        return out

    @staticmethod
    def _lookup_at_or_before(
        series: List[Tuple[int, Any, ...]],
        ts: int,
    ) -> Optional[Tuple[int, Any, ...]]:
        """Return the last series entry with timestamp <= ts."""
        if not series:
            return None
        keys = [row[0] for row in series]
        idx = bisect.bisect_right(keys, ts) - 1
        if idx < 0:
            return None
        return series[idx]

    @staticmethod
    def _lookup_candle_at_or_before(
        candles: List[DBCandle],
        ts: int,
    ) -> Optional[DBCandle]:
        if not candles:
            return None
        keys = [c.timestamp_ms for c in candles]
        idx = bisect.bisect_right(keys, ts) - 1
        if idx < 0:
            return None
        return candles[idx]

    @staticmethod
    def _to_indicator_candle(c: Optional[DBCandle]) -> Optional[Candle]:
        if c is None:
            return None
        return Candle(
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            volume=c.volume,
            timestamp_ms=c.timestamp_ms,
            open_interest=c.oi_total,
        )

    def _build_market_event(
        self,
        symbol: str,
        ts: int,
        c1m: DBCandle,
        data: Dict[str, Any],
    ) -> MarketEvent:
        c5 = self._lookup_candle_at_or_before(data["candles_5m"], ts)
        c15 = self._lookup_candle_at_or_before(data["candles_15m"], ts)
        c1h = self._lookup_candle_at_or_before(data["candles_1h"], ts)

        if c15 is not None:
            ind15 = self._to_indicator_candle(c15)
            hist: List[Candle] = data["hist_15m"]
            if not hist or hist[-1].timestamp_ms != ind15.timestamp_ms:
                hist.append(ind15)
                if len(hist) > 50:
                    data["hist_15m"] = hist[-50:]
                else:
                    data["hist_15m"] = hist

        funding_row = self._lookup_at_or_before(data["funding_ts"], ts)
        oi_row = self._lookup_at_or_before(data["oi_ts"], ts)

        adx = None
        hist_15m = data.get("hist_15m", [])
        if len(hist_15m) >= 29:
            adx = calculate_adx(hist_15m, 14)

        spread_pct = None
        oir = None
        bid_ask_ratio = None
        if self.cfg.use_microstructure_proxy and c1m.close > 0:
            spread_pct = (c1m.high - c1m.low) / c1m.close
            if c1m.high > c1m.low:
                oir = ((c1m.close - c1m.low) - (c1m.high - c1m.close)) / (c1m.high - c1m.low)
                bid_ask_ratio = 1.0 + oir * 0.5

        return MarketEvent(
            symbol=symbol,
            price=c1m.close,
            timestamp_ms=ts,
            candle_1m=self._to_indicator_candle(c1m),
            candle_5m=self._to_indicator_candle(c5),
            candle_15m=self._to_indicator_candle(c15),
            candle_1h=self._to_indicator_candle(c1h),
            funding=funding_row[1] if funding_row else None,
            predicted_funding=funding_row[1] if funding_row else None,
            oi_total=oi_row[1] if oi_row else None,
            oi_delta=oi_row[2] if oi_row else None,
            volume_1m=c1m.volume,
            adx_14=adx,
            orderbook_spread_pct=spread_pct,
            orderbook_oir=oir,
            orderbook_bid_ask_ratio=bid_ask_ratio,
        )

    def _apply_live_parity(
        self,
        signal: Signal,
        event: MarketEvent,
        ts: int,
    ) -> Optional[Signal]:
        """Regime weights, cooldown, Kelly sizing, daily trade cap."""
        day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
        if self._current_day != day:
            self._current_day = day
            self._daily_trade_count = {}

        if self._daily_trade_count.get(day, 0) >= self.cfg.max_daily_trades:
            return None

        strat_key = regime_strategy_name(signal)
        if self.cfg.use_cooldown:
            until = self._cooldown_state.get(f"{strat_key}:{signal.symbol}", 0)
            if ts < until:
                return None

        adjusted = [signal]
        if self.cfg.use_regime_weights and self.cfg.regime_weights:
            adjusted = apply_regime_weights(
                adjusted,
                event.adx_14,
                self.cfg.regime_weights,
                self.cfg.adx_trend_threshold,
                self.cfg.adx_range_threshold,
            )
            if not adjusted:
                return None
            signal = adjusted[0]

        if self.cfg.use_kelly:
            mult = self._kelly.get_size_multiplier()
            signal = Signal(
                strategy=signal.strategy,
                symbol=signal.symbol,
                side=signal.side,
                confidence=signal.confidence,
                size_pct=min(signal.size_pct * mult, 0.20),
                entry_price=signal.entry_price,
                stop_loss_pct=signal.stop_loss_pct,
                take_profit_pct=signal.take_profit_pct,
                reason=signal.reason,
                metadata=signal.metadata,
            )

        return signal

    def _note_trade_closed(self, pos: _OpenPosition, ts: int, pnl_pct: float) -> None:
        if self.cfg.use_cooldown:
            strat = (pos.metadata or {}).get("original_strategy") or pos.strategy
            key = f"{strat}:{pos.symbol}"
            if pnl_pct < 0:
                prev = self._cooldown_state.get(key, 0)
                self._cooldown_state[key] = max(prev, ts + self.cfg.cooldown_base_ms)
            else:
                self._cooldown_state.pop(key, None)
        if self._kelly:
            self._kelly.record_trade(pnl_pct)

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
        if signal.symbol in self.positions_by_symbol:
            return capital

        entry_price = self._apply_slippage(price, signal.side, "entry")
        # size_pct is a fraction of capital (0.01 = 1%)
        notional = capital * safe_float(signal.size_pct, 0.0)
        if notional <= 0 or notional > capital:
            return capital

        entry_commission = notional * (self.cfg.commission_pct / 100.0)
        capital -= entry_commission

        stop_loss_pct = safe_float(signal.stop_loss_pct, 0.0)
        stop: Optional[float] = None
        if stop_loss_pct > 0:
            if signal.side == "long":
                stop = entry_price * (1.0 - stop_loss_pct)
            else:
                stop = entry_price * (1.0 + stop_loss_pct)

        tp: Optional[float] = None
        tp_pct = signal.take_profit_pct
        if tp_pct is not None and tp_pct > 0:
            if signal.side == "long":
                tp = entry_price * (1.0 + tp_pct)
            else:
                tp = entry_price * (1.0 - tp_pct)

        pos = _OpenPosition(
            id=self._next_position_id,
            strategy=signal.strategy,
            symbol=signal.symbol,
            side=signal.side,
            entry_price=entry_price,
            entry_time_ms=ts,
            size=notional / entry_price if entry_price > 0 else 0.0,
            stop_loss_price=stop,
            take_profit_price=tp,
            metadata=dict(signal.metadata or {}),
        )
        self.positions[pos.id] = pos
        self.positions_by_symbol[signal.symbol] = pos.id
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
        pos = self.positions.pop(pos_id, None)
        if pos is None:
            return capital
        self.positions_by_symbol.pop(pos.symbol, None)

        exit_price = self._apply_slippage(price, pos.side, "exit")
        entry_notional = pos.entry_price * pos.size
        exit_notional = exit_price * pos.size
        fee_rate = self.cfg.commission_pct / 100.0
        total_fees = (entry_notional + exit_notional) * fee_rate

        if pos.side == "long":
            gross_pnl = (exit_price - pos.entry_price) * pos.size
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.size

        net_pnl = gross_pnl - total_fees
        pnl_pct = safe_divide(net_pnl, entry_notional, 0.0)
        capital += net_pnl
        self._note_trade_closed(pos, ts, pnl_pct)

        attr_strategy = (pos.metadata or {}).get("original_strategy") or pos.strategy
        self.closed_trades.append({
            "id": pos.id,
            "strategy": pos.strategy,
            "sub_strategy": attr_strategy,
            "symbol": pos.symbol,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "entry_time": pos.entry_time_ms,
            "exit_time": ts,
            "size": pos.size,
            "pnl_usd": round(net_pnl, 4),
            "pnl_pct": round(pnl_pct * 100, 4),
            "exit_reason": reason,
        })
        return capital

    def _process_exits(self, event: MarketEvent, capital: float) -> float:
        pos_id = self.positions_by_symbol.get(event.symbol)
        if pos_id is None:
            return capital

        pos = self.positions.get(pos_id)
        if pos is None:
            return capital

        price = event.price

        bt_position = Position(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            size=pos.size,
            entry_time_ms=pos.entry_time_ms,
            stop_loss_price=pos.stop_loss_price,
            take_profit_price=pos.take_profit_price,
            metadata={
                **pos.metadata,
                "strategy": pos.strategy,
            },
        )
        exit_sig = self.strategy.on_position(bt_position, event)
        if exit_sig is not None:
            return self._close_position(pos_id, price, event.timestamp_ms, exit_sig.reason, capital)

        if pos.stop_loss_price is not None:
            if pos.side == "long" and price <= pos.stop_loss_price:
                return self._close_position(pos_id, price, event.timestamp_ms, "stop_loss", capital)
            if pos.side == "short" and price >= pos.stop_loss_price:
                return self._close_position(pos_id, price, event.timestamp_ms, "stop_loss", capital)

        if pos.take_profit_price is not None:
            if pos.side == "long" and price >= pos.take_profit_price:
                return self._close_position(pos_id, price, event.timestamp_ms, "take_profit", capital)
            if pos.side == "short" and price <= pos.take_profit_price:
                return self._close_position(pos_id, price, event.timestamp_ms, "take_profit", capital)

        return capital

    def _unrealised_pnl(self, current_price: float, symbol: str) -> float:
        pos_id = self.positions_by_symbol.get(symbol)
        if pos_id is None:
            return 0.0
        pos = self.positions.get(pos_id)
        if pos is None:
            return 0.0
        if pos.side == "long":
            return (current_price - pos.entry_price) * pos.size
        return (pos.entry_price - current_price) * pos.size

    def _apply_slippage(self, price: float, side: str, direction: str) -> float:
        bps = self.cfg.slippage_bps / 10_000.0
        if side == "long":
            if direction == "entry":
                return price * (1.0 + bps)
            return price * (1.0 - bps)
        if direction == "entry":
            return price * (1.0 - bps)
        return price * (1.0 + bps)
