"""Trade execution engine — paper, testnet, and mainnet modes.

All modes share the same state-tracking and DB-persistence layer so that
switching between paper and live does not change the bookkeeping contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.order_lifecycle import (
    ORDER_CANCELLED,
    ORDER_CLOSE_PENDING,
    ORDER_FILLED,
    ORDER_PARTIAL,
    ORDER_PENDING_SUBMISSION,
    ORDER_REJECTED,
    ORDER_RESTING,
    ORDER_SUBMISSION_UNKNOWN,
    AmbiguousOrderResponse,
    LiveExecutionError,
    OrderFillSnapshot,
    extract_order_id,
    parse_hyperliquid_close_response,
    parse_hyperliquid_entry_response,
    parse_order_fill_snapshot,
)
from src.data.database import Database, TradeEntry, TradeExit
from src.core.regime import regime_strategy_name
from src.strategies.base import MarketEvent, Position, Signal
from src.utils.config import Config
from src.utils.helpers import safe_float, safe_divide, utc_now, utc_timestamp_ms

logger = logging.getLogger(__name__)


# v3.1.22: order status constants used by the OMS background poller.
ORDER_STATUS_FILLED: str = "filled"
ORDER_STATUS_PARTIAL: str = "partial"
ORDER_STATUS_OPEN: str = "open"
ORDER_STATUS_CANCELLED: str = "cancelled"
ORDER_STATUS_REJECTED: str = "rejected"
ORDER_STATUS_TIMEOUT: str = "timeout"

# v3.1.22: how long to wait for a live order to fill before treating
# it as a timeout. The 60s default matches Hyperliquid's REST
# acknowledgement window for limit orders.
DEFAULT_LIVE_ORDER_TIMEOUT_S: float = 60.0

# v3.1.22: how often the background task polls open orders.
DEFAULT_OMS_POLL_INTERVAL_S: float = 5.0


@dataclass
class TradeResult:
    """Result of an entry or exit operation.

    ``status`` is ``'open'`` for a live trade, ``'closed'`` for an exited
    trade, or ``'rejected'`` when the entry was refused (debounce, duplicate,
    risk gate, etc.).  Callers **must** check ``status`` before reading
    trade-specific fields.
    """

    trade_id: int
    symbol: str
    side: str
    entry_price: float
    exit_price: Optional[float]
    size: float
    pnl_usd: float
    pnl_pct: float
    status: str  # 'open' | 'closed'
    reason: str
    timestamp_ms: int
    entry_fee: float = 0.0  # fee paid on entry (for portfolio cash tracking)
    funding_total: float = 0.0  # accumulated funding cashflow over the hold
    pnl_pct_capital: float = 0.0  # pnl_pct as fraction of total capital (for Kelly)
    exchange_order_id: Optional[str] = None  # v3.1.22: HL order id for OMS tracking


@dataclass
class KillSwitchResult:
    """Outcome of a live kill-switch invocation."""

    orders_cancelled: int = 0
    positions_closed: List[str] = field(default_factory=list)
    exchange_flat: bool = False
    errors: List[str] = field(default_factory=list)


class ExecutionEngine:
    """Execute trades across three modes:

    * **paper** — simulate fills at current price, no external API calls.
    * **testnet** — submit to Hyperliquid testnet, track order status.
    * **mainnet** — submit to Hyperliquid mainnet (requires manual activation).

    Args:
        config:  Application configuration.
        db:      SQLite database for trade persistence.
        mode:    One of ``'paper'``, ``'testnet'``, ``'mainnet'``.
    """

    VALID_MODES: List[str] = ["paper", "testnet", "mainnet"]

    def __init__(
        self,
        config: Config,
        db: Database,
        mode: str = "paper",
    ) -> None:
        if mode not in self.VALID_MODES:
            raise ValueError(f"Invalid mode '{mode}'. Must be one of: {self.VALID_MODES}")

        self._config = config
        self._db = db
        self._mode = mode

        # Fee model — Hyperliquid taker fee (paper & live)
        self._taker_fee_pct: float = safe_float(
            config.get("risk.taker_fee_pct", 0.045)
        ) / 100.0

        # Slippage model for paper trading
        self._paper_slippage_pct: float = safe_float(
            config.get("risk.paper_slippage_pct", 0.05)
        ) / 100.0

        maker_cfg = config.get("execution.maker_orders", {}) or {}
        self._maker_orders_enabled = bool(maker_cfg.get("enabled", False))
        self._maker_fee_pct: float = safe_float(
            maker_cfg.get("maker_fee_pct", 0.015)
        ) / 100.0
        self._maker_timeout_ms: int = int(maker_cfg.get("timeout_ms", 30_000))

        # Mainnet safety gate — HIGH-007: require explicit env var + config flag
        env_mainnet = os.environ.get("HYPERLIQUID_MAINNET_ENABLED", "").lower() in ("1", "true", "yes")
        cfg_mainnet = bool(config.get("exchange.mainnet_enabled", False))
        self._mainnet_enabled: bool = env_mainnet and cfg_mainnet
        if self._mode == "mainnet" and not self._mainnet_enabled:
            raise RuntimeError(
                "Mainnet mode blocked: set both HYPERLIQUID_MAINNET_ENABLED=1 env var "
                "AND exchange.mainnet_enabled=true in config to enable live trading."
            )
        if mode == "mainnet" and not self._mainnet_enabled:
            raise RuntimeError(
                "Mainnet mode requested but 'exchange.mainnet_enabled' is False in config. "
                "Set it to True explicitly to trade real funds."
            )

        # REST client (lazy-initialised for testnet / mainnet)
        self._rest_client: Optional[Any] = None
        self._live_client: Optional[Any] = None
        self._live_signing_ready: bool = False

        # v3.1.17 C10: cache initial capital for Kelly's pnl_pct_capital
        # (Kelly expects PnL/capital, not PnL/notional).
        self._initial_capital: float = safe_float(
            config.get("risk.initial_capital", config.get("backtest.initial_capital", 10_000.0))
        )

        # In-memory open trade index: symbol → TradeResult
        self._open_trades: Dict[str, TradeResult] = {}
        self._lock = asyncio.Lock()

        # Entry debounce: symbol → timestamp_ms (prevent rapid re-entry)
        self._last_entry_ms: Dict[str, int] = {}
        self._entry_debounce_ms: int = int(
            config.get("execution.entry_debounce_ms", 5_000)
        )  # 5s default

        # v3.1.22: OMS order tracking (live mode only). Maps HL
        # order id → {symbol, side, size, price, status, timestamp,
        # filled_size, trade_id}. The background poller walks this
        # dict and updates each row.
        self._live_orders: Dict[str, Dict[str, Any]] = {}
        self._order_status_callbacks: List[Any] = []
        self._oms_poll_interval_s: float = float(
            config.get("execution.oms_poll_interval_s", DEFAULT_OMS_POLL_INTERVAL_S)
        )
        self._live_order_timeout_s: float = float(
            config.get("execution.live_order_timeout_s", DEFAULT_LIVE_ORDER_TIMEOUT_S)
        )
        self._oms_task: Optional[asyncio.Task[Any]] = None
        self._oms_alert_callback: Optional[Any] = None

        # Phase 01: portfolio reference for rollback (injected by engine)
        self._portfolio: Optional[Any] = None

        # Phase 01: symbols blocked after ambiguous HL responses
        self._blocked_symbols: Dict[str, str] = {}

        # Kill-switch latch: prevents OMS finalize paths from reinserting
        # positions after an emergency flatten. Cleared on open().
        self._kill_switch_active: bool = False

        # Phase 01: idempotency — client order ids already submitted
        self._submitted_client_order_ids: Dict[str, int] = {}

        # Phase 01: align clamp semantics with RiskManager
        config_max_pos_pct = safe_float(
            config.get("risk.max_position_size_pct", 5.0)
        ) / 100.0
        self._max_position_size_pct = min(
            config_max_pos_pct,
            self.MAX_POSITION_SIZE_CEILING,
        )
        config_leverage = safe_float(config.get("risk.leverage_max", 1.0))
        self._leverage_max = max(1.0, min(config_leverage, 20.0))

        native_cfg = config.get("execution.native_protection", {}) or {}
        self._native_protection_enabled = bool(native_cfg.get("enabled", True))

        # Phase 03: injected by engine after live client is ready
        self._protection_manager: Optional[Any] = None
        self._reconciler: Optional[Any] = None

    MAX_POSITION_SIZE_CEILING: float = 0.20

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_portfolio(self, portfolio: Any) -> None:
        """Inject PortfolioState for live rollback paths (Phase 01)."""
        self._portfolio = portfolio

    def set_protection_manager(self, manager: Any) -> None:
        """Inject :class:`NativeProtectionManager` (Phase 03)."""
        self._protection_manager = manager

    def set_reconciler(self, reconciler: Any) -> None:
        """Inject :class:`ExchangeReconciler` for entry gating (Phase 03)."""
        self._reconciler = reconciler

    def reconciliation_blocks_entries(self) -> bool:
        recon = self._reconciler
        if recon is None:
            return False
        return bool(getattr(recon, "entries_blocked", lambda: False)())

    def reconciliation_block_reason(self) -> Optional[str]:
        recon = self._reconciler
        if recon is None:
            return None
        return getattr(recon, "block_reason", lambda: None)()

    def set_oms_alert_callback(self, cb: Any) -> None:
        """Register ``cb(event: str, order_id: str, record: dict)`` for OMS alerts."""
        if callable(cb):
            self._oms_alert_callback = cb

    def is_symbol_blocked(self, symbol: str) -> bool:
        return symbol.upper() in {
            k.upper() for k in self._blocked_symbols
        }

    def get_symbol_block_reason(self, symbol: str) -> Optional[str]:
        for key, reason in self._blocked_symbols.items():
            if key.upper() == symbol.upper():
                return reason
        return None

    def block_symbol(self, symbol: str, reason: str) -> None:
        self._blocked_symbols[symbol.upper()] = reason
        logger.error(
            "ExecutionEngine blocked entries for %s — %s",
            symbol,
            reason,
        )

    def unblock_symbol(self, symbol: str) -> None:
        self._blocked_symbols.pop(symbol.upper(), None)

    def get_blocked_symbols(self) -> List[str]:
        """Return currently blocked symbols (uppercase)."""
        return list(self._blocked_symbols.keys())

    def _require_live_execution_ready(self) -> None:
        """Fail closed when live prerequisites are missing."""
        if self._rest_client is None:
            raise LiveExecutionError("rest_client_unavailable")
        if not self._live_signing_ready or self._live_client is None:
            raise LiveExecutionError("signing_not_configured")

    @staticmethod
    def _make_client_order_id(symbol: str, side: str, timestamp_ms: int) -> str:
        return f"hlbot-{symbol.upper()}-{side}-{timestamp_ms}"

    def _map_lifecycle_to_oms_status(self, lifecycle_state: str) -> str:
        mapping = {
            ORDER_PENDING_SUBMISSION: ORDER_STATUS_OPEN,
            ORDER_RESTING: ORDER_STATUS_OPEN,
            ORDER_PARTIAL: ORDER_STATUS_PARTIAL,
            ORDER_FILLED: ORDER_STATUS_FILLED,
            ORDER_CLOSE_PENDING: ORDER_STATUS_OPEN,
            ORDER_SUBMISSION_UNKNOWN: ORDER_STATUS_OPEN,
        }
        return mapping.get(lifecycle_state, ORDER_STATUS_OPEN)

    async def _rollback_live_entry(
        self,
        portfolio: Any,
        symbol: str,
        trade_id: int,
        reason: str,
    ) -> None:
        """Atomic rollback after a failed live entry."""
        target = portfolio if portfolio is not None else self._portfolio
        if target is not None:
            try:
                await target.cancel_position(symbol)
            except Exception as inner_exc:  # noqa: BLE001
                logger.exception("cancel_position rollback failed: %s", inner_exc)
        async with self._lock:
            self._open_trades.pop(symbol, None)
            self._last_entry_ms.pop(symbol, None)
        if trade_id > 0:
            try:
                self._db.update_trade_status(
                    trade_id,
                    status="cancelled",
                    reason=reason[:100],
                )
            except Exception as inner_exc:  # noqa: BLE001
                logger.exception("update_trade_status rollback failed: %s", inner_exc)

    async def open(self) -> None:
        """Prepare the execution engine (open REST session if needed)."""
        # Re-arm after kill_switch: a fresh open() is the explicit reset point.
        self._kill_switch_active = False
        if self._mode in ("testnet", "mainnet"):
            from src.exchanges.hyperliquid_rest import HyperliquidRESTClient
            from src.exchanges.hyperliquid_live import HyperliquidLiveClient, resolve_private_key

            use_testnet = self._mode == "testnet"
            self._rest_client = HyperliquidRESTClient(use_testnet=use_testnet)
            await self._rest_client.open()
            logger.info("ExecutionEngine REST client opened (mode=%s)", self._mode)

            private_key = resolve_private_key()
            if private_key:
                self._live_client = HyperliquidLiveClient(
                    private_key,
                    use_testnet=use_testnet,
                )
                await self._live_client.open()
                self._live_signing_ready = True
                logger.info(
                    "ExecutionEngine live signing ready (wallet=%s)",
                    self._live_client.wallet_address,
                )
            else:
                logger.warning(
                    "Live mode=%s but no signing key — set %s or vault key '%s'. "
                    "Orders will be logged only.",
                    self._mode,
                    "HYPERLIQUID_PRIVATE_KEY",
                    "hyperliquid_private_key",
                )

    async def close(self) -> None:
        """Gracefully close any open REST / live sessions."""
        await self.stop_oms_loop()
        if self._live_client is not None:
            try:
                close_fn = getattr(self._live_client, "close", None)
                if close_fn is not None:
                    maybe = close_fn()
                    if asyncio.iscoroutine(maybe) or asyncio.isfuture(maybe):
                        await maybe
            except Exception as exc:  # noqa: BLE001
                logger.warning("ExecutionEngine live client close failed: %s", exc)
            finally:
                self._live_client = None
                self._live_signing_ready = False
        else:
            self._live_signing_ready = False

        if self._rest_client is not None:
            try:
                await self._rest_client.close()
                logger.info("ExecutionEngine REST client closed")
            except Exception as exc:  # noqa: BLE001
                logger.warning("ExecutionEngine REST client close failed: %s", exc)
            finally:
                self._rest_client = None

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    async def enter_position(
        self,
        signal: Signal,
        portfolio: Any,  # PortfolioState
        market_event: Optional[MarketEvent] = None,
        candles_1m: Optional[List[Any]] = None,
        orderbook: Optional[Any] = None,
        estimated_slippage_bps: Optional[float] = None,
    ) -> TradeResult:
        """Open a new position.

        In **paper** mode the fill is simulated instantly at *signal.entry_price*.
        In **testnet** / **mainnet** mode an order is submitted via the REST
        client and the result is tracked.

        v3.1.22:
          * ``estimated_slippage_bps`` — when provided (typically
            from the engine's L2 ``estimate_slippage``), used in
            place of the flat ``risk.paper_slippage_pct`` for paper
            fills so simulation matches L2 reality.
          * ``candles_1m`` / ``orderbook`` — accepted for symmetry
            with future analytics; not consumed in this method.
          * The returned ``TradeResult.exchange_order_id`` is set
            whenever a live order is placed, so the OMS poller can
            later look it up.

        QW2: when *market_event* is provided, a JSON snapshot of the regime
        (ADX, OIR, funding, imbalance, etc.) is stored alongside the trade
        for post-mortem analysis.

        Returns a :class:`TradeResult` in all cases.
        """
        now_ms = utc_timestamp_ms()

        # --- Debounce: prevent rapid re-entry for same symbol ---
        last_entry = self._last_entry_ms.get(signal.symbol, 0)
        if now_ms - last_entry < self._entry_debounce_ms:
            logger.warning(
                "enter_position REJECTED %s — debounce active (%d ms remaining)",
                signal.symbol,
                self._entry_debounce_ms - (now_ms - last_entry),
            )
            return TradeResult(
                trade_id=0, symbol=signal.symbol, side=signal.side,
                entry_price=0.0, exit_price=None, size=0.0,
                pnl_usd=0.0, pnl_pct=0.0, status="rejected",
                reason=f"debounce_{self._entry_debounce_ms}ms",
                timestamp_ms=now_ms,
            )

        # --- Check if already have open trade for this symbol ---
        async with self._lock:
            if signal.symbol in self._open_trades:
                existing = self._open_trades[signal.symbol]
                logger.warning(
                    "enter_position REJECTED %s — already have open trade (id=%d)",
                    signal.symbol, existing.trade_id,
                )
                return TradeResult(
                    trade_id=0, symbol=signal.symbol, side=signal.side,
                    entry_price=0.0, exit_price=None, size=0.0,
                    pnl_usd=0.0, pnl_pct=0.0, status="rejected",
                    reason="duplicate_position",
                    timestamp_ms=now_ms,
                )

        # Cross-process guard: another main.py may share this SQLite DB.
        try:
            db_open = self._db.get_open_trades()
            for row in db_open:
                if str(row.get("symbol", "")).upper() == signal.symbol.upper():
                    logger.warning(
                        "enter_position REJECTED %s — open trade in DB (id=%s)",
                        signal.symbol, row.get("id"),
                    )
                    return TradeResult(
                        trade_id=0, symbol=signal.symbol, side=signal.side,
                        entry_price=0.0, exit_price=None, size=0.0,
                        pnl_usd=0.0, pnl_pct=0.0, status="rejected",
                        reason="duplicate_position_db",
                        timestamp_ms=now_ms,
                    )
        except Exception:
            logger.exception("Failed DB duplicate-position check for %s", signal.symbol)

        if self._mode in ("testnet", "mainnet") and self.reconciliation_blocks_entries():
            block_reason = self.reconciliation_block_reason() or "reconciliation_blocked"
            logger.warning(
                "enter_position REJECTED %s — reconciliation gate (%s)",
                signal.symbol,
                block_reason,
            )
            return TradeResult(
                trade_id=0, symbol=signal.symbol, side=signal.side,
                entry_price=0.0, exit_price=None, size=0.0,
                pnl_usd=0.0, pnl_pct=0.0, status="rejected",
                reason=f"reconciliation_blocked:{block_reason}",
                timestamp_ms=now_ms,
            )

        if self._mode in ("testnet", "mainnet") and self.is_symbol_blocked(signal.symbol):
            block_reason = self.get_symbol_block_reason(signal.symbol) or "symbol_blocked"
            logger.warning(
                "enter_position REJECTED %s — blocked until reconciliation (%s)",
                signal.symbol,
                block_reason,
            )
            return TradeResult(
                trade_id=0, symbol=signal.symbol, side=signal.side,
                entry_price=0.0, exit_price=None, size=0.0,
                pnl_usd=0.0, pnl_pct=0.0, status="rejected",
                reason=f"symbol_blocked:{block_reason}",
                timestamp_ms=now_ms,
            )

        client_order_id = self._make_client_order_id(signal.symbol, signal.side, now_ms)
        if client_order_id in self._submitted_client_order_ids:
            prior_trade = self._submitted_client_order_ids[client_order_id]
            logger.warning(
                "enter_position REJECTED %s — duplicate client_order_id (trade_id=%s)",
                signal.symbol,
                prior_trade,
            )
            return TradeResult(
                trade_id=0, symbol=signal.symbol, side=signal.side,
                entry_price=0.0, exit_price=None, size=0.0,
                pnl_usd=0.0, pnl_pct=0.0, status="rejected",
                reason="duplicate_client_order_id",
                timestamp_ms=now_ms,
            )

        raw_price = safe_float(signal.entry_price)
        if raw_price <= 0.0:
            logger.error("enter_position: invalid price %.4f for %s", raw_price, signal.symbol)
            raise ValueError(f"Invalid entry price for {signal.symbol}: {raw_price}")

        meta = signal.metadata or {}
        order_type = str(meta.get("order_type", "market"))
        entry_fee_pct = safe_float(meta.get("entry_fee_pct"), self._taker_fee_pct)

        size = safe_float(meta.get("calculated_size", 0.0))
        if size <= 0.0:
            logger.error("enter_position: zero size for %s", signal.symbol)
            raise ValueError(f"Calculated position size is zero for {signal.symbol}")

        # Compute fill price (paper slippage for market; limit at bid/ask for maker)
        if order_type == "limit_maker":
            fill_price = safe_float(meta.get("limit_price"), raw_price)
            if fill_price <= 0.0:
                fill_price = raw_price
        elif self._mode == "paper":
            # v3.1.22: prefer the L2-derived estimate when the engine
            # supplies one. ``estimated_slippage_bps`` is in basis
            # points (e.g. 5 bps = 0.05%) so divide by 10_000.
            if estimated_slippage_bps is not None and estimated_slippage_bps > 0:
                slippage = float(estimated_slippage_bps) / 10_000.0
            else:
                slippage = self._paper_slippage_pct
            if signal.side == "long":
                fill_price = raw_price * (1.0 + slippage)
            else:
                fill_price = raw_price * (1.0 - slippage)
        else:
            fill_price = raw_price

        # Phase 01: clamp using the same config semantics as RiskManager
        capital = await portfolio.current_capital
        max_notional = capital * self._max_position_size_pct * self._leverage_max
        current_notional = fill_price * size

        if current_notional > max_notional and fill_price > 0:
            old_size = size
            size = max_notional / fill_price
            logger.warning(
                "POSITION SIZE CLAMPED for %s — %.6f → %.6f "
                "(notional $%.2f > max $%.2f, cap=%.1f%% × %.1fx)",
                signal.symbol,
                old_size,
                size,
                current_notional,
                max_notional,
                self._max_position_size_pct * 100.0,
                self._leverage_max,
            )

        # Re-validate after clamp: non-positive capital → max_notional ≤ 0 → size ≤ 0
        if size <= 0.0:
            logger.error(
                "enter_position: non-positive size after notional clamp for %s "
                "(capital=%.4f, max_notional=%.4f) — capital was non-positive",
                signal.symbol,
                capital,
                max_notional,
            )
            raise ValueError(f"Calculated position size is zero for {signal.symbol}")

        notional = fill_price * size
        entry_fee = notional * entry_fee_pct

        # Persist entry to DB — attribute to originating sub-strategy when ensemble
        sub_strategy = regime_strategy_name(signal)

        # QW2: build trade journal snapshot (regime context at entry)
        snapshot_json: Optional[str] = None
        signal_meta_json: Optional[str] = None
        entry_adx: Optional[float] = None
        entry_oir: Optional[float] = None
        entry_funding: Optional[float] = None
        entry_predicted_funding: Optional[float] = None
        entry_bid_ask_imbalance: Optional[float] = None
        entry_volume_1m: Optional[float] = None
        if market_event is not None:
            try:
                snapshot = {
                    "adx_14": market_event.adx_14,
                    "atr_14": market_event.atr_14,
                    "rsi_14": market_event.rsi_14,
                    "ema_20": market_event.ema_20,
                    "funding": market_event.funding,
                    "predicted_funding": market_event.predicted_funding,
                    "funding_avg": market_event.funding_avg,
                    "funding_weighted": market_event.funding_weighted,
                    "predicted_funding_avg": market_event.predicted_funding_avg,
                    "oi_total": market_event.oi_total,
                    "oi_total_aggregated": market_event.oi_total_aggregated,
                    "oi_delta": market_event.oi_delta,
                    "oi_exchange_count": market_event.oi_exchange_count,
                    "volume_1m": market_event.volume_1m,
                    "bid_ask_imbalance": market_event.bid_ask_imbalance,
                    "vwap_15m": market_event.vwap_15m,
                    "orderbook_spread_pct": market_event.orderbook_spread_pct,
                    "orderbook_oir": market_event.orderbook_oir,
                    "orderbook_depth_quality": market_event.orderbook_depth_quality,
                    "orderbook_bid_ask_ratio": market_event.orderbook_bid_ask_ratio,
                    "liquidation_notional_5m": market_event.liquidation_notional_5m,
                    "liquidation_count_5m": market_event.liquidation_count_5m,
                    "market_data_health": market_event.market_data_health,
                    "market_data_stale": market_event.market_data_stale,
                    "price": market_event.price,
                }
                # Extract candle-derived fields if available
                c1m = market_event.candle_1m
                if c1m is not None:
                    snapshot["candle_1m_buy_volume"] = float(getattr(c1m, "buy_volume", 0.0) or 0.0)
                    snapshot["candle_1m_sell_volume"] = float(getattr(c1m, "sell_volume", 0.0) or 0.0)
                    snapshot["candle_1m_cvd"] = snapshot["candle_1m_buy_volume"] - snapshot["candle_1m_sell_volume"]
                snapshot_json = json.dumps(snapshot, default=str)
            except (TypeError, ValueError) as exc:
                logger.debug("QW2 snapshot build failed (non-fatal): %s", exc)
                snapshot_json = None

            # Map regime fields for SQL-friendly filtering
            entry_adx = market_event.adx_14
            entry_oir = market_event.orderbook_oir
            entry_funding = market_event.funding
            entry_predicted_funding = market_event.predicted_funding
            entry_bid_ask_imbalance = market_event.bid_ask_imbalance
            entry_volume_1m = market_event.volume_1m

        # Encode signal metadata for the journal (include stop/TP for restore)
        try:
            meta = dict(signal.metadata) if signal.metadata else {}
            if signal.stop_loss_pct is not None and signal.stop_loss_pct > 0:
                meta["stop_loss_pct"] = float(signal.stop_loss_pct)
            if signal.take_profit_pct is not None and signal.take_profit_pct > 0:
                meta["take_profit_pct"] = float(signal.take_profit_pct)
            signal_meta_json = json.dumps(meta, default=str)
        except (TypeError, ValueError):
            signal_meta_json = None

        exchange_order_id: Optional[str] = None
        lifecycle_state = ORDER_FILLED if self._mode == "paper" else ORDER_PENDING_SUBMISSION

        if self._mode in ("testnet", "mainnet"):
            self._require_live_execution_ready()
            try:
                hl_response = await self._submit_live_order(
                    signal, size, fill_price
                )
                parsed = parse_hyperliquid_entry_response(hl_response)
                if parsed.ambiguous:
                    lifecycle_state = ORDER_SUBMISSION_UNKNOWN
                    exchange_order_id = parsed.exchange_order_id
                    self.block_symbol(
                        signal.symbol,
                        f"submission_unknown:{parsed.error_message or 'ambiguous'}",
                    )
                elif parsed.error_message and parsed.lifecycle_state == ORDER_REJECTED:
                    raise LiveExecutionError(parsed.error_message)
                else:
                    lifecycle_state = parsed.lifecycle_state
                    exchange_order_id = parsed.exchange_order_id
            except LiveExecutionError:
                raise
            except AmbiguousOrderResponse:
                raise
            except Exception as exc:
                logger.error(
                    "Live order failed for %s before DB persist: %s",
                    signal.symbol,
                    exc,
                    exc_info=True,
                )
                raise LiveExecutionError(str(exc)) from exc

        trade_status = "open"
        if self._mode in ("testnet", "mainnet"):
            if lifecycle_state == ORDER_RESTING:
                trade_status = ORDER_RESTING
            elif lifecycle_state in (ORDER_PENDING_SUBMISSION, ORDER_SUBMISSION_UNKNOWN):
                trade_status = lifecycle_state

        entry_record = TradeEntry(
            symbol=signal.symbol,
            side=signal.side,
            entry_price=fill_price,
            entry_time=now_ms,
            size=size,
            strategy=signal.strategy,
            sub_strategy=str(sub_strategy) if sub_strategy else None,
            status=trade_status,
            entry_adx=entry_adx,
            entry_oir=entry_oir,
            entry_funding=entry_funding,
            entry_predicted_funding=entry_predicted_funding,
            entry_bid_ask_imbalance=entry_bid_ask_imbalance,
            entry_volume_1m=entry_volume_1m,
            entry_market_snapshot=snapshot_json,
            signal_metadata=signal_meta_json,
            entry_fee=entry_fee,
        )

        trade_id = self._db.save_trade_entry(entry_record)
        self._submitted_client_order_ids[client_order_id] = int(trade_id)

        if self._mode in ("testnet", "mainnet"):
            self._db.update_trade_order_tracking(
                int(trade_id),
                exchange_order_id=exchange_order_id,
                client_order_id=client_order_id,
                order_submitted_at=now_ms,
                filled_size=float(size) if lifecycle_state == ORDER_FILLED else 0.0,
            )

        if self._mode in ("testnet", "mainnet") and exchange_order_id:
            oms_status = self._map_lifecycle_to_oms_status(lifecycle_state)
            target_size = float(size)
            filled = target_size if lifecycle_state == ORDER_FILLED else 0.0
            self._live_orders[exchange_order_id] = {
                "symbol": signal.symbol,
                "side": signal.side,
                "size": target_size,
                "price": float(fill_price),
                "filled_size": filled,
                "remaining_size": max(0.0, target_size - filled),
                "avg_fill_price": float(fill_price) if filled > 0 else 0.0,
                "cumulative_fee": float(entry_fee) if filled > 0 else 0.0,
                "status": oms_status,
                "lifecycle_state": lifecycle_state,
                "timestamp": time.time(),
                "submitted_at_ms": now_ms,
                "last_fill_at_ms": now_ms if filled > 0 else None,
                "trade_id": int(trade_id),
                "order_type": order_type,
                "client_order_id": client_order_id,
                "exchange_order_id": exchange_order_id,
                "applied_fill_size": filled,
                "terminal_handled": lifecycle_state in (ORDER_FILLED, ORDER_REJECTED),
                "processed_events": [],
            }
            logger.info(
                "OMS: tracking order %s state=%s (symbol=%s trade_id=%d)",
                exchange_order_id,
                lifecycle_state,
                signal.symbol,
                trade_id,
            )
            if lifecycle_state == ORDER_FILLED:
                self._fire_order_callbacks(
                    exchange_order_id,
                    ORDER_STATUS_FILLED,
                    self._live_orders[exchange_order_id],
                )

        result_status = "open"
        if self._mode in ("testnet", "mainnet") and lifecycle_state not in (
            ORDER_FILLED,
            ORDER_PARTIAL,
        ):
            result_status = "pending"

        result = TradeResult(
            trade_id=trade_id,
            symbol=signal.symbol,
            side=signal.side,
            entry_price=fill_price,
            exit_price=None,
            size=size,
            pnl_usd=0.0,
            pnl_pct=0.0,
            status=result_status,
            reason=signal.reason,
            timestamp_ms=now_ms,
            entry_fee=entry_fee,
            exchange_order_id=exchange_order_id,
        )

        async with self._lock:
            if result_status == "open":
                self._open_trades[signal.symbol] = result
            self._last_entry_ms[signal.symbol] = now_ms

        logger.info(
            "ENTER  mode=%s id=%d %s %s size=%.6f @ %.4f order=%s fee=%.4f%% ($%.2f) (%s)",
            self._mode,
            trade_id,
            signal.symbol,
            signal.side,
            size,
            fill_price,
            order_type,
            entry_fee_pct * 100.0,
            entry_fee,
            signal.reason,
        )
        return result

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    async def close_position(
        self,
        position: Position,
        exit_price: float,
        reason: str,
    ) -> TradeResult:
        """Close an existing open position.

        Computes realized PnL, updates the DB trade row, and removes the
        position from the internal open-trade index.
        """
        now_ms = utc_timestamp_ms()
        raw_exit = safe_float(exit_price)
        if raw_exit <= 0.0:
            logger.error("close_position: invalid exit price %.4f for %s", raw_exit, position.symbol)
            raise ValueError(f"Invalid exit price for {position.symbol}: {raw_exit}")

        # Paper slippage: worse fill for market exits; maker exits at touch
        pos_meta = position.metadata or {}
        exit_fee_pct = safe_float(pos_meta.get("exit_fee_pct"), self._taker_fee_pct)
        exit_slip = safe_float(pos_meta.get("exit_slippage_pct"), self._paper_slippage_pct)

        if self._mode == "paper" and exit_slip > 0:
            if position.side == "long":
                fill_exit = raw_exit * (1.0 - exit_slip)
            else:
                fill_exit = raw_exit * (1.0 + exit_slip)
        else:
            fill_exit = raw_exit

        notional = position.entry_price * position.size
        exit_notional = fill_exit * position.size
        exit_fee = exit_notional * exit_fee_pct

        async with self._lock:
            open_trade = self._open_trades.pop(position.symbol, None)

        if open_trade is None:
            logger.warning("close_position: no open trade found for %s", position.symbol)
            # Build a synthetic result so callers don't crash
            entry_fee = 0.0
            pnl_usd = self._compute_pnl(position, fill_exit) - exit_fee - entry_fee
            pnl_pct = safe_divide(pnl_usd, notional, 0.0)
            return TradeResult(
                trade_id=-1,
                symbol=position.symbol,
                side=position.side,
                entry_price=position.entry_price,
                exit_price=fill_exit,
                size=position.size,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                status="closed",
                reason=reason,
                timestamp_ms=now_ms,
            )

        entry_fee = safe_float(open_trade.entry_fee, 0.0)
        pnl_usd = self._compute_pnl(position, fill_exit) - exit_fee - entry_fee
        pnl_pct = safe_divide(pnl_usd, notional, 0.0)
        # v3.1.17 C5: read accumulated funding for the trade journal.
        funding_total = safe_float(
            (position.metadata or {}).get("funding_total", 0.0), 0.0
        )
        # v3.1.17 C10: also compute PnL as a fraction of total capital
        # (Kelly expects PnL/capital, not PnL/notional).
        capital = safe_float(getattr(self, "_initial_capital", 0.0), 0.0)
        pnl_pct_capital = safe_divide(pnl_usd, capital, 0.0) if capital > 0 else 0.0

        if self._mode in ("testnet", "mainnet"):
            self._require_live_execution_ready()
            if self._protection_manager is not None:
                await self._protection_manager.cancel_protection(position.symbol)
            try:
                hl_response = await self._submit_live_close(position, fill_exit)
                parsed = parse_hyperliquid_close_response(hl_response)
                if parsed.lifecycle_state != ORDER_FILLED:
                    if parsed.ambiguous:
                        self.block_symbol(
                            position.symbol,
                            f"ambiguous_close:{parsed.error_message or 'unknown'}",
                        )
                        raise AmbiguousOrderResponse(
                            parsed.error_message or "ambiguous_close_response"
                        )
                    raise LiveExecutionError(
                        parsed.error_message or "live_close_rejected"
                    )
            except (LiveExecutionError, AmbiguousOrderResponse):
                async with self._lock:
                    self._open_trades[position.symbol] = open_trade
                await self._restore_protection_after_failed_close(position, open_trade)
                raise
            except Exception as exc:
                logger.error(
                    "Live close failed for %s — keeping trade_id=%d open: %s",
                    position.symbol,
                    open_trade.trade_id,
                    exc,
                    exc_info=True,
                )
                async with self._lock:
                    self._open_trades[position.symbol] = open_trade
                await self._restore_protection_after_failed_close(position, open_trade)
                raise LiveExecutionError(str(exc)) from exc

        exit_record = TradeExit(
            trade_id=open_trade.trade_id,
            exit_price=fill_exit,
            exit_time=now_ms,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            status="closed",
            funding_paid=funding_total,
        )
        self._db.update_trade_exit(exit_record)

        result = TradeResult(
            trade_id=open_trade.trade_id,
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=fill_exit,
            size=position.size,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            status="closed",
            reason=reason,
            timestamp_ms=now_ms,
            funding_total=funding_total,
            pnl_pct_capital=pnl_pct_capital,
        )

        logger.info(
            "EXIT   mode=%s id=%d %s %s pnl=%.2f (%.2f%%) fee=$%.2f reason=%s",
            self._mode,
            result.trade_id,
            position.symbol,
            position.side,
            pnl_usd,
            pnl_pct * 100.0,
            exit_fee,
            reason,
        )
        return result

    # ------------------------------------------------------------------
    # Price tracking
    # ------------------------------------------------------------------

    async def update_position_prices(self, prices: Dict[str, float]) -> None:
        """Update the last-known price for every tracked open position.

        Called by the engine on every price tick so that the portfolio's
        unrealized PnL stays current.
        """
        async with self._lock:
            for symbol, price in prices.items():
                price_f = safe_float(price)
                if price_f <= 0.0:
                    logger.warning("update_position_prices: invalid price %.4f for %s", price_f, symbol)
                    continue
                trade = self._open_trades.get(symbol)
                if trade is not None:
                    # Mutate in-place — TradeResult is mutable (dataclass, not frozen)
                    trade.exit_price = price_f  # Re-use field as "current mark price"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_pnl(self, position: Position, exit_price: float) -> float:
        """Compute realized PnL in USD for a position closing at *exit_price*."""
        if position.side == "long":
            return (exit_price - position.entry_price) * position.size
        return (position.entry_price - exit_price) * position.size

    async def _submit_live_order(
        self,
        signal: Signal,
        size: float,
        price: float,
    ) -> Dict[str, Any]:
        """Submit a signed order to Hyperliquid (testnet or mainnet).

        Phase 01: caller must run ``_require_live_execution_ready`` first.
        Raises on SDK/network failure; returns the raw HL response dict.
        """
        self._require_live_execution_ready()

        meta = signal.metadata or {}
        order_type = str(meta.get("order_type", "market"))
        post_only = bool(meta.get("post_only", False))
        limit_price = safe_float(meta.get("limit_price"), price)

        if order_type == "limit_maker":
            logger.info(
                "LIVE LIMIT MAKER (Alo) %s %s size=%.6f @ %.4f post_only=%s",
                signal.symbol,
                signal.side,
                size,
                limit_price,
                post_only,
            )
        else:
            logger.info(
                "LIVE MARKET ORDER %s %s size=%.6f @ ~%.4f",
                signal.symbol,
                signal.side,
                size,
                price,
            )

        return await self._live_client.place_entry(
            signal.symbol,
            signal.side,
            size,
            order_type=order_type,
            limit_price=limit_price,
            post_only=post_only,
        )

    @staticmethod
    def _extract_order_id(hl_response: Optional[Dict[str, Any]]) -> Optional[str]:
        """Pull the HL order id out of a (potentially nested) response."""
        return extract_order_id(hl_response)

    async def _submit_live_close(
        self,
        position: Position,
        exit_price: float,
    ) -> Dict[str, Any]:
        """Submit a signed closing order to Hyperliquid.

        Phase 01: returns the raw HL response; raises on failure.
        """
        self._require_live_execution_ready()

        close_side = "short" if position.side == "long" else "long"
        logger.info(
            "LIVE CLOSE (submit) %s %s size=%.6f @ %.4f",
            position.symbol,
            close_side,
            position.size,
            exit_price,
        )

        return await self._live_client.close_position(position.symbol, position.size)

    # ------------------------------------------------------------------
    # Phase 03 hooks — native SL/TP (not implemented in Phase 01)
    # ------------------------------------------------------------------

    async def place_native_stop_loss(
        self,
        position: Position,
        stop_price: float,
    ) -> Optional[str]:
        """Place a reduce-only native stop trigger (Phase 03)."""
        if not self._native_protection_enabled or self._protection_manager is None:
            return None
        result = await self._protection_manager.ensure_protection(
            position,
            filled_size=position.size,
            stop_price=stop_price,
            take_profit_price=position.take_profit_price,
            trade_id=(position.metadata or {}).get("trade_id"),
        )
        return result.sl_order_id

    async def place_native_take_profit(
        self,
        position: Position,
        take_profit_price: float,
    ) -> Optional[str]:
        """Place a reduce-only native take-profit trigger (Phase 03)."""
        if not self._native_protection_enabled or self._protection_manager is None:
            return None
        result = await self._protection_manager.ensure_protection(
            position,
            filled_size=position.size,
            stop_price=position.stop_loss_price,
            take_profit_price=take_profit_price,
            trade_id=(position.metadata or {}).get("trade_id"),
        )
        return result.tp_order_id

    async def cancel_native_protection(
        self,
        symbol: str,
        trigger_order_ids: List[str],
    ) -> None:
        """Cancel native SL/TP triggers for *symbol* (Phase 03)."""
        if self._protection_manager is not None:
            await self._protection_manager.cancel_protection(symbol)
            return
        if self._live_client is None:
            return
        for oid in trigger_order_ids:
            if not oid:
                continue
            try:
                await self._live_client.cancel_order(symbol, int(oid))
            except Exception as exc:  # noqa: BLE001
                logger.warning("cancel_native_protection %s oid=%s: %s", symbol, oid, exc)

    async def _restore_protection_after_failed_close(
        self,
        position: Position,
        open_trade: TradeResult,
    ) -> None:
        """Re-arm native SL/TP after a failed live close left the position open.

        Protection was cancelled before the close submit; on failure the
        position must not remain naked on the exchange.
        """
        order_id = str(open_trade.exchange_order_id or "")
        record = {
            "symbol": position.symbol,
            "trade_id": open_trade.trade_id,
            "side": position.side,
            "size": position.size,
        }
        if not self._native_protection_enabled or self._protection_manager is None:
            logger.critical(
                "CLOSE FAILED — protection NOT restored for %s trade_id=%s "
                "(no protection manager / native protection disabled). "
                "LIVE CAPITAL MAY BE UNPROTECTED.",
                position.symbol,
                open_trade.trade_id,
            )
            self._emit_oms_alert("close_failed_protection_not_restored", order_id, record)
            return
        try:
            result = await self._protection_manager.ensure_protection(
                position,
                filled_size=position.size,
                stop_price=position.stop_loss_price,
                take_profit_price=position.take_profit_price,
                trade_id=open_trade.trade_id,
            )
            needs_sl = bool(position.stop_loss_price and position.stop_loss_price > 0)
            restored_ok = not result.errors and (not needs_sl or bool(result.sl_order_id))
            if restored_ok:
                logger.error(
                    "CLOSE FAILED — protection restored for %s trade_id=%s "
                    "(sl=%s tp=%s)",
                    position.symbol,
                    open_trade.trade_id,
                    result.sl_order_id,
                    result.tp_order_id,
                )
                self._emit_oms_alert(
                    "close_failed_protection_restored", order_id, record,
                )
            else:
                logger.critical(
                    "CLOSE FAILED — protection could NOT be restored for %s "
                    "trade_id=%s errors=%s. LIVE CAPITAL MAY BE UNPROTECTED.",
                    position.symbol,
                    open_trade.trade_id,
                    result.errors,
                )
                self._emit_oms_alert(
                    "close_failed_protection_not_restored", order_id, record,
                )
        except Exception as exc:  # noqa: BLE001
            logger.critical(
                "CLOSE FAILED — protection restore raised for %s trade_id=%s: %s. "
                "LIVE CAPITAL MAY BE UNPROTECTED.",
                position.symbol,
                open_trade.trade_id,
                exc,
                exc_info=True,
            )
            self._emit_oms_alert(
                "close_failed_protection_not_restored", order_id, record,
            )

    async def kill_switch(self) -> KillSwitchResult:
        """Cancel all orders, flatten positions, confirm exchange is flat.

        Always latches ``_kill_switch_active``, stops the OMS poller, and
        clears ``_live_orders`` so finalize paths cannot resurrect positions.
        Paper mode clears local state only (``exchange_flat=True``).
        Re-arm via :meth:`open` (or process restart).
        """
        result = KillSwitchResult()
        self._kill_switch_active = True
        try:
            await self.stop_oms_loop()
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"oms_stop:{exc}")
        self._live_orders.clear()

        if self._mode not in ("testnet", "mainnet"):
            if self._portfolio is not None:
                try:
                    mem = await self._portfolio.positions
                    for sym in list(mem.keys()):
                        try:
                            await self._portfolio.cancel_position(sym)
                            result.positions_closed.append(sym)
                        except Exception as exc:  # noqa: BLE001
                            result.errors.append(f"local_cancel_{sym}:{exc}")
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"portfolio_positions:{exc}")
            async with self._lock:
                self._open_trades.clear()
            result.exchange_flat = True
            logger.warning(
                "KILL SWITCH (paper): local positions cleared, OMS stopped, "
                "errors=%d — re-arm via open() or restart",
                len(result.errors),
            )
            return result

        self._require_live_execution_ready()
        try:
            result.orders_cancelled = await self._live_client.cancel_all_orders()
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"cancel_orders:{exc}")

        try:
            flat_results = await self._live_client.flatten_all_positions()
            for item in flat_results:
                if item.get("ok"):
                    sym = str(item.get("symbol", ""))
                    if sym:
                        result.positions_closed.append(sym)
                else:
                    result.errors.append(
                        f"flatten_{item.get('symbol')}:{item.get('error')}"
                    )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"flatten:{exc}")

        try:
            result.exchange_flat = await self._live_client.confirm_flat()
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"confirm_flat:{exc}")
            result.exchange_flat = False

        if self._protection_manager is not None:
            for sym in list(result.positions_closed):
                try:
                    await self._protection_manager.cancel_protection(sym)
                except Exception:  # noqa: BLE001
                    pass

        if self._portfolio is not None:
            mem = await self._portfolio.positions
            for sym in list(mem.keys()):
                try:
                    await self._portfolio.cancel_position(sym)
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"local_cancel_{sym}:{exc}")

        async with self._lock:
            self._open_trades.clear()

        logger.warning(
            "KILL SWITCH complete: cancelled=%d closed=%s flat=%s errors=%d "
            "(OMS stopped; re-arm via open() or restart)",
            result.orders_cancelled,
            result.positions_closed,
            result.exchange_flat,
            len(result.errors),
        )
        return result

    # ------------------------------------------------------------------
    # State recovery
    # ------------------------------------------------------------------

    async def load_open_trades(self, *args, **kwargs) -> list:
        """Load open trades from the DB into the in-memory index.

        Called once by the engine during startup so that positions opened
        in a previous session are tracked correctly.

        CRIT-009 FIX: Accept *args, **kwargs for backward compatibility
        with callers that may pass extra arguments.

        Returns the list of loaded TradeResult objects so callers can
        sync them into PortfolioState.
        """
        # Ignore any extra args for backward compatibility
        if args or kwargs:
            logger.debug("load_open_trades called with extra args (ignored): args=%s kwargs=%s", args, kwargs)
        
        rows = self._db.get_open_trades()
        loaded: list = []
        async with self._lock:
            for row in rows:
                symbol = row["symbol"]
                result = TradeResult(
                    trade_id=row["id"],
                    symbol=symbol,
                    side=row["side"],
                    entry_price=safe_float(row["entry_price"]),
                    exit_price=None,
                    size=safe_float(row["size"]),
                    pnl_usd=safe_float(row.get("pnl_usd", 0.0)),
                    pnl_pct=safe_float(row.get("pnl_pct", 0.0)),
                    status="open",
                    reason=f"restored_from_db:{row.get('strategy', 'unknown')}",
                    timestamp_ms=safe_float(row["entry_time"]),
                    entry_fee=safe_float(row.get("entry_fee", 0.0)),
                )
                self._open_trades[symbol] = result
                loaded.append(result)
        logger.info("Loaded %d open trades from DB", len(loaded))
        return loaded

    async def load_pending_orders(self) -> int:
        """Restore in-flight live orders from DB after restart."""
        if self._mode not in ("testnet", "mainnet"):
            return 0
        rows = self._db.get_pending_live_orders()
        restored = 0
        now_s = time.time()
        for row in rows:
            oid = str(row.get("exchange_order_id") or "")
            if not oid or oid in self._live_orders:
                continue
            target = safe_float(row.get("size"))
            filled = safe_float(row.get("filled_size", 0.0))
            lifecycle = str(row.get("status") or ORDER_RESTING)
            self._live_orders[oid] = {
                "symbol": row["symbol"],
                "side": row["side"],
                "size": target,
                "price": safe_float(row.get("entry_price")),
                "filled_size": filled,
                "remaining_size": max(0.0, target - filled),
                "avg_fill_price": safe_float(row.get("avg_fill_price", row.get("entry_price"))),
                "cumulative_fee": safe_float(row.get("cumulative_order_fee", row.get("entry_fee"))),
                "status": self._map_lifecycle_to_oms_status(lifecycle),
                "lifecycle_state": lifecycle,
                "timestamp": now_s,
                "submitted_at_ms": int(row.get("order_submitted_at") or row.get("entry_time") or 0),
                "last_fill_at_ms": row.get("last_fill_at"),
                "trade_id": int(row["id"]),
                "order_type": "market",
                "client_order_id": row.get("client_order_id"),
                "exchange_order_id": oid,
                "applied_fill_size": filled,
                "terminal_handled": False,
                "processed_events": [],
            }
            client_id = row.get("client_order_id")
            if client_id:
                self._submitted_client_order_ids[str(client_id)] = int(row["id"])
            restored += 1
        if restored:
            logger.info("OMS: restored %d pending live order(s) from DB", restored)
        return restored

    # ------------------------------------------------------------------
    # v3.1.22 / Phase 02: OMS — live order tracking + background poller
    # ------------------------------------------------------------------

    def register_order_callback(self, cb: Any) -> None:
        """Register a callback fired on every order status change.

        Signature: ``cb(order_id: str, status: str, record: dict)``.
        Used by the engine to update its internal state and notify
        notifiers.
        """
        if callable(cb):
            self._order_status_callbacks.append(cb)

    def get_tracked_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Return the in-memory record for *order_id* or None."""
        return self._live_orders.get(order_id)

    def get_open_orders(self) -> Dict[str, Dict[str, Any]]:
        """Return all orders still in 'open' or 'partial' state."""
        return {
            oid: rec
            for oid, rec in self._live_orders.items()
            if rec.get("status") in (ORDER_STATUS_OPEN, ORDER_STATUS_PARTIAL)
        }

    async def fetch_order_snapshot(self, order_id: str, record: Dict[str, Any]) -> OrderFillSnapshot:
        """Query Hyperliquid for fill-aware order status."""
        target_size = safe_float(record.get("size"))
        reference_price = safe_float(record.get("price"))
        if self._live_client is None:
            return OrderFillSnapshot(
                status=record.get("status", ORDER_STATUS_OPEN),
                lifecycle_state=str(record.get("lifecycle_state", ORDER_RESTING)),
                filled_size=safe_float(record.get("filled_size")),
                remaining_size=safe_float(record.get("remaining_size")),
                avg_fill_price=safe_float(record.get("avg_fill_price")),
                cumulative_fee=safe_float(record.get("cumulative_fee")),
                last_fill_at_ms=record.get("last_fill_at_ms"),
            )
        try:
            status_resp = await self._live_client.get_order_status(order_id)
            fills = await self._live_client.get_order_fills(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_order_snapshot: HL error for %s: %s", order_id, exc)
            return OrderFillSnapshot(
                status=record.get("status", ORDER_STATUS_OPEN),
                lifecycle_state=str(record.get("lifecycle_state", ORDER_RESTING)),
                filled_size=safe_float(record.get("filled_size")),
                remaining_size=safe_float(record.get("remaining_size")),
                avg_fill_price=safe_float(record.get("avg_fill_price")),
                cumulative_fee=safe_float(record.get("cumulative_fee")),
                last_fill_at_ms=record.get("last_fill_at_ms"),
                raw_status="query_error",
            )

        snapshot = parse_order_fill_snapshot(
            status_resp,
            fills,
            order_id=order_id,
            target_size=target_size,
            reference_price=reference_price,
        )
        if snapshot.raw_status.lower() in ("unknown", "") and snapshot.filled_size <= 0:
            self._emit_oms_alert("unknown_status", order_id, record)
        return snapshot

    async def check_order_status(self, order_id: str) -> str:
        """Return OMS status string for *order_id* (legacy helper)."""
        record = self._live_orders.get(order_id, {})
        snapshot = await self.fetch_order_snapshot(order_id, record)
        return snapshot.status

    @staticmethod
    def _normalise_hl_status(raw: str) -> str:
        """Map a free-form HL status string to our constants."""
        raw = (raw or "").lower()
        if "filled" in raw or raw == "closed":
            return ORDER_STATUS_FILLED
        if "partial" in raw:
            return ORDER_STATUS_PARTIAL
        if "cancel" in raw:
            return ORDER_STATUS_CANCELLED
        if "reject" in raw or "error" in raw:
            return ORDER_STATUS_REJECTED
        return ORDER_STATUS_OPEN

    async def start_oms_loop(self) -> None:
        """Start the background poller for live orders.

        Idempotent: a second call is a no-op. Stop with
        :meth:`stop_oms_loop`.
        """
        if self._oms_task is not None and not self._oms_task.done():
            return
        if self._mode not in ("testnet", "mainnet"):
            return
        self._oms_task = asyncio.create_task(self._oms_loop())
        logger.info("OMS poller started (interval=%.1fs)", self._oms_poll_interval_s)

    async def stop_oms_loop(self) -> None:
        """Stop the background poller. Idempotent."""
        task = self._oms_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._oms_task = None
        logger.info("OMS poller stopped")

    async def _oms_loop(self) -> None:
        """Background task: poll every open order, take action on fill/timeout/reject."""
        try:
            while True:
                await asyncio.sleep(self._oms_poll_interval_s)
                if not self._live_orders:
                    continue
                open_orders = self.get_open_orders()
                if not open_orders:
                    continue
                for order_id, record in list(open_orders.items()):
                    await self._poll_one_order(order_id, record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("OMS loop crashed: %s", exc)

    async def _poll_one_order(self, order_id: str, record: Dict[str, Any]) -> None:
        """Check one order; apply fill deltas; handle terminal/timeout paths."""
        if record.get("terminal_handled"):
            return

        snapshot = await self.fetch_order_snapshot(order_id, record)
        now_ts = time.time()
        submitted_ms = int(record.get("submitted_at_ms") or 0)
        if submitted_ms > 0:
            age_s = now_ts - (submitted_ms / 1000.0)
        else:
            age_s = now_ts - safe_float(record.get("timestamp", now_ts), now_ts)

        event_key = (
            f"{snapshot.status}:{snapshot.filled_size:.8f}:"
            f"{snapshot.remaining_size:.8f}"
        )
        processed = record.setdefault("processed_events", [])
        if event_key in processed and snapshot.status in (
            ORDER_STATUS_OPEN,
            ORDER_STATUS_PARTIAL,
        ):
            if age_s <= self._live_order_timeout_s:
                return
        processed.append(event_key)

        self._apply_snapshot_to_record(record, snapshot)

        applied_before = safe_float(record.get("applied_fill_size"))
        fill_delta = snapshot.filled_size - applied_before
        if fill_delta > 0:
            await self._apply_fill_delta(order_id, record, snapshot, fill_delta)
            record["applied_fill_size"] = snapshot.filled_size
            trade_id = int(record.get("trade_id", 0))
            if trade_id > 0:
                try:
                    self._db.update_trade_native_protection(
                        trade_id,
                        applied_fill_size=snapshot.filled_size,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("applied_fill_size persist failed: %s", exc)

        if snapshot.status == ORDER_STATUS_FILLED:
            await self._finalize_filled_order(order_id, record, snapshot)
            return

        if snapshot.status == ORDER_STATUS_REJECTED:
            await self._handle_terminal_order(
                order_id, record, reason="rejected", snapshot=snapshot,
            )
            return

        if snapshot.status == ORDER_STATUS_CANCELLED:
            await self._handle_terminal_order(
                order_id, record, reason="cancelled", snapshot=snapshot,
            )
            return

        if snapshot.status == ORDER_STATUS_PARTIAL:
            self._fire_order_callbacks(order_id, ORDER_STATUS_PARTIAL, record)
            self._persist_order_record(record)
            if age_s > self._live_order_timeout_s:
                await self._handle_partial_timeout(order_id, record, age_s)
            return

        # Still open/resting
        if age_s > self._live_order_timeout_s:
            await self._handle_open_timeout(order_id, record, age_s)

    def _apply_snapshot_to_record(
        self,
        record: Dict[str, Any],
        snapshot: OrderFillSnapshot,
    ) -> None:
        record["status"] = snapshot.status
        record["lifecycle_state"] = snapshot.lifecycle_state
        record["filled_size"] = snapshot.filled_size
        record["remaining_size"] = snapshot.remaining_size
        if snapshot.avg_fill_price > 0:
            record["avg_fill_price"] = snapshot.avg_fill_price
        if snapshot.cumulative_fee > 0:
            record["cumulative_fee"] = snapshot.cumulative_fee
        if snapshot.last_fill_at_ms:
            record["last_fill_at_ms"] = snapshot.last_fill_at_ms

    async def _apply_fill_delta(
        self,
        order_id: str,
        record: Dict[str, Any],
        snapshot: OrderFillSnapshot,
        fill_delta: float,
    ) -> None:
        """Notify callbacks of newly confirmed fill size (idempotent by delta)."""
        self._fire_order_callbacks(order_id, snapshot.status, record)
        logger.info(
            "OMS: order %s fill delta=%.6f total=%.6f (symbol=%s trade_id=%s)",
            order_id,
            fill_delta,
            snapshot.filled_size,
            record.get("symbol"),
            record.get("trade_id"),
        )

    async def _finalize_filled_order(
        self,
        order_id: str,
        record: Dict[str, Any],
        snapshot: OrderFillSnapshot,
    ) -> None:
        if record.get("terminal_handled"):
            return
        if self._kill_switch_active:
            logger.critical(
                "OMS: kill_switch active — refusing to reinsert filled order %s "
                "symbol=%s trade_id=%s",
                order_id, record.get("symbol"), record.get("trade_id"),
            )
            self._live_orders.pop(order_id, None)
            record["terminal_handled"] = True
            return
        record["terminal_handled"] = True
        self._fire_order_callbacks(order_id, ORDER_STATUS_FILLED, record)
        trade_id = int(record.get("trade_id", 0))
        symbol = str(record.get("symbol", ""))
        avg_px = safe_float(snapshot.avg_fill_price, safe_float(record.get("price")))
        fee = safe_float(snapshot.cumulative_fee)
        try:
            self._db.update_trade_order_tracking(
                trade_id,
                status="open",
                filled_size=snapshot.filled_size,
                size=snapshot.filled_size,
                avg_fill_price=avg_px,
                cumulative_order_fee=fee,
                entry_price=avg_px,
                entry_fee=fee,
                last_fill_at=snapshot.last_fill_at_ms,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("OMS finalize: DB update failed: %s", exc)

        async with self._lock:
            existing = self._open_trades.get(symbol)
            if existing is None:
                self._open_trades[symbol] = TradeResult(
                    trade_id=trade_id,
                    symbol=symbol,
                    side=str(record.get("side", "long")),
                    entry_price=avg_px,
                    exit_price=None,
                    size=snapshot.filled_size,
                    pnl_usd=0.0,
                    pnl_pct=0.0,
                    status="open",
                    reason="oms_filled",
                    timestamp_ms=int(record.get("submitted_at_ms", 0)),
                    entry_fee=fee,
                    exchange_order_id=order_id,
                )
            else:
                existing.size = snapshot.filled_size
                existing.entry_price = avg_px
                existing.entry_fee = fee
                existing.status = "open"

        self._live_orders.pop(order_id, None)
        logger.info(
            "OMS: order %s FILLED (symbol=%s trade_id=%s size=%.6f)",
            order_id, symbol, trade_id, snapshot.filled_size,
        )

    async def _handle_terminal_order(
        self,
        order_id: str,
        record: Dict[str, Any],
        *,
        reason: str,
        snapshot: OrderFillSnapshot,
    ) -> None:
        if record.get("terminal_handled"):
            return
        record["terminal_handled"] = True
        filled = safe_float(snapshot.filled_size)
        if filled > 0:
            await self._apply_fill_delta(order_id, record, snapshot, filled - safe_float(record.get("applied_fill_size")))
            record["applied_fill_size"] = filled
            await self._finalize_partial_exposure(order_id, record, snapshot, reason)
            return

        self._fire_order_callbacks(order_id, snapshot.status, record)
        logger.error(
            "OMS: order %s %s — rolling back trade_id=%s",
            order_id, reason.upper(), record.get("trade_id"),
        )
        await self._rollback_order(order_id, record, reason=reason)

    async def _finalize_partial_exposure(
        self,
        order_id: str,
        record: Dict[str, Any],
        snapshot: OrderFillSnapshot,
        reason: str,
    ) -> None:
        """Keep confirmed partial fill; drop residual tracking only."""
        if self._kill_switch_active:
            logger.critical(
                "OMS: kill_switch active — refusing to reinsert partial order %s "
                "symbol=%s trade_id=%s",
                order_id, record.get("symbol"), record.get("trade_id"),
            )
            self._live_orders.pop(order_id, None)
            return
        trade_id = int(record.get("trade_id", 0))
        symbol = str(record.get("symbol", ""))
        avg_px = safe_float(snapshot.avg_fill_price, safe_float(record.get("price")))
        fee = safe_float(snapshot.cumulative_fee)
        try:
            self._db.update_trade_order_tracking(
                trade_id,
                status="open",
                filled_size=snapshot.filled_size,
                size=snapshot.filled_size,
                avg_fill_price=avg_px,
                cumulative_order_fee=fee,
                entry_price=avg_px,
                entry_fee=fee,
                last_fill_at=snapshot.last_fill_at_ms,
                reason=f"oms_partial_{reason}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("OMS partial finalize: DB update failed: %s", exc)

        async with self._lock:
            self._open_trades[symbol] = TradeResult(
                trade_id=trade_id,
                symbol=symbol,
                side=str(record.get("side", "long")),
                entry_price=avg_px,
                exit_price=None,
                size=snapshot.filled_size,
                pnl_usd=0.0,
                pnl_pct=0.0,
                status="open",
                reason=f"oms_partial_{reason}",
                timestamp_ms=int(record.get("submitted_at_ms", 0)),
                entry_fee=fee,
                exchange_order_id=order_id,
            )

        self._emit_oms_alert("partial_residual", order_id, record)
        self._live_orders.pop(order_id, None)
        logger.warning(
            "OMS: order %s partial preserved size=%.6f after %s",
            order_id, snapshot.filled_size, reason,
        )

    async def _handle_open_timeout(self, order_id: str, record: Dict[str, Any], age_s: float) -> None:
        if record.get("terminal_handled"):
            return
        record["terminal_handled"] = True
        record["status"] = ORDER_STATUS_TIMEOUT
        self._fire_order_callbacks(order_id, ORDER_STATUS_TIMEOUT, record)
        self._emit_oms_alert("timeout", order_id, record)
        logger.error(
            "OMS: order %s TIMEOUT after %.1fs — cancelling and rolling back",
            order_id, age_s,
        )
        await self._cancel_timed_out_order(order_id)
        await self._rollback_order(order_id, record, reason=f"timeout_{age_s:.0f}s")

    async def _handle_partial_timeout(
        self,
        order_id: str,
        record: Dict[str, Any],
        age_s: float,
    ) -> None:
        if record.get("terminal_handled"):
            return
        record["terminal_handled"] = True
        record["status"] = ORDER_STATUS_TIMEOUT
        self._fire_order_callbacks(order_id, ORDER_STATUS_TIMEOUT, record)
        self._emit_oms_alert("timeout", order_id, record)
        logger.error(
            "OMS: partial order %s TIMEOUT after %.1fs — cancelling residual %.6f",
            order_id,
            age_s,
            safe_float(record.get("remaining_size")),
        )
        cancel_ok = await self._cancel_timed_out_order(order_id)
        snapshot = OrderFillSnapshot(
            status=ORDER_STATUS_PARTIAL,
            lifecycle_state=ORDER_PARTIAL,
            filled_size=safe_float(record.get("filled_size")),
            remaining_size=0.0,
            avg_fill_price=safe_float(record.get("avg_fill_price")),
            cumulative_fee=safe_float(record.get("cumulative_fee")),
            last_fill_at_ms=record.get("last_fill_at_ms"),
        )
        if not cancel_ok:
            self._emit_oms_alert("cancel_failed", order_id, record)
        await self._finalize_partial_exposure(
            order_id, record, snapshot, reason=f"timeout_{age_s:.0f}s",
        )

    def _persist_order_record(self, record: Dict[str, Any]) -> None:
        trade_id = int(record.get("trade_id", 0))
        if trade_id <= 0:
            return
        try:
            self._db.update_trade_order_tracking(
                trade_id,
                status=str(record.get("lifecycle_state", ORDER_PARTIAL)),
                filled_size=safe_float(record.get("filled_size")),
                avg_fill_price=safe_float(record.get("avg_fill_price")),
                cumulative_order_fee=safe_float(record.get("cumulative_fee")),
                last_fill_at=record.get("last_fill_at_ms"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("OMS persist failed: %s", exc)

    async def _cancel_timed_out_order(self, order_id: str) -> bool:
        """Best-effort cancel of a timed-out live order. Returns success flag."""
        record = self._live_orders.get(order_id)
        if not record or self._live_client is None:
            return False
        try:
            symbol = str(record.get("symbol", ""))
            await self._live_client.cancel_order(symbol, int(order_id))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "OMS: cancel failed for %s: %s (partial exposure preserved if any)",
                order_id, exc,
            )
            return False

    async def _rollback_order(
        self,
        order_id: str,
        record: Dict[str, Any],
        reason: str,
    ) -> None:
        """Apply the C9 rollback (cancel + DB status update)."""
        symbol = record.get("symbol", "")
        trade_id = record.get("trade_id", 0)
        try:
            portfolio = self._portfolio
            if portfolio is not None:
                await portfolio.cancel_position(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OMS rollback: cancel_position failed: %s", exc)
        try:
            async with self._lock:
                self._open_trades.pop(symbol, None)
                self._last_entry_ms.pop(symbol, None)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._db.update_trade_status(
                int(trade_id),
                status="cancelled",
                reason=f"oms_rollback:{reason}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("OMS rollback: update_trade_status failed: %s", exc)
        # Drop the tracking record so we don't keep polling a dead order
        self._live_orders.pop(order_id, None)

    def _emit_oms_alert(self, event: str, order_id: str, record: Dict[str, Any]) -> None:
        cb = self._oms_alert_callback
        if cb is None:
            return
        try:
            cb(event, order_id, record)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OMS alert callback raised: %s", exc)

    def _fire_order_callbacks(
        self,
        order_id: str,
        status: str,
        record: Dict[str, Any],
    ) -> None:
        for cb in self._order_status_callbacks:
            try:
                cb(order_id, status, record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("OMS callback raised: %s", exc)
