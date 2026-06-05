"""Trade execution engine — paper, testnet, and mainnet modes.

All modes share the same state-tracking and DB-persistence layer so that
switching between paper and live does not change the bookkeeping contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.data.database import Database, TradeEntry, TradeExit
from src.strategies.base import MarketEvent, Position, Signal
from src.utils.config import Config
from src.utils.helpers import safe_float, safe_divide, utc_now, utc_timestamp_ms

logger = logging.getLogger(__name__)


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

        # In-memory open trade index: symbol → TradeResult
        self._open_trades: Dict[str, TradeResult] = {}
        self._lock = asyncio.Lock()

        # Entry debounce: symbol → timestamp_ms (prevent rapid re-entry)
        self._last_entry_ms: Dict[str, int] = {}
        self._entry_debounce_ms: int = int(
            config.get("execution.entry_debounce_ms", 5_000)
        )  # 5s default

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
    ) -> TradeResult:
        """Open a new position.

        In **paper** mode the fill is simulated instantly at *signal.entry_price*.
        In **testnet** / **mainnet** mode an order is submitted via the REST
        client and the result is tracked.

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

        # Persist entry to DB
        sub_strategy = meta.get("original_strategy")
        if signal.strategy != "StrategyEnsemble":
            sub_strategy = signal.strategy

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
        )
        trade_id = self._db.save_trade_entry(entry_record)

        if self._mode in ("testnet", "mainnet"):
            await self._submit_live_order(signal, size, fill_price)

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

        # Update DB
        exit_record = TradeExit(
            trade_id=open_trade.trade_id,
            exit_price=fill_exit,
            exit_time=now_ms,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            status="closed",
        )
        self._db.update_trade_exit(exit_record)

        if self._mode in ("testnet", "mainnet"):
            await self._submit_live_close(position, fill_exit)

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
    ) -> None:
        """Submit a signed order to Hyperliquid (testnet or mainnet)."""
        if self._rest_client is None:
            logger.error("REST client not available in live mode")
            return

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
            return

        try:
            await self._live_client.place_entry(
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
