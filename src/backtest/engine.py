"""Backtest engine for the Hyperliquid trading bot.

Uses the same strategy types as live trading (src.strategies.base).
Walks merged multi-symbol 1m candles chronologically, feeds MarketEvents
to a strategy (typically StrategyEnsemble), and simulates fills with fees
and slippage.

v3.1.19: backtest now uses the *real* RiskManager (drawdown circuit,
exposure caps, correlation rejection, ATR sizing), the volatility
circuit breaker, the funding-reset blackout, dynamic cooldown, and
size-aware slippage. The full set of gates that protect live trading
now also protect backtest results.
"""

from __future__ import annotations

import bisect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.backtest.metrics import calculate_metrics
from src.core.funding_blackout import FundingBlackoutFilter
from src.core.kelly_sizer import KellySizer
from src.core.regime import apply_regime_weights, regime_strategy_name
from src.core.risk_manager import RiskManager
from src.core.tca import passes_tca_check
from src.core.volatility_circuit import VolatilityCircuitBreaker
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
    cooldown_base_ms: int = 30 * 60_000   # v3.1.19: was 1h, now 30m (match live)
    cooldown_max_ms: int = 120 * 60_000   # v3.1.19: 2h max
    cooldown_multiplier: float = 2.0      # v3.1.19: double per consecutive loss
    max_daily_trades: int = 5
    # v3.1.19: live parity toggles — the new gates can be disabled per
    # backtest run (e.g. when sweeping parameters and you want to see
    # the unfiltered signal set).
    use_risk_manager: bool = True
    use_volatility_circuit: bool = True
    use_funding_blackout: bool = True
    use_size_aware_slippage: bool = True
    # Maker-vs-taker fee model (v3.1.19)
    maker_fee_pct: float = 0.01          # 0.01% per side
    use_maker_for_strategies: Tuple[str, ...] = (
        "OrderBookScalper", "VWAPDeviation", "DonchianBreakout",
        "VolatilityBreakout", "CVDOrderFlow",
    )


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
    # v3.1.19: accumulated funding cost (USD) over the life of the
    # position. Deducted from realised PnL on close.
    funding_paid: float = 0.0
    # Last funding settlement timestamp (next settlement = this + 1h).
    next_funding_ts: int = 0


class _BacktestPortfolioProxy:
    """Minimal PortfolioState stand-in for RiskManager.can_enter.

    The real RiskManager only reads ``positions``, ``daily_trades``,
    ``daily_pnl``, ``current_capital`` and ``get_max_drawdown()`` —
    we provide exactly that surface.
    """

    def __init__(self, engine: "BacktestEngine") -> None:
        self._engine = engine

    @property
    def positions(self) -> Dict[str, Position]:
        out: Dict[str, Position] = {}
        for pos_id, pos in self._engine.positions.items():
            out[pos.symbol] = Position(
                symbol=pos.symbol,
                side=pos.side,
                entry_price=pos.entry_price,
                size=pos.size,
                entry_time_ms=pos.entry_time_ms,
                stop_loss_price=pos.stop_loss_price,
                take_profit_price=pos.take_profit_price,
                metadata=pos.metadata,
            )
        return out

    @property
    def daily_trades(self) -> int:
        if self._engine._current_day is None:
            return 0
        return self._engine._daily_trade_count.get(self._engine._current_day, 0)

    @property
    def daily_pnl(self) -> float:
        return self._engine._daily_pnl

    @property
    def current_capital(self) -> float:
        return self._engine._capital

    def get_max_drawdown(self) -> float:
        return self._engine._max_drawdown_pct


