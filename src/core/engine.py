"""Main trading engine — orchestrates data flow, strategies, risk, and execution.

The engine subscribes to the :class:`DataBus`, builds :class:`MarketEvent`s,
feeds them to registered strategies, and gates every entry signal through
the :class:`RiskManager` before handing approved trades to the
:class:`ExecutionEngine`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from src.data.database import (
    Database,
    PortfolioSnapshot,
    SignalRecord,
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
      1. ``start()`` — subscribe to DataBus, load DB state, begin event loop.
      2. ``stop()``  — close positions, save state, unsubscribe.

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
        self._latest_candles: Dict[str, Dict[str, Optional[Candle]]] = {
            sym: {"1m": None, "5m": None, "15m": None, "1h": None}
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

        # Track which topics we subscribed to so we can unsubscribe on stop
        self._subscribed_callbacks: Dict[str, Any] = {}

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
            for tf in ("1m", "5m", "15m", "1h"):
                cb_candle = self._make_candle_callback(symbol, tf)
                await self._bus.subscribe(f"candle_complete:{tf}:{symbol}", cb_candle)
                self._subscribed_callbacks[f"candle_complete:{tf}:{symbol}"] = cb_candle

        logger.info(
            "TradingEngine running — symbols=%s strategies=%s",
            self._symbols,
            [s.name for s in self._strategies],
        )

    async def stop(self) -> None:
        """Graceful shutdown: close all positions, save state, unsubscribe."""
        if not self._running:
            return

        logger.info("TradingEngine stopping …")
        self._running = False
        if self._shutdown_event is not None:
            self._shutdown_event.set()

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
                logger.warning("No last price for %s during shutdown — skipping close", symbol)

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
        async with self._event_lock:
            if not self._running:
                return

            # --- Build MarketEvent ---
            event = self._build_market_event(symbol)
            if event is None:
                return

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

        # Build the MarketEvent
        return MarketEvent(
            symbol=symbol,
            price=price,
            timestamp_ms=tick.timestamp_ms,
            candle_1m=candles.get("1m"),
            candle_5m=candles.get("5m"),
            candle_15m=candles.get("15m"),
            candle_1h=candles.get("1h"),
            funding=safe_float(ctx.funding_rate) if ctx else None,
            predicted_funding=safe_float(ctx.predicted_funding) if ctx else None,
            oi_total=safe_float(ctx.open_interest) if ctx else None,
            oi_delta=None,  # Would require history; strategies compute from metadata if needed
            volume_1m=None,  # Populated by Binance integration or CandleBuilder
            bid_ask_imbalance=None,
            vwap_15m=None,
        )

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

        # --- Risk check ---
        # PortfolioState properties are async, so we need to await them.
        # RiskManager.can_enter expects the portfolio object directly; it
        # accesses properties synchronously.  We gather the values first.
        capital = await self._portfolio.current_capital
        positions = await self._portfolio.positions
        daily_pnl = await self._portfolio.daily_pnl
        daily_trades = await self._portfolio.daily_trades

        # Build a lightweight sync-read proxy for the risk manager
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
                "Signal REJECTED %s %s (confidence=%.2f) — %s",
                signal.symbol,
                signal.side,
                signal.confidence,
                reason,
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
            logger.warning("Position size zero for %s — skipping", signal.symbol)
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
            return

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
            logger.warning("No price for %s — cannot execute exit", position.symbol)
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
            logger.info("No prior portfolio snapshot found — starting fresh")

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
