"""Backtest engine for the Hyperliquid trading bot.

Uses the same strategy types as live trading (src.strategies.base).
Walks merged multi-symbol 1m candles chronologically, feeds MarketEvents
to a strategy (typically StrategyEnsemble), and simulates fills with fees
and slippage.

Phase 05: shared SignalPipeline gate ordering, RiskManager sizing,
maker/taker fees via order_router, intrabar stop/TP on 1m high/low, and
run manifests for reproducibility.
"""

from __future__ import annotations

import bisect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from src.backtest.metrics import calculate_metrics
from src.backtest.data_contract import (
    DataContractResult,
    assert_data_contract_or_raise,
    evaluate_data_contract,
)
from src.backtest.mfe_mae import (
    ExcursionTracker,
    build_price_index,
    compute_intrade_excursion_fields,
    enrich_trades_post_exit,
    strip_post_exit_from_trades,
)
from src.backtest.continuous_segments import DEFAULT_RESEARCH_GAP_MS, is_cross_gap, resolve_gap_ms
from src.backtest.replay_data_quality import ReplayDataQualityGate
from src.backtest.run_manifest import build_run_manifest
from src.backtest.symbol_rounding import round_position_size
from src.core.funding_blackout import FundingBlackoutFilter
from src.core.liquidation_accumulator import LiquidationAccumulator
from src.core.order_router import OrderSpec
from src.core.regime import regime_strategy_name
from src.core.risk_manager import RiskManager
from src.core.signal_pipeline import PipelineContext, SignalPipeline
from src.core.volatility_circuit import VolatilityCircuitBreaker
from src.data.database import Candle as DBCandle
from src.data.database import Database
from src.strategies.base import MarketEvent, Position, Signal, Strategy
from src.strategies.indicators import Candle, calculate_adx
from src.utils.config import Config, get_strategy_section, resolve_kelly_enabled
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
    use_microstructure_proxy: bool = True
    intrabar_conflict_policy: str = "pessimistic"  # pessimistic | optimistic
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
    use_external_feeds_replay: bool = True


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
    # Phase 07: excursion tracker key (stored separately on engine)
    excursion_id: int = 0


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
        risk_config: Optional[Union[Config, Dict[str, Any]]] = None,
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
        self._daily_trade_count: Dict[str, int] = {}
        self._current_day: Optional[str] = None
        self._daily_pnl: float = 0.0
        self._capital: float = float(self.cfg.initial_capital)
        self._peak_capital: float = float(self.cfg.initial_capital)
        self._max_drawdown_pct: float = 0.0
        self._pipeline_ctx = PipelineContext()
        self._full_config: Optional[Config] = None
        self._last_candle_close: Dict[str, float] = {}
        self._excursion_trackers: Dict[int, ExcursionTracker] = {}
        self._next_excursion_id = 1
        self._data_contract: Optional[DataContractResult] = None
        self._price_index: Dict[str, List[Tuple[int, float]]] = {}
        self._research_gap_ms = DEFAULT_RESEARCH_GAP_MS
        if risk_config is not None:
            self._full_config = (
                risk_config if isinstance(risk_config, Config) else Config(risk_config)
            )

        self._risk_manager: Optional[RiskManager] = None
        self._pipeline: Optional[SignalPipeline] = None
        self._vol_circuit: Optional[VolatilityCircuitBreaker] = None
        self._funding_blackout: Optional[FundingBlackoutFilter] = None

        if self.cfg.use_risk_manager and self._full_config is not None:
            try:
                self._risk_manager = RiskManager(self._full_config, db=None)
                logger.info("Backtest: real RiskManager enabled")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Backtest: failed to build RiskManager (%s) — falling back to "
                    "local checks only", exc,
                )
                self._risk_manager = None

        if self._full_config is not None and self._risk_manager is not None:
            if self.cfg.use_volatility_circuit:
                try:
                    vol_cfg = self._full_config.get("risk.volatility_circuit_breaker", {}) or {}
                    self._vol_circuit = VolatilityCircuitBreaker.from_config_dict(vol_cfg)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Backtest: vol_circuit init failed: %s", exc)
            if self.cfg.use_funding_blackout:
                try:
                    fb_cfg = self._full_config.get("risk.funding_blackout", {}) or {}
                    self._funding_blackout = FundingBlackoutFilter.from_config_dict(fb_cfg)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Backtest: funding_blackout init failed: %s", exc)
            self._pipeline = SignalPipeline(
                self._full_config,
                self._risk_manager,
                vol_circuit=self._vol_circuit,
                funding_blackout=self._funding_blackout,
                replay_quality=ReplayDataQualityGate.from_config(self._full_config),
                use_regime_weights=self.cfg.use_regime_weights,
                use_cooldown=self.cfg.use_cooldown,
                use_debounce=True,
                tca_enabled=self.cfg.tca_enabled,
                for_backtest=True,
            )

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

        if self._full_config is not None:
            from src.data.research_database import ResearchDatabase

            is_research_db = isinstance(self.db, ResearchDatabase)
            refuse_on_fail = is_research_db and bool(
                self._full_config.get("research.refuse_insufficient_feeds", True)
            )
            strict = bool(self._full_config.get("research.strict_mode", False))
            strat_names = self._active_strategy_names()
            self._data_contract = evaluate_data_contract(
                self.db,
                self.symbols,
                start_ms=start_ms,
                end_ms=end_ms,
                config=self._full_config,
                active_strategies=strat_names,
                refuse_on_fail=refuse_on_fail,
                strict_research=strict and is_research_db,
            )
            if refuse_on_fail:
                assert_data_contract_or_raise(self._data_contract)

        self._price_index = build_price_index(timeline)

        symbol_data = {
            sym: self._load_symbol_data(sym, start_ms, end_ms)
            for sym in self.symbols
        }

        self._pipeline_ctx.replay_audit = {
            sym: ReplayDataQualityGate.audit_symbol_window(
                sym,
                symbol_data[sym].get("candles_1m", []),
                start_ms,
                end_ms,
                funding_ts=symbol_data[sym].get("funding_ts"),
                oi_ts=symbol_data[sym].get("oi_ts"),
            )
            for sym in self.symbols
        }

        research_cfg = (self._full_config.get("research", {}) or {}) if self._full_config else {}
        research_gap_ms = resolve_gap_ms(
            "1m",
            gap_intervals=int(research_cfg.get("gap_intervals", 2)),
            gap_intervals_by_tf=research_cfg.get("gap_intervals_by_tf"),
            max_gap_ms=research_cfg.get("max_gap_ms"),
        )
        self._research_gap_ms = research_gap_ms

        capital = self.cfg.initial_capital
        self._capital = capital
        last_snapshot_ts = 0

        for idx, (ts, symbol, c1m) in enumerate(timeline):
            data = symbol_data[symbol]
            prev_bar = self._pipeline_ctx.last_bar_ts.get(symbol)
            if is_cross_gap(prev_bar, ts, max_gap_ms=self._research_gap_ms):
                capital = self._flatten_symbol_at_gap(symbol, ts, c1m.close, capital)
            if self.cfg.use_external_feeds_replay:
                self._advance_liquidation_replay(data, ts)
            event = self._build_market_event(symbol, ts, c1m, data)
            if self._pipeline is not None:
                funding_row = self._lookup_at_or_before(data.get("funding_ts", []), ts)
                if funding_row is not None:
                    self._pipeline_ctx.funding_ts_at[symbol] = int(funding_row[0])
                oi_row = self._lookup_at_or_before(data.get("oi_ts", []), ts)
                if oi_row is not None:
                    self._pipeline_ctx.oi_ts_at[symbol] = int(oi_row[0])

            # v3.1.19: apply hourly funding to every open position whose
            # settlement boundary has been crossed since the last bar.
            capital = self._settle_funding(event, capital, ts)

            self._update_excursions(symbol, c1m, ts)
            capital = self._process_exits(event, capital, c1m)

            if (
                symbol not in self.positions_by_symbol
                and len(self.positions) < self.cfg.max_positions
            ):
                if self._vol_circuit is not None and event.candle_1h is not None:
                    atr = self._atr_pct(event.candle_1h)
                    if atr is not None:
                        self._vol_circuit.update(symbol, atr, ts)

                self._roll_day(ts)

                if (
                    self.cfg.max_daily_trades > 0
                    and self._daily_trade_count.get(self._current_day or "", 0)
                    >= self.cfg.max_daily_trades
                ):
                    continue

                signal = self.strategy.on_data(event)
                if signal is None:
                    continue

                if self._pipeline is not None:
                    signal = self._pipeline.preprocess_signal(signal, event)
                    if signal is None:
                        continue

                signal, order_spec = self._resolve_fees(signal)

                if self._pipeline is not None:
                    decision = self._pipeline.evaluate_gates(
                        signal,
                        event,
                        _BacktestPortfolioProxy(self),
                        self._pipeline_ctx,
                        order_spec=order_spec,
                        has_orderbook=False,
                        skip_tca=False,
                    )
                    if not decision.approved:
                        logger.debug(
                            "Backtest: %s %s rejected by %s: %s",
                            signal.symbol, signal.side, decision.gate, decision.reason,
                        )
                        continue
                    signal = decision.signal or signal
                elif self._risk_manager is not None:
                    ok, reason = self._risk_manager.can_enter(
                        signal, _BacktestPortfolioProxy(self),
                    )
                    if not ok:
                        logger.debug(
                            "Backtest: %s %s rejected by risk gate: %s",
                            signal.symbol, signal.side, reason,
                        )
                        continue

                capital = self._open_position(
                    signal, event.price, ts, capital, order_spec=order_spec,
                )
                self._capital = capital
                if signal.symbol in self.positions_by_symbol and self._pipeline is not None:
                    self._pipeline.record_trade_opened(
                        signal.strategy, signal.symbol, event, self._pipeline_ctx,
                    )
                day = self._current_day or time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
                self._daily_trade_count[day] = self._daily_trade_count.get(day, 0) + 1

            self._pipeline_ctx.last_bar_ts[symbol] = ts

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

        trade_analytics = enrich_trades_post_exit(self.closed_trades, self._price_index)
        execution_trades = strip_post_exit_from_trades(self.closed_trades)

        manifest = {}
        if self._full_config is not None:
            tca_mode = str(self._full_config.get("backtest.tca_mode", "proxy"))
            dc_tier = (
                self._data_contract.fidelity_tier if self._data_contract else None
            )
            dc_summary = (
                self._data_contract.summary() if self._data_contract else None
            )
            data_source = (
                self._data_contract.data_source if self._data_contract else "sqlite_candles"
            )
            manifest = build_run_manifest(
                self._full_config,
                symbols=self.symbols,
                data_source=data_source,
                use_microstructure_proxy=self.cfg.use_microstructure_proxy,
                kelly_effective=resolve_kelly_enabled(self._full_config, for_backtest=True),
                tca_mode=tca_mode,
                data_contract_tier=dc_tier,
                data_contract_summary=dc_summary,
                gate_manifest=(
                    self._pipeline.gate_manifest() if self._pipeline is not None else None
                ),
            )

        return {
            "equity_curve": self.equity_curve,
            "trades": execution_trades,
            "trade_analytics": trade_analytics,
            "metrics": metrics,
            "capital": capital,
            "total_return": metrics["total_return"],
            "manifest": manifest,
        }

    def _active_strategy_names(self) -> List[str]:
        """Collect strategy names for per-strategy fidelity evaluation."""
        names: List[str] = []
        try:
            if hasattr(self.strategy, "strategies"):
                for s in self.strategy.strategies:
                    names.append(str(getattr(s, "name", s.__class__.__name__)))
            else:
                names.append(str(getattr(self.strategy, "name", self.strategy.__class__.__name__)))
        except Exception:
            names.append(str(getattr(self.strategy, "name", "unknown")))
        return names

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
            "candles_1m": self.db.get_candles(
                symbol, "1m", limit=500_000, start_ms=start_ms, end_ms=end_ms
            ),
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
            "liquidations": self._load_liquidation_series(symbol, start_ms, end_ms),
            "liq_idx": 0,
            "liq_acc": LiquidationAccumulator(),
            "binance_perp_ts": self._load_binance_perp_series(symbol, start_ms, end_ms),
            "candles_15m_ind": [],
            "hist_15m": [],
        }

    def _load_funding_series(
        self,
        symbol: str,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> List[Tuple[int, float, float]]:
        if not self.cfg.use_funding:
            return []
        rows = self.db.get_funding_history(symbol, limit=500_000)
        out: List[Tuple[int, float, float]] = []
        for r in rows:
            ts = int(r["timestamp"])
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts > end_ms:
                continue
            if r.get("current") is None:
                continue
            current = float(r["current"])
            predicted_raw = r.get("predicted")
            predicted = (
                float(predicted_raw) if predicted_raw is not None else current
            )
            out.append((ts, current, predicted))
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
            if r.get("oi_total") is None:
                continue
            oi_total = float(r["oi_total"])
            oi_delta_raw = r.get("oi_delta")
            oi_delta = float(oi_delta_raw) if oi_delta_raw is not None else 0.0
            out.append((ts, oi_total, oi_delta))
        out.sort(key=lambda x: x[0])
        return out

    def _load_liquidation_series(
        self,
        symbol: str,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> List[Tuple[int, float, str, str]]:
        if not self.cfg.use_external_feeds_replay:
            return []
        rows = self.db.get_liquidation_events(
            symbol, limit=500_000, start_ms=start_ms, end_ms=end_ms,
        )
        return [
            (
                int(r["timestamp_ms"]),
                float(r["notional_usd"]),
                str(r["side"]),
                str(r.get("source") or "binance"),
            )
            for r in rows
        ]

    def _load_binance_perp_series(
        self,
        symbol: str,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> List[Tuple[int, float]]:
        if not self.cfg.use_external_feeds_replay:
            return []
        rows = self.db.get_binance_perp_prices(
            symbol, limit=500_000, start_ms=start_ms, end_ms=end_ms,
        )
        out = [(int(r["timestamp_ms"]), float(r["price"])) for r in rows]
        out.sort(key=lambda x: x[0])
        return out

    def _advance_liquidation_replay(
        self,
        data: Dict[str, Any],
        ts: int,
    ) -> LiquidationAccumulator:
        """Feed liquidation rows up to *ts* into the per-symbol accumulator."""
        acc: LiquidationAccumulator = data["liq_acc"]
        liq_list: List[Tuple[int, float, str, str]] = data.get("liquidations", [])
        idx: int = data.get("liq_idx", 0)
        while idx < len(liq_list) and liq_list[idx][0] <= ts:
            _ts, notional, side, source = liq_list[idx]
            acc.record(_ts, notional, side, source)
            idx += 1
        data["liq_idx"] = idx
        acc.prune(ts)
        return acc

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
            buy_volume=c.buy_volume,
            sell_volume=c.sell_volume,
            trade_count=getattr(c, "trade_count", 0),
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
            self._pipeline_ctx.candles_15m_history[symbol] = list(data["hist_15m"])
            if self._pipeline is not None:
                prev = self._last_candle_close.get(symbol)
                curr = float(c15.close)
                if prev is not None and prev > 0.0:
                    self._pipeline.correlation_monitor.add_candle_return(
                        symbol, prev, curr,
                    )
                self._last_candle_close[symbol] = curr

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

        funding_current = funding_row[1] if funding_row else None
        funding_predicted = (
            funding_row[2] if funding_row and len(funding_row) > 2 else funding_current
        )

        liq_notional: Optional[float] = None
        liq_side: Optional[str] = None
        liq_count: Optional[int] = None
        liq_source: Optional[str] = None
        liq_feed_ready = False
        if self.cfg.use_external_feeds_replay:
            acc: LiquidationAccumulator = data["liq_acc"]
            liq_notional, liq_side, liq_count = acc.stats()
            liq_source = acc.source
            liq_feed_ready = bool(data.get("liquidations"))

        bn_perp_row = self._lookup_at_or_before(data.get("binance_perp_ts", []), ts)
        bn_perp_mid = bn_perp_row[1] if bn_perp_row else None
        bn_perp_ts = bn_perp_row[0] if bn_perp_row else None

        return MarketEvent(
            symbol=symbol,
            price=c1m.close,
            timestamp_ms=ts,
            candle_1m=self._to_indicator_candle(c1m),
            candle_5m=self._to_indicator_candle(c5),
            candle_15m=self._to_indicator_candle(c15),
            candle_1h=self._to_indicator_candle(c1h),
            funding=funding_current,
            predicted_funding=funding_predicted,
            oi_total=oi_row[1] if oi_row else None,
            oi_delta=oi_row[2] if oi_row else None,
            volume_1m=c1m.volume,
            adx_14=adx,
            orderbook_spread_pct=spread_pct,
            orderbook_oir=oir,
            orderbook_bid_ask_ratio=bid_ask_ratio,
            liquidation_notional_5m=liq_notional,
            liquidation_side_5m=liq_side,
            liquidation_count_5m=liq_count,
            liquidation_data_source=liq_source,
            liquidation_feed_ready=liq_feed_ready,
            binance_perp_mid=bn_perp_mid,
            binance_perp_timestamp_ms=bn_perp_ts,
        )

    def _roll_day(self, ts: int) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
        if self._current_day != day:
            self._current_day = day
            self._daily_trade_count = {}
            self._daily_pnl = 0.0

    def _resolve_fees(self, signal: Signal) -> Tuple[Signal, OrderSpec]:
        if self._pipeline is not None:
            return self._pipeline.resolve_order_fees(signal)
        fee = self.cfg.commission_pct / 100.0
        slip = self.cfg.paper_slippage_pct / 100.0
        spec = OrderSpec(
            order_type="market",
            limit_price=None,
            entry_fee_pct=fee,
            exit_fee_pct=fee,
            entry_slippage_pct=slip,
            exit_slippage_pct=slip,
        )
        return signal, spec

    def _note_trade_closed(
        self,
        pos: _OpenPosition,
        pnl_pct: float,
        exit_reason: str,
    ) -> None:
        strat = (pos.metadata or {}).get("original_strategy") or pos.strategy
        if self._pipeline is not None:
            self._pipeline.record_trade_closed(
                strat, pos.symbol, pnl_pct, self._pipeline_ctx,
            )
        if self._risk_manager is not None:
            class _Trade:
                pass
            t = _Trade()
            t.pnl_usd = 0.0
            t.pnl_pct = pnl_pct
            t.symbol = pos.symbol
            t.reason = exit_reason
            self._risk_manager.on_trade_closed(t)

    def _atr_pct(self, candle: Any) -> Optional[float]:
        """ATR(1) proxy from a single candle: range / close."""
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
        *,
        order_spec: Optional[OrderSpec] = None,
    ) -> float:
        if signal.symbol in self.positions_by_symbol:
            return capital

        entry_price_raw = safe_float(signal.entry_price, 0.0) or price
        atr_pct = safe_float((signal.metadata or {}).get("atr_pct"), 0.0)
        if atr_pct <= 0.0:
            atr_pct = safe_float(signal.stop_loss_pct, 0.0) / 2.0
        if atr_pct <= 0.0:
            atr_pct = 0.005

        sized_signal = Signal(
            strategy=signal.strategy,
            symbol=signal.symbol,
            side=signal.side,
            confidence=signal.confidence,
            size_pct=signal.size_pct,
            entry_price=entry_price_raw,
            stop_loss_pct=signal.stop_loss_pct,
            take_profit_pct=signal.take_profit_pct,
            reason=signal.reason,
            metadata={**(signal.metadata or {}), "atr_pct": atr_pct},
        )

        if self._risk_manager is not None:
            size = self._risk_manager.calculate_position_size(
                sized_signal, capital, atr_pct,
            )
        else:
            stop = safe_float(signal.stop_loss_pct, 0.01)
            notional = safe_divide(capital * safe_float(signal.size_pct, 0.0), stop, 0.0)
            size = safe_divide(notional, entry_price_raw, 0.0)

        size = round_position_size(
            signal.symbol, size, self._full_config,
        )
        if size <= 0.0:
            return capital

        slip_bps = self.cfg.slippage_bps
        if order_spec is not None and order_spec.entry_slippage_pct > 0:
            slip_bps = order_spec.entry_slippage_pct * 10_000.0
        notional_est = size * entry_price_raw
        entry_price = self._apply_slippage(
            entry_price_raw,
            signal.side,
            "entry",
            order_size_usd=notional_est,
            slippage_bps_override=slip_bps if order_spec and order_spec.order_type == "limit_maker" else None,
        )
        if entry_price <= 0.0:
            return capital

        if order_spec is not None:
            fee_rate = order_spec.entry_fee_pct
        else:
            fee_rate = self.cfg.commission_pct / 100.0
        entry_notional = entry_price * size
        entry_commission = entry_notional * fee_rate
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

        meta = dict(signal.metadata or {})
        if order_spec is not None:
            meta["entry_fee_pct"] = order_spec.entry_fee_pct
            meta["exit_fee_pct"] = order_spec.exit_fee_pct
            meta["entry_slippage_pct"] = order_spec.entry_slippage_pct
            meta["exit_slippage_pct"] = order_spec.exit_slippage_pct
            meta["order_type"] = order_spec.order_type

        pos = _OpenPosition(
            id=self._next_position_id,
            strategy=signal.strategy,
            symbol=signal.symbol,
            side=signal.side,
            entry_price=entry_price,
            entry_time_ms=ts,
            size=size,
            stop_loss_price=stop,
            take_profit_price=tp,
            metadata=meta,
            next_funding_ts=ts + 3_600_000,
            excursion_id=self._next_excursion_id,
        )
        risk_usd = 0.0
        if stop is not None and entry_price > 0 and size > 0:
            if signal.side == "long":
                risk_usd = abs(entry_price - stop) * size
            else:
                risk_usd = abs(stop - entry_price) * size
        self._excursion_trackers[self._next_excursion_id] = ExcursionTracker(
            entry_price=entry_price,
            entry_time_ms=ts,
            side=signal.side,
            risk_usd=risk_usd,
        )
        self._next_excursion_id += 1
        self.positions[pos.id] = pos
        self.positions_by_symbol[signal.symbol] = pos.id
        self._next_position_id += 1
        return capital

    def _flatten_symbol_at_gap(
        self,
        symbol: str,
        ts: int,
        price: float,
        capital: float,
    ) -> float:
        """Force-close open positions when replay crosses a research data gap."""
        pos_id = self.positions_by_symbol.get(symbol)
        if pos_id is None:
            return capital
        logger.info(
            "Backtest gap flatten %s at %d (gap > %dms)",
            symbol,
            ts,
            self._research_gap_ms,
        )
        return self._close_position(pos_id, price, ts, "research_gap_flatten", capital)

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

        # Fees from order routing metadata (maker/taker parity with live)
        entry_fee_rate = safe_float(
            (pos.metadata or {}).get("entry_fee_pct"),
            self.cfg.commission_pct / 100.0,
        )
        exit_fee_rate = safe_float(
            (pos.metadata or {}).get("exit_fee_pct"),
            self.cfg.commission_pct / 100.0,
        )
        total_fees = (entry_notional * entry_fee_rate) + (exit_notional * exit_fee_rate)

        if pos.side == "long":
            gross_pnl = (exit_price - pos.entry_price) * pos.size
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.size

        # v3.1.19: include accumulated funding cost in realised PnL.
        net_pnl = gross_pnl - total_fees + pos.funding_paid
        pnl_pct = safe_divide(net_pnl, entry_notional, 0.0)
        capital += net_pnl
        self._daily_pnl += net_pnl
        self._note_trade_closed(pos, pnl_pct, reason)
        self._capital = capital

        risk_usd = 0.0
        if pos.stop_loss_price is not None and pos.entry_price > 0 and pos.size > 0:
            if pos.side == "long":
                risk_usd = abs(pos.entry_price - pos.stop_loss_price) * pos.size
            else:
                risk_usd = abs(pos.stop_loss_price - pos.entry_price) * pos.size
        r_multiple = safe_divide(net_pnl, risk_usd, 0.0) if risk_usd > 0 else 0.0

        attr_strategy = (pos.metadata or {}).get("original_strategy") or pos.strategy
        trade_record: Dict[str, Any] = {
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
            "risk_usd": round(risk_usd, 4),
            "r_multiple": round(r_multiple, 4),
            "exit_reason": reason,
            "funding_paid": round(pos.funding_paid, 4),
            "fees_paid": round(total_fees, 4),
        }
        tracker = self._excursion_trackers.pop(pos.excursion_id, None)
        if tracker is not None:
            trade_record.update(
                compute_intrade_excursion_fields(
                    tracker,
                    size=pos.size,
                    net_pnl_usd=net_pnl,
                    fees_paid=total_fees,
                )
            )
        self.closed_trades.append(trade_record)
        return capital

    def _update_excursions(self, symbol: str, c1m: DBCandle, ts: int) -> None:
        """Update running MFE/MAE for open position on this symbol."""
        pos_id = self.positions_by_symbol.get(symbol)
        if pos_id is None:
            return
        pos = self.positions.get(pos_id)
        if pos is None:
            return
        tracker = self._excursion_trackers.get(pos.excursion_id)
        if tracker is None:
            return
        high = safe_float(c1m.high, 0.0)
        low = safe_float(c1m.low, 0.0)
        if high > 0 and low > 0:
            tracker.update_bar(high, low, ts)

    def _process_exits(
        self,
        event: MarketEvent,
        capital: float,
        c1m: DBCandle,
    ) -> float:
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

        intrabar = self._intrabar_stop_tp(pos, c1m)
        if intrabar is not None:
            reason, fill_price = intrabar
            return self._close_position(pos_id, fill_price, event.timestamp_ms, reason, capital)

        return capital

    def _intrabar_stop_tp(
        self,
        pos: _OpenPosition,
        c1m: DBCandle,
    ) -> Optional[Tuple[str, float]]:
        """Resolve stop/TP using 1m high/low; pessimistic when both touch."""
        high = safe_float(c1m.high, 0.0)
        low = safe_float(c1m.low, 0.0)
        if high <= 0.0 or low <= 0.0 or high < low:
            return None

        sl_hit = False
        tp_hit = False
        if pos.stop_loss_price is not None:
            if pos.side == "long" and low <= pos.stop_loss_price:
                sl_hit = True
            elif pos.side == "short" and high >= pos.stop_loss_price:
                sl_hit = True
        if pos.take_profit_price is not None:
            if pos.side == "long" and high >= pos.take_profit_price:
                tp_hit = True
            elif pos.side == "short" and low <= pos.take_profit_price:
                tp_hit = True

        if not sl_hit and not tp_hit:
            return None

        pessimistic = self.cfg.intrabar_conflict_policy != "optimistic"
        if sl_hit and tp_hit:
            if pessimistic:
                return ("stop_loss", float(pos.stop_loss_price))
            return ("take_profit", float(pos.take_profit_price))
        if sl_hit:
            return ("stop_loss", float(pos.stop_loss_price))
        return ("take_profit", float(pos.take_profit_price))

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
        slippage_bps_override: Optional[float] = None,
    ) -> float:
        """Apply slippage to a fill price."""
        if slippage_bps_override is not None and slippage_bps_override <= 0:
            return price
        bps = slippage_bps_override if slippage_bps_override is not None else self.cfg.slippage_bps
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


def build_backtest_config_from_yaml(cfg: Union[Config, Dict[str, Any]]) -> BacktestConfig:
    """Construct BacktestConfig from merged application config."""
    if not isinstance(cfg, Config):
        cfg = Config(cfg)
    from src.utils.config import get_strategy_section, phase08_enabled

    cooldown_cfg = get_strategy_section(cfg, "cooldown")
    use_regime = bool(cfg.get("backtest.use_regime_weights", True))
    if phase08_enabled(cfg):
        use_regime = False
    return BacktestConfig(
        initial_capital=float(cfg.get("backtest.initial_capital", 100_000.0)),
        commission_pct=float(cfg.get("backtest.commission_pct", cfg.get("risk.taker_fee_pct", 0.04))),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 5)),
        tca_enabled=bool(cfg.get("execution.tca_enabled", True)),
        min_edge_buffer_pct=float(cfg.get("execution.min_edge_buffer_pct", 0.05)),
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.05)),
        use_regime_weights=use_regime,
        use_cooldown=bool(cfg.get("backtest.use_cooldown", True)),
        use_microstructure_proxy=bool(cfg.get("backtest.use_microstructure_proxy", True)),
        intrabar_conflict_policy=str(cfg.get("backtest.intrabar_conflict_policy", "pessimistic")),
        regime_weights=cfg.get("strategy.regime_weights", {}),
        adx_trend_threshold=float(cfg.get("strategy.adx_trend_threshold", 25.0)),
        adx_range_threshold=float(cfg.get("strategy.adx_range_threshold", 20.0)),
        cooldown_base_ms=int(safe_float(cooldown_cfg.get("base_minutes", 60)) * 60_000),
        cooldown_max_ms=int(safe_float(cooldown_cfg.get("max_minutes", 240)) * 60_000),
        cooldown_multiplier=safe_float(cooldown_cfg.get("multiplier", 2.0)),
        max_daily_trades=int(cfg.get("risk.max_daily_trades", 5)),
    )