class BacktestEngine:
    """Walks chronologically through DB data and simulates strategy execution."""

    def __init__(
        self,
        database: Database,
        strategy: Strategy,
        config: Optional[BacktestConfig] = None,
        symbols: Optional[List[str]] = None,
        risk_config: Optional[Dict[str, Any]] = None,
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
        self._consecutive_losses: Dict[str, int] = {}
        self._daily_trade_count: Dict[str, int] = {}
        self._current_day: Optional[str] = None
        self._daily_pnl: float = 0.0
        self._capital: float = float(self.cfg.initial_capital)
        self._peak_capital: float = float(self.cfg.initial_capital)
        self._max_drawdown_pct: float = 0.0
        self._kelly = KellySizer(min_trades=20, half_kelly=True)

        # v3.1.19: real RiskManager (drawdown circuit, exposure caps,
        # correlation, max-positions, daily loss, etc.). db=None is OK
        # because backtest never calls into DB-bound helpers.
        self._risk_manager: Optional[RiskManager] = None
        if self.cfg.use_risk_manager:
            try:
                self._risk_manager = RiskManager(risk_config or {}, db=None)
                logger.info("Backtest: real RiskManager enabled")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Backtest: failed to build RiskManager (%s) — falling back to "
                    "local checks only", exc,
                )
                self._risk_manager = None

        # v3.1.19: intraday volatility circuit breaker (soft gate).
        self._vol_circuit: Optional[VolatilityCircuitBreaker] = None
        if self.cfg.use_volatility_circuit and risk_config is not None:
            try:
                vol_cfg = risk_config.get("volatility_circuit_breaker", {}) or {}
                self._vol_circuit = VolatilityCircuitBreaker.from_config_dict(vol_cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Backtest: vol_circuit init failed: %s", exc)

        # v3.1.19: time-of-day funding-reset blackout.
        self._funding_blackout: Optional[FundingBlackoutFilter] = None
        if self.cfg.use_funding_blackout and risk_config is not None:
            try:
                fb_cfg = risk_config.get("funding_blackout", {}) or {}
                self._funding_blackout = FundingBlackoutFilter.from_config_dict(fb_cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Backtest: funding_blackout init failed: %s", exc)

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
        self._capital = capital
        last_snapshot_ts = 0

        for idx, (ts, symbol, c1m) in enumerate(timeline):
            data = symbol_data[symbol]
            event = self._build_market_event(symbol, ts, c1m, data)

            # v3.1.19: apply hourly funding to every open position whose
            # settlement boundary has been crossed since the last bar.
            capital = self._settle_funding(event, capital, ts)

            capital = self._process_exits(event, capital)

            if (
                symbol not in self.positions_by_symbol
                and len(self.positions) < self.cfg.max_positions
            ):
                # v3.1.19: feed 1h ATR to the volatility circuit breaker
                # (soft gate). Falls through silently if disabled.
                if self._vol_circuit is not None and event.candle_1h is not None:
                    atr = self._atr_pct(event.candle_1h)
                    if atr is not None:
                        self._vol_circuit.update(symbol, atr, ts)

                # v3.1.19: time-of-day funding-reset blackout.
                if (
                    self._funding_blackout is not None
                    and self._funding_blackout.is_blocked(ts)
                ):
                    continue

                signal = self.strategy.on_data(event)
                if signal is not None:
                    signal = self._apply_live_parity(signal, event, ts)
                    if signal is None:
                        continue

                    # v3.1.19: real RiskManager gate.
                    if self._risk_manager is not None:
                        ok, reason = self._risk_manager.can_enter(
                            signal, _BacktestPortfolioProxy(self),
                        )
                        if not ok:
                            logger.debug(
                                "Backtest: %s %s rejected by risk gate: %s",
                                signal.symbol, signal.side, reason,
                            )
                            continue

                    # v3.1.19: volatility circuit breaker check.
                    if (
                        self._vol_circuit is not None
                        and self._vol_circuit.is_blocked(symbol, ts)
                    ):
                        logger.debug(
                            "Backtest: %s rejected by vol circuit (block_remaining_sec=%d)",
                            symbol, self._vol_circuit.block_remaining_sec(symbol, ts),
                        )
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
                    self._capital = capital
                    day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
                    self._daily_trade_count[day] = self._daily_trade_count.get(day, 0) + 1

            if ts - last_snapshot_ts >= 3_600_000 or idx == len(timeline) - 1:
                open_pnl = self._unrealised_pnl(event.price, symbol)
                self.equity_curve.append((ts, capital + open_pnl))
                last_snapshot_ts = ts
                # v3.1.19: track peak / drawdown
                total_equity = capital + open_pnl
                if total_equity > self._peak_capital:
                    self._peak_capital = total_equity
                dd = (self._peak_capital - total_equity) / self._peak_capital if self._peak_capital > 0 else 0.0
                if dd > self._max_drawdown_pct:
                    self._max_drawdown_pct = dd

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
            self._daily_pnl = 0.0  # v3.1.19: reset daily PnL on day roll

        if (
            self.cfg.max_daily_trades > 0
            and self._daily_trade_count.get(day, 0) >= self.cfg.max_daily_trades
        ):
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
                # v3.1.19: dynamic doubling on consecutive losses,
                # matches the live cooldown governor. Reset on win.
                consecutive = self._consecutive_losses.get(key, 0) + 1
                self._consecutive_losses[key] = consecutive
                cooldown_ms = min(
                    self.cfg.cooldown_base_ms
                    * (self.cfg.cooldown_multiplier ** (consecutive - 1)),
                    self.cfg.cooldown_max_ms,
                )
                self._cooldown_state[key] = ts + int(cooldown_ms)
            else:
                self._cooldown_state.pop(key, None)
                self._consecutive_losses.pop(key, None)
        if self._kelly:
            self._kelly.record_trade(pnl_pct)

    # ------------------------------------------------------------------
    # v3.1.19: live-parity helpers
    # ------------------------------------------------------------------

    def _atr_pct(self, candle: Any) -> Optional[float]:
        """ATR(1) proxy from a single candle: range / close.

        Sufficient for the volatility circuit breaker; no need for a
        full 14-bar ATR over the 1h series in backtest.
        """
        try:
            close = float(candle.close)
            high = float(candle.high)
            low = float(candle.low)
        except (TypeError, ValueError, AttributeError):
            return None
        if close <= 0.0 or high < low:
            return None
        return (high - low) / close

    def _settle_funding(
        self,
        event: MarketEvent,
        capital: float,
        ts: int,
    ) -> float:
        """Apply hourly funding to every open position whose 1h boundary
        has been crossed since the last bar.

        Longs pay when funding_rate > 0; shorts receive. The cumulative
        cost is stored on the position's ``funding_paid`` field and
        deducted from realised PnL on close.
        """
        if not self.cfg.use_funding:
            return capital
        for pos in self.positions.values():
            if pos.next_funding_ts == 0:
                # First settlement 1h after entry.
                pos.next_funding_ts = pos.entry_time_ms + 3_600_000
                continue
            if ts < pos.next_funding_ts:
                continue
            # Look up the latest funding rate for this symbol.
            rate = event.predicted_funding or event.funding
            if rate is None:
                # No data — advance the clock and skip.
                pos.next_funding_ts += 3_600_000
                continue
            notional = pos.entry_price * pos.size
            if pos.side == "long":
                cashflow = -notional * float(rate)   # longs pay when rate>0
            else:
                cashflow = notional * float(rate)    # shorts receive when rate>0
            pos.funding_paid += cashflow
            capital += cashflow
            self._daily_pnl += cashflow
            pos.next_funding_ts += 3_600_000
        return capital

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

        # size_pct is a fraction of capital (0.01 = 1%)
        notional = capital * safe_float(signal.size_pct, 0.0)
        if notional <= 0 or notional > capital:
            return capital

        entry_price = self._apply_slippage(
            price, signal.side, "entry", order_size_usd=notional,
        )
        if entry_price <= 0.0:
            return capital

        # v3.1.19: maker vs taker fee based on strategy routing
        attr_strategy = regime_strategy_name(signal)
        if attr_strategy in self.cfg.use_maker_for_strategies:
            fee_rate = self.cfg.maker_fee_pct / 100.0
        else:
            fee_rate = self.cfg.commission_pct / 100.0
        entry_commission = notional * fee_rate
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
            next_funding_ts=ts + 3_600_000,  # v3.1.19: first settlement 1h after entry
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

        entry_notional = pos.entry_price * pos.size
        exit_notional_now = price * pos.size  # raw notional for size-aware slippage
        exit_price = self._apply_slippage(
            price, pos.side, "exit", order_size_usd=exit_notional_now,
        )
        exit_notional = exit_price * pos.size

        # v3.1.19: maker vs taker fee based on strategy routing
        attr_strategy = (pos.metadata or {}).get("original_strategy") or pos.strategy
        if attr_strategy in self.cfg.use_maker_for_strategies:
            fee_rate = self.cfg.maker_fee_pct / 100.0
        else:
            fee_rate = self.cfg.commission_pct / 100.0
        total_fees = (entry_notional + exit_notional) * fee_rate

        if pos.side == "long":
            gross_pnl = (exit_price - pos.entry_price) * pos.size
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.size

        # v3.1.19: include accumulated funding cost in realised PnL.
        net_pnl = gross_pnl - total_fees + pos.funding_paid
        pnl_pct = safe_divide(net_pnl, entry_notional, 0.0)
        capital += net_pnl
        self._daily_pnl += net_pnl
        self._note_trade_closed(pos, ts, pnl_pct)
        self._capital = capital

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
            "funding_paid": round(pos.funding_paid, 4),
            "fees_paid": round(total_fees, 4),
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

    def _apply_slippage(
        self,
        price: float,
        side: str,
        direction: str,
        order_size_usd: float = 0.0,
    ) -> float:
        """Apply slippage to a fill price.

        v3.1.19: size-aware scaling. The base ``slippage_bps`` is the
        cost of a "typical" order (1% of initial capital). Larger orders
        scale by ``sqrt(order_size / typical)`` — square-root impact
        model is a reasonable approximation for crypto L2 books where
        impact grows slower than linearly.
        """
        bps = self.cfg.slippage_bps
        if self.cfg.use_size_aware_slippage and order_size_usd > 0:
            typical = float(self.cfg.initial_capital) * 0.01
            if typical > 0:
                size_mult = max(1.0, (order_size_usd / typical) ** 0.5)
                bps = bps * size_mult
        bps_frac = bps / 10_000.0
        if side == "long":
            if direction == "entry":
                return price * (1.0 + bps_frac)
            return price * (1.0 - bps_frac)
        if direction == "entry":
            return price * (1.0 - bps_frac)
        return price * (1.0 + bps_frac)
