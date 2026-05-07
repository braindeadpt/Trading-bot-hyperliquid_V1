"""Main trading engine - orchestrates data flow, strategies, risk, and execution.

The engine subscribes to the :class:`DataBus`, builds :class:`MarketEvent`s,
feeds them to registered strategies, and gates every entry signal through
the :class:`RiskManager` before handing approved trades to the
:class:`ExecutionEngine`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from src.data.database import (
    Database,
    PortfolioSnapshot,
    SignalRecord,
)
from src.exchanges.funding_aggregator import (
    AggregatedFundingOI,
    FundingOIAggregator,
)
from src.exchanges.hyperliquid_ws import (
    DataBus,
    HlAssetCtx,
    HlPriceTick,
)
from src.strategies.base import (
    ExitSignal,
    MarketEvent,
    Position,
    Signal,
    Strategy,
)
from src.strategies.indicators import Candle
from src.utils.config import Config
from src.utils.helpers import safe_float, safe_divide, utc_timestamp_ms

from .execution import ExecutionEngine, TradeResult
from .portfolio import PortfolioState
from .risk_manager import RiskManager

logger = logging.getLogger(__name__)


class TradingEngine:
    """Central orchestrator that wires data → strategies → risk → execution.

    Lifecycle:
      1. ``start()`` - subscribe to DataBus, load DB state, begin event loop.
      2. ``stop()``  - close positions, save state, unsubscribe.

    The engine is **async-safe**; all mutable state is guarded by locks.
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        data_bus: DataBus,
        strategies: List[Strategy],
        risk_manager: RiskManager,
        executor: ExecutionEngine,
    ) -> None:
        self._config = config
        self._db = db
        self._bus = data_bus
        self._strategies = list(strategies)
        self._risk = risk_manager
        self._executor = executor

        # Symbols to trade (from config)
        self._symbols: List[str] = list(config.get("symbols", ["BTC", "ETH", "SOL"]))

        # In-memory cache of latest data per symbol
        self._latest_price: Dict[str, HlPriceTick] = {}
        self._latest_ctx: Dict[str, HlAssetCtx] = {}
        self._latest_candles: Dict[str, Dict[int, Optional[Candle]]] = {
            sym: {60: None, 300: None, 900: None, 3600: None}
            for sym in self._symbols
        }

        # Portfolio state (creates fresh; DB recovery happens in start())
        initial_capital = safe_float(
            config.get("backtest.initial_capital", 100_000.0)
        )
        self._portfolio = PortfolioState(initial_capital)

        # Internal state
        self._running: bool = False
        self._shutdown_event: Optional[asyncio.Event] = None
        self._event_lock = asyncio.Lock()
        self._start_time: Optional[float] = None

        # Track which topics we subscribed to so we can unsubscribe on stop
        self._subscribed_callbacks: Dict[str, Any] = {}

        # ── Cross-exchange funding + OI aggregator ──
        self._funding_aggregator = FundingOIAggregator()
        self._latest_agg_funding: Dict[str, AggregatedFundingOI] = {}
        self._funding_poll_task: Optional[asyncio.Task] = None

        # ── Dashboard tracking (real-time introspection) ──
        self._last_market_events: Dict[str, Dict] = {}
        self._signal_history: List[Dict] = []
        self._decision_history: List[Dict] = []
        self._tick_stats = {"total": 0, "per_second": 0.0, "last_tick_time": 0.0, "tick_times": []}
        self._last_error: Optional[str] = None
        self._on_dashboard_tick: Optional[Any] = None  # callback set by main.py

    # ── Public properties for dashboard / external access ──
    @property
    def portfolio(self) -> PortfolioState:
        return self._portfolio

    @property
    def database(self) -> Database:
        return self._db

    @property
    def data_bus(self) -> DataBus:
        return self._bus

    @property
    def risk_manager(self) -> RiskManager:
        return self._risk

    @property
    def executor(self) -> ExecutionEngine:
        return self._executor

    @property
    def uptime_sec(self) -> int:
        if self._start_time is None:
            return 0
        return int(time.time() - self._start_time)

    @property
    def memory_mb(self) -> float:
        try:
            import psutil
            proc = psutil.Process()
            return round(proc.memory_info().rss / (1024 * 1024), 1)
        except Exception:
            return 0.0

    @property
    def daily_trade_count(self) -> int:
        return getattr(self._portfolio, "sync_daily_trades", lambda: 0)()

    @property
    def positions(self) -> Dict[str, Any]:
        return self._portfolio.get_positions_sync() if hasattr(self._portfolio, "get_positions_sync") else {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the engine: load state, subscribe to DataBus, begin processing."""
        if self._running:
            logger.warning("TradingEngine.start() called while already running")
            return

        self._running = True
        self._shutdown_event = asyncio.Event()
        self._start_time = time.time()
        logger.info("TradingEngine starting …")

        # 1. Open executor session
        await self._executor.open()

        # 2. Recover DB state
        await self._recover_state()

        # 3. Subscribe to DataBus topics per symbol
        for symbol in self._symbols:
            # Price ticks
            cb_price = self._make_price_callback(symbol)
            await self._bus.subscribe(f"price:{symbol}", cb_price)
            self._subscribed_callbacks[f"price:{symbol}"] = cb_price

            # Asset context (OI, funding)
            cb_ctx = self._make_ctx_callback(symbol)
            await self._bus.subscribe(f"ctx:{symbol}", cb_ctx)
            self._subscribed_callbacks[f"ctx:{symbol}"] = cb_ctx

            # Completed candles per timeframe
            for tf in (60, 300, 900, 3600):
                cb_candle = self._make_candle_callback(symbol, tf)
                await self._bus.subscribe(f"candle_complete:{tf}:{symbol}", cb_candle)
                self._subscribed_callbacks[f"candle_complete:{tf}:{symbol}"] = cb_candle

        logger.info(
            "TradingEngine running - symbols=%s strategies=%s",
            self._symbols,
            [s.name for s in self._strategies],
        )

        # 4. Start background funding + OI polling
        self._funding_poll_task = asyncio.create_task(self._poll_funding_loop())
        logger.info("FundingAggregator polling started (interval=30s)")

    async def _poll_funding_loop(self) -> None:
        """Background task: poll cross-exchange funding + OI every 30s."""
        while self._running:
            try:
                results = await self._funding_aggregator.poll(self._symbols)
                for sym, data in results.items():
                    if data:
                        self._latest_agg_funding[sym] = data
                logger.info(
                    "FundingAggregator updated for %d symbols (exchanges=%s)",
                    len(results),
                    ", ".join(
                        sorted(
                            set(
                                ex
                                for d in results.values()
                                for ex in d.by_exchange.keys()
                            )
                        )
                    ) if results else "none",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("FundingAggregator poll failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait() if self._shutdown_event else asyncio.sleep(30),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        """Graceful shutdown: close all positions, save state, unsubscribe."""
        if not self._running:
            return

        logger.info("TradingEngine stopping …")
        self._running = False
        if self._shutdown_event is not None:
            self._shutdown_event.set()

        # Cancel funding poll task
        if self._funding_poll_task and not self._funding_poll_task.done():
            self._funding_poll_task.cancel()
            try:
                await self._funding_poll_task
            except asyncio.CancelledError:
                pass

        # 1. Unsubscribe from DataBus
        for topic, callback in self._subscribed_callbacks.items():
            try:
                await self._bus.unsubscribe(topic, callback)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Unsubscribe error on %s: %s", topic, exc)
        self._subscribed_callbacks.clear()

        # 2. Close all open positions (market order at last known price)
        positions = await self._portfolio.positions
        for symbol, position in positions.items():
            last_price = self._latest_price.get(symbol)
            if last_price is not None:
                await self._execute_exit(position, last_price.mid, reason="engine_shutdown")
            else:
                logger.warning("No last price for %s during shutdown - skipping close", symbol)

        # 3. Save final portfolio snapshot
        await self._save_portfolio_snapshot()

        # 4. Close executor
        await self._executor.close()

        logger.info("TradingEngine stopped")

    # ------------------------------------------------------------------
    # DataBus callbacks
    # ------------------------------------------------------------------

    def _make_price_callback(self, symbol: str):
        """Factory: returns an async callback for price:* topics."""
        async def _on_price(tick: HlPriceTick) -> None:
            self._latest_price[symbol] = tick
            await self._on_market_event(symbol)
        return _on_price

    def _make_ctx_callback(self, symbol: str):
        """Factory: returns an async callback for ctx:* topics."""
        async def _on_ctx(ctx: HlAssetCtx) -> None:
            self._latest_ctx[symbol] = ctx
        return _on_ctx

    def _make_candle_callback(self, symbol: str, timeframe: str):
        """Factory: returns an async callback for candle_complete:* topics."""
        async def _on_candle(candle: Candle) -> None:
            self._latest_candles[symbol][timeframe] = candle
        return _on_candle

    # ------------------------------------------------------------------
    # Core event loop (per-symbol)
    # ------------------------------------------------------------------

    async def _on_market_event(self, symbol: str) -> None:
        """Process a market update for *symbol*.

        This is the heart of the engine:
          1. Build a MarketEvent from cached data.
          2. Feed to each strategy for entry signals.
          3. Update portfolio prices and check for exit signals.
          4. Execute approved entries / exits.
          5. Persist everything to the DB.
        """
        logger.info("Processing market event for %s", symbol)
        async with self._event_lock:
            if not self._running:
                return

            # --- Build MarketEvent ---
            event = self._build_market_event(symbol)
            if event is None:
                return

            # --- Dashboard tracking ---
            now = time.time()
            self._tick_stats["total"] += 1
            self._tick_stats["last_tick_time"] = now
            # Keep last 60 tick timestamps for per-second calculation
            self._tick_stats["tick_times"].append(now)
            self._tick_stats["tick_times"] = [t for t in self._tick_stats["tick_times"] if now - t <= 1.0]
            self._tick_stats["per_second"] = len(self._tick_stats["tick_times"])

            self._last_market_events[symbol] = {
                "symbol": symbol,
                "price": event.price,
                "timestamp_ms": event.timestamp_ms,
                "funding": event.funding,
                "predicted_funding": event.predicted_funding,
                "oi_total": event.oi_total,
                "oi_delta": event.oi_delta,
                "volume_1m": event.volume_1m,
                "bid_ask_imbalance": event.bid_ask_imbalance,
                "vwap_15m": event.vwap_15m,
                "funding_avg": event.funding_avg,
                "funding_weighted": event.funding_weighted,
                "predicted_funding_avg": event.predicted_funding_avg,
                "oi_total_aggregated": event.oi_total_aggregated,
                "oi_exchange_count": event.oi_exchange_count,
                "candles": {
                    "1m": event.candle_1m is not None,
                    "5m": event.candle_5m is not None,
                    "15m": event.candle_15m is not None,
                    "1h": event.candle_1h is not None,
                },
                "processed_at": now,
            }

            # --- Dashboard callback (fire-and-forget, don't block) ---
            if self._on_dashboard_tick:
                try:
                    cb = self._on_dashboard_tick
                    if asyncio.iscoroutinefunction(cb):
                        asyncio.create_task(cb())
                    else:
                        cb()
                except Exception:
                    pass

            # --- Update portfolio prices (triggers unrealized PnL) ---
            await self._portfolio.update_price(symbol, event.price)

            # --- Update executor price tracking ---
            await self._executor.update_position_prices({symbol: event.price})

            # --- 1. Strategy entry signals ---
            signals: List[Signal] = []
            for strategy in self._strategies:
                if not getattr(strategy, "enabled", True):
                    continue
                try:
                    sig = strategy.on_data(event)
                    if sig is not None:
                        signals.append(sig)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Strategy %s error on %s: %s", strategy.name, symbol, exc)

            if signals:
                # Conflict resolution: pick highest confidence
                best_signal = max(signals, key=lambda s: s.confidence)
                await self._process_entry_signal(best_signal, event)

            # --- 2. Strategy exit signals (only if position exists) ---
            exit_triggered = False
            positions = await self._portfolio.positions
            position = positions.get(symbol)
            if position is not None:
                for strategy in self._strategies:
                    if not getattr(strategy, "enabled", True):
                        continue
                    try:
                        exit_sig = strategy.on_position(position, event)
                        if exit_sig is not None:
                            await self._process_exit_signal(exit_sig, position)
                            exit_triggered = True
                            break  # One exit signal is enough
                    except Exception as exc:
                        logger.exception(
                            "Strategy %s exit error on %s: %s", strategy.name, symbol, exc
                        )

            # --- 3. Stop-loss / take-profit hard exits ---
            if not exit_triggered:
                positions = await self._portfolio.positions
                position = positions.get(symbol)
                if position is not None:
                    await self._check_hard_stops(position, event.price)

            # --- 4. Periodic snapshot (throttled) ---
            await self._maybe_save_snapshot()

    def _build_market_event(self, symbol: str) -> Optional[MarketEvent]:
        """Assemble a MarketEvent from the latest cached data for *symbol*."""
        tick = self._latest_price.get(symbol)
        if tick is None:
            return None

        ctx = self._latest_ctx.get(symbol)
        candles = self._latest_candles.get(symbol, {})

        price = safe_float(tick.mid)
        if price <= 0.0:
            return None

        # Cross-exchange aggregated funding + OI (if available)
        agg = self._latest_agg_funding.get(symbol)

        # Build the MarketEvent
        event = MarketEvent(
            symbol=symbol,
            price=price,
            timestamp_ms=tick.timestamp_ms,
            candle_1m=candles.get(60),
            candle_5m=candles.get(300),
            candle_15m=candles.get(900),
            candle_1h=candles.get(3600),
            funding=safe_float(ctx.funding_rate) if ctx else None,
            predicted_funding=safe_float(ctx.predicted_funding) if ctx else None,
            oi_total=safe_float(ctx.open_interest) if ctx else None,
            oi_delta=getattr(candles.get(900), 'oi_delta', None) if candles.get(900) else None,
            volume_1m=getattr(candles.get(60), 'volume', None) if candles.get(60) else None,
            bid_ask_imbalance=self._calc_imbalance(candles.get(900)),
            vwap_15m=getattr(candles.get(900), 'vwap', None) if candles.get(900) else None,
            # Cross-exchange aggregated data
            funding_avg=agg.funding_avg if agg else None,
            funding_weighted=agg.funding_weighted if agg else None,
            predicted_funding_avg=agg.predicted_funding_avg if agg else None,
            oi_total_aggregated=agg.oi_total if agg else None,
            oi_exchange_count=agg.exchange_count if agg else 0,
        )

        # Log orderflow metrics for debugging
        logger.info(
            "MarketEvent %s: price=%.2f, funding=%.6f, predicted=%s, oi=%.2f, "
            "agg_funding=%s, agg_oi=%s, exchanges=%d, imbalance=%s",
            symbol, event.price,
            event.funding or 0,
            f"{event.predicted_funding:.6f}" if event.predicted_funding else "N/A",
            event.oi_total or 0,
            f"{event.funding_avg:.6f}" if event.funding_avg else "N/A",
            f"{event.oi_total_aggregated:,.0f}" if event.oi_total_aggregated else "N/A",
            event.oi_exchange_count,
            event.bid_ask_imbalance,
        )

        return event

    def _calc_imbalance(self, candle_15m) -> Optional[float]:
        """Compute bid/ask imbalance from 15m candle buy/sell volume."""
        if candle_15m is None:
            return None
        buy = getattr(candle_15m, 'buy_volume', 0.0)
        sell = getattr(candle_15m, 'sell_volume', 0.0)
        total = buy + sell
        if total <= 0:
            return None
        return (buy - sell) / total

    # ------------------------------------------------------------------
    # Signal processing
    # ------------------------------------------------------------------

    async def _process_entry_signal(self, signal: Signal, event: MarketEvent) -> None:
        """Gate an entry signal through risk management and execute if approved."""
        # Save signal to DB for audit trail
        self._db.save_signal(
            SignalRecord(
                symbol=signal.symbol,
                side=signal.side,
                confidence=signal.confidence,
                strategy=signal.strategy,
                price=event.price,
                timestamp=event.timestamp_ms,
                reason=signal.reason,
            )
        )

        # --- Signal tracking for dashboard ---
        sig_time = datetime.fromtimestamp(event.timestamp_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")
        sig_record = {
            "time": sig_time,
            "strategy": signal.strategy,
            "symbol": signal.symbol,
            "side": signal.side,
            "confidence": signal.confidence,
            "price": event.price,
            "reason": signal.reason,
            "status": "pending",
            "risk_reason": "",
            "size": 0,
        }
        self._signal_history.insert(0, sig_record)
        self._signal_history = self._signal_history[:100]

        # --- Risk check ---
        capital = await self._portfolio.current_capital
        positions = await self._portfolio.positions
        daily_pnl = await self._portfolio.daily_pnl
        daily_trades = await self._portfolio.daily_trades

        portfolio_proxy = _PortfolioProxy(
            capital=capital,
            positions=positions,
            daily_pnl=daily_pnl,
            daily_trades=daily_trades,
            max_drawdown=await self._portfolio.get_max_drawdown(),
        )

        approved, reason = self._risk.can_enter(signal, portfolio_proxy)
        if not approved:
            logger.info(
                "Signal REJECTED %s %s (confidence=%.2f) - %s",
                signal.symbol,
                signal.side,
                signal.confidence,
                reason,
            )
            sig_record["status"] = "rejected"
            sig_record["risk_reason"] = reason
            self._decision_history.insert(0, {
                "time": sig_time,
                "type": "risk",
                "symbol": signal.symbol,
                "side": signal.side,
                "result": "rejected",
                "reason": reason,
            })
            self._decision_history = self._decision_history[:100]
            return

        # --- Position sizing ---
        # ATR may be pre-computed in the signal metadata or we estimate from stop_loss_pct
        atr_pct = safe_float(signal.metadata.get("atr_pct"))
        if atr_pct <= 0.0:
            # Fallback: derive ATR pct from signal's stop_loss_pct if provided
            atr_pct = safe_float(signal.stop_loss_pct) / 2.0
        if atr_pct <= 0.0:
            atr_pct = 0.005  # 0.5% default

        size = self._risk.calculate_position_size(signal, capital, atr_pct)
        if size <= 0.0:
            logger.warning("Position size zero for %s - skipping", signal.symbol)
            return

        # Enrich signal metadata with computed size
        signal = Signal(
            strategy=signal.strategy,
            symbol=signal.symbol,
            side=signal.side,
            confidence=signal.confidence,
            size_pct=signal.size_pct,
            entry_price=signal.entry_price or event.price,
            stop_loss_pct=signal.stop_loss_pct,
            take_profit_pct=signal.take_profit_pct,
            reason=signal.reason,
            metadata={**signal.metadata, "calculated_size": size, "atr_pct": atr_pct},
        )

        # --- Compute stop distance from ATR (same formula as RiskManager) ---
        stop_distance_pct = max(2.0 * atr_pct, 0.005)

        if signal.side == "long":
            stop_loss_price = signal.entry_price * (1.0 - stop_distance_pct)
            take_profit_price = (
                signal.entry_price * (1.0 + signal.take_profit_pct)
                if signal.take_profit_pct else None
            )
        else:
            stop_loss_price = signal.entry_price * (1.0 + stop_distance_pct)
            take_profit_price = (
                signal.entry_price * (1.0 - signal.take_profit_pct)
                if signal.take_profit_pct else None
            )

        # --- Execute ---
        try:
            result = await self._executor.enter_position(signal, self._portfolio)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Execution failed for %s: %s", signal.symbol, exc)
            sig_record["status"] = "failed"
            sig_record["risk_reason"] = str(exc)[:80]
            self._decision_history.insert(0, {
                "time": sig_time,
                "type": "execution",
                "symbol": signal.symbol,
                "side": signal.side,
                "result": "failed",
                "reason": str(exc)[:80],
            })
            self._decision_history = self._decision_history[:100]
            return

        sig_record["status"] = "executed"
        sig_record["size"] = result.size
        self._decision_history.insert(0, {
            "time": sig_time,
            "type": "execution",
            "symbol": signal.symbol,
            "side": signal.side,
            "result": "executed",
            "reason": f"size={result.size:.6f} @ {result.entry_price:.2f}",
        })
        self._decision_history = self._decision_history[:100]

        # --- Update portfolio ---
        notional = result.entry_price * result.size
        position = Position(
            symbol=result.symbol,
            side=result.side,
            entry_price=result.entry_price,
            size=result.size,
            entry_time_ms=result.timestamp_ms,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            unrealized_pnl=0.0,
            metadata={"strategy": signal.strategy, "trade_id": result.trade_id},
        )
        await self._portfolio.add_position(position, cost=notional)

        logger.info(
            "Signal EXECUTED %s %s size=%.6f @ %.4f (id=%d)",
            signal.symbol,
            signal.side,
            result.size,
            result.entry_price,
            result.trade_id,
        )

    async def _process_exit_signal(self, exit_signal: ExitSignal, position: Position) -> None:
        """Execute a strategy-driven exit."""
        last_price = self._latest_price.get(position.symbol)
        if last_price is None:
            logger.warning("No price for %s - cannot execute exit", position.symbol)
            return

        await self._execute_exit(position, last_price.mid, reason=exit_signal.reason)

    async def _check_hard_stops(self, position: Position, current_price: float) -> None:
        """Check stop-loss and take-profit levels and exit if breached."""
        if position.stop_loss_price is not None:
            if position.side == "long" and current_price <= position.stop_loss_price:
                await self._execute_exit(position, current_price, reason="stop_loss")
                return
            if position.side == "short" and current_price >= position.stop_loss_price:
                await self._execute_exit(position, current_price, reason="stop_loss")
                return

        if position.take_profit_price is not None:
            if position.side == "long" and current_price >= position.take_profit_price:
                await self._execute_exit(position, current_price, reason="take_profit")
                return
            if position.side == "short" and current_price <= position.take_profit_price:
                await self._execute_exit(position, current_price, reason="take_profit")
                return

    async def _execute_exit(
        self,
        position: Position,
        exit_price: float,
        reason: str,
    ) -> None:
        """Close a position and update all downstream state."""
        try:
            result = await self._executor.close_position(position, exit_price, reason)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Exit execution failed for %s: %s", position.symbol, exc)
            self._decision_history.insert(0, {
                "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "type": "exit",
                "symbol": position.symbol,
                "side": position.side,
                "result": "failed",
                "reason": str(exc)[:80],
            })
            self._decision_history = self._decision_history[:100]
            return

        # Update portfolio
        await self._portfolio.remove_position(
            symbol=position.symbol,
            exit_price=exit_price,
            pnl_usd=result.pnl_usd,
            pnl_pct=result.pnl_pct,
            reason=reason,
        )

        # Update risk manager metrics
        self._risk.on_trade_closed(result)

        self._decision_history.insert(0, {
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "type": "exit",
            "symbol": position.symbol,
            "side": position.side,
            "result": "closed",
            "reason": f"{reason} pnl={result.pnl_usd:.2f} ({result.pnl_pct*100:.2f}%)",
        })
        self._decision_history = self._decision_history[:100]

        logger.info(
            "Position CLOSED %s pnl=%.2f (%.2f%%) reason=%s",
            position.symbol,
            result.pnl_usd,
            result.pnl_pct * 100.0,
            reason,
        )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    async def _recover_state(self) -> None:
        """Load open positions and portfolio state from the DB on startup."""
        # Load open trades from DB → executor
        await self._executor.load_open_trades()

        # Load latest portfolio snapshot
        history = self._db.get_portfolio_history(limit=1)
        if history:
            snap = history[0]
            try:
                import json
                positions_data = json.loads(snap["positions_json"])
                await self._portfolio.from_dict({
                    "cash": snap["capital"],
                    "peak_capital": snap["capital"],  # Will be re-estimated from prices
                    "daily_pnl": snap["daily_pnl"],
                    "positions": positions_data,
                })
                logger.info("Recovered portfolio snapshot from DB: capital=%.2f", snap["capital"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to restore portfolio snapshot: %s", exc)
        else:
            logger.info("No prior portfolio snapshot found - starting fresh")

    async def _save_portfolio_snapshot(self) -> None:
        """Persist the current portfolio state to the DB."""
        state = await self._portfolio.to_dict()
        try:
            import json
            snapshot = PortfolioSnapshot(
                timestamp=utc_timestamp_ms(),
                capital=state["current_capital"],
                daily_pnl=state["daily_pnl"],
                positions_json=json.dumps(state.get("positions", {})),
            )
            self._db.save_portfolio_snapshot(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to save portfolio snapshot: %s", exc)

    async def _maybe_save_snapshot(self) -> None:
        """Save portfolio snapshot every ~60 seconds (simple throttle)."""
        # Use a simple time-based throttle stored on the instance
        now = utc_timestamp_ms()
        if not hasattr(self, "_last_snapshot_ms"):
            self._last_snapshot_ms = 0
        if now - self._last_snapshot_ms >= 60_000:
            await self._save_portfolio_snapshot()
            self._last_snapshot_ms = now

    # ------------------------------------------------------------------
    # Health & introspection
    # ------------------------------------------------------------------

    async def get_portfolio(self) -> PortfolioState:
        """Return the live portfolio state (read-only access)."""
        return self._portfolio

    async def get_risk_metrics(self) -> Dict[str, Any]:
        """Return current risk-manager metrics."""
        return self._risk.get_metrics()

    def is_running(self) -> bool:
        """Return True if the engine is currently active."""
        return self._running


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

class _PortfolioProxy:
    """Lightweight synchronous proxy for PortfolioState values.

    The RiskManager expects a portfolio-like object with synchronous
    property access.  We gather the async values once and wrap them here
    so that ``can_enter()`` can run without awaiting.
    """

    def __init__(
        self,
        capital: float,
        positions: Dict[str, Position],
        daily_pnl: float,
        daily_trades: int,
        max_drawdown: float,
    ) -> None:
        self._capital = capital
        self._positions = positions
        self._daily_pnl = daily_pnl
        self._daily_trades = daily_trades
        self._max_drawdown = max_drawdown

    @property
    def current_capital(self) -> float:
        return self._capital

    @property
    def positions(self) -> Dict[str, Position]:
        return self._positions

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def daily_trades(self) -> int:
        return self._daily_trades

    def get_max_drawdown(self) -> float:
        return self._max_drawdown
