"""Main trading engine - orchestrates data flow, strategies, risk, and execution.

The engine subscribes to the :class:`DataBus`, builds :class:`MarketEvent`s,
feeds them to registered strategies, and gates every entry signal through
the :class:`RiskManager` before handing approved trades to the
:class:`ExecutionEngine`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from src.data.database import (
    Candle as DBCandle,
    Database,
    FundingRecord,
    LiquidationRecord,
    OIRecord,
    PortfolioSnapshot,
    SignalRecord,
)
from src.data.market_data_health import (
    MarketDataHealthSummary,
    MarketDataHealthTracker,
    SymbolFeedHealth,
    compute_feed_status,
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
from src.exchanges.funding_normalize import (
    is_valid_funding,
    normalize_funding_to_8h,
)
from src.exchanges.hl_predicted_funding import HyperliquidPredictedFundingClient
from src.exchanges.binance_price_bridge import BinanceMidTick
from src.exchanges.binance_perp_price_bridge import BinancePerpMidTick
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
from src.strategies.indicators import (
    Candle,
    calculate_adx,
    calculate_mfi,
    calculate_obv_slope,
    calculate_vwap_multi_tf,
)
from src.utils.config import Config, get_strategy_section, get_trading_symbols, resolve_kelly_enabled
from src.utils.helpers import safe_float, optional_float, safe_divide, utc_timestamp_ms, resolve_trade_stop_levels

from .execution import ExecutionEngine, TradeResult
from .portfolio import PortfolioState
from .risk_manager import RiskManager
from .kelly_sizer import KellySizer
from .correlation_monitor import CorrelationMonitor
from .strategy_governor import StrategyGovernor
from .runtime_state import restore_runtime_state
from .regime import apply_regime_weights as apply_regime_weights_fn
from .regime import regime_strategy_name
from .phase08_regime_router import route_phase08_signals, SequentialContradictionGuard
from src.utils.config import phase08_enabled
from .order_router import resolve_order_routing
from .signal_pipeline import PipelineContext, SignalPipeline
from .background_tasks import BackgroundTasks
from .risk_state import RiskState
from .volatility_circuit import VolatilityCircuitBreaker
from .funding_blackout import FundingBlackoutFilter

logger = logging.getLogger(__name__)

# Cooldown funding-reset applies only to funding-driven strategies (Task 2.4).
_FUNDING_COOLDOWN_STRATEGIES = frozenset({
    "FundingExtreme",
    "FundingArbitrage",
    "FundingMomentum",
})


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
        shadow_strategies: Optional[List[Strategy]] = None,
    ) -> None:
        self._config = config
        self._db = db
        self._bus = data_bus
        self._strategies = list(strategies)
        self._shadow_strategies = list(shadow_strategies or [])
        self._risk = risk_manager
        self._executor = executor
        self._notifier = notifier
        self._mode = str(config.get("mode", "paper"))

        # ── Kelly Criterion sizer (Task 4.4) ──
        kelly_cfg = get_strategy_section(config, "kelly")
        self._kelly_enabled = resolve_kelly_enabled(config, for_backtest=False)
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

        self._shadow_recorder: Optional[Any] = None
        if self._shadow_strategies:
            from src.research.shadow_recorder import ShadowRecorder

            self._shadow_recorder = ShadowRecorder()
            logger.info(
                "Phase08 shadow mode: %d strategies (no execution) — %s",
                len(self._shadow_strategies),
                [s.name for s in self._shadow_strategies],
            )

        p08 = config.get("strategy.phase08", {}) or {}
        self._phase08_enabled = phase08_enabled(config)
        router_cfg = p08.get("regime_router", {}) or {}
        self._phase08_regime_router = (
            self._phase08_enabled and bool(router_cfg.get("enabled", True))
        )
        self._phase08_adx_range = float(
            router_cfg.get("adx_range_threshold", config.get("strategy.adx_range_threshold", 20.0))
        )
        self._phase08_adx_trend = float(
            router_cfg.get("adx_trend_threshold", config.get("strategy.adx_trend_threshold", 25.0))
        )
        self._phase08_paper_only = bool(p08.get("paper_only", True))
        seq_ms = int(router_cfg.get("sequential_contradiction_block_ms", 3_600_000))
        self._phase08_seq_guard: Optional[SequentialContradictionGuard] = (
            SequentialContradictionGuard(seq_ms) if self._phase08_regime_router else None
        )
        adx_cfg = p08.get("adx", {}) or {}
        self._adx_tf_s = int(adx_cfg.get("timeframe_s", 900))
        self._adx_closed_only = bool(adx_cfg.get("closed_candles_only", True))

        # ── C1: Intraday volatility circuit breaker (per-symbol) ──
        vol_cfg = config.get("risk.volatility_circuit_breaker", {}) or {}
        self._vol_circuit = VolatilityCircuitBreaker.from_config_dict(vol_cfg)

        # ── C2: Funding-reset blackout (global, per-time-of-day) ──
        fb_cfg = config.get("risk.funding_blackout", {}) or {}
        self._funding_blackout = FundingBlackoutFilter.from_config_dict(fb_cfg)

        trail = config.get("execution.trailing_stop", {}) or {}
        self._trailing_enabled = bool(trail.get("enabled", True))
        self._trailing_activation_pct = safe_float(trail.get("activation_pct", 0.005))
        self._trailing_distance_pct = safe_float(trail.get("trail_pct", 0.003))
        raw_trail_exclude = trail.get("exclude_strategies", [])
        self._trailing_exclude_strategies: Set[str] = (
            {str(s) for s in raw_trail_exclude}
            if isinstance(raw_trail_exclude, list)
            else set()
        )

        # Symbols to trade — canonical list from config (assets/symbols unified at load).
        self._symbols: List[str] = get_trading_symbols(config)

        # Slippage threshold (fraction, e.g. 0.002 = 0.2%)
        self._max_slippage_pct = safe_float(
            config.get("risk.max_slippage_pct", 0.2)
        ) / 100.0

        # Minimum fill ratio from L2 book (fraction, e.g. 0.8 = 80%)
        self._min_fill_ratio = safe_float(
            config.get("risk.min_fill_ratio", 0.8)
        )

        # ── Cooldown manager (Task 2.4) ──
        cooldown_cfg = get_strategy_section(config, "cooldown")
        self._cooldown_base_ms = int(safe_float(cooldown_cfg.get("base_minutes", 60)) * 60_000)
        self._cooldown_max_ms = int(safe_float(cooldown_cfg.get("max_minutes", 240)) * 60_000)
        self._cooldown_multiplier = safe_float(cooldown_cfg.get("multiplier", 2.0))
        self._funding_strong_threshold = safe_float(
            config.get("strategy.mean_reversion.strong_threshold", 0.0001)
        )
        # Per (strategy, symbol) cooldown state
        self._cooldown_state: Dict[str, Dict[str, Any]] = {}

        # ── Anti-chasing filter (engine-level) ──
        _chase = config.get("risk.chase_filter", {}) or {}
        self._chase_filter_enabled = bool(_chase.get("enabled", True))
        self._chase_lookback_hours = safe_float(_chase.get("lookback_hours", 3.0))
        self._chase_max_runup_pct = safe_float(_chase.get("max_runup_pct", 0.008))
        _chase_exempt = _chase.get(
            "exempt_strategies",
            ["VolatilityBreakout", "DonchianBreakout"],
        )
        self._chase_exempt_strategies: Set[str] = (
            set(_chase_exempt) if isinstance(_chase_exempt, list) else set()
        )

        # ── Per-symbol risk sizing multiplier ──
        _sym_mult = config.get("risk.symbol_risk_multiplier", {}) or {}
        self._symbol_risk_multipliers: Dict[str, float] = {
            str(sym): safe_float(mult, 1.0)
            for sym, mult in _sym_mult.items()
        } if isinstance(_sym_mult, dict) else {}

        # Realized correlation monitor (positions heat) — before SignalPipeline
        _gov = config.get("strategy.portfolio_governance", {})
        corr_lookback = int(
            _gov.get("max_correlation_lookback", config.get("portfolio.max_correlation_lookback", 60))
        )
        self._correlation_monitor = CorrelationMonitor(lookback=corr_lookback)
        self._last_candle_close: Dict[str, float] = {}

        self._pipeline_ctx = PipelineContext(cooldown_state=self._cooldown_state)
        self._signal_pipeline = SignalPipeline(
            config,
            risk_manager,
            kelly_sizer=self._kelly_sizer,
            vol_circuit=self._vol_circuit,
            funding_blackout=self._funding_blackout,
            correlation_monitor=self._correlation_monitor,
            feed_block_fn=self._entry_feed_block_reason,
            use_regime_weights=False,
            kelly_enabled=self._kelly_enabled,
            tca_enabled=bool(config.get("execution.tca_enabled", True)),
            for_backtest=False,
        )

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

        # In-memory cache of latest data per symbol
        self._latest_price: Dict[str, HlPriceTick] = {}
        self._mark_prices_sync: Dict[str, float] = {}
        self._mark_prices_lock = threading.Lock()
        self._latest_binance_mid: Dict[str, BinanceMidTick] = {}
        self._latest_binance_perp_mid: Dict[str, BinancePerpMidTick] = {}
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

        # 5m candle history for OBV / MFI (v3.1.15 observability)
        self._candles_5m_history: Dict[str, Any] = {
            sym: collections.deque(maxlen=200)
            for sym in self._symbols
        }

        # Portfolio state (creates fresh; DB recovery happens in start())
        initial_capital = safe_float(
            config.get("risk.initial_capital", config.get("backtest.initial_capital", 10_000.0))
        )
        self._portfolio = PortfolioState(initial_capital)

        # Volatility circuit + funding blackout wired above via SignalPipeline

        # Internal state
        self._running: bool = False
        self._shutdown_event: Optional[asyncio.Event] = None
        # B2: per-symbol locks replace the old global _event_lock so events
        # for different symbols process in parallel. PortfolioState and the
        # trade-execution subsystem keep their own internal locks for
        # cross-symbol writes.
        self._symbol_locks: Dict[str, asyncio.Lock] = {
            sym: asyncio.Lock() for sym in self._symbols
        }
        # Held only by cross-symbol operations (e.g. circuit-breaker flatten)
        # that must not interleave with themselves.
        self._cross_symbol_lock = asyncio.Lock()
        self._start_time: Optional[float] = None
        # v3.1.17 C8: pending flatten request (reason, skip_symbol) is set
        # inside the per-symbol lock and processed *after* release, so the
        # cross-symbol flatten call doesn't hold the symbol A lock while
        # waiting for symbol B's lock (deadlock risk).
        self._pending_flatten: Optional[Tuple[str, Optional[str]]] = None
        # v3.1.17 C8: second leg of a FundingArbitrage pair (set inside the
        # current symbol's lock; processed after release).
        self._pending_funding_pair: Optional[Signal] = None

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
        self._summary_task: Optional[asyncio.Task] = None
        # v3.1.17 C9: reconciliation loop (testnet/mainnet only)
        self._reconcile_task: Optional[asyncio.Task] = None
        self._reconciler: Optional[Any] = None
        self._protection_manager: Optional[Any] = None
        recon_cfg = config.get("reconciliation", {}) or {}
        self._reconciliation_enabled = bool(recon_cfg.get("enabled", True))
        self._reconciliation_interval_sec = float(recon_cfg.get("interval_sec", 60))
        self._reconciliation_block_when_stale = bool(
            recon_cfg.get("block_entries_when_stale", True)
        )
        native_cfg = config.get("execution.native_protection", {}) or {}
        self._native_protection_enabled = bool(native_cfg.get("enabled", True))
        self._software_stop_redundancy = bool(
            native_cfg.get("software_stop_redundancy", True)
        )
        self._last_ws_healthy_time: float = time.time()
        self._ws_disconnect_start: Optional[float] = None
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
        _coinalyze_key = (
            config.get("market_data.coinalyze_api_key")
            or os.environ.get("COINALYZE_API_KEY")
        )
        _md = config.get("market_data", {}) or {}
        _stale_sec = float(_md.get("funding_stale_max_sec", 300))
        _connect_t = float(_md.get("funding_connect_timeout_sec", 10))
        _total_t = float(_md.get("funding_total_timeout_sec", 25))
        self._min_exchanges_green = int(_md.get("min_exchanges_for_green", 2))
        self._funding_aggregator = FundingOIAggregator(
            coinalyze_key=str(_coinalyze_key) if _coinalyze_key else None,
            stale_max_sec=_stale_sec,
            connect_timeout=_connect_t,
            total_timeout=_total_t,
        )
        self._latest_agg_funding: Dict[str, AggregatedFundingOI] = {}
        self._funding_poll_task: Optional[asyncio.Task] = None
        self._market_data_health: Dict[str, SymbolFeedHealth] = {}
        _health_window = float(_md.get("health_history_window_sec", 3600))
        self._health_tracker = MarketDataHealthTracker(window_sec=_health_window)
        self._market_data_health_summary = MarketDataHealthSummary()
        self._md_red_since: Optional[float] = None
        self._md_alert_after_sec = float(_md.get("alert_red_after_sec", 300))
        self._md_alert_cooldown_sec = float(_md.get("alert_red_cooldown_sec", 900))
        self._block_entries_on_feed_stale = bool(
            _md.get("block_entries_on_stale", _md.get("block_entries_on_red", True))
        )
        self._block_entries_on_ws_unhealthy = bool(
            _md.get("block_entries_on_ws_unhealthy", True)
        )
        self._feed_health_evaluated: bool = False
        self._feed_health_ready: bool = False
        self._restore_invocation_count = 0
        self._last_md_alert_ts: float = 0.0
        _hl_testnet = bool(config.get("exchange.hyperliquid.testnet", False))
        self._hl_predicted = HyperliquidPredictedFundingClient(
            use_testnet=_hl_testnet,
            stale_max_sec=_stale_sec,
            connect_timeout=_connect_t,
            total_timeout=_total_t,
        )
        self._hl_predicted_poll_sec: int = int(
            _md.get("hl_predicted_funding_poll_sec", 90)
        )
        self._funding_poll_sec: int = int(_md.get("funding_poll_sec", 30))
        self._last_hl_predicted_poll: float = 0.0
        self._last_saved_oi_total: Dict[str, float] = {}

        # ── Trailing stop management ──
        self._trailing_data: Dict[str, Dict] = {}  # symbol -> trailing stop state

        # Circuit-breaker alert state (notify once per trip)
        self._circuit_breaker_notified: bool = False

        # Minimum hold time before evaluating exits (prevent 0s exits)
        self._min_hold_time_ms: int = int(
            config.get("engine.min_hold_time_ms", 30_000)
        )  # 30s default

        # Per-symbol entry debounce state lives in PipelineContext.last_entry_ms
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

        # ── Notifier fire-and-forget queue (B3) ──
        self._notify_tasks: "set[asyncio.Task[Any]]" = set()
        self._notify_max_pending: int = int(
            getattr(config, "get", lambda *_: None)("alerts.max_pending", 100) or 100
        )
        self._notify_concurrency: int = int(
            getattr(config, "get", lambda *_: None)("alerts.max_concurrent", 4) or 4
        )
        self._notify_sema: Optional[asyncio.Semaphore] = None  # bound in start()

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

    def get_mark_prices_sync(self) -> Dict[str, float]:
        """Thread-safe latest mids for dashboard / Telegram (updated every tick)."""
        with self._mark_prices_lock:
            return dict(self._mark_prices_sync)

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
    def last_tick_age_sec(self) -> Optional[float]:
        """Seconds since the last market tick arrived, or None if no tick yet."""
        last = self._tick_stats.get("last_tick_time") or 0.0
        if last <= 0:
            return None
        return max(0.0, time.time() - last)

    # ── Notifier fire-and-forget (B3) ──

    def _get_symbol_lock(self, symbol: str) -> asyncio.Lock:
        """Return the per-symbol asyncio lock, creating on demand.

        B2: a per-symbol lock replaces the previous global ``_event_lock``
        so events for different symbols (BTC vs ETH vs SOL) process in
        parallel. Same-symbol events stay serialized, preserving the
        read-your-writes consistency the strategy code depends on.
        """
        lock = self._symbol_locks.get(symbol)
        if lock is None:
            lock = asyncio.Lock()
            self._symbol_locks[symbol] = lock
        return lock

    def _notify(self, coro_factory: Callable[[], "asyncio.Future[Any]"]) -> None:
        """Schedule a notifier coroutine fire-and-forget.

        Bounded by ``_notify_max_pending`` (drops oldest when full) and
        ``_notify_concurrency`` (semaphore limits parallel HTTP calls).
        Prevents a slow Telegram/Discord response from stalling the engine loop.
        """
        if self._notifier is None:
            return

        if self._notify_sema is None:
            self._notify_sema = asyncio.Semaphore(self._notify_concurrency)

        # Cap pending tasks to avoid unbounded memory growth on alert storms.
        pending = [t for t in self._notify_tasks if not t.done()]
        if len(pending) >= self._notify_max_pending:
            logger.warning(
                "Notifier backlog full (%d pending) — dropping new alert",
                len(pending),
            )
            return

        async def _runner() -> None:
            assert self._notify_sema is not None
            try:
                async with self._notify_sema:
                    await coro_factory()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Notifier task failed")

        task = asyncio.create_task(_runner())
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    @property
    def _background_tasks(self) -> BackgroundTasks:
        """Lazily-created holder for the engine's background loop bodies.

        Lazy so that tests building the engine via
        ``TradingEngine.__new__(TradingEngine)`` (bypassing ``__init__``)
        still work — the object is only constructed on first access, and
        it just wraps ``self``, so it needs no attributes to exist yet.
        """
        impl = self.__dict__.get("_background_tasks_impl")
        if impl is None:
            impl = BackgroundTasks(self)
            self.__dict__["_background_tasks_impl"] = impl
        return impl

    @property
    def _risk_state(self) -> RiskState:
        """Lazily-created holder for engine-owned runtime risk state.

        See ``_background_tasks`` for why this is lazy rather than set in
        ``__init__``.
        """
        impl = self.__dict__.get("_risk_state_impl")
        if impl is None:
            impl = RiskState(self)
            self.__dict__["_risk_state_impl"] = impl
        return impl

    @property
    def positions(self) -> Dict[str, Any]:
        return self._portfolio.get_positions_sync() if hasattr(self._portfolio, "get_positions_sync") else {}

    @property
    def portfolio_snapshot_sync(self):
        """Return an atomic frozen snapshot of portfolio state (B2b).

        Safe to call from sync contexts (e.g. the dashboard emitter
        thread). Performs a single internal lock acquisition, eliminating
        the TOCTOU window between separate sync reads.
        """
        if not hasattr(self._portfolio, "snapshot_sync"):
            return None
        try:
            return self._portfolio.snapshot_sync()
        except Exception:
            logger.exception("portfolio_snapshot_sync failed")
            return None

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
        logger.info(
            "Effective risk: leverage=%.1fx max_daily_loss=%.1f%% max_daily_trades=%d max_pos=%.1f%%",
            float(self._config.get("risk.leverage_max", 1.0)),
            float(self._config.get("risk.max_daily_loss_pct", 0.0)),
            int(self._config.get("risk.max_daily_trades", 0)),
            float(self._config.get("risk.max_position_size_pct", 0.0)),
        )

        # 1. Open executor session
        await self._executor.open()
        if hasattr(self._executor, "set_portfolio"):
            self._executor.set_portfolio(self._portfolio)
        if self._mode in ("testnet", "mainnet") and self._reconciliation_enabled:
            self._init_live_reconciliation()
        if hasattr(self._executor, "set_oms_alert_callback"):
            self._executor.set_oms_alert_callback(self._on_oms_alert)
        if hasattr(self._executor, "register_order_callback"):
            self._executor.register_order_callback(self._on_oms_status_change)

        # 2. Recover DB state (single restore path)
        await self._recover_state()
        await self._portfolio.reconcile_peaks()
        dd0 = await self._portfolio.get_max_drawdown()
        if not self._risk.is_circuit_breaker_tripped():
            self._risk.check_drawdown(dd0)
        equity0 = await self._portfolio.current_capital
        logger.info(
            "Portfolio ready: equity=%.2f drawdown=%.2f%% circuit_breaker=%s daily_dd=%s stop_streak=%d",
            equity0,
            dd0 * 100.0,
            self._risk.is_circuit_breaker_tripped(),
            self._risk.is_daily_drawdown_circuit_tripped(),
            self._risk.daily_stop_loss_count,
        )
        if self._kelly_enabled:
            self._seed_kelly_from_db()
        else:
            logger.info("Kelly sizing disabled by strategy.kelly.enabled=false")

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

            cb_bn = self._make_binance_price_callback(symbol)
            await self._bus.subscribe(f"binance_price:{symbol}", cb_bn)
            self._subscribed_callbacks[f"binance_price:{symbol}"] = cb_bn

            cb_bn_perp = self._make_binance_perp_price_callback(symbol)
            await self._bus.subscribe(f"binance_perp_price:{symbol}", cb_bn_perp)
            self._subscribed_callbacks[f"binance_perp_price:{symbol}"] = cb_bn_perp

        logger.info(
            "TradingEngine running - symbols=%s strategies=%s",
            self._symbols,
            [s.name for s in self._strategies],
        )

        # 4. Start background funding + OI polling
        self._funding_poll_task = asyncio.create_task(self._poll_funding_loop())
        logger.info("FundingAggregator polling started (interval=30s)")

        # 6. Start periodic summary loop
        self._summary_task = asyncio.create_task(self._periodic_summary_loop())
        logger.info("Periodic summary loop started (interval=900s)")

        # 7. Start reconciliation loop (testnet/mainnet only) — v3.1.17 C9
        if self._mode in ("testnet", "mainnet"):
            self._reconcile_task = asyncio.create_task(self._reconcile_loop())
            logger.info("Reconciliation loop started (interval=60s)")

        # 8. v3.1.22: start WS health monitoring loop. The
        # _ws_health_loop() coroutine has existed since v3.1.18 but
        # was never wired into a task. Without it, a silent WS
        # disconnect would not be flagged until the next failed
        # trade.
        if self._hl_ws_client is not None:
            self._ws_health_check_task = asyncio.create_task(
                self._ws_health_loop()
            )
            logger.info("WS health monitoring loop started")

        if self._mode in ("testnet", "mainnet") and hasattr(self._executor, "start_oms_loop"):
            await self._executor.start_oms_loop()
            logger.info("OMS poller started from TradingEngine")

        if self._reconciler is not None:
            try:
                report = await self._reconciler.reconcile_once(executor=self._executor)
                logger.info(
                    "Startup reconciliation: success=%s exchange_pos=%d local=%d",
                    report.success,
                    len(report.exchange_positions),
                    len(report.local_symbols),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Startup reconciliation failed: %s", exc)

    def _init_live_reconciliation(self) -> None:
        """Wire Phase 03 reconciler + native protection manager."""
        from src.core.native_protection import NativeProtectionManager
        from src.core.reconciliation import ExchangeReconciler

        live_client = getattr(self._executor, "_live_client", None)
        if live_client is None or not getattr(self._executor, "_live_signing_ready", False):
            logger.warning("Live reconciliation skipped — signing client not ready")
            return

        recon_cfg = self._config.get("reconciliation", {}) or {}
        self._protection_manager = NativeProtectionManager(live_client, self._db)
        self._reconciler = ExchangeReconciler(
            live_client=live_client,
            portfolio=self._portfolio,
            db=self._db,
            protection=self._protection_manager,
            orphan_exchange_policy=str(
                recon_cfg.get("orphan_exchange_policy", "ADOPT_AND_PROTECT")
            ),
            mismatch_policy=str(recon_cfg.get("mismatch_policy", "HALT")),
            stale_threshold_sec=float(recon_cfg.get("stale_threshold_sec", 120)),
            alert_callback=self._reconciliation_alert,
        )
        if hasattr(self._executor, "set_protection_manager"):
            self._executor.set_protection_manager(self._protection_manager)
        if hasattr(self._executor, "set_reconciler"):
            self._executor.set_reconciler(self._reconciler)

    def _reconciliation_alert(self, message: str, level: str = "warning") -> None:
        if self._notifier is not None:
            self._notify(lambda m=message, lv=level: self._notifier.send_alert(m, lv))

    def _on_oms_status_change(self, order_id: str, status: str, record: Dict[str, Any]) -> None:
        asyncio.create_task(self._handle_oms_status_change(order_id, status, record))

    def _on_oms_alert(self, event: str, order_id: str, record: Dict[str, Any]) -> None:
        if self._notifier is None:
            return
        symbol = str(record.get("symbol", "?"))
        trade_id = record.get("trade_id", "?")
        filled = safe_float(record.get("filled_size"))
        remaining = safe_float(record.get("remaining_size"))
        if event == "timeout":
            msg = (
                f"OMS TIMEOUT: {symbol} order={order_id} trade_id={trade_id} "
                f"filled={filled:.6f} remaining={remaining:.6f}"
            )
            level = "error"
        elif event == "partial_residual":
            msg = (
                f"OMS PARTIAL RESIDUAL: {symbol} order={order_id} "
                f"kept={filled:.6f} cancelled_remaining={remaining:.6f}"
            )
            level = "warning"
        elif event == "cancel_failed":
            msg = (
                f"OMS CANCEL FAILED: {symbol} order={order_id} "
                f"partial exposure preserved size={filled:.6f}"
            )
            level = "error"
        else:
            msg = (
                f"OMS UNKNOWN STATUS: {symbol} order={order_id} event={event}"
            )
            level = "warning"
        self._notify(lambda m=msg, lv=level: self._notifier.send_alert(m, lv))

    async def _handle_oms_status_change(
        self,
        order_id: str,
        status: str,
        record: Dict[str, Any],
    ) -> None:
        """Open or extend portfolio exposure from confirmed live fills."""
        from src.core.execution import ORDER_STATUS_FILLED, ORDER_STATUS_PARTIAL

        if status not in (ORDER_STATUS_FILLED, ORDER_STATUS_PARTIAL):
            return

        trade_id = int(record.get("trade_id", 0))
        if trade_id <= 0:
            return

        db_row = self._db.get_trade_by_id(trade_id) or {}
        filled_size = safe_float(record.get("filled_size"))
        if filled_size <= 0:
            return

        avg_px = safe_float(record.get("avg_fill_price"), safe_float(record.get("price")))
        fee = safe_float(record.get("cumulative_fee"))
        symbol = str(record.get("symbol", ""))
        side = str(record.get("side", "long"))
        applied_before = safe_float(record.get("_portfolio_applied_fill", 0.0))
        delta = filled_size - applied_before
        if delta <= 0:
            return

        sl_price, tp_price = resolve_trade_stop_levels(
            entry_price=avg_px,
            side=side,
            signal_metadata=db_row.get("signal_metadata"),
        )
        strategy = str(db_row.get("strategy") or "unknown")
        sub_strategy = str(db_row.get("sub_strategy") or strategy)
        pos = Position(
            symbol=symbol,
            side=side,
            entry_price=avg_px,
            size=delta,
            entry_time_ms=int(record.get("submitted_at_ms") or db_row.get("entry_time") or 0),
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            unrealized_pnl=0.0,
            current_price=avg_px,
            metadata={
                "strategy": strategy,
                "sub_strategy": sub_strategy,
                "trade_id": trade_id,
                "exchange_order_id": order_id,
            },
        )
        fee_delta = fee * safe_divide(delta, filled_size, 0.0) if filled_size > 0 else 0.0
        cost_delta = (avg_px * delta) + fee_delta
        await self._portfolio.apply_entry_fill(
            symbol,
            filled_size=delta,
            avg_fill_price=avg_px,
            additional_cost=cost_delta,
            fee_delta=fee_delta,
            position=pos,
        )
        # First confirmed live fill: arm sequential contradiction guard
        if applied_before <= 0 and self._phase08_seq_guard is not None and symbol and side:
            ts_ms = int(record.get("submitted_at_ms") or db_row.get("entry_time") or 0)
            if ts_ms <= 0:
                ts_ms = int(time.time() * 1000)
            self._phase08_seq_guard.record(symbol, side, ts_ms)
        record["_portfolio_applied_fill"] = filled_size
        trade_id = int(record.get("trade_id", 0))
        try:
            self._db.update_trade_native_protection(
                trade_id,
                applied_fill_size=filled_size,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("applied_fill_size DB update failed: %s", exc)

        await self._place_native_protection_after_fill(
            symbol=symbol,
            trade_id=trade_id,
            filled_size=filled_size,
            sl_price=sl_price,
            tp_price=tp_price,
            side=side,
            avg_px=avg_px,
            record=record,
        )

        if status == ORDER_STATUS_FILLED and self._notifier is not None:
            self._notify(
                lambda: self._notifier.trade_entry(
                    symbol,
                    side,
                    filled_size,
                    avg_px,
                    strategy,
                    stop_loss=sl_price,
                    take_profit=tp_price,
                    notional_usd=filled_size * avg_px,
                )
            )

    async def _place_native_protection_after_fill(
        self,
        *,
        symbol: str,
        trade_id: int,
        filled_size: float,
        sl_price: Optional[float],
        tp_price: Optional[float],
        side: str,
        avg_px: float,
        record: Dict[str, Any],
    ) -> None:
        """Place or resize native SL/TP triggers for confirmed fill size."""
        if not self._native_protection_enabled or self._protection_manager is None:
            return
        if self._mode not in ("testnet", "mainnet"):
            return

        positions = await self._portfolio.positions
        pos = positions.get(symbol)
        if pos is None:
            from src.strategies.base import Position

            pos = Position(
                symbol=symbol,
                side=side,
                entry_price=avg_px,
                size=filled_size,
                entry_time_ms=int(record.get("submitted_at_ms") or 0),
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
                metadata={"trade_id": trade_id},
            )
        else:
            pos = pos  # use live book position with updated size

        try:
            if sl_price is not None and sl_price > 0:
                self._db.enrich_trade_stop_metadata(
                    trade_id=trade_id,
                    stop_loss_price=float(sl_price),
                    take_profit_price=float(tp_price) if tp_price else None,
                    stop_loss_pct=0.0,
                    take_profit_pct=None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("enrich_trade_stop_metadata failed: %s", exc)

        result = await self._protection_manager.ensure_protection(
            pos,
            filled_size=filled_size,
            stop_price=sl_price,
            take_profit_price=tp_price,
            trade_id=trade_id,
        )
        if result.sl_order_id or result.tp_order_id:
            meta = pos.metadata or {}
            meta["native_protection_active"] = True
            meta["native_sl_oid"] = result.sl_order_id
            meta["native_tp_oid"] = result.tp_order_id
            meta["native_protected_size"] = result.protected_size
            await self._portfolio.update_position_metadata(symbol, meta)
        if result.errors:
            logger.error(
                "Native protection errors for %s trade_id=%s: %s",
                symbol, trade_id, result.errors,
            )

    async def _reconcile_loop(self) -> None:
        """Reconcile local state with Hyperliquid user_state (Phase 03)."""
        await self._background_tasks.reconcile_loop()

    async def kill_switch(self) -> Any:
        """Emergency flatten: cancel orders, close positions, confirm exchange flat."""
        if not hasattr(self._executor, "kill_switch"):
            raise RuntimeError("Kill switch not available in this execution mode")
        result = await self._executor.kill_switch()
        if self._reconciler is not None:
            await self._reconciler.reconcile_once(executor=self._executor)
        return result

    async def _ws_health_loop(self) -> None:
        """Background task: check WS health every 30s."""
        await self._background_tasks.ws_health_loop()

    async def _periodic_summary_loop(self) -> None:
        """Log a structured summary every 15 min: exposure, DD, PnL, active strategies."""
        await self._background_tasks.periodic_summary_loop()

    def _refresh_market_data_health(self) -> None:
        """Rebuild per-symbol feed health from latest polls."""
        now_ms = int(time.time() * 1000)
        for sym in self._symbols:
            agg = self._latest_agg_funding.get(sym)
            hl = self._hl_predicted.get(sym)
            ctx = self._latest_ctx.get(sym)
            cex_ok = agg is not None and agg.exchange_count > 0
            cex_stale = bool(agg.stale) if agg else True
            cex_age = agg.age_sec if agg else 9999.0
            if agg and not agg.stale and agg.timestamp_ms:
                cex_age = (now_ms - agg.timestamp_ms) / 1000.0
            cex_exchanges = sorted(agg.by_exchange.keys()) if agg else []
            hl_ok = hl is not None and bool(hl.venues)
            hl_stale = bool(hl.stale) if hl else True
            hl_age = hl.age_sec if hl else 9999.0
            if hl and not hl.stale and hl.timestamp_ms:
                hl_age = (now_ms - hl.timestamp_ms) / 1000.0
            status = compute_feed_status(
                cex_ok=cex_ok,
                cex_stale=cex_stale,
                cex_exchange_count=agg.exchange_count if agg else 0,
                min_exchanges=self._min_exchanges_green,
                hl_ok=hl_ok,
                hl_stale=hl_stale,
            )
            self._health_tracker.record(
                sym,
                cex_ok=cex_ok,
                hl_ok=hl_ok,
                status=status,
                timestamp_ms=now_ms,
            )
            polls, failed, fail_rate = self._health_tracker.symbol_stats(sym)
            funding_hl_ws: Optional[float] = None
            if ctx is not None and ctx.funding_rate is not None:
                funding_hl_ws = normalize_funding_to_8h(
                    ctx.funding_rate,
                    float(ctx.funding_interval_hours or 1.0),
                )
            self._market_data_health[sym] = SymbolFeedHealth(
                symbol=sym,
                cex_ok=cex_ok,
                cex_stale=cex_stale,
                cex_age_sec=cex_age,
                cex_exchanges=cex_exchanges,
                cex_exchange_count=agg.exchange_count if agg else 0,
                hl_predicted_ok=hl_ok,
                hl_predicted_stale=hl_stale,
                hl_predicted_age_sec=hl_age,
                hl_venues=sorted(hl.venues.keys()) if hl else [],
                status=status,
                polls_1h=polls,
                failed_polls_1h=failed,
                failure_rate_1h=fail_rate,
                funding_hl_ws=funding_hl_ws,
                funding_hl_predicted_8h=(
                    hl.predicted_funding_hl_8h if hl else None
                ),
                funding_cex_avg_8h=agg.funding_avg if agg else None,
                oi_cex_usd=agg.oi_total if agg else None,
                oi_hl=getattr(ctx, "open_interest", None) if ctx else None,
            )

        polls_all, failed_all, rate_all, _ = self._health_tracker.overall_stats()
        statuses = [h.status for h in self._market_data_health.values()]
        if not statuses:
            overall = "red"
        elif any(s == "red" for s in statuses):
            overall = "red"
        elif any(s == "yellow" for s in statuses):
            overall = "yellow"
        else:
            overall = "green"
        red_since = 0.0
        if overall == "red":
            if self._md_red_since is None:
                self._md_red_since = time.time()
            red_since = time.time() - self._md_red_since
        else:
            self._md_red_since = None

        self._market_data_health_summary = MarketDataHealthSummary(
            overall=overall,
            symbols=dict(self._market_data_health),
            polls_1h=polls_all,
            failed_polls_1h=failed_all,
            failure_rate_1h=rate_all,
            red_since_sec=red_since,
        )
        self._feed_health_evaluated = True
        if overall != "red":
            self._feed_health_ready = True

    async def _check_market_data_alerts(self) -> None:
        """Telegram alert when fleet health stays red beyond threshold."""
        if self._notifier is None:
            return
        summary = self._market_data_health_summary
        if summary.overall != "red":
            return
        if summary.red_since_sec < self._md_alert_after_sec:
            return
        now = time.time()
        if now - self._last_md_alert_ts < self._md_alert_cooldown_sec:
            return
        self._last_md_alert_ts = now
        details_lines = []
        for sym, h in summary.symbols.items():
            if h.status == "red":
                details_lines.append(
                    f"{sym}: cex={h.cex_exchange_count} hl_ok={h.hl_predicted_ok}"
                )
        details = "\n".join(details_lines[:8]) or "all symbols degraded"
        overall = summary.overall
        red_min = summary.red_since_sec / 60.0
        self._notify(
            lambda: self._notifier.market_data_health_red(
                overall, details, red_min
            )
        )

    def _persist_funding_oi_snapshot(self) -> None:
        """Write latest funding + OI to SQLite for backtest replay."""
        now_ms = int(time.time() * 1000)
        for sym in self._symbols:
            agg = self._latest_agg_funding.get(sym)
            hl = self._hl_predicted.get(sym)

            predicted: Optional[float] = None
            if hl is not None and hl.predicted_funding_hl_8h is not None:
                predicted = float(hl.predicted_funding_hl_8h)

            current: Optional[float] = None
            if agg is not None:
                if agg.funding_weighted is not None:
                    current = float(agg.funding_weighted)
                elif agg.funding_avg is not None:
                    current = float(agg.funding_avg)
            if current is None and predicted is not None:
                current = predicted

            if current is not None:
                try:
                    self._db.save_funding(
                        FundingRecord(
                            symbol=sym,
                            current=current,
                            predicted=predicted if predicted is not None else current,
                            timestamp=now_ms,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("save_funding %s failed: %s", sym, exc)

            oi_total: Optional[float] = None
            if agg is not None and agg.oi_total is not None:
                oi_total = float(agg.oi_total)
            if oi_total is not None:
                prev = self._last_saved_oi_total.get(sym)
                oi_delta = (oi_total - prev) if prev is not None else 0.0
                self._last_saved_oi_total[sym] = oi_total
                try:
                    self._db.save_oi(
                        OIRecord(
                            symbol=sym,
                            oi_total=oi_total,
                            oi_delta=oi_delta,
                            timestamp=now_ms,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("save_oi %s failed: %s", sym, exc)

    async def _poll_funding_loop(self) -> None:
        """Background task: poll CEX funding/OI and HL predictedFundings."""
        await self._background_tasks.poll_funding_loop()

    def _should_close_positions_on_shutdown(self) -> bool:
        """True when graceful stop should flatten all open positions.

        Paper/testnet default is false (restore from DB on next start).
        Mainnet override is true until SL/TP are placed as native trigger
        orders on the exchange via the Hyperliquid SDK at entry time.
        """
        return self._risk_state.should_close_positions_on_shutdown()

    def _seed_kelly_from_db(self) -> None:
        """Pre-load KellySizer from recent closed trades in SQLite."""
        self._risk_state.seed_kelly_from_db()

    async def stop(self) -> None:
        """Graceful shutdown: close all positions, save state, unsubscribe."""
        if not self._running:
            return

        logger.info("TradingEngine stopping …")
        self._running = False
        if self._shutdown_event is not None:
            self._shutdown_event.set()

        # Cancel background tasks
        for task_name in (
            "_funding_poll_task", "_ws_health_check_task", "_summary_task",
            "_reconcile_task",
        ):
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

        if self._mode in ("testnet", "mainnet") and hasattr(self._executor, "stop_oms_loop"):
            await self._executor.stop_oms_loop()

        # 2. Close all open positions (market order at last known price)
        # v3.1.42: gated by engine.close_positions_on_shutdown; when unset,
        # flatten_on_stop under the order-routing config applies. Paper/testnet
        # preserves positions; mainnet flattens until SDK native SL/TP triggers exist.
        flatten_on_stop = self._should_close_positions_on_shutdown()
        positions_snapshot = await self._portfolio.positions
        if not flatten_on_stop and positions_snapshot:
            logger.info(
                "Preserving %d open position(s) across restart "
                "(close_positions_on_shutdown=false); will reconcile on next start.",
                len(positions_snapshot),
            )
        for symbol, position in positions_snapshot.items():
            if not flatten_on_stop:
                # Skip the close, but still log + persist.
                last_price = self._latest_price.get(symbol)
                mark = getattr(last_price, "mid", None) if last_price is not None else None
                logger.info(
                    "Position preserved (flatten_on_stop=false): %s side=%s size=%.6f mark=%s",
                    symbol, position.side, position.size, mark,
                )
                continue
            last_price = self._latest_price.get(symbol)
            if last_price is not None:
                await self._execute_exit(position, last_price.mid, reason="engine_shutdown")
            else:
                logger.warning("No last price for %s during shutdown - skipping close", symbol)

        # 3. Save final portfolio snapshot
        await self._save_portfolio_snapshot()
        self._persist_runtime_state()

        # 4. Close executor
        await self._executor.close()

        # 5. Drain pending notifier tasks (B3) — cap wait at 5s
        if self._notify_tasks:
            pending = [t for t in self._notify_tasks if not t.done()]
            if pending:
                logger.info("Draining %d pending notifier tasks …", len(pending))
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Notifier drain timed out — %d tasks may be cancelled", len(pending))

        logger.info("TradingEngine stopped")

    # ------------------------------------------------------------------
    # DataBus callbacks
    # ------------------------------------------------------------------

    def _make_price_callback(self, symbol: str):
        """Factory: returns an async callback for price:* topics."""
        async def _on_price(tick: HlPriceTick) -> None:
            self._latest_price[symbol] = tick
            mid = float(getattr(tick, "mid", 0) or 0)
            if mid > 0:
                with self._mark_prices_lock:
                    self._mark_prices_sync[symbol] = mid
            await self._on_market_event(symbol)
        return _on_price

    def _make_binance_price_callback(self, symbol: str):
        """Factory: returns an async callback for binance_price:* topics."""
        async def _on_binance_price(tick: BinanceMidTick) -> None:
            self._latest_binance_mid[symbol] = tick
        return _on_binance_price

    def _make_binance_perp_price_callback(self, symbol: str):
        """Factory: returns an async callback for binance_perp_price:* topics."""
        async def _on_binance_perp_price(tick: BinancePerpMidTick) -> None:
            self._latest_binance_perp_mid[symbol] = tick
        return _on_binance_perp_price

    def _make_ctx_callback(self, symbol: str):
        """Factory: returns an async callback for ctx:* topics.

        v3.1.17 C5: on every ctx update, settle funding for the open
        position so cash + daily_pnl reflect the hourly payment.
        """
        async def _on_ctx(ctx: HlAssetCtx) -> None:
            self._latest_ctx[symbol] = ctx
            # Apply funding for the open position (no-op if none).
            await self._settle_funding_for_symbol(symbol, ctx)
        return _on_ctx

    async def _settle_funding_for_symbol(
        self,
        symbol: str,
        ctx: HlAssetCtx,
    ) -> None:
        """Settle funding for the open position (if any).

        Hyperliquid broadcasts ctx updates frequently; funding accrues
        hourly. ``PortfolioState.apply_funding`` enforces the 1h gate.
        """
        if ctx is None or ctx.funding_rate is None:
            return
        try:
            positions = await self._portfolio.positions
        except Exception:
            return
        pos = positions.get(symbol)
        if pos is None:
            return
        try:
            mark = safe_float(ctx.mark_price, 0.0)
            if mark <= 0.0:
                tick = self._latest_price.get(symbol)
                mark = safe_float(getattr(tick, "mid", 0.0), 0.0) if tick else 0.0
            if mark <= 0.0:
                mark = safe_float(pos.current_price, 0.0) or safe_float(pos.entry_price, 0.0)
            await self._portfolio.apply_funding(
                symbol=symbol,
                funding_rate=ctx.funding_rate,
                position_size=pos.size,
                side=pos.side,
                mark_price=mark,
            )
            # v3.1.23: persist the per-trade funding running total so it
            # shows up in the dashboard "Trades" panel.
            pos_after = (await self._portfolio.positions).get(symbol)
            if pos_after is not None:
                funding_total = safe_float(
                    (pos_after.metadata or {}).get("funding_total", 0.0), 0.0,
                )
                trade_id = safe_float(
                    (pos_after.metadata or {}).get("trade_id"), None,
                )
                if trade_id is not None and trade_id > 0:
                    try:
                        self._db.update_trade_funding(int(trade_id), funding_total)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "DB funding persist failed for trade %s: %s",
                            trade_id, exc,
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Funding settlement failed for %s: %s", symbol, exc)

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
                if self._adx_closed_only or self._phase08_enabled:
                    hist = self._candles_15m_history[symbol]
                    if len(hist) >= 2 * self._adx_period + 1:
                        from src.strategies.indicators import calculate_adx
                        adx = calculate_adx(list(hist), self._adx_period)
                        if adx is not None:
                            self._latest_adx[symbol] = adx
            # Append 5m candles to history for OBV / MFI (v3.1.15)
            if timeframe == 300:
                self._candles_5m_history[symbol].append(candle)
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
                    funding_rate=optional_float(getattr(candle, 'funding', None)),
                    oi_total=optional_float(getattr(candle, 'oi_close', None)),
                    oi_delta=optional_float(getattr(candle, 'oi_delta', None)),
                    buy_volume=float(getattr(candle, 'buy_volume', 0)),
                    sell_volume=float(getattr(candle, 'sell_volume', 0)),
                    trade_count=int(getattr(candle, 'trade_count', 0)),
                )
                self._db.save_candle(db_candle, tf_name)
            except Exception as exc:
                logger.warning("Failed to persist candle for %s %s: %s", symbol, tf_name, exc)

            # ── C1: Feed 1h candle into the volatility circuit breaker ──
            if timeframe == 3600:
                try:
                    high = float(getattr(candle, "high", 0))
                    low = float(getattr(candle, "low", 0))
                    close = float(getattr(candle, "close", 0))
                    ts_ms = int(getattr(candle, "timestamp_ms", utc_timestamp_ms()))
                    if high > 0 and low > 0 and close > 0:
                        atr_proxy = (high - low) / close
                        self._vol_circuit.update(symbol, atr_proxy, ts_ms)
                except Exception:
                    logger.exception("vol_circuit update failed for %s", symbol)
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
        logger.debug("Processing market event for %s", symbol)

        # Check if WS is healthy — warn if stale data
        if self._hl_ws_client is not None:
            ws_ok = getattr(self._hl_ws_client, 'is_healthy', True)
            if not ws_ok and not self._ws_health_warned:
                logger.warning("WS appears unhealthy — market data may be stale")
                self._ws_health_warned = True

        async with self._get_symbol_lock(symbol):
            if not self._running:
                return

            # --- Circuit breaker alert (notify once per trip) ---
            if self._notifier is not None:
                cb_tripped = self._risk.is_circuit_breaker_tripped()
                if cb_tripped and not self._circuit_breaker_notified:
                    self._circuit_breaker_notified = True
                    reason = getattr(self._risk, '_circuit_breaker_reason', 'unknown')
                    self._notify(
                        lambda: self._notifier.circuit_breaker(
                            reason=reason, action='Trading halted'
                        )
                    )
                elif not cb_tripped and self._circuit_breaker_notified:
                    self._circuit_breaker_notified = False

            # --- Build MarketEvent ---
            event = self._build_market_event(symbol)
            if event is None:
                return

            self._strategy_governor.evaluate(event.timestamp_ms)

            # --- v3.1.15: Volume indicators (OBV slope, MFI, VWAP multi-TF) ---
            # Pure observability. Stored in _last_market_events and pushed
            # to the dashboard. NOT consumed by strategies (zero impact on
            # signal generation).
            obv_slope_5m: Optional[float] = None
            mfi_5m: Optional[float] = None
            hist_5m = list(self._candles_5m_history.get(symbol, []))
            hist_15m_local = list(self._candles_15m_history.get(symbol, []))
            if hist_5m:
                obv_slope_5m = calculate_obv_slope(hist_5m, lookback=14)
                mfi_5m = calculate_mfi(hist_5m, period=14)
            vwap_by_tf: Dict[str, Optional[float]] = calculate_vwap_multi_tf({
                "1m":  [event.candle_1m]   if event.candle_1m  else [],
                "5m":  hist_5m[-20:]       if hist_5m          else [],
                "15m": hist_15m_local[-20:] if hist_15m_local   else [],
                "1h":  [event.candle_1h]   if event.candle_1h  else [],
            })

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
                # v3.1.23: surface ADX to the dashboard so the regime panel
                # shows real numbers (not "unknown").
                "adx_14": event.adx_14,
                # v3.1.15: volume-derived observability (NOT used in strategies)
                "obv_slope_5m": obv_slope_5m,
                "mfi_5m": mfi_5m,
                "vwap_1m_rolling": vwap_by_tf.get("1m"),
                "vwap_5m_rolling": vwap_by_tf.get("5m"),
                "vwap_15m_rolling": vwap_by_tf.get("15m"),
                "vwap_1h_rolling": vwap_by_tf.get("1h"),
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
                self._pending_flatten = ("drawdown_circuit_breaker", symbol)
                self._circuit_breaker_notified = True

            equity = await self._portfolio.current_capital
            self._risk.evaluate_daily_drawdown(equity)
            if self._risk.request_daily_dd_flatten():
                self._pending_flatten = ("daily_drawdown_circuit", symbol)

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

            # --- Phase08 shadow strategies (observability only, no execution) ---
            self._evaluate_shadow_strategies(event, symbol)

            # --- 1. Strategy entry signals ---
            # Top-level list is typically [StrategyEnsemble]; see _strategy_is_operational.
            signals: List[Signal] = []
            for strategy in self._strategies:
                if not self._strategy_is_operational(strategy):
                    continue
                try:
                    sig = strategy.on_data(event)
                    if sig is not None:
                        gov_reason = self._governor_blocks_signal(sig)
                        if gov_reason is not None:
                            logger.info(
                                "Signal REJECTED %s %s — %s",
                                sig.symbol,
                                sig.side,
                                gov_reason,
                            )
                            self._persist_decision(
                                decision_type="governor",
                                symbol=sig.symbol,
                                side=sig.side,
                                strategy=self._signal_strategy_name(sig),
                                signal_confidence=sig.confidence,
                                ts_ms=event.timestamp_ms,
                                result="rejected",
                                reason=gov_reason,
                            )
                            continue
                        signals.append(sig)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Strategy %s error on %s: %s", strategy.name, symbol, exc)

            if signals:
                if self._phase08_regime_router:
                    routed, reject_reason = route_phase08_signals(
                        signals,
                        self._latest_adx.get(symbol),
                        adx_range_threshold=self._phase08_adx_range,
                        adx_trend_threshold=self._phase08_adx_trend,
                        symbol=symbol,
                        seq_guard=self._phase08_seq_guard,
                        timestamp_ms=event.timestamp_ms,
                    )
                    if reject_reason:
                        self._persist_decision(
                            decision_type="phase08_regime",
                            symbol=symbol,
                            side=signals[0].side if signals else "",
                            strategy=self._signal_strategy_name(signals[0]) if signals else "",
                            signal_confidence=signals[0].confidence if signals else 0.0,
                            ts_ms=event.timestamp_ms,
                            result="rejected",
                            reason=reject_reason,
                        )
                    if not routed:
                        pass
                    else:
                        best_signal = max(routed, key=lambda s: s.confidence)
                        await self._process_entry_signal(best_signal, event)
                else:
                    weighted_signals = self._apply_regime_weights(signals, symbol)
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
                        if not self._strategy_is_operational(strategy):
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

        # v3.1.17 C8: process any deferred flatten request *after* the
        # per-symbol lock is released. Acquiring another symbol's lock
        # while holding this one would risk deadlock against that
        # symbol's tick coroutine.
        pending = self._pending_flatten
        if pending is not None:
            self._pending_flatten = None
            reason, skip_symbol = pending
            try:
                await self._flatten_all_positions_safe(reason, skip_symbol=skip_symbol)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Deferred flatten failed: %s", exc)

        # v3.1.17 C8: process the second leg of a FundingArbitrage pair
        # (if queued) outside this symbol's lock.
        if self._pending_funding_pair is not None:
            try:
                await self._process_pending_funding_pair(event)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Deferred funding pair failed: %s", exc)

    def _evaluate_shadow_strategies(self, event: MarketEvent, symbol: str) -> None:
        """Run Phase08 shadow strategies and log hypothetical decisions.

        Shadow instances are separate objects — no governor, cooldown, portfolio,
        or execution side-effects.
        """
        if not self._shadow_strategies or self._shadow_recorder is None:
            return
        from src.research.shadow_recorder import (
            ShadowDecision,
            build_enriched_market_snapshot,
        )

        for strategy in self._shadow_strategies:
            if getattr(strategy, "_shadow_instance", False) is False:
                logger.warning("Shadow strategy missing _shadow_instance flag: %s", strategy.name)
            try:
                sig = strategy.on_data(event)
                if sig is None:
                    continue
                # Observability-only enrichment: bracket params + metadata so
                # the offline shadow outcome evaluator can simulate SL/TP.
                # Must never affect trading gates or execution paths.
                self._shadow_recorder.record(
                    ShadowDecision(
                        symbol=symbol,
                        strategy=strategy.name,
                        variant="phase08_shadow",
                        side=sig.side,
                        would_enter=True,
                        reason="entry_signal",
                        timestamp_ms=event.timestamp_ms,
                        market_snapshot=build_enriched_market_snapshot(
                            price=event.price,
                            confidence=float(sig.confidence),
                            stop_loss_pct=float(sig.stop_loss_pct or 0.0),
                            take_profit_pct=(
                                float(sig.take_profit_pct)
                                if sig.take_profit_pct is not None
                                else None
                            ),
                            size_pct=float(sig.size_pct or 0.0),
                            metadata=dict(sig.metadata or {}),
                        ),
                    )
                )
                logger.debug(
                    "Shadow signal %s %s %s conf=%.2f (not executed)",
                    strategy.name,
                    symbol,
                    sig.side,
                    sig.confidence,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Shadow strategy %s error on %s: %s", strategy.name, symbol, exc)

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
        hl_pred = self._hl_predicted.get(symbol)

        funding_hl_ws: Optional[float] = None
        if ctx is not None and ctx.funding_rate is not None:
            funding_hl_ws = normalize_funding_to_8h(
                ctx.funding_rate,
                float(ctx.funding_interval_hours or 1.0),
            )

        predicted_hl_8h: Optional[float] = None
        hl_stale = False
        if hl_pred is not None and not hl_pred.stale:
            predicted_hl_8h = hl_pred.predicted_funding_hl_8h
        elif hl_pred is not None and hl_pred.stale:
            hl_stale = True
            predicted_hl_8h = hl_pred.predicted_funding_hl_8h

        funding_cex = agg.funding_avg if agg else None
        predicted_cex = agg.predicted_funding_avg if agg else None
        cex_stale = bool(agg.stale) if agg else False

        # Prefer HL INFO predicted, then CEX aggregate; funding from WS (8h) then CEX avg
        predicted_funding = predicted_hl_8h if is_valid_funding(predicted_hl_8h) else predicted_cex
        funding = funding_hl_ws if is_valid_funding(funding_hl_ws) else funding_cex

        if (cex_stale or hl_stale) and symbol in self._market_data_health:
            health = self._market_data_health[symbol]
            if health.status == "red":
                logger.debug(
                    "MarketEvent %s: feed health RED (cex_stale=%s hl_stale=%s)",
                    symbol,
                    cex_stale,
                    hl_stale,
                )

        # Orderbook metrics (if available)
        ob = self._latest_orderbook.get(symbol)

        # ── ADX (frozen 15m closed candles — updated on candle_complete only) ──
        adx = self._latest_adx.get(symbol)

        # ── Liquidation stats (Task 3.3) ──
        liq_notional, liq_side, liq_count = self._get_liquidation_stats(symbol)

        predicted_by_venue: Optional[Dict[str, float]] = None
        if hl_pred and hl_pred.venues:
            predicted_by_venue = {
                v: vf.funding_rate_8h
                for v, vf in hl_pred.venues.items()
                if is_valid_funding(vf.funding_rate_8h)
            }

        feed = self._market_data_health.get(symbol)
        feed_status = feed.status if feed else None
        feed_stale = bool(
            (cex_stale or hl_stale) and feed_status in ("yellow", "red")
        )

        bn_tick = self._latest_binance_mid.get(symbol)
        bn_perp_tick = self._latest_binance_perp_mid.get(symbol)

        event = MarketEvent(
            symbol=symbol,
            price=price,
            timestamp_ms=tick.timestamp_ms,
            candle_1m=candles.get(60),
            candle_5m=candles.get(300),
            candle_15m=candles.get(900),
            candle_1h=candles.get(3600),
            funding=funding,
            predicted_funding=predicted_funding,
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
            predicted_funding_by_venue=predicted_by_venue,
            market_data_health=feed_status,
            market_data_stale=feed_stale,
            binance_mid=bn_tick.price if bn_tick is not None else None,
            binance_timestamp_ms=bn_tick.timestamp_ms if bn_tick is not None else None,
            binance_perp_mid=bn_perp_tick.price if bn_perp_tick is not None else None,
            binance_perp_timestamp_ms=(
                bn_perp_tick.timestamp_ms if bn_perp_tick is not None else None
            ),
        )

        logger.debug(
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

    # ------------------------------------------------------------------
    # QW1: Decision audit (in-memory ring + DB persistence)
    # ------------------------------------------------------------------

    def _persist_decision(
        self,
        decision_type: str,
        symbol: str,
        result: str,
        reason: str,
        side: Optional[str] = None,
        strategy: Optional[str] = None,
        signal_confidence: Optional[float] = None,
        ts_ms: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a single gate decision in both the dashboard ring and DB.

        QW1: replaces the bare ``_decision_history.insert(0, ...)`` pattern
        with a unified call that also persists to the ``decision_audit``
        table.  DB writes are best-effort — failures are logged but do not
        disrupt the live trading flow.

        Parameters
        ----------
        decision_type : str
            Gate name:  'cooldown' | 'correlation' | 'risk' | 'vol_circuit'
            | 'funding_blackout' | 'tca' | 'execution' | 'exit' | 'ensemble'.
        result : str
            'accepted' | 'rejected' | 'executed'.
        reason : str
            Human-readable explanation.
        """
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        time_str = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%H:%M:%S")

        # 1. In-memory ring (preserves existing dashboard behaviour)
        self._decision_history.insert(0, {
            "time": time_str,
            "type": decision_type,
            "symbol": symbol,
            "side": side,
            "result": result,
            "reason": reason,
        })
        self._decision_history = self._decision_history[:100]

        # 2. Persistent audit row (best-effort)
        try:
            if getattr(self, "_db", None) is not None:
                self._db.save_decision(
                    timestamp=ts_ms,
                    decision_type=decision_type,
                    symbol=symbol,
                    side=side,
                    strategy=strategy,
                    signal_confidence=signal_confidence,
                    result=result,
                    reason=reason,
                    metadata=metadata,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("decision_audit persist failed (non-fatal): %s", exc)

    def _extract_market_snapshot(
        self,
        event: MarketEvent,
        signal: Optional[Signal] = None,
    ) -> Dict[str, Any]:
        """Build a JSON-serializable regime snapshot for trade journal.

        QW2: captures the full market context at the moment a trade
        enters so post-mortem analysis can correlate outcomes with
        regime (ADX, OIR, funding, imbalance, etc.).
        """
        c_1m = event.candle_1m
        buy_vol = float(getattr(c_1m, "buy_volume", 0.0) or 0.0) if c_1m else 0.0
        sell_vol = float(getattr(c_1m, "sell_volume", 0.0) or 0.0) if c_1m else 0.0

        snapshot: Dict[str, Any] = {
            "price": event.price,
            "adx_14": event.adx_14,
            "atr_14": event.atr_14,
            "rsi_14": event.rsi_14,
            "ema_20": event.ema_20,
            "funding": event.funding,
            "predicted_funding": event.predicted_funding,
            "funding_avg": event.funding_avg,
            "funding_weighted": event.funding_weighted,
            "predicted_funding_avg": event.predicted_funding_avg,
            "oi_total": event.oi_total,
            "oi_total_aggregated": event.oi_total_aggregated,
            "oi_delta": event.oi_delta,
            "oi_exchange_count": event.oi_exchange_count,
            "volume_1m": event.volume_1m,
            "bid_ask_imbalance": event.bid_ask_imbalance,
            "vwap_15m": event.vwap_15m,
            "orderbook_spread_pct": event.orderbook_spread_pct,
            "orderbook_oir": event.orderbook_oir,
            "orderbook_depth_quality": event.orderbook_depth_quality,
            "orderbook_bid_ask_ratio": event.orderbook_bid_ask_ratio,
            "cvd_1m": buy_vol - sell_vol,
            "cvd_buy_vol_1m": buy_vol,
            "cvd_sell_vol_1m": sell_vol,
            "liquidation_notional_5m": event.liquidation_notional_5m,
            "liquidation_count_5m": event.liquidation_count_5m,
            "market_data_health": event.market_data_health,
            "market_data_stale": event.market_data_stale,
        }
        if signal is not None:
            snapshot["signal"] = {
                "strategy": signal.strategy,
                "side": signal.side,
                "confidence": signal.confidence,
                "size_pct": signal.size_pct,
                "reason": signal.reason,
                "stop_loss_pct": signal.stop_loss_pct,
                "take_profit_pct": signal.take_profit_pct,
            }
        return snapshot

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
            try:
                self._db.save_liquidation(
                    LiquidationRecord(
                        symbol=symbol,
                        timestamp_ms=timestamp_ms,
                        notional_usd=notional,
                        side=side,
                        source=source,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("save_liquidation %s failed: %s", symbol, exc)
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
    def _strategy_is_operational(strategy: Any) -> bool:
        """True when a top-level strategy should run on_data / on_position."""
        if hasattr(strategy, "is_active"):
            return bool(strategy.is_active())
        return True

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

        v3.1.17 C8: the second leg runs *after* the per-symbol lock is
        released (we already hold the current symbol's lock), so we
        never try to acquire another symbol's lock while holding this
        one (deadlock). The first leg is processed inline; the second
        leg is queued via ``_pending_funding_pair`` and executed by
        ``_process_pending_funding_pair`` outside the lock.
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
        # Use the correct price for each leg to avoid wrong entry_price
        priced_long = self._price_signal(long_sig)
        priced_short = self._price_signal(short_sig)
        logger.info(
            "FundingArbitrage PAIR selected — LONG %s @ %.2f, SHORT %s @ %.2f, spread=%.4f%%",
            priced_long.symbol,
            self._latest_price.get(priced_long.symbol, type('P', (), {'mid': 0.0})()).mid,
            priced_short.symbol,
            self._latest_price.get(priced_short.symbol, type('P', (), {'mid': 0.0})()).mid,
            (priced_short.metadata.get("funding", 0) - priced_long.metadata.get("funding", 0)) * 100,
        )

        # Process the leg whose symbol matches the current lock inline
        # (we already hold its lock). Queue the other leg for after
        # the lock is released.
        current_symbol = event.symbol
        if priced_long.symbol == current_symbol:
            await self._process_entry_signal(priced_long, event)
            self._pending_funding_pair = priced_short
        elif priced_short.symbol == current_symbol:
            await self._process_entry_signal(priced_short, event)
            self._pending_funding_pair = priced_long
        else:
            # Neither leg matches the current symbol — process the
            # first one now and queue the second.
            await self._process_entry_signal(priced_long, event)
            self._pending_funding_pair = priced_short

    def _price_signal(self, sig: Signal) -> Signal:
        """Return a copy of *sig* with ``entry_price`` set to the latest mid."""
        tick = self._latest_price.get(sig.symbol)
        if tick is None or tick.mid <= 0:
            return sig
        return Signal(
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

    async def _process_pending_funding_pair(self, event: MarketEvent) -> None:
        """Process the second leg of a FundingArbitrage pair (after lock release)."""
        sig = self._pending_funding_pair
        if sig is None:
            return
        self._pending_funding_pair = None
        priced = self._price_signal(sig)
        try:
            await self._process_entry_signal(priced, event)
        except Exception as exc:  # noqa: BLE001
            logger.exception("FundingArbitrage second leg failed: %s", exc)

    # ------------------------------------------------------------------
    # Anti-chasing filter
    # ------------------------------------------------------------------

    def _directional_runup_pct(self, symbol: str, side: str) -> Optional[float]:
        """Return favourable run-up over lookback window (fraction, e.g. 0.01 = 1%)."""
        hist = list(self._candles_15m_history.get(symbol, []))
        if len(hist) < 2:
            return None
        bars = max(2, int(self._chase_lookback_hours * 4.0))
        window = hist[-bars:] if len(hist) >= bars else hist
        if len(window) < 2:
            return None
        start_px = safe_float(getattr(window[0], "close", 0.0), 0.0)
        end_px = safe_float(getattr(window[-1], "close", 0.0), 0.0)
        if start_px <= 0.0:
            return None
        ret = (end_px - start_px) / start_px
        if side == "long":
            return max(ret, 0.0)
        return max(-ret, 0.0)

    def _check_chase_filter(self, signal: Signal) -> Optional[str]:
        """Reject entries that chase an extended move (all strategies unless exempt)."""
        if not self._chase_filter_enabled:
            return None
        if signal.strategy in self._chase_exempt_strategies:
            return None
        runup = self._directional_runup_pct(signal.symbol, signal.side)
        if runup is None:
            return None
        if runup > self._chase_max_runup_pct:
            return (
                f"chase runup={runup * 100:.2f}% "
                f"> {self._chase_max_runup_pct * 100:.2f}% "
                f"over {self._chase_lookback_hours:.1f}h"
            )
        return None

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
          2. Funding normalizes — funding strategies only, when entry
             funding was material (|entry| > strong_threshold)
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

        # 2. Funding normalization reset (funding strategies with material entry funding)
        if strategy in _FUNDING_COOLDOWN_STRATEGIES:
            entry_funding = state.get("funding")
            strong_th = self._funding_strong_threshold
            if (
                entry_funding is not None
                and abs(safe_float(entry_funding, 0.0)) > strong_th
            ):
                funding = event.funding or event.predicted_funding
                normalize_th = strong_th * 0.5
                if funding is not None and abs(funding) < normalize_th:
                    logger.info(
                        "Cooldown RESET %s — funding normalized to %.4f%% "
                        "(entry was %.4f%%)",
                        key, funding * 100, entry_funding * 100,
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
        self._signal_pipeline._cooldown.on_entry(
            strategy, symbol, event, self._pipeline_ctx.cooldown_state,
        )

    def _update_cooldown_on_exit(
        self,
        strategy: str,
        symbol: str,
        pnl_pct: float,
    ) -> None:
        """Update cooldown state after a position is closed."""
        self._signal_pipeline._cooldown.on_exit(
            strategy, symbol, pnl_pct, self._pipeline_ctx.cooldown_state,
        )

    # ------------------------------------------------------------------
    # Signal processing
    # ------------------------------------------------------------------

    async def _process_entry_signal(self, signal: Signal, event: MarketEvent) -> None:
        """Gate an entry signal through risk management and execute if approved."""
        if self._phase08_enabled and self._phase08_paper_only and self._mode != "paper":
            logger.warning(
                "Phase08 REJECT %s %s — execution allowed in paper mode only (mode=%s)",
                signal.symbol,
                signal.side,
                self._mode,
            )
            return

        # Sequential contradiction guard records ONLY after an accepted fill
        # (see paper executed path + OMS fill callback). Recording here used to
        # lock the opposite side for 1h even when risk/TCA later rejected.

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

        self._pipeline_ctx.candles_15m_history = {
            sym: list(hist) for sym, hist in self._candles_15m_history.items()
        }
        preprocessed = self._signal_pipeline.preprocess_signal(signal, event)
        if preprocessed is None:
            return
        signal = preprocessed

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

        decision = self._signal_pipeline.evaluate_gates(
            signal, event, portfolio_proxy, self._pipeline_ctx, skip_tca=True,
        )
        if not decision.approved:
            gate = decision.gate
            reason = decision.reason
            if gate == "entry_debounce":
                logger.warning(
                    "Signal DEBOUNCED %s %s — %s",
                    signal.symbol, signal.side, reason,
                )
                return
            logger.info(
                "Signal REJECTED %s %s — %s",
                signal.symbol, signal.side, reason,
            )
            sig_record["status"] = "rejected"
            sig_record["risk_reason"] = reason
            strat_stats = self._strategy_stats.get(signal.strategy)
            if strat_stats:
                strat_stats["rejected_signals"] += 1
                if strat_stats["signal_history"]:
                    strat_stats["signal_history"][0]["status"] = "rejected"
            decision_type_map = {
                "feed_health": "feed_health",
                "cooldown": "cooldown",
                "vol_circuit": "vol_circuit",
                "funding_blackout": "funding_blackout",
                "chase_filter": "chase",
                "correlation": "correlation",
                "risk": "risk",
            }
            metadata: Dict[str, Any] = {}
            if gate == "correlation" and "r(" in reason:
                parts = reason.split("r(")[1].split(")")[0].split(",")
                if len(parts) == 2:
                    metadata["conflict_symbol"] = parts[1].strip()
            self._persist_decision(
                decision_type=decision_type_map.get(gate, gate),
                symbol=signal.symbol,
                side=signal.side,
                strategy=signal.strategy,
                signal_confidence=signal.confidence,
                ts_ms=event.timestamp_ms,
                result="rejected",
                reason=reason,
                metadata=metadata or None,
            )
            return

        signal = decision.signal or signal

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

        # --- C1: Intraday volatility circuit breaker (soft gate) ---
        if self._mode in ("testnet", "mainnet") and self._executor.is_symbol_blocked(signal.symbol):
            block_reason = self._executor.get_symbol_block_reason(signal.symbol) or "symbol_blocked"
            logger.info(
                "Signal REJECTED %s %s — execution block active (%s)",
                signal.symbol,
                signal.side,
                block_reason,
            )
            sig_record["status"] = "rejected"
            sig_record["risk_reason"] = f"execution_block:{block_reason}"
            strat_stats = self._strategy_stats.get(signal.strategy)
            if strat_stats:
                strat_stats["rejected_signals"] += 1
            self._persist_decision(
                decision_type="execution",
                symbol=signal.symbol,
                side=signal.side,
                strategy=signal.strategy,
                signal_confidence=signal.confidence,
                ts_ms=event.timestamp_ms,
                result="rejected",
                reason=f"execution_block:{block_reason}",
            )
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

        # Enrich signal metadata with computed size and sub-strategy attribution
        sub_strategy = regime_strategy_name(signal)
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
            metadata={
                **signal.metadata,
                "calculated_size": size,
                "atr_pct": atr_pct,
                "sub_strategy": sub_strategy,
            },
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
                self._persist_decision(
                    decision_type="risk",
                    symbol=signal.symbol,
                    side=signal.side,
                    strategy=signal.strategy,
                    signal_confidence=signal.confidence,
                    ts_ms=event.timestamp_ms,
                    result="rejected",
                    reason=f"fill_ratio {fill_ratio*100:.1f}% < {self._min_fill_ratio*100:.0f}%",
                    metadata={"fill_ratio": float(fill_ratio), "min_fill_ratio": float(self._min_fill_ratio)},
                )
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
                self._persist_decision(
                    decision_type="risk",
                    symbol=signal.symbol,
                    side=signal.side,
                    strategy=signal.strategy,
                    signal_confidence=signal.confidence,
                    ts_ms=event.timestamp_ms,
                    result="rejected",
                    reason=f"slippage {slippage*100:.3f}% > {self._max_slippage_pct*100:.2f}%",
                    metadata={"slippage": float(slippage), "max_slippage_pct": float(self._max_slippage_pct)},
                )
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
        tca_decision = self._signal_pipeline.evaluate_tca_gate(
            signal,
            order_spec=order_spec,
            has_orderbook=ob_raw is not None,
        )
        if not tca_decision.approved:
            logger.info(
                "Signal REJECTED %s %s — %s",
                signal.symbol, signal.side, tca_decision.reason,
            )
            sig_record["status"] = "rejected"
            sig_record["risk_reason"] = tca_decision.reason
            self._persist_decision(
                decision_type="tca",
                symbol=signal.symbol,
                side=signal.side,
                strategy=signal.strategy,
                signal_confidence=signal.confidence,
                ts_ms=event.timestamp_ms,
                result="rejected",
                reason=tca_decision.reason,
            )
            return
        if (
            tca_decision.reason
            and tca_decision.reason != "tca_skipped_no_edge_estimate"
        ):
            logger.info("TCA %s %s — %s", signal.symbol, signal.side, tca_decision.reason)

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
        # v3.1.22: forward the L2-derived slippage estimate to
        # enter_position so paper fills match the simulated slippage
        # (instead of the flat ``risk.paper_slippage_pct`` fallback).
        # ``slippage_for_tca`` was computed earlier as either the L2
        # estimate (when an orderbook is available) or 0.0.
        slip_bps = (
            float(slippage_for_tca) * 10_000.0
            if slippage_for_tca and slippage_for_tca > 0
            else None
        )
        try:
            result = await self._executor.enter_position(
                signal,
                self._portfolio,
                market_event=event,
                estimated_slippage_bps=slip_bps,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Execution failed for %s: %s", signal.symbol, exc)
            sig_record["status"] = "failed"
            sig_record["risk_reason"] = str(exc)[:80]
            self._persist_decision(
                decision_type="execution",
                symbol=signal.symbol,
                side=signal.side,
                strategy=signal.strategy,
                signal_confidence=signal.confidence,
                ts_ms=event.timestamp_ms,
                result="failed",
                reason=str(exc)[:80],
            )
            return

        # Handle rejection (debounce, duplicate, etc.)
        if result.status == "rejected":
            logger.info(
                "Signal REJECTED %s %s — %s",
                signal.symbol, signal.side, result.reason,
            )
            sig_record["status"] = "rejected"
            sig_record["risk_reason"] = result.reason
            self._persist_decision(
                decision_type="execution",
                symbol=signal.symbol,
                side=signal.side,
                strategy=signal.strategy,
                signal_confidence=signal.confidence,
                ts_ms=event.timestamp_ms,
                result="rejected",
                reason=result.reason,
            )
            return

        if result.status == "pending":
            logger.info(
                "Signal PENDING %s %s — live order submitted, awaiting fill (id=%d)",
                signal.symbol,
                signal.side,
                result.trade_id,
            )
            sig_record["status"] = "pending"
            self._persist_decision(
                decision_type="execution",
                symbol=signal.symbol,
                side=signal.side,
                strategy=signal.strategy,
                signal_confidence=signal.confidence,
                ts_ms=event.timestamp_ms,
                result="pending",
                reason=f"awaiting_fill trade_id={result.trade_id}",
                metadata={"trade_id": int(result.trade_id)},
            )
            return

        sig_record["status"] = "executed"
        sig_record["size"] = result.size
        # Update strategy stats
        strat_stats = self._strategy_stats.get(signal.strategy)
        if strat_stats:
            strat_stats["approved_signals"] += 1
            if strat_stats["signal_history"]:
                strat_stats["signal_history"][0]["status"] = "executed"

        # Phase08: lock sequential flip only after a real accepted fill
        if self._phase08_seq_guard is not None:
            self._phase08_seq_guard.record(
                signal.symbol, signal.side, event.timestamp_ms,
            )

        self._persist_decision(
            decision_type="execution",
            symbol=signal.symbol,
            side=signal.side,
            strategy=signal.strategy,
            signal_confidence=signal.confidence,
            ts_ms=event.timestamp_ms,
            result="executed",
            reason=f"size={result.size:.6f} @ {result.entry_price:.2f}",
            metadata={"trade_id": int(result.trade_id), "size": float(result.size), "entry_price": float(result.entry_price)},
        )

        # --- Update portfolio ---
        # Live fills are credited exclusively via OMS → apply_entry_fill (Phase 03).
        if self._mode == "paper":
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
                current_price=result.entry_price,
                metadata={
                    "strategy": signal.strategy,
                    "sub_strategy": sub_strategy,
                    "trade_id": result.trade_id,
                    "stop_loss_pct": stop_distance_pct,
                    "entry_price": result.entry_price,
                    **signal.metadata,
                },
            )
            await self._portfolio.add_position(position, cost=total_cost)

            # v3.1.42: persist stop/TP levels for position restore after restart
            try:
                self._db.enrich_trade_stop_metadata(
                    trade_id=int(result.trade_id),
                    stop_loss_price=float(stop_loss_price),
                    take_profit_price=(
                        float(take_profit_price) if take_profit_price is not None else None
                    ),
                    stop_loss_pct=float(stop_distance_pct),
                    take_profit_pct=(
                        float(signal.take_profit_pct)
                        if signal.take_profit_pct is not None
                        else None
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to persist stop metadata for trade %s: %s",
                    result.trade_id,
                    exc,
                )
        else:
            logger.info(
                "Live entry submitted — portfolio update deferred to OMS fill callback "
                "(trade_id=%d symbol=%s)",
                result.trade_id,
                result.symbol,
            )

        # CRIT-011 FIX: Persist portfolio snapshot after every trade entry
        await self._maybe_save_snapshot(force=True)

        # --- Record entry for debounce + cooldown (shared pipeline) ---
        self._signal_pipeline.record_trade_opened(
            signal.strategy, signal.symbol, event, self._pipeline_ctx,
        )

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
            sym, side, size, price, strat = (
                signal.symbol,
                signal.side,
                result.size,
                result.entry_price,
                signal.strategy,
            )
            self._notify(
                lambda: self._notifier.trade_entry(
                    symbol=sym,
                    side=side,
                    size=size,
                    price=price,
                    strategy=strat,
                    stop_loss=float(stop_loss_price) if stop_loss_price else None,
                    take_profit=float(take_profit_price) if take_profit_price else None,
                    notional_usd=size * price,
                )
            )

    async def _process_exit_signal(self, exit_signal: ExitSignal, position: Position) -> None:
        """Execute a strategy-driven exit."""
        last_price = self._latest_price.get(position.symbol)
        if last_price is None:
            logger.warning("No price for %s - cannot execute exit", position.symbol)
            return

        await self._execute_exit(position, last_price.mid, reason=exit_signal.reason)

    async def _check_hard_stops(self, position: Position, current_price: float) -> None:
        """Check trailing stop, stop-loss and take-profit levels.

        In live mode with native protection, software stops are redundancy only —
        native triggers on the exchange are the primary protection when the bot
        is offline.
        """
        native_active = bool((position.metadata or {}).get("native_protection_active"))
        live_mode = self._mode in ("testnet", "mainnet")

        if self._trailing_enabled and position.entry_price > 0:
            await self._maybe_update_trailing_stop(position, current_price)
            positions = await self._portfolio.positions
            position = positions.get(position.symbol, position)

        if position.stop_loss_price is not None:
            sl_hit = (
                position.side == "long" and current_price <= position.stop_loss_price
            ) or (
                position.side == "short" and current_price >= position.stop_loss_price
            )
            if sl_hit:
                if live_mode and native_active and self._software_stop_redundancy:
                    logger.warning(
                        "Software stop redundancy firing for %s — native trigger should "
                        "have closed on exchange",
                        position.symbol,
                    )
                await self._execute_exit(position, current_price, reason="stop_loss")
                return

        if position.take_profit_price is not None:
            tp_hit = (
                position.side == "long" and current_price >= position.take_profit_price
            ) or (
                position.side == "short" and current_price <= position.take_profit_price
            )
            if tp_hit:
                if live_mode and native_active and self._software_stop_redundancy:
                    logger.warning(
                        "Software TP redundancy firing for %s",
                        position.symbol,
                    )
                await self._execute_exit(position, current_price, reason="take_profit")
                return

    async def _maybe_update_trailing_stop(
        self,
        position: Position,
        current_price: float,
    ) -> None:
        """Ratchet stop-loss once unrealised profit exceeds activation threshold."""
        if self._trailing_excluded_for_position(position):
            return

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
            await self._sync_native_stop(position.symbol, new_stop)
        else:
            trough = min(trail.get("trough_price", entry), current_price)
            trail["trough_price"] = trough
            new_stop = trough * (1.0 + self._trailing_distance_pct)
            await self._portfolio.update_stop_loss(position.symbol, new_stop, "short")
            await self._sync_native_stop(position.symbol, new_stop)

    async def _sync_native_stop(self, symbol: str, new_stop: float) -> None:
        """Resize native SL trigger when trailing stop ratchets (live only)."""
        if not self._native_protection_enabled or self._protection_manager is None:
            return
        if self._mode not in ("testnet", "mainnet"):
            return
        positions = await self._portfolio.positions
        pos = positions.get(symbol)
        if pos is None or not (pos.metadata or {}).get("native_protection_active"):
            return
        trade_id = (pos.metadata or {}).get("trade_id")
        await self._protection_manager.ensure_protection(
            pos,
            filled_size=pos.size,
            stop_price=new_stop,
            take_profit_price=pos.take_profit_price,
            trade_id=int(trade_id) if trade_id else None,
        )

    def _trailing_excluded_for_position(self, position: Position) -> bool:
        """True when sub-strategy manages its own TP/exit (no engine trailing)."""
        if not self._trailing_exclude_strategies:
            return False
        meta = position.metadata or {}
        sub = str(
            meta.get("sub_strategy")
            or meta.get("original_strategy")
            or ""
        )
        return sub in self._trailing_exclude_strategies

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
            self._persist_decision(
                decision_type="exit",
                symbol=position.symbol,
                side=position.side,
                result="failed",
                reason=str(exc)[:80],
            )
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
        # v3.1.17 C10: Kelly expects PnL / capital, not PnL / notional.
        # Use pnl_pct_capital (computed in execution.close_position) so a
        # 20%-of-capital position is not over-stated as a 100% return.
        self._kelly_sizer.record_trade(result.pnl_pct_capital)

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
        self._persist_runtime_state()

        # --- Record per-strategy PnL row (for dashboard drill-down) ---
        try:
            attr_strategy = (
                position.metadata.get("sub_strategy")
                or regime_strategy_name(
                    Signal(
                        strategy=str(position.metadata.get("strategy", "unknown")),
                        symbol=position.symbol,
                        side=position.side,
                        confidence=1.0,
                        size_pct=0.0,
                        metadata=position.metadata,
                    )
                )
            )
            self._db.record_strategy_pnl(
                strategy=attr_strategy,
                symbol=position.symbol,
                side=position.side,
                pnl_usd=result.pnl_usd,
                pnl_pct=result.pnl_pct,
                size=position.size,
                entry_time=position.entry_time_ms,
                exit_time=result.exit_time_ms if hasattr(result, "exit_time_ms") else int(time.time() * 1000),
                exit_reason=reason,
                trade_id=position.metadata.get("trade_id") if position.metadata else None,
            )
        except Exception:
            logger.exception("Failed to record strategy_pnl row for %s", position.symbol)

        self._persist_decision(
            decision_type="exit",
            symbol=position.symbol,
            side=position.side,
            strategy=str(position.metadata.get("strategy", "") or "") if position.metadata else None,
            result="closed",
            reason=f"{reason} pnl={result.pnl_usd:.2f} ({result.pnl_pct*100:.2f}%)",
            metadata={"pnl_usd": float(result.pnl_usd), "pnl_pct": float(result.pnl_pct), "exit_reason": reason},
        )

        logger.info(
            "Position CLOSED %s pnl=%.2f (%.2f%%) reason=%s",
            position.symbol,
            result.pnl_usd,
            result.pnl_pct * 100.0,
            reason,
        )

        # --- Notify trade exit ---
        if self._notifier is not None:
            sym = position.symbol
            side = position.side
            pnl = result.pnl_usd
            exit_px = result.exit_price if hasattr(result, 'exit_price') else result.entry_price
            strat = position.metadata.get("strategy", "unknown")
            self._notify(
                lambda: self._notifier.trade_exit(
                    symbol=sym,
                    side=side,
                    pnl=pnl,
                    exit_price=exit_px,
                    strategy=strat,
                    exit_reason=reason,
                    pnl_pct=float(result.pnl_pct) if hasattr(result, "pnl_pct") else None,
                )
            )

    async def _flatten_all_positions(self, _current_price: float) -> None:
        """Emergency liquidation of all open positions (circuit breaker).

        Kept for backward compatibility — forwards to
        :meth:`_flatten_all_positions_safe` which acquires each
        symbol's per-symbol lock independently. The caller MUST NOT
        hold any symbol's lock when invoking this method.
        """
        await self._flatten_all_positions_safe(
            reason="circuit_breaker_drawdown", skip_symbol=None,
        )

    async def _flatten_all_positions_safe(
        self,
        reason: str,
        skip_symbol: Optional[str] = None,
    ) -> None:
        """Flatten every open position, acquiring each per-symbol lock.

        v3.1.17 C8: acquires ``_get_symbol_lock(symbol)`` for every
        position before exiting it. This prevents double-exit races
        where two coroutines exit the same position and double-count
        PnL in the risk manager, Kelly sizer, and strategy_pnl.

        ``skip_symbol`` is the symbol whose lock the caller already
        holds; we acquire and exit every *other* symbol.
        """
        positions = await self._portfolio.positions
        if not positions:
            return
        logger.critical(
            "FLATTENING %d position(s) — reason=%s", len(positions), reason,
        )
        for sym in list(positions.keys()):
            if sym == skip_symbol:
                continue
            try:
                async with self._get_symbol_lock(sym):
                    # Re-check: a concurrent tick may have closed this
                    # position while we were waiting for the lock.
                    current_positions = await self._portfolio.positions
                    current = current_positions.get(sym)
                    if current is None:
                        continue
                    tick = self._latest_price.get(sym)
                    if tick is None or tick.mid <= 0:
                        logger.warning("No price for %s — skipping flatten", sym)
                        continue
                    await self._execute_exit(
                        current, tick.mid, reason=f"flatten:{reason}",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Flatten failed for %s: %s", sym, exc)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    async def _recover_state(self) -> None:
        """Load open positions and portfolio state from the DB on startup."""
        self._restore_invocation_count += 1
        # Load open trades from DB → executor
        await self._executor.load_open_trades()
        if hasattr(self._executor, "load_pending_orders"):
            await self._executor.load_pending_orders()

        # Load latest portfolio snapshot
        history = self._db.get_portfolio_history(limit=1)
        daily_peak = 0.0
        if history:
            snap = history[0]
            try:
                import json
                positions_data = json.loads(snap["positions_json"])
                meta = positions_data.pop("_meta", {}) if isinstance(positions_data, dict) else {}
                if not isinstance(meta, dict):
                    meta = {}
                daily_peak = safe_float(
                    meta.get("daily_peak_capital", snap.get("daily_peak_capital", snap["capital"])),
                    snap["capital"],
                )
                await self._portfolio.from_dict({
                    "capital": snap["capital"],
                    "current_capital": snap["capital"],
                    "peak_capital": snap.get("peak_capital", snap["capital"]),
                    "daily_peak_capital": daily_peak,
                    "initial_capital": snap.get("initial_capital", snap["capital"]),
                    "daily_pnl": snap["daily_pnl"],
                    "cash": meta.get("cash"),
                    "day_start_equity": meta.get("day_start_equity"),
                    "last_reset_date": meta.get("last_reset_date"),
                    "daily_trades": meta.get("daily_trades"),
                    "total_trades_closed": meta.get("total_trades_closed"),
                    "positions": positions_data,
                })
                logger.info("Recovered portfolio snapshot from DB: capital=%.2f", snap["capital"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to restore portfolio snapshot: %s", exc)
        else:
            logger.info("No prior portfolio snapshot found - starting fresh")

        await self._sync_open_trades_to_portfolio()

        snap_daily = history[0].get("daily_pnl") if history else None
        try:
            await self._portfolio.reconcile_daily_from_db(
                self._db,
                snap_daily_pnl=snap_daily,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to reconcile daily PnL from DB: %s", exc)

        equity = await self._portfolio.current_capital
        self._cooldown_state = restore_runtime_state(
            self._db,
            self._risk,
            base_ms=self._cooldown_base_ms,
            max_ms=self._cooldown_max_ms,
            multiplier=self._cooldown_multiplier,
            portfolio_daily_peak=daily_peak,
            portfolio_capital=equity,
        )

        # Restore recent candles from DB for faster strategy warm-up
        await self._restore_candles_from_db()

    async def _sync_open_trades_to_portfolio(self) -> None:
        """Mirror executor open trades into portfolio (idempotent on restart)."""
        open_rows = self._db.get_open_trades()
        if not open_rows:
            return
        by_id = {int(row["id"]): row for row in open_rows}
        current = await self._portfolio.positions
        for trade in list(self._executor._open_trades.values()):
            if trade.symbol in current:
                logger.debug(
                    "Open trade %s already in portfolio — skip restore",
                    trade.symbol,
                )
                continue
            db_row = by_id.get(int(trade.trade_id), {})
            restored_strategy = str(
                db_row.get("strategy")
                or (
                    trade.reason.split(":", 1)[1]
                    if ":" in (trade.reason or "")
                    else "unknown"
                )
            )
            sl_price, tp_price = resolve_trade_stop_levels(
                entry_price=trade.entry_price,
                side=trade.side,
                signal_metadata=db_row.get("signal_metadata"),
            )
            notional = trade.entry_price * trade.size
            total_cost = notional + safe_float(getattr(trade, "entry_fee", 0.0))
            pos = Position(
                symbol=trade.symbol,
                side=trade.side,
                entry_price=trade.entry_price,
                size=trade.size,
                entry_time_ms=int(trade.timestamp_ms),
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
                unrealized_pnl=0.0,
                metadata={
                    "strategy": restored_strategy,
                    "sub_strategy": restored_strategy,
                    "trade_id": trade.trade_id,
                    "restored_from_db": True,
                },
            )
            try:
                await self._portfolio.add_position(pos, cost=total_cost)
                logger.info(
                    "Restored position into portfolio: %s %s size=%.6f @ %.2f "
                    "(id=%d sl=%s tp=%s)",
                    trade.symbol,
                    trade.side,
                    trade.size,
                    trade.entry_price,
                    trade.trade_id,
                    f"{sl_price:.4f}" if sl_price else "None",
                    f"{tp_price:.4f}" if tp_price else "None",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to restore position %s into portfolio: %s",
                    trade.symbol,
                    exc,
                )

    async def _restore_candles_from_db(self) -> None:
        """Load recent candle history from DB into in-memory caches
        and inject into all strategy states for instant warm-up.
        """
        tf_map = {60: "1m", 300: "5m", 900: "15m", 3600: "1h"}
        restore_limits = {60: 200, 300: 200, 900: 200, 3600: 250}
        all_strategies: List[Any] = []
        for s in self._strategies:
            all_strategies.append(s)
            sub = getattr(s, "_strategies", None)
            if isinstance(sub, dict):
                all_strategies.extend(sub.values())

        for symbol in self._symbols:
            for tf_s, tf_name in tf_map.items():
                try:
                    rows = self._db.get_candles(
                        symbol, tf_name, limit=restore_limits.get(tf_s, 200)
                    )
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
                                buy_volume=getattr(row, "buy_volume", 0.0) or 0.0,
                                sell_volume=getattr(row, "sell_volume", 0.0) or 0.0,
                                trade_count=getattr(row, "trade_count", 0) or 0,
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

    @property
    def restore_invocation_count(self) -> int:
        """How many times ``_recover_state`` ran (expect 1 per process)."""
        return self._restore_invocation_count

    def _persist_runtime_state(self) -> None:
        self._risk_state.persist_runtime_state()

    def _entry_feed_block_reason(self, symbol: str) -> Optional[str]:
        """Return a rejection reason when feeds are too stale for new entries."""
        if self._block_entries_on_ws_unhealthy and self._hl_ws_client is not None:
            if not getattr(self._hl_ws_client, "is_healthy", True):
                return "ws_unhealthy"
        if self._block_entries_on_feed_stale:
            if not self._feed_health_evaluated:
                return "feed_health_pending"
            if not self._feed_health_ready:
                return "feed_health_not_ready"
            feed = self._market_data_health.get(symbol)
            if feed is not None and feed.status == "red":
                return f"feed_red:{symbol}"
            if self._market_data_health_summary.overall == "red":
                return "feed_red:overall"
        if self._reconciliation_block_when_stale and self._reconciler is not None:
            if self._reconciler.entries_blocked():
                reason = self._reconciler.block_reason()
                if reason:
                    return reason
        return None

    def _signal_strategy_name(self, signal: Signal) -> str:
        meta = signal.metadata or {}
        original = meta.get("original_strategy")
        if original:
            return str(original)
        if signal.strategy not in ("StrategyEnsemble", "DirectRouter"):
            return str(signal.strategy)
        return str(signal.strategy)

    def _governor_blocks_signal(self, signal: Signal) -> Optional[str]:
        """Return audit reason when strategy governor disables this signal."""
        if not self._strategy_governor.is_enabled(self._signal_strategy_name(signal)):
            return f"governor_disabled:{self._signal_strategy_name(signal)}"
        return None

    async def _save_portfolio_snapshot(self) -> None:
        """Persist the current portfolio state to the DB."""
        state = await self._portfolio.to_dict()
        try:
            import json
            positions_out = dict(state.get("positions", {}))
            positions_out["_meta"] = {
                "cash": state.get("cash"),
                "day_start_equity": state.get("day_start_equity"),
                "day_start_unrealized": state.get("day_start_unrealized"),
                "daily_peak_capital": state.get("daily_peak_capital"),
                "last_reset_date": state.get("last_reset_date"),
                "daily_trades": state.get("daily_trades"),
                "total_trades_closed": state.get("total_trades_closed"),
                "daily_realized_pnl": state.get("daily_realized_pnl", state.get("daily_pnl")),
            }
            snapshot = PortfolioSnapshot(
                timestamp=utc_timestamp_ms(),
                capital=state["current_capital"],
                peak_capital=state["peak_capital"],
                daily_pnl=state["daily_pnl"],
                positions_json=json.dumps(positions_out),
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
