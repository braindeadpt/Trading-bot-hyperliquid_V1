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
            config.get("risk.taker_fee_pct", 0.035)
        ) / 100.0

        # Slippage model for paper trading
        self._paper_slippage_pct: float = safe_float(
            config.get("risk.paper_slippage_pct", 0.05)
        ) / 100.0

        maker_cfg = config.get("execution.maker_orders", {}) or {}
        self._maker_orders_enabled = bool(maker_cfg.get("enabled", False))
        self._maker_fee_pct: float = safe_float(
            maker_cfg.get("maker_fee_pct", 0.01)
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Prepare the execution engine (open REST session if needed)."""
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
        """Gracefully close any open REST session."""
        self._live_client = None
        self._live_signing_ready = False
        if self._rest_client is not None:
            await self._rest_client.close()
            self._rest_client = None
            logger.info("ExecutionEngine REST client closed")

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

        raw_price = safe_float(signal.entry_price)
        if raw_price <= 0.0:
            logger.error("enter_position: invalid price %.4f for %s", raw_price, signal.symbol)
            raise ValueError(f"Invalid entry price for {signal.symbol}: {raw_price}")

        meta = signal.metadata or {}
        order_type = str(meta.get("order_type", "market"))
        entry_fee_pct = safe_float(meta.get("entry_fee_pct"), self._taker_fee_pct)

        size = safe_float(signal.metadata.get("calculated_size", 0.0))
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

        # CRIT-003 FIX: Clamp position size to hard limits
        # Max position size = 20% of capital, max leverage consideration
        max_position_size_pct = 0.20  # 20% of capital
        capital = await portfolio.current_capital
        max_size_by_capital = (capital * max_position_size_pct) / fill_price if fill_price > 0 else 0.0
        
        # Also enforce max notional limit
        max_notional = capital * max_position_size_pct
        current_notional = fill_price * size
        
        if current_notional > max_notional:
            old_size = size
            size = max_notional / fill_price if fill_price > 0 else size
            logger.warning(
                "CRIT-003: POSITION SIZE CLAMPED for %s — %.6f → %.6f "
                "(notional $%.2f > max $%.2f, %.1f%% of capital)",
                signal.symbol, old_size, size, current_notional, max_notional,
                (current_notional / capital * 100) if capital > 0 else 0,
            )

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

        # Encode signal metadata for the journal
        try:
            signal_meta_json = json.dumps(signal.metadata, default=str)
        except (TypeError, ValueError):
            signal_meta_json = None

        entry_record = TradeEntry(
            symbol=signal.symbol,
            side=signal.side,
            entry_price=fill_price,
            entry_time=now_ms,
            size=size,
            strategy=signal.strategy,
            sub_strategy=str(sub_strategy) if sub_strategy else None,
            status="open",
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

        # v3.1.22: HL order id (only set in live mode after _submit_live_order)
        exchange_order_id: Optional[str] = None

        if self._mode in ("testnet", "mainnet"):
            # v3.1.17 C9: on live order failure, roll back the phantom
            # position we just wrote to the DB and portfolio so we are
            # not left holding inventory that the exchange never saw.
            try:
                hl_response = await self._submit_live_order(
                    signal, size, fill_price
                )
                # v3.1.22: capture the HL order id for OMS tracking.
                # HL responses come back as either
                # {"status": "ok", "response": {"data": {"statuses": [...]}}}
                # or {"raw": ...}. Try the common locations.
                exchange_order_id = self._extract_order_id(hl_response)
                if exchange_order_id:
                    self._live_orders[exchange_order_id] = {
                        "symbol": signal.symbol,
                        "side": signal.side,
                        "size": float(size),
                        "price": float(fill_price),
                        "filled_size": 0.0,
                        "status": ORDER_STATUS_OPEN,
                        "timestamp": time.time(),
                        "trade_id": int(trade_id),
                        "order_type": order_type,
                    }
                    logger.info(
                        "OMS: tracking order %s (symbol=%s trade_id=%d)",
                        exchange_order_id, signal.symbol, trade_id,
                    )
            except Exception as exc:
                logger.error(
                    "Live order failed for %s — rolling back trade_id=%d: %s",
                    signal.symbol, trade_id, exc,
                    exc_info=True,
                )
                try:
                    await self._portfolio.cancel_position(signal.symbol)
                except Exception as inner_exc:  # noqa: BLE001
                    logger.exception("cancel_position rollback failed: %s", inner_exc)
                async with self._lock:
                    self._open_trades.pop(signal.symbol, None)
                    self._last_entry_ms.pop(signal.symbol, None)
                try:
                    self._db.update_trade_status(
                        trade_id,
                        status="cancelled",
                        reason=f"live_order_failed: {str(exc)[:100]}",
                    )
                except Exception as inner_exc:  # noqa: BLE001
                    logger.exception("update_trade_status rollback failed: %s", inner_exc)
                raise

        result = TradeResult(
            trade_id=trade_id,
            symbol=signal.symbol,
            side=signal.side,
            entry_price=fill_price,
            exit_price=None,
            size=size,
            pnl_usd=0.0,
            pnl_pct=0.0,
            status="open",
            reason=signal.reason,
            timestamp_ms=now_ms,
            entry_fee=entry_fee,
            exchange_order_id=exchange_order_id,
        )

        async with self._lock:
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

        # Update DB
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

        if self._mode in ("testnet", "mainnet"):
            # v3.1.17 C9: if the live close fails, revert the DB status
            # back to 'open' so reconciliation can re-attempt. Also
            # re-insert the open_trades entry we popped above.
            try:
                await self._submit_live_close(position, fill_exit)
            except Exception as exc:
                logger.error(
                    "Live close failed for %s — rolling back trade_id=%d: %s",
                    position.symbol, open_trade.trade_id, exc,
                    exc_info=True,
                )
                try:
                    self._db.update_trade_status(
                        open_trade.trade_id,
                        status="open",
                        reason=f"live_close_failed: {str(exc)[:100]}",
                    )
                except Exception as inner_exc:  # noqa: BLE001
                    logger.exception("update_trade_status rollback failed: %s", inner_exc)
                # Re-insert the open trade so the in-memory index matches DB.
                async with self._lock:
                    self._open_trades[position.symbol] = open_trade
                raise

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

        CRIT-001 FIX: Validate that the price symbol matches the trade symbol
        to prevent cross-symbol price corruption.
        """
        async with self._lock:
            for symbol, price in prices.items():
                price_f = safe_float(price)
                if price_f <= 0.0:
                    logger.warning("update_position_prices: invalid price %.4f for %s", price_f, symbol)
                    continue
                trade = self._open_trades.get(symbol)
                if trade is not None:
                    # CRIT-001: Validate symbol match before updating
                    if trade.symbol != symbol:
                        logger.critical(
                            "CRIT-001: SYMBOL MISMATCH — trade.symbol=%s vs price.symbol=%s. "
                            "Rejecting price update to prevent cross-contamination.",
                            trade.symbol, symbol,
                        )
                        continue
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

        v3.1.22: returns the raw HL response dict so the caller can
        extract the order id for OMS tracking. When signing is not
        configured we return an empty dict (callers treat missing id
        as "best-effort, no tracking").
        """
        if self._rest_client is None:
            logger.error("REST client not available in live mode")
            return {}

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

        if not self._live_signing_ready or self._live_client is None:
            logger.warning(
                "Skipping live order submission for %s — signing not configured",
                signal.symbol,
            )
            return {}

        try:
            return await self._live_client.place_entry(
                signal.symbol,
                signal.side,
                size,
                order_type=order_type,
                limit_price=limit_price,
                post_only=post_only,
            )
        except Exception as exc:
            logger.error(
                "Live order failed %s %s: %s",
                signal.symbol,
                signal.side,
                exc,
                exc_info=True,
            )
            raise

    @staticmethod
    def _extract_order_id(hl_response: Optional[Dict[str, Any]]) -> Optional[str]:
        """Pull the HL order id out of a (potentially nested) response.

        Hyperliquid responses come back as:
          {"status": "ok", "response": {"data": {"statuses": [
              {"filled": {"oid": 123, ...}},
              {"resting": {"oid": 456, ...}}
          ]}}}
        or the exchange may return the order id under "oid" directly.
        """
        if not isinstance(hl_response, dict):
            return None
        # Direct key
        for key in ("oid", "orderId", "order_id", "id"):
            if key in hl_response and hl_response[key] is not None:
                return str(hl_response[key])
        # Nested under response.data.statuses[*]
        try:
            statuses = (
                hl_response.get("response", {})
                .get("data", {})
                .get("statuses", [])
            )
            if statuses:
                first = statuses[0]
                if isinstance(first, dict):
                    for sub in first.values():
                        if isinstance(sub, dict) and "oid" in sub:
                            return str(sub["oid"])
        except (AttributeError, TypeError):
            pass
        return None

    async def _submit_live_close(
        self,
        position: Position,
        exit_price: float,
    ) -> None:
        """Submit a signed closing order to Hyperliquid."""
        if self._rest_client is None:
            logger.error("REST client not available in live mode")
            return

        close_side = "short" if position.side == "long" else "long"
        logger.info(
            "LIVE CLOSE (submit) %s %s size=%.6f @ %.4f",
            position.symbol,
            close_side,
            position.size,
            exit_price,
        )

        if not self._live_signing_ready or self._live_client is None:
            logger.warning(
                "Skipping live close for %s — signing not configured",
                position.symbol,
            )
            return

        try:
            await self._live_client.close_position(position.symbol, position.size)
        except Exception as exc:
            logger.error(
                "Live close failed %s: %s",
                position.symbol,
                exc,
                exc_info=True,
            )

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

    # ------------------------------------------------------------------
    # v3.1.22: OMS — live order tracking + background poller
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

    async def check_order_status(self, order_id: str) -> str:
        """Query Hyperliquid REST for the current status of *order_id*.

        Returns one of the ``ORDER_STATUS_*`` constants. Returns
        ``ORDER_STATUS_OPEN`` if the order id is unknown to HL (e.g.
        the bot was restarted and lost the in-memory record); the
        OMS poller treats that as a soft signal to keep watching.
        """
        if self._rest_client is None:
            return ORDER_STATUS_OPEN
        try:
            resp = await self._rest_client.get_order_status(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "check_order_status: REST error for %s: %s",
                order_id, exc,
            )
            return self._live_orders.get(order_id, {}).get(
                "status", ORDER_STATUS_OPEN
            )

        if not isinstance(resp, dict):
            return ORDER_STATUS_OPEN

        # HL returns either:
        #   {"order": {"status": "filled", ...}}
        #   {"statuses": ["open"]}
        order = resp.get("order")
        if isinstance(order, dict):
            raw = str(order.get("status", "open")).lower()
            return self._normalise_hl_status(raw)
        raw = str(resp.get("status", "open")).lower()
        return self._normalise_hl_status(raw)

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
        """Check one order; update its status; take action if terminal."""
        status = await self.check_order_status(order_id)
        now_ts = time.time()
        age_s = now_ts - safe_float(record.get("timestamp", now_ts), now_ts)

        previous = record.get("status", ORDER_STATUS_OPEN)
        if status == previous and status not in (ORDER_STATUS_OPEN, ORDER_STATUS_PARTIAL):
            return

        if status in (ORDER_STATUS_FILLED, ORDER_STATUS_PARTIAL, ORDER_STATUS_CANCELLED, ORDER_STATUS_REJECTED):
            record["status"] = status
            self._fire_order_callbacks(order_id, status, record)
            if status == ORDER_STATUS_REJECTED:
                logger.error(
                    "OMS: order %s REJECTED — rolling back trade_id=%s",
                    order_id, record.get("trade_id"),
                )
                await self._rollback_order(order_id, record, reason="rejected")
            elif status == ORDER_STATUS_CANCELLED:
                logger.warning(
                    "OMS: order %s CANCELLED — rolling back trade_id=%s",
                    order_id, record.get("trade_id"),
                )
                await self._rollback_order(order_id, record, reason="cancelled")
            elif status == ORDER_STATUS_FILLED:
                logger.info(
                    "OMS: order %s FILLED (symbol=%s trade_id=%s age=%.1fs)",
                    order_id, record.get("symbol"), record.get("trade_id"), age_s,
                )
            return

        # Status still open/partial: check timeout
        if age_s > self._live_order_timeout_s:
            logger.error(
                "OMS: order %s TIMEOUT after %.1fs — cancelling and rolling back",
                order_id, age_s,
            )
            record["status"] = ORDER_STATUS_TIMEOUT
            self._fire_order_callbacks(order_id, ORDER_STATUS_TIMEOUT, record)
            await self._cancel_timed_out_order(order_id)
            await self._rollback_order(
                order_id, record, reason=f"timeout_{age_s:.0f}s"
            )

    async def _cancel_timed_out_order(self, order_id: str) -> None:
        """Best-effort cancel of a timed-out live order."""
        record = self._live_orders.get(order_id)
        if not record or self._rest_client is None:
            return
        try:
            symbol = record.get("symbol", "")
            await self._rest_client.cancel_order(symbol, order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "OMS: cancel failed for %s: %s (will rely on auto-cancel)",
                order_id, exc,
            )

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
            portfolio = getattr(self, "_portfolio", None)
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
