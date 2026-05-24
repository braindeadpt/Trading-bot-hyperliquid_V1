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
from typing import Any, Callable, Dict, List, Optional, Set

from src.data.database import (
    Candle as DBCandle,
    Database,
    PortfolioSnapshot,
    SignalRecord,
)
from src.data.orderbook_metrics import (
    OrderbookMetrics,
    PriceLevel,
    calculate_metrics,
    estimate_slippage,
)
from src.exchanges.funding_aggregator import (
    AggregatedFundingOI,
    FundingOIAggregator,
)
from src.exchanges.hyperliquid_ws import (
    DataBus,
    HlAssetCtx,
    HlOrderbook,
    HlPriceTick,
)
from src.strategies.base import (
    ExitSignal,
    MarketEvent,
    Position,
    Signal,
    Strategy,
)
from src.strategies.indicators import Candle, calculate_adx
from src.utils.config import Config
from src.utils.helpers import safe_float, safe_divide, utc_timestamp_ms

from .execution import ExecutionEngine, TradeResult
from .portfolio import PortfolioState
from .risk_manager import RiskManager
from .kelly_sizer import KellySizer
from .correlation_monitor import CorrelationMonitor
from .strategy_governor import StrategyGovernor
from .regime import apply_regime_weights as apply_regime_weights_fn
from .regime import regime_strategy_name
from .order_router import resolve_order_routing
from .tca import passes_tca_check

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
        notifier: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._db = db
        self._bus = data_bus
        self._strategies = list(strategies)
        self._risk = risk_manager
        self._executor = executor
        self._notifier = notifier

        # ── Kelly Criterion sizer (Task 4.4) ──
        kelly_cfg = config.get("kelly", {})
        self._kelly_sizer = KellySizer(
            min_trades=int(kelly_cfg.get("min_trades", 20)),
            half_kelly=bool(kelly_cfg.get("half_kelly", True)),
            max_multiplier=safe_float(kelly_cfg.get("max_multiplier", 2.0)),
            min_multiplier=safe_float(kelly_cfg.get("min_multiplier", 0.25)),
            lookback_trades=int(kelly_cfg.get("lookback_trades", 50)),
        )

        self._strategy_governor = StrategyGovernor(config, db)
        for strat in self._strategies:
            if hasattr(strat, "set_governor"):
                strat.set_governor(self._strategy_governor)

        trail = config.get("execution.trailing_stop", {}) or {}
        self._trailing_enabled = bool(trail.get("enabled", True))
        self._trailing_activation_pct = safe_float(trail.get("activation_pct", 0.005))
        self._trailing_distance_pct = safe_float(trail.get("trail_pct", 0.003))

        # Symbols to trade (from config)
        self._symbols: List[str] = list(config.get("symbols", ["BTC", "ETH", "SOL"]))

        # Slippage threshold (fraction, e.g. 0.002 = 0.2%)
        self._max_slippage_pct = safe_float(
            config.get("risk.max_slippage_pct", 0.2)
        ) / 100.0

        # Minimum fill ratio from L2 book (fraction, e.g. 0.8 = 80%)
        self._min_fill_ratio = safe_float(
            config.get("risk.min_fill_ratio", 0.8)
        )

        # ── Cooldown manager (Task 2.4) ──
        self._cooldown_base_ms = int(config.get("cooldown.base_minutes", 60) * 60_000)
        self._cooldown_max_ms = int(config.get("cooldown.max_minutes", 240) * 60_000)
        self._cooldown_multiplier = safe_float(config.get("cooldown.multiplier", 2.0))
        # Per (strategy, symbol) cooldown state
        self._cooldown_state: Dict[str, Dict[str, Any]] = {}

        # ── Regime filter (ADX-based strategy weighting) ──
        self._adx_period = int(config.get("strategy.adx_period", 14))
        self._adx_trend_threshold = safe_float(
            config.get("strategy.adx_trend_threshold", 25.0)
        )
        self._adx_range_threshold = safe_float(
            config.get("strategy.adx_range_threshold", 20.0)
        )
        self._regime_weights = config.get("strategy.regime_weights", {
            "trend": {"SmartMoneyFlow": 1.3, "FundingExtreme": 0.7},
            "range": {"SmartMoneyFlow": 0.7, "FundingExtreme": 1.3},
        })
        self._latest_adx: Dict[str, float] = {}
        self._candles_15m_history: Dict[str, Any] = {}  # symbol -> deque

        # In-memory cache of latest data per symbol
        self._latest_price: Dict[str, HlPriceTick] = {}
        self._latest_ctx: Dict[str, HlAssetCtx] = {}
        self._latest_candles: Dict[str, Dict[int, Optional[Candle]]] = {
            sym: {60: None, 300: None, 900: None, 3600: None}
            for sym in self._symbols
        }

        # 15m candle history for ADX calculation (regime filter)
        import collections
        self._candles_15m_history: Dict[str, Any] = {
            sym: collections.deque(maxlen=50)
            for sym in self._symbols
        }

        # Portfolio state (creates fresh; DB recovery happens in start())
        initial_capital = safe_float(
            config.get("risk.initial_capital", config.get("backtest.initial_capital", 10_000.0))
        )
        self._portfolio = PortfolioState(initial_capital)

        # Realized correlation monitor (positions heat)
        _gov = config.get("strategy.portfolio_governance", {})
        corr_lookback = int(
            _gov.get("max_correlation_lookback", config.get("portfolio.max_correlation_lookback", 60))
        )
        self._correlation_monitor = CorrelationMonitor(lookback=corr_lookback)
        # Last close per symbol for return calc
        self._last_candle_close: Dict[str, float] = {}

        # Internal state
        self._running: bool = False
        self._shutdown_event: Optional[asyncio.Event] = None
        self._event_lock = asyncio.Lock()
        self._start_time: Optional[float] = None

        # Track which topics we subscribed to so we can unsubscribe on stop
        self._subscribed_callbacks: Dict[str, Any] = {}

        # Dashboard task references (prevent Python 3.14 deallocation crash)
        self._dashboard_tasks: set = set()

        # ── Latest orderbook per symbol ──
        self._latest_orderbook: Dict[str, OrderbookMetrics] = {}
        self._latest_orderbook_raw: Dict[str, Any] = {}  # HlOrderbook

        # ── Latest funding + liquidation tracking (Task 3.3) ──
        # WS health check
        self._hl_ws_client: Optional[Any] = None
        self._ws_health_check_task: Optional[asyncio.Task] = None
        self._last_ws_healthy_time: float = time.time()
        self._ws_health_warned: bool = False

        self._latest_funding: Dict[str, float] = {}
        self._latest_oi_delta: Dict[str, Optional[float]] = {}
        self._liquidation_acc: Dict[str, Any] = {
            sym: {
                "window_ms": 5 * 60_000,  # 5 min window
                "events": collections.deque(),  # (timestamp_ms, notional, side)
                "source": None,
            }
            for sym in self._symbols
        }
        self._liquidation_source_mode = str(
            config.get("market_data.liquidation_source", "auto")
        ).lower()
        self._liquidation_feed_warmup = int(
            config.get("strategy.liquidation_catcher.feed_warmup_events", 1)
        )
        self._binance_liquidation_events = 0
        self._liquidation_feed_ready = False
        self._liquidation_feed_ready_logged = False
        self._latest_long_short_ratio: Dict[str, float] = {}
        self._latest_short_ratio: Dict[str, float] = {}

        # TCA (transaction cost analysis)
        self._tca_enabled = bool(config.get("execution.tca_enabled", True))
        self._tca_min_buffer = safe_float(
            config.get("execution.min_edge_buffer_pct", 0.05)
        ) / 100.0
        self._taker_fee_pct = safe_float(config.get("risk.taker_fee_pct", 0.035)) / 100.0
        self._paper_slippage_pct = safe_float(
            config.get("risk.paper_slippage_pct", 0.05)
        ) / 100.0
        self._last_prices: Dict[str, float] = {}
        self._last_price_ts: Dict[str, int] = {}

        # ── Cross-exchange funding + OI aggregator ──
        self._funding_aggregator = FundingOIAggregator()
        self._latest_agg_funding: Dict[str, AggregatedFundingOI] = {}
        self._funding_poll_task: Optional[asyncio.Task] = None

        # ── Trailing stop management ──
        self._trailing_data: Dict[str, Dict] = {}  # symbol -> trailing stop state

        # Circuit-breaker alert state (notify once per trip)
        self._circuit_breaker_notified: bool = False

        # Minimum hold time before evaluating exits (prevent 0s exits)
        self._min_hold_time_ms: int = int(
            config.get("engine.min_hold_time_ms", 30_000)
        )  # 30s default

        # Per-symbol entry debounce (prevent duplicate entries)
        self._last_entry_signal_ms: Dict[str, int] = {}
        self._entry_signal_debounce_ms: int = int(
            config.get("engine.entry_signal_debounce_ms", 5_000)
        )  # 5s default
        self._last_market_events: Dict[str, Dict] = {}
        self._signal_history: List[Dict] = []
        self._decision_history: List[Dict] = []

        # Strategy-level stats for dashboard drill-down (Task 5.3)
        self._strategy_stats: Dict[str, Dict[str, Any]] = {
            getattr(s, "name", "unknown"): {
                "total_signals": 0,
                "approved_signals": 0,
                "rejected_signals": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_pnl": 0.0,
                "avg_confidence": 0.0,
                "last_signal_time": None,
                "signal_history": [],  # last 20 signals for this strategy
            }
            for s in strategies
        }
        self._tick_stats = {"total": 0, "per_second": 0.0, "last_tick_time": 0.0, "tick_times": []}
        self._last_error: Optional[str] = None
        # CRIT-004: Dashboard callback — private, only set via validated setter
        self._on_dashboard_tick: Optional[Callable[[], Any]] = None

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

    # CRIT-004: Validated setter for dashboard callback
    def set_ws_client(self, ws_client: Any) -> None:
        """Store reference to the Hyperliquid WS client for health monitoring."""
        self._hl_ws_client = ws_client

    @property
    def on_dashboard_tick(self) -> Optional[Callable[[], Any]]:
        return self._on_dashboard_tick

    @on_dashboard_tick.setter
    def on_dashboard_tick(self, value: Any) -> None:
        if value is not None and not callable(value):
            raise TypeError("on_dashboard_tick must be a callable or None")
        self._on_dashboard_tick = value

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

            # L2 Orderbook
            cb_ob = self._make_orderbook_callback(symbol)
            await self._bus.subscribe(f"orderbook:{symbol}", cb_ob)
            self._subscribed_callbacks[f"orderbook:{symbol}"] = cb_ob
            logger.info("Subscribed to orderbook:%s", symbol)

            # Completed candles per timeframe
            for tf in (60, 300, 900, 3600):
                cb_candle = self._make_candle_callback(symbol, tf)
                await self._bus.subscribe(f"candle_complete:{tf}:{symbol}", cb_candle)
                self._subscribed_callbacks[f"candle_complete:{tf}:{symbol}"] = cb_candle

            cb_liq = self._make_liquidation_callback(symbol)
            await self._bus.subscribe(f"liquidation:{symbol}", cb_liq)
            self._subscribed_callbacks[f"liquidation:{symbol}"] = cb_liq

            cb_ls = self._make_ls_ratio_callback(symbol)
            await self._bus.subscribe(f"ls_ratio:{symbol}", cb_ls)
            self._subscribed_callbacks[f"ls_ratio:{symbol}"] = cb_ls

        logger.info(
            "TradingEngine running - symbols=%s strategies=%s",
            self._symbols,
            [s.name for s in self._strategies],
        )

        # 4. Start background funding + OI polling
        self._funding_poll_task = asyncio.create_task(self._poll_funding_loop())
        logger.info("FundingAggregator polling started (interval=30s)")

        # 5. Start WS health check
        self._ws_health_check_task = asyncio.create_task(self._ws_health_loop())
        logger.info("WS health check started")

    async def _ws_health_loop(self) -> None:
        """Background task: check WS health every 30s and log warnings."""
        while self._running:
            try:
                await asyncio.sleep(30)
                if self._hl_ws_client is None:
                    continue
                healthy = getattr(self._hl_ws_client, 'is_healthy', True)
                now = time.time()
                if not healthy:
                    if not self._ws_health_warned:
                        logger.warning("WS health check FAILED — no data for >90s")
                        self._ws_health_warned = True
                    if self._notifier is not None:
                        await self._notifier.send_alert("⚠️ WS disconnected — market data stalled")
                else:
                    self._last_ws_healthy_time = now
                    if self._ws_health_warned:
                        logger.info("WS health RESTORED")
                        self._ws_health_warned = False
                        if self._notifier is not None:
                            await self._notifier.send_alert("✅ WS reconnected")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("WS health check error")

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

        # Cancel background tasks
        for task_name in ("_funding_poll_task", "_ws_health_check_task"):
            task = getattr(self, task_name, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
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

    def _make_orderbook_callback(self, symbol: str):
        """Factory: returns an async callback for orderbook:* topics."""
        async def _on_orderbook(book: HlOrderbook) -> None:
            # Keep raw book for slippage estimation
            self._latest_orderbook_raw[symbol] = book
            # Convert to our PriceLevel format and calculate metrics
            bids = [PriceLevel(price=b.price, size=b.size) for b in book.bids]
            asks = [PriceLevel(price=a.price, size=a.size) for a in book.asks]
            metrics = calculate_metrics(
                bids=bids,
                asks=asks,
                symbol=symbol,
                timestamp_ms=book.timestamp_ms,
            )
            self._latest_orderbook[symbol] = metrics
            logger.debug(
                "Orderbook %s: spread=%.4f%% OIR=%.3f depth_quality=%.3f",
                symbol,
                metrics.spread_pct * 100,
                metrics.oir_10levels,
                metrics.depth_quality,
            )
        return _on_orderbook

    def _make_candle_callback(self, symbol: str, timeframe: int):
        """Factory: returns an async callback for candle_complete:* topics."""
        tf_name = {60: "1m", 300: "5m", 900: "15m", 3600: "1h"}.get(timeframe, f"{timeframe}s")

        async def _on_candle(candle: Candle) -> None:
            self._latest_candles[symbol][timeframe] = candle
            # Append 15m candles to history for ADX (regime filter)
            if timeframe == 900:
                self._candles_15m_history[symbol].append(candle)
            # Feed return to correlation monitor (any timeframe, 15m preferred)
            if timeframe == 900 and hasattr(candle, 'close'):
                prev = self._last_candle_close.get(symbol)
                curr = float(candle.close)
                if prev is not None and prev > 0:
                    self._correlation_monitor.add_candle_return(symbol, prev, curr)
                self._last_candle_close[symbol] = curr
            # Persist candle to DB (FIX: candles table was staying empty)
            try:
                db_candle = DBCandle(
                    symbol=symbol,
                    timestamp_ms=getattr(candle, 'timestamp_ms', getattr(candle, 'close_time_ms', 0)),
                    open=float(getattr(candle, 'open', getattr(candle, 'open_price', 0))),
                    high=float(getattr(candle, 'high', getattr(candle, 'high_price', 0))),
                    low=float(getattr(candle, 'low', getattr(candle, 'low_price', 0))),
                    close=float(getattr(candle, 'close', getattr(candle, 'close_price', 0))),
                    volume=float(getattr(candle, 'volume', 0)),
                    funding_rate=float(getattr(candle, 'funding', 0)),
                    oi_total=float(getattr(candle, 'oi_close', 0)),
                    oi_delta=float(getattr(candle, 'oi_delta', 0)),
                )
                self._db.save_candle(db_candle, tf_name)
            except Exception as exc:
                logger.warning("Failed to persist candle for %s %s: %s", symbol, tf_name, exc)
        return _on_candle

    def _make_liquidation_callback(self, symbol: str):
        """Factory: Binance force-order liquidation events."""

        async def _on_liquidation(event: Any) -> None:
            notional = safe_float(getattr(event, "notional_usd", 0.0))
            side = getattr(event, "side", None)
            ts = int(getattr(event, "timestamp_ms", utc_timestamp_ms()))
            if notional > 0 and side:
                self._record_liquidation(symbol, ts, notional, side, "binance")

        return _on_liquidation

    def _make_ls_ratio_callback(self, symbol: str):
        """Factory: Binance global long/short account ratio updates."""

        async def _on_ls_ratio(snap: Any) -> None:
            long_r = getattr(snap, "long_ratio", None)
            short_r = getattr(snap, "short_ratio", None)
            if long_r is not None:
                self._latest_long_short_ratio[symbol] = safe_float(long_r)
            if short_r is not None:
                self._latest_short_ratio[symbol] = safe_float(short_r)

        return _on_ls_ratio

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

        # Check if WS is healthy — warn if stale data
        if self._hl_ws_client is not None:
            ws_ok = getattr(self._hl_ws_client, 'is_healthy', True)
            if not ws_ok and not self._ws_health_warned:
                logger.warning("WS appears unhealthy — market data may be stale")
                self._ws_health_warned = True

        async with self._event_lock:
            if not self._running:
                return

            # --- Circuit breaker alert (notify once per trip) ---
            if self._notifier is not None:
                cb_tripped = self._risk.is_circuit_breaker_tripped()
                if cb_tripped and not self._circuit_breaker_notified:
                    self._circuit_breaker_notified = True
                    reason = getattr(self._risk, '_circuit_breaker_reason', 'unknown')
                    await self._notifier.circuit_breaker(reason=reason, action='Trading halted')
                elif not cb_tripped and self._circuit_breaker_notified:
                    self._circuit_breaker_notified = False

            # --- Build MarketEvent ---
            event = self._build_market_event(symbol)
            if event is None:
                return

            self._strategy_governor.evaluate(event.timestamp_ms)

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
                "orderbook_spread_pct": event.orderbook_spread_pct,
                "orderbook_oir": event.orderbook_oir,
                "orderbook_depth_quality": event.orderbook_depth_quality,
                "orderbook_bid_ask_ratio": event.orderbook_bid_ask_ratio,
                "orderbook_largest_bid_wall": event.orderbook_largest_bid_wall,
                "orderbook_largest_ask_wall": event.orderbook_largest_ask_wall,
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
                        task = asyncio.create_task(cb())
                        self._dashboard_tasks.add(task)
                        task.add_done_callback(self._dashboard_tasks.discard)
                    else:
                        cb()
                except Exception:
                    pass

            # --- Update portfolio prices (triggers unrealized PnL) ---
            await self._portfolio.update_price(symbol, event.price)

            # --- Drawdown circuit breaker check (every price tick) ---
            max_dd = await self._portfolio.get_max_drawdown()
            if self._risk.check_drawdown(max_dd):
                logger.critical(
                    "DRAWDOWN CIRCUIT BREAKER TRIPPED at %.2f%% — halting new entries",
                    max_dd * 100.0,
                )
                # CRIT-010 FIX: Flatten all open positions immediately when circuit breaker trips
                await self._flatten_all_positions(event.price)
                # Also reset the circuit breaker notification flag so it can re-trigger
                self._circuit_breaker_notified = True

            # --- Update executor price tracking ---
            await self._executor.update_position_prices({symbol: event.price})

            # --- Cache funding for FundingArbitrage pair scan ---
            if event.funding is not None:
                self._latest_funding[symbol] = event.funding
            elif event.predicted_funding is not None:
                self._latest_funding[symbol] = event.predicted_funding
            self._latest_oi_delta[symbol] = event.oi_delta

            # --- Liquidation stats (Task 3.3) ---
            if self._liquidation_source_mode == "proxy":
                self._accumulate_liquidation_proxy(symbol, event)
            elif self._liquidation_source_mode == "auto":
                pre_stats = self._get_liquidation_stats(symbol)
                if pre_stats[0] is None:
                    self._accumulate_liquidation_proxy(symbol, event)
            liq_notional, liq_side, liq_count = self._get_liquidation_stats(symbol)

            # --- FundingArbitrage pair scan (after all symbols have been seen) ---
            await self._maybe_scan_funding_arbitrage(event)

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
                # Regime-based confidence weighting
                weighted_signals = self._apply_regime_weights(signals, symbol)
                # Conflict resolution: pick highest weighted confidence
                best_signal = max(weighted_signals, key=lambda s: s.confidence)
                await self._process_entry_signal(best_signal, event)

            # --- 2. Strategy exit signals (only if position exists) ---
            exit_triggered = False
            positions = await self._portfolio.positions
            position = positions.get(symbol)
            if position is not None:
                # Only the strategy that opened the position can suggest exits
                position_strategy = position.metadata.get("strategy") if position.metadata else None

                # Enforce minimum hold time before any exit evaluation
                hold_time_ms = event.timestamp_ms - position.entry_time_ms
                if hold_time_ms < self._min_hold_time_ms:
                    logger.debug(
                        "EXIT SKIP %s — hold_time=%dms < min=%dms",
                        symbol, hold_time_ms, self._min_hold_time_ms,
                    )
                else:
                    for strategy in self._strategies:
                        if not getattr(strategy, "enabled", True):
                            continue
                        # Skip if another strategy opened this position
                        # If position_strategy is None, NO strategy can exit (safety)
                        if position_strategy != strategy.name:
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

        # Orderbook metrics (if available)
        ob = self._latest_orderbook.get(symbol)

        # ── ADX calculation (regime filter) ──
        adx = None
        hist_15m = self._candles_15m_history.get(symbol)
        if hist_15m is not None and len(hist_15m) >= 2 * self._adx_period + 1:
            adx = calculate_adx(list(hist_15m), self._adx_period)
            if adx is not None:
                self._latest_adx[symbol] = adx

        # ── Liquidation stats (Task 3.3) ──
        liq_notional, liq_side, liq_count = self._get_liquidation_stats(symbol)

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
            # Orderbook microstructure
            orderbook_spread_pct=ob.spread_pct if ob else None,
            orderbook_oir=ob.oir_10levels if ob else None,
            orderbook_depth_quality=ob.depth_quality if ob else None,
            orderbook_bid_ask_ratio=ob.bid_ask_ratio if ob else None,
            orderbook_largest_bid_wall=ob.largest_bid_wall_price if ob else None,
            orderbook_largest_ask_wall=ob.largest_ask_wall_price if ob else None,
            # Regime filter
            adx_14=adx,
            # Liquidation data (Task 3.3)
            liquidation_notional_5m=liq_notional,
            liquidation_side_5m=liq_side,
            liquidation_count_5m=liq_count,
            liquidation_data_source=self._get_liquidation_source(symbol),
            liquidation_feed_ready=self._liquidation_feed_ready,
            oi_long_ratio=self._latest_long_short_ratio.get(symbol),
            oi_short_ratio=self._latest_short_ratio.get(symbol),
        )

        # Log orderflow metrics for debugging
        logger.info(
            "MarketEvent %s: price=%.2f, funding=%.6f, predicted=%s, oi=%.2f, "
            "agg_funding=%s, agg_oi=%s, exchanges=%d, "
            "spread=%s, oir=%s, depth=%s, imbalance=%s, adx=%s",
            symbol, event.price,
            event.funding or 0,
            f"{event.predicted_funding:.6f}" if event.predicted_funding else "N/A",
            event.oi_total or 0,
            f"{event.funding_avg:.6f}" if event.funding_avg else "N/A",
            f"{event.oi_total_aggregated:,.0f}" if event.oi_total_aggregated else "N/A",
            event.oi_exchange_count,
            f"{event.orderbook_spread_pct*100:.4f}%" if event.orderbook_spread_pct else "N/A",
            f"{event.orderbook_oir:.3f}" if event.orderbook_oir else "N/A",
            f"{event.orderbook_depth_quality:.3f}" if event.orderbook_depth_quality else "N/A",
            event.bid_ask_imbalance,
            f"{event.adx_14:.1f}" if event.adx_14 is not None else "N/A",
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

    def _accumulate_liquidation_proxy(
        self,
        symbol: str,
        event: MarketEvent,
    ) -> None:
        """Estimate liquidations from sharp price moves + volume.

        This is a proxy until real liquidation WebSocket data is wired.
        A sharp drop with OI decreasing suggests long liquidations.
        A sharp pump with OI decreasing suggests short liquidations.
        """
        acc = self._liquidation_acc[symbol]
        now = event.timestamp_ms

        # Clean old events outside 5-min window
        while acc["events"] and acc["events"][0][0] < now - acc["window_ms"]:
            acc["events"].popleft()

        last_price = self._last_prices.get(symbol)
        last_ts = self._last_price_ts.get(symbol)

        if last_price is not None and last_ts is not None and last_price > 0:
            dt_ms = now - last_ts
            if dt_ms > 0:
                price_change = (event.price - last_price) / last_price
                # Annualized velocity
                velocity = abs(price_change) * (3_600_000 / dt_ms)  # per hour

                # Proxy liquidation detection:
                # Sharp move (>5% per hour) + OI decreasing
                if velocity > 0.05:  # 5% per hour
                    oi_delta = event.oi_delta
                    if oi_delta is not None and oi_delta < 0:
                        # Estimate notional: use volume_1m as proxy
                        volume_1m = event.volume_1m or 0
                        # Rough estimate: assume 30% of volume is liquidations
                        est_notional = volume_1m * event.price * 0.30
                        # Determine side based on price direction
                        if price_change < 0:
                            liq_side = "long"  # longs liquidated, price drops
                        else:
                            liq_side = "short"  # shorts liquidated, price pumps

                        acc["events"].append((now, est_notional, liq_side))
                        logger.debug(
                            "Liquidation proxy %s: %.1fM %s "
                            "(price_change=%.2f%%, OI_delta=%.0f)",
                            symbol,
                            est_notional / 1_000_000,
                            liq_side,
                            price_change * 100,
                            oi_delta,
                        )

        # Update last known price
        self._last_prices[symbol] = event.price
        self._last_price_ts[symbol] = now

    def _record_liquidation(
        self,
        symbol: str,
        timestamp_ms: int,
        notional: float,
        side: str,
        source: str,
    ) -> None:
        """Append a liquidation event to the rolling 5-minute window."""
        acc = self._liquidation_acc.get(symbol)
        if acc is None:
            return
        while acc["events"] and acc["events"][0][0] < timestamp_ms - acc["window_ms"]:
            acc["events"].popleft()
        acc["events"].append((timestamp_ms, notional, side))
        acc["source"] = source
        if source == "binance":
            self._binance_liquidation_events += 1
            if (
                not self._liquidation_feed_ready
                and self._binance_liquidation_events >= self._liquidation_feed_warmup
            ):
                self._liquidation_feed_ready = True
                if not self._liquidation_feed_ready_logged:
                    self._liquidation_feed_ready_logged = True
                    logger.info(
                        "Liquidation feed READY — %d Binance event(s) received "
                        "(warmup=%d). Auto-enable strategies may activate.",
                        self._binance_liquidation_events,
                        self._liquidation_feed_warmup,
                    )

    def _get_liquidation_source(self, symbol: str) -> Optional[str]:
        acc = self._liquidation_acc.get(symbol)
        if acc is None or not acc["events"]:
            return None
        return acc.get("source")

    def _get_liquidation_stats(
        self,
        symbol: str,
    ) -> Tuple[Optional[float], Optional[str], Optional[int]]:
        """Return (notional_5m, side_5m, count_5m) from accumulator.

        Returns the dominant side by notional.
        """
        acc = self._liquidation_acc[symbol]
        if not acc["events"]:
            return None, None, None

        total_long = 0.0
        total_short = 0.0
        count_long = 0
        count_short = 0

        for _, notional, side in acc["events"]:
            if side == "long":
                total_long += notional
                count_long += 1
            else:
                total_short += notional
                count_short += 1

        # Return dominant side
        if total_long >= total_short and total_long > 0:
            return total_long, "long", count_long
        elif total_short > 0:
            return total_short, "short", count_short
        return None, None, None

    def _apply_regime_weights(
        self,
        signals: List[Signal],
        symbol: str,
    ) -> List[Signal]:
        """Adjust signal confidence based on ADX regime.

        ADX > 25 (trending) → boost trend-following, penalize mean-reversion.
        ADX < 20 (ranging)   → boost mean-reversion, penalize trend-following.
        20-25 (neutral)      → no adjustment.

        Returns signals with modified confidence (logged but original preserved
        in metadata).
        """
        adx = self._latest_adx.get(symbol)
        if adx is None:
            return signals

        if adx > self._adx_trend_threshold:
            regime = "trend"
        elif adx < self._adx_range_threshold:
            regime = "range"
        else:
            return signals

        adjusted = apply_regime_weights_fn(
            signals,
            adx,
            self._regime_weights,
            self._adx_trend_threshold,
            self._adx_range_threshold,
        )
        result: List[Signal] = []
        for sig in adjusted:
            meta = dict(sig.metadata or {})
            w = meta.get("regime_multiplier", 1.0)
            if w != 1.0:
                raw = meta.get("confidence_raw", sig.confidence)
                logger.info(
                    "Regime %s (ADX=%.1f) — %s confidence %.2f → %.2f (weight %.2f)",
                    regime, adx, regime_strategy_name(sig), raw, sig.confidence, w,
                )
            result.append(
                Signal(
                    strategy=sig.strategy,
                    symbol=sig.symbol,
                    side=sig.side,
                    confidence=sig.confidence,
                    size_pct=sig.size_pct,
                    entry_price=sig.entry_price,
                    stop_loss_pct=sig.stop_loss_pct,
                    take_profit_pct=sig.take_profit_pct,
                    reason=sig.reason,
                    metadata={**meta, "regime": regime, "adx": adx},
                )
            )
        return result

    @staticmethod
    def _regime_strategy_name(sig: Signal) -> str:
        return regime_strategy_name(sig)

    def _find_strategy(self, name: str) -> Optional[Any]:
        """Return a registered strategy by name, searching inside StrategyEnsemble."""
        for s in self._strategies:
            if s.name == name:
                return s
            sub_map = getattr(s, "_strategies", None)
            if isinstance(sub_map, dict) and name in sub_map:
                return sub_map[name]
        return None

    def _estimate_fill_ratio(
        self,
        signal: Signal,
        size: float,
        ob_raw: Any,  # HlOrderbook
    ) -> float:
        """Return the fraction of *size* that the L2 book can absorb.

        Uses the same side logic as _estimate_slippage:
          long  → asks (we buy)
          short → bids (we sell)
        """
        from src.data.orderbook_metrics import calculate_fill_ratio, PriceLevel
        if signal.side == "long":
            levels = [PriceLevel(price=a.price, size=a.size) for a in ob_raw.asks]
        else:
            levels = [PriceLevel(price=b.price, size=b.size) for b in ob_raw.bids]
        return calculate_fill_ratio(levels, size)

    def _estimate_slippage(
        self,
        signal: Signal,
        size: float,
        ob_raw: Any,  # HlOrderbook
    ) -> float:
        """Estimate market-order slippage from L2 book for the proposed size.

        Returns slippage as a fraction (e.g. 0.002 = 0.2%).
        """
        from src.data.orderbook_metrics import estimate_slippage, PriceLevel
        if signal.side == "long":
            # Buying = hit asks
            levels = [PriceLevel(price=a.price, size=a.size) for a in ob_raw.asks]
            side = "buy"
        else:
            # Selling = hit bids
            levels = [PriceLevel(price=b.price, size=b.size) for b in ob_raw.bids]
            side = "sell"
        return estimate_slippage(levels, size, side)

    # ------------------------------------------------------------------
    # FundingArbitrage pair scan (Task 3.1)
    # ------------------------------------------------------------------

    async def _maybe_scan_funding_arbitrage(self, event: MarketEvent) -> None:
        """Periodically scan for cross-asset funding arbitrage opportunities.

        Only runs when we have funding data for all configured symbols.
        Produces paired signals (long + short) for the engine.
        """
        # Find the FundingArbitrage strategy instance (direct or inside ensemble)
        arb_strategy = self._find_strategy("FundingArbitrage")
        if arb_strategy is None:
            return  # Strategy not enabled
        if hasattr(arb_strategy, "is_active") and not arb_strategy.is_active():
            return

        # Throttle: only scan every 60 seconds
        now = event.timestamp_ms
        if hasattr(self, "_last_funding_scan_ms"):
            if now - self._last_funding_scan_ms < 60_000:
                return
        self._last_funding_scan_ms = now

        # Check if we have funding for all symbols
        missing = [sym for sym in self._symbols if sym not in self._latest_funding]
        if missing:
            logger.info(
                "FundingArbitrage scan skipped — missing funding for %s", missing,
            )
            return

        # Guard: don't re-enter while a pair is active
        if arb_strategy._active_pair is not None:
            logger.debug(
                "FundingArbitrage scan skipped — active pair %s", arb_strategy._active_pair,
            )
            return

        logger.info("FundingArbitrage scan starting — symbols=%s", self._symbols)

        # Scan for pair opportunity
        pair = arb_strategy.scan_pair_opportunity(
            funding_map=self._latest_funding,
            oi_delta_map=self._latest_oi_delta,
            timestamp_ms=now,
        )
        if pair is None:
            logger.info("FundingArbitrage scan — no pair opportunity found")
            return

        long_sig, short_sig = pair
        logger.info(
            "FundingArbitrage PAIR selected — LONG %s @ %.2f, SHORT %s @ %.2f, spread=%.4f%%",
            long_sig.symbol,
            self._latest_price.get(long_sig.symbol, type('P', (), {'mid': 0.0})()).mid,
            short_sig.symbol,
            self._latest_price.get(short_sig.symbol, type('P', (), {'mid': 0.0})()).mid,
            (short_sig.metadata.get("funding", 0) - long_sig.metadata.get("funding", 0)) * 100,
        )

        # Process both legs through the full pipeline
        # Use the correct price for each leg to avoid wrong entry_price
        for sig in (long_sig, short_sig):
            tick = self._latest_price.get(sig.symbol)
            if tick is not None:
                sig = Signal(
                    strategy=sig.strategy,
                    symbol=sig.symbol,
                    side=sig.side,
                    confidence=sig.confidence,
                    size_pct=sig.size_pct,
                    entry_price=tick.mid,
                    stop_loss_pct=sig.stop_loss_pct,
                    take_profit_pct=sig.take_profit_pct,
                    reason=sig.reason,
                    metadata=sig.metadata,
                )
            await self._process_entry_signal(sig, event)

    # ------------------------------------------------------------------
    # Cooldown manager (Task 2.4)
    # ------------------------------------------------------------------

    def _cooldown_key(self, strategy: str, symbol: str) -> str:
        return f"{strategy}:{symbol}"

    def _is_in_cooldown(
        self,
        strategy: str,
        symbol: str,
        event: MarketEvent,
    ) -> Tuple[bool, Optional[str]]:
        """Check if a (strategy, symbol) pair is in cooldown.

        Returns (in_cooldown, reason_string). Cooldown resets when:
          1. Time elapsed >= current cooldown duration
          2. Funding normalizes (|funding| < strong_threshold * 0.5)
          3. ADX regime changed from when the trade was entered
        """
        key = self._cooldown_key(strategy, symbol)
        state = self._cooldown_state.get(key)
        if state is None:
            return False, None

        now = event.timestamp_ms
        elapsed = now - state["last_trade_ms"]

        # 1. Time-based reset
        if elapsed >= state["duration_ms"]:
            logger.info(
                "Cooldown EXPIRED %s — %.1f min elapsed (limit %.1f min)",
                key, elapsed / 60_000, state["duration_ms"] / 60_000,
            )
            del self._cooldown_state[key]
            return False, None

        # 2. Funding normalization reset
        # Use a simple threshold: if current funding is mild, reset cooldown
        funding = event.funding or event.predicted_funding
        if funding is not None and abs(funding) < 0.002:  # 0.2% = "normal"
            logger.info(
                "Cooldown RESET %s — funding normalized to %.4f%%",
                key, funding * 100,
            )
            del self._cooldown_state[key]
            return False, None

        # 3. ADX regime change reset
        current_adx = event.adx_14
        if current_adx is not None and state.get("adx") is not None:
            old_regime = (
                "trend" if state["adx"] > self._adx_trend_threshold
                else "range" if state["adx"] < self._adx_range_threshold
                else "neutral"
            )
            new_regime = (
                "trend" if current_adx > self._adx_trend_threshold
                else "range" if current_adx < self._adx_range_threshold
                else "neutral"
            )
            if old_regime != new_regime and new_regime != "neutral":
                logger.info(
                    "Cooldown RESET %s — regime changed %s → %s (ADX %.1f → %.1f)",
                    key, old_regime, new_regime, state["adx"], current_adx,
                )
                del self._cooldown_state[key]
                return False, None

        remaining = (state["duration_ms"] - elapsed) / 60_000
        return True, f"cooldown {remaining:.1f}min remaining ({state['consecutive_losses']} losses)"

    def _update_cooldown_on_entry(
        self,
        strategy: str,
        symbol: str,
        event: MarketEvent,
    ) -> None:
        """Record entry time and context for future cooldown checks."""
        key = self._cooldown_key(strategy, symbol)
        self._cooldown_state[key] = {
            "last_trade_ms": event.timestamp_ms,
            "duration_ms": self._cooldown_base_ms,  # will be updated on exit
            "consecutive_losses": self._cooldown_state.get(key, {}).get("consecutive_losses", 0),
            "adx": event.adx_14,
            "funding": event.funding or event.predicted_funding,
        }

    def _update_cooldown_on_exit(
        self,
        strategy: str,
        symbol: str,
        pnl_pct: float,
    ) -> None:
        """Update cooldown state after a position is closed.

        After loss: double cooldown duration (cap at max).
        After win: reset consecutive losses and cooldown.
        """
        key = self._cooldown_key(strategy, symbol)
        state = self._cooldown_state.get(key)
        if state is None:
            return  # shouldn't happen but be safe

        if pnl_pct > 0:
            # WIN — reset cooldown
            state["consecutive_losses"] = 0
            state["duration_ms"] = self._cooldown_base_ms
            logger.info(
                "Cooldown RESET %s — WIN (pnl=%.2f%%). Back to base %.1f min.",
                key, pnl_pct * 100, self._cooldown_base_ms / 60_000,
            )
        else:
            # LOSS — increase cooldown
            state["consecutive_losses"] += 1
            new_duration = min(
                self._cooldown_base_ms * (self._cooldown_multiplier ** state["consecutive_losses"]),
                self._cooldown_max_ms,
            )
            state["duration_ms"] = int(new_duration)
            logger.info(
                "Cooldown INCREASED %s — LOSS #%d (pnl=%.2f%%). "
                "Next cooldown: %.1f min (max %.1f min)",
                key, state["consecutive_losses"], pnl_pct * 100,
                state["duration_ms"] / 60_000,
                self._cooldown_max_ms / 60_000,
            )

    # ------------------------------------------------------------------
    # Signal processing
    # ------------------------------------------------------------------

    async def _process_entry_signal(self, signal: Signal, event: MarketEvent) -> None:
        """Gate an entry signal through risk management and execute if approved."""

        # --- Engine-level entry debounce (prevent duplicate signals) ---
        last_sig_ms = self._last_entry_signal_ms.get(signal.symbol, 0)
        if event.timestamp_ms - last_sig_ms < self._entry_signal_debounce_ms:
            logger.warning(
                "Signal DEBOUNCED %s %s — last entry was %dms ago (min %dms)",
                signal.symbol, signal.side,
                event.timestamp_ms - last_sig_ms,
                self._entry_signal_debounce_ms,
            )
            return

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

        # --- Cooldown check (Task 2.4) ---
        in_cooldown, cooldown_reason = self._is_in_cooldown(
            signal.strategy, signal.symbol, event,
        )
        if in_cooldown:
            logger.info(
                "Signal REJECTED %s %s — %s",
                signal.symbol, signal.side, cooldown_reason,
            )
            sig_record["status"] = "rejected"
            sig_record["risk_reason"] = cooldown_reason
            self._decision_history.insert(0, {
                "time": sig_time,
                "type": "cooldown",
                "symbol": signal.symbol,
                "side": signal.side,
                "result": "rejected",
                "reason": cooldown_reason,
            })
            self._decision_history = self._decision_history[:100]
            return

        # --- Kelly Criterion sizing (Task 4.4) ---
        kelly_mult = self._kelly_sizer.get_size_multiplier()
        if kelly_mult != 1.0:
            # Create new signal with Kelly-adjusted size
            signal = Signal(
                strategy=signal.strategy,
                symbol=signal.symbol,
                side=signal.side,
                confidence=signal.confidence,
                size_pct=signal.size_pct * kelly_mult,
                entry_price=signal.entry_price,
                stop_loss_pct=signal.stop_loss_pct,
                take_profit_pct=signal.take_profit_pct,
                reason=f"{signal.reason} (kelly:{kelly_mult:.2f}x)",
                metadata={**signal.metadata, "kelly_multiplier": kelly_mult},
            )
            logger.info(
                "Kelly sizing applied: %s %s — base=%.4f → adjusted=%.4f (mult=%.3f)",
                signal.symbol, signal.side,
                signal.size_pct / kelly_mult, signal.size_pct, kelly_mult,
            )

        # --- Update strategy stats (Task 5.3) ---
        strat_stats = self._strategy_stats.get(signal.strategy)
        if strat_stats:
            strat_stats["total_signals"] += 1
            strat_stats["last_signal_time"] = sig_time
            strat_stats["signal_history"].insert(0, {
                "time": sig_time,
                "symbol": signal.symbol,
                "side": signal.side,
                "confidence": signal.confidence,
                "status": "pending",
            })
            strat_stats["signal_history"] = strat_stats["signal_history"][:20]

        # --- Risk check ---
        capital = await self._portfolio.current_capital
        positions = await self._portfolio.positions
        daily_pnl = await self._portfolio.daily_pnl
        daily_trades = await self._portfolio.daily_trades

        # --- Correlation check (max realized correlation) ---
        _gov = self._config.get("strategy.portfolio_governance", {})
        corr_threshold = safe_float(
            _gov.get("max_correlation", self._config.get("portfolio.max_correlation", 0.70))
        )
        if corr_threshold > 0 and positions:
            existing_syms = list(positions.keys())
            if signal.symbol not in existing_syms:
                violated, conflict_sym, corr = self._correlation_monitor.would_violate(
                    signal.symbol, existing_syms, corr_threshold
                )
                if violated:
                    reason = (
                        f"Correlation limit: |r({signal.symbol},{conflict_sym})|="
                        f"{abs(corr):.2f} > {corr_threshold:.2f}"
                    )
                    logger.info("Signal REJECTED %s %s — %s", signal.symbol, signal.side, reason)
                    sig_record["status"] = "rejected"
                    sig_record["risk_reason"] = reason
                    strat_stats = self._strategy_stats.get(signal.strategy)
                    if strat_stats:
                        strat_stats["rejected_signals"] += 1
                        if strat_stats["signal_history"]:
                            strat_stats["signal_history"][0]["status"] = "rejected"
                    self._decision_history.insert(0, {
                        "time": sig_time,
                        "type": "correlation",
                        "symbol": signal.symbol,
                        "side": signal.side,
                        "result": "rejected",
                        "reason": reason,
                    })
                    self._decision_history = self._decision_history[:100]
                    return

        # --- Risk check ---
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
            # Update strategy stats
            strat_stats = self._strategy_stats.get(signal.strategy)
            if strat_stats:
                strat_stats["rejected_signals"] += 1
                if strat_stats["signal_history"]:
                    strat_stats["signal_history"][0]["status"] = "rejected"
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

        ob_raw = self._latest_orderbook_raw.get(signal.symbol)
        signal, order_spec = resolve_order_routing(signal, self._config, ob_raw)

        slippage_for_tca = self._paper_slippage_pct
        if order_spec.order_type == "limit_maker":
            logger.info(
                "Maker route %s %s — limit @ %.4f (fee entry=%.4f%% exit=%.4f%%)",
                signal.symbol,
                signal.side,
                order_spec.limit_price or 0.0,
                order_spec.entry_fee_pct * 100,
                order_spec.exit_fee_pct * 100,
            )
            slippage_for_tca = order_spec.exit_slippage_pct
        elif ob_raw is not None:
            # 1. Fill ratio check — can the book cover enough of the size?
            fill_ratio = self._estimate_fill_ratio(signal, size, ob_raw)
            if fill_ratio < self._min_fill_ratio:
                logger.warning(
                    "Signal REJECTED %s %s - fill_ratio %.1f%% < %.0f%% "
                    "(book_depth=%.4f, size=%.6f)",
                    signal.symbol,
                    signal.side,
                    fill_ratio * 100,
                    self._min_fill_ratio * 100,
                    fill_ratio * size,
                    size,
                )
                sig_record["status"] = "rejected"
                sig_record["risk_reason"] = (
                    f"fill_ratio {fill_ratio*100:.1f}% < {self._min_fill_ratio*100:.0f}%"
                )
                self._decision_history.insert(0, {
                    "time": sig_time,
                    "type": "risk",
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "result": "rejected",
                    "reason": (
                        f"fill_ratio {fill_ratio*100:.1f}% < {self._min_fill_ratio*100:.0f}%"
                    ),
                })
                self._decision_history = self._decision_history[:100]
                return

            # 2. Slippage check — is the price impact acceptable?
            slippage = self._estimate_slippage(signal, size, ob_raw)
            if slippage > self._max_slippage_pct:
                logger.warning(
                    "Signal REJECTED %s %s - slippage %.3f%% > %.2f%% (size=%.6f)",
                    signal.symbol,
                    signal.side,
                    slippage * 100,
                    self._max_slippage_pct * 100,
                    size,
                )
                sig_record["status"] = "rejected"
                sig_record["risk_reason"] = f"slippage {slippage*100:.3f}% > {self._max_slippage_pct*100:.2f}%"
                self._decision_history.insert(0, {
                    "time": sig_time,
                    "type": "risk",
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "result": "rejected",
                    "reason": f"slippage {slippage*100:.3f}% > {self._max_slippage_pct*100:.2f}%",
                })
                self._decision_history = self._decision_history[:100]
                return
            logger.info(
                "Slippage OK %s %s - %.3f%% <= %.2f%% (size=%.6f)",
                signal.symbol,
                signal.side,
                slippage * 100,
                self._max_slippage_pct * 100,
                size,
            )
            slippage_for_tca = slippage
        else:
            logger.warning(
                "Slippage check skipped %s - no L2 book available",
                signal.symbol,
            )

        # --- TCA: reject if expected edge does not cover fees + slippage ---
        if self._tca_enabled:
            tca_ok, tca_reason = passes_tca_check(
                signal,
                self._taker_fee_pct,
                slippage_for_tca,
                self._tca_min_buffer,
                entry_fee_pct=order_spec.entry_fee_pct,
                exit_fee_pct=order_spec.exit_fee_pct,
                entry_slippage_pct=order_spec.entry_slippage_pct,
                exit_slippage_pct=order_spec.exit_slippage_pct,
            )
            if not tca_ok:
                logger.info(
                    "Signal REJECTED %s %s — %s",
                    signal.symbol, signal.side, tca_reason,
                )
                sig_record["status"] = "rejected"
                sig_record["risk_reason"] = tca_reason
                self._decision_history.insert(0, {
                    "time": sig_time,
                    "type": "tca",
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "result": "rejected",
                    "reason": tca_reason,
                })
                self._decision_history = self._decision_history[:100]
                return
            if tca_reason != "tca_skipped_no_edge_estimate":
                logger.info("TCA %s %s — %s", signal.symbol, signal.side, tca_reason)

        # --- Compute stop distance ---
        # Use strategy's ATR-based stop if provided, else fall back to engine calc
        if signal.stop_loss_pct is not None and signal.stop_loss_pct > 0:
            stop_distance_pct = signal.stop_loss_pct
        else:
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
        # Update strategy stats
        strat_stats = self._strategy_stats.get(signal.strategy)
        if strat_stats:
            strat_stats["approved_signals"] += 1
            if strat_stats["signal_history"]:
                strat_stats["signal_history"][0]["status"] = "executed"

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
        total_cost = notional + result.entry_fee
        position = Position(
            symbol=result.symbol,
            side=result.side,
            entry_price=result.entry_price,
            size=result.size,
            entry_time_ms=result.timestamp_ms,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            unrealized_pnl=0.0,
            metadata={
                "strategy": signal.strategy,
                "trade_id": result.trade_id,
                "stop_loss_pct": stop_distance_pct,
                "entry_price": result.entry_price,
                **signal.metadata,
            },
        )
        await self._portfolio.add_position(position, cost=total_cost)

        # CRIT-011 FIX: Persist portfolio snapshot after every trade entry
        await self._maybe_save_snapshot(force=True)

        # --- Record entry timestamp for debounce ---
        self._last_entry_signal_ms[signal.symbol] = result.timestamp_ms

        # --- Update cooldown state on entry (Task 2.4) ---
        self._update_cooldown_on_entry(signal.strategy, signal.symbol, event)

        logger.info(
            "Signal EXECUTED %s %s size=%.6f @ %.4f (id=%d)",
            signal.symbol,
            signal.side,
            result.size,
            result.entry_price,
            result.trade_id,
        )

        # --- Notify trade entry ---
        if self._notifier is not None:
            await self._notifier.trade_entry(
                symbol=signal.symbol,
                side=signal.side,
                size=result.size,
                price=result.entry_price,
                strategy=signal.strategy,
            )

    async def _process_exit_signal(self, exit_signal: ExitSignal, position: Position) -> None:
        """Execute a strategy-driven exit."""
        last_price = self._latest_price.get(position.symbol)
        if last_price is None:
            logger.warning("No price for %s - cannot execute exit", position.symbol)
            return

        await self._execute_exit(position, last_price.mid, reason=exit_signal.reason)

    async def _check_hard_stops(self, position: Position, current_price: float) -> None:
        """Check trailing stop, stop-loss and take-profit levels."""
        if self._trailing_enabled and position.entry_price > 0:
            await self._maybe_update_trailing_stop(position, current_price)
            positions = await self._portfolio.positions
            position = positions.get(position.symbol, position)

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

    async def _maybe_update_trailing_stop(
        self,
        position: Position,
        current_price: float,
    ) -> None:
        """Ratchet stop-loss once unrealised profit exceeds activation threshold."""
        entry = position.entry_price
        if entry <= 0:
            return

        if position.side == "long":
            pnl_pct = (current_price - entry) / entry
        else:
            pnl_pct = (entry - current_price) / entry

        if pnl_pct < self._trailing_activation_pct:
            return

        trail = self._trailing_data.setdefault(position.symbol, {})
        if position.side == "long":
            peak = max(trail.get("peak_price", entry), current_price)
            trail["peak_price"] = peak
            new_stop = peak * (1.0 - self._trailing_distance_pct)
            await self._portfolio.update_stop_loss(position.symbol, new_stop, "long")
        else:
            trough = min(trail.get("trough_price", entry), current_price)
            trail["trough_price"] = trough
            new_stop = trough * (1.0 + self._trailing_distance_pct)
            await self._portfolio.update_stop_loss(position.symbol, new_stop, "short")

    async def _execute_exit(
        self,
        position: Position,
        exit_price: float,
        reason: str,
    ) -> None:
        """Close a position and update all downstream state."""
        # Clean up trailing stop data
        if position.symbol in self._trailing_data:
            del self._trailing_data[position.symbol]

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

        # CRIT-011 FIX: Persist portfolio snapshot after every trade exit
        await self._maybe_save_snapshot(force=True)

        # Update risk manager metrics
        self._risk.on_trade_closed(result)

        # --- Update Kelly sizer with trade result (Task 4.4) ---
        self._kelly_sizer.record_trade(result.pnl_pct)

        # --- Update strategy stats on exit (Task 5.3) ---
        strategy = position.metadata.get("strategy", "unknown")

        # Clear FundingArbitrage active pair when any leg closes
        if strategy == "FundingArbitrage":
            for s in self._strategies:
                if s.name == "FundingArbitrage":
                    s.clear_active_pair()
                    break

        strat_stats = self._strategy_stats.get(strategy)
        if strat_stats:
            if result.pnl_pct > 0:
                strat_stats["winning_trades"] += 1
            else:
                strat_stats["losing_trades"] += 1
            strat_stats["total_pnl"] += result.pnl_pct
            total_trades = strat_stats["winning_trades"] + strat_stats["losing_trades"]
            strat_stats["win_rate"] = (
                strat_stats["winning_trades"] / total_trades if total_trades > 0 else 0.0
            )
            strat_stats["avg_pnl"] = (
                strat_stats["total_pnl"] / total_trades if total_trades > 0 else 0.0
            )

        # --- Update cooldown state on exit (Task 2.4) ---
        strategy = position.metadata.get("strategy", "unknown")
        self._update_cooldown_on_exit(strategy, position.symbol, result.pnl_pct)

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

        # --- Notify trade exit ---
        if self._notifier is not None:
            await self._notifier.trade_exit(
                symbol=position.symbol,
                side=position.side,
                pnl=result.pnl_usd,
                exit_price=result.exit_price if hasattr(result, 'exit_price') else result.entry_price,
                strategy=position.metadata.get("strategy", "unknown"),
            )

    async def _flatten_all_positions(self, _current_price: float) -> None:
        """Emergency liquidation of all open positions (circuit breaker).

        Uses per-symbol prices from _latest_price cache to avoid
        cross-symbol price corruption (FIX: trade #22 bug).
        """
        positions = await self._portfolio.positions
        if not positions:
            return
        logger.critical("FLATTENING %d position(s) due to circuit breaker", len(positions))
        for pos in positions.values():
            try:
                tick = self._latest_price.get(pos.symbol)
                if tick is None or tick.mid <= 0:
                    logger.warning("No price for %s — skipping flatten", pos.symbol)
                    continue
                await self._execute_exit(pos, tick.mid, reason="circuit_breaker_drawdown")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Flatten failed for %s: %s", pos.symbol, exc)

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
                    "peak_capital": snap.get("peak_capital", snap["capital"]),
                    "daily_peak_capital": snap.get("daily_peak_capital", snap["capital"]),
                    "initial_capital": snap.get("initial_capital", snap["capital"]),
                    "daily_pnl": snap["daily_pnl"],
                    "positions": positions_data,
                })
                logger.info("Recovered portfolio snapshot from DB: capital=%.2f", snap["capital"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to restore portfolio snapshot: %s", exc)
        else:
            logger.info("No prior portfolio snapshot found - starting fresh")

        # 3. Restore recent candles from DB for faster strategy warm-up
        await self._restore_candles_from_db()

    async def _restore_candles_from_db(self) -> None:
        """Load recent candle history from DB into in-memory caches
        and inject into all strategy states for instant warm-up.
        """
        tf_map = {60: "1m", 300: "5m", 900: "15m", 3600: "1h"}
        all_strategies: List[Any] = []
        for s in self._strategies:
            all_strategies.append(s)
            sub = getattr(s, "_strategies", None)
            if isinstance(sub, dict):
                all_strategies.extend(sub.values())

        for symbol in self._symbols:
            for tf_s, tf_name in tf_map.items():
                try:
                    rows = self._db.get_candles(symbol, tf_name, limit=100)
                    if rows:
                        candles = []
                        for row in rows:
                            candle = Candle(
                                open=row.open,
                                high=row.high,
                                low=row.low,
                                close=row.close,
                                volume=row.volume,
                                timestamp_ms=row.timestamp_ms,
                                open_interest=row.oi_total,
                            )
                            candles.append(candle)
                        if candles:
                            self._latest_candles[symbol][tf_s] = candles[-1]
                            if tf_s == 900:
                                self._candles_15m_history[symbol].extend(candles)
                            self._inject_candles(symbol, tf_s, candles, all_strategies)
                            logger.info(
                                "Restored %d %s candles for %s from DB",
                                len(candles), tf_name, symbol,
                            )
                except Exception as exc:
                    logger.warning(
                        "Failed to restore %s candles for %s: %s",
                        tf_name, symbol, exc,
                    )

    def _inject_candles(
        self, symbol: str, tf_s: int, candles: List, strategies: List[Any]
    ) -> None:
        """Inject historical candles into strategy candle buffers."""
        tf_attr = {60: "candles_1m", 300: "candles_5m", 900: "candles_15m", 3600: "candles_1h"}
        attr_name = tf_attr.get(tf_s)
        if attr_name is None:
            return
        for s in strategies:
            try:
                # Strategy has on_candle method (VWAPDeviation, LiquidationCatcher)
                if hasattr(s, "on_candle") and tf_s == 3600:
                    for c in candles:
                        s.on_candle(c, symbol)  # type: ignore
                    continue
                # Strategy has internal candle state via _get_state
                if hasattr(s, "_get_state"):
                    state = s._get_state(symbol)
                    deq = getattr(state, attr_name, None)
                    if deq is not None and hasattr(deq, "extend"):
                        deq.extend(candles)
            except Exception:
                pass

    async def _save_portfolio_snapshot(self) -> None:
        """Persist the current portfolio state to the DB."""
        state = await self._portfolio.to_dict()
        try:
            import json
            snapshot = PortfolioSnapshot(
                timestamp=utc_timestamp_ms(),
                capital=state["current_capital"],
                peak_capital=state["peak_capital"],
                daily_pnl=state["daily_pnl"],
                positions_json=json.dumps(state.get("positions", {})),
            )
            self._db.save_portfolio_snapshot(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to save portfolio snapshot: %s", exc)

    async def _maybe_save_snapshot(self, force: bool = False) -> None:
        """Save portfolio snapshot every ~60 seconds (simple throttle).

        CRIT-011 FIX: Accept force=True to bypass throttle for critical
        events (trade entry/exit).
        """
        now = utc_timestamp_ms()
        if not hasattr(self, "_last_snapshot_ms"):
            self._last_snapshot_ms = 0
        if force or now - self._last_snapshot_ms >= 60_000:
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
