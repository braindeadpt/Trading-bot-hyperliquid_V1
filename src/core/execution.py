"""Trade execution engine — paper, testnet, and mainnet modes.

All modes share the same state-tracking and DB-persistence layer so that
switching between paper and live does not change the bookkeeping contract.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.data.database import Database, TradeEntry, TradeExit
from src.strategies.base import Position, Signal
from src.utils.config import Config
from src.utils.helpers import safe_float, safe_divide, utc_now, utc_timestamp_ms

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    """Result of an entry or exit operation."""

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

        # Mainnet safety gate
        self._mainnet_enabled: bool = bool(config.get("exchange.mainnet_enabled", False))
        if mode == "mainnet" and not self._mainnet_enabled:
            raise RuntimeError(
                "Mainnet mode requested but 'exchange.mainnet_enabled' is False in config. "
                "Set it to True explicitly to trade real funds."
            )

        # REST client (lazy-initialised for testnet / mainnet)
        self._rest_client: Optional[Any] = None

        # In-memory open trade index: symbol → TradeResult
        self._open_trades: Dict[str, TradeResult] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Prepare the execution engine (open REST session if needed)."""
        if self._mode in ("testnet", "mainnet"):
            from src.exchanges.hyperliquid_rest import HyperliquidRESTClient

            use_testnet = self._mode == "testnet"
            self._rest_client = HyperliquidRESTClient(use_testnet=use_testnet)
            await self._rest_client.open()
            logger.info("ExecutionEngine REST client opened (mode=%s)", self._mode)

    async def close(self) -> None:
        """Gracefully close any open REST session."""
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
    ) -> TradeResult:
        """Open a new position.

        In **paper** mode the fill is simulated instantly at *signal.entry_price*.
        In **testnet** / **mainnet** mode an order is submitted via the REST
        client and the result is tracked.

        Returns a :class:`TradeResult` in all cases.
        """
        now_ms = utc_timestamp_ms()
        price = safe_float(signal.entry_price)
        if price <= 0.0:
            logger.error("enter_position: invalid price %.4f for %s", price, signal.symbol)
            raise ValueError(f"Invalid entry price for {signal.symbol}: {price}")

        size = safe_float(signal.metadata.get("calculated_size", 0.0))
        if size <= 0.0:
            logger.error("enter_position: zero size for %s", signal.symbol)
            raise ValueError(f"Calculated position size is zero for {signal.symbol}")

        # Persist entry to DB
        entry_record = TradeEntry(
            symbol=signal.symbol,
            side=signal.side,
            entry_price=price,
            entry_time=now_ms,
            size=size,
            strategy=signal.strategy,
            status="open",
        )
        trade_id = self._db.save_trade_entry(entry_record)

        if self._mode in ("testnet", "mainnet"):
            await self._submit_live_order(signal, size, price)

        result = TradeResult(
            trade_id=trade_id,
            symbol=signal.symbol,
            side=signal.side,
            entry_price=price,
            exit_price=None,
            size=size,
            pnl_usd=0.0,
            pnl_pct=0.0,
            status="open",
            reason=signal.reason,
            timestamp_ms=now_ms,
        )

        async with self._lock:
            self._open_trades[signal.symbol] = result

        logger.info(
            "ENTER  mode=%s id=%d %s %s size=%.6f @ %.4f (%s)",
            self._mode,
            trade_id,
            signal.symbol,
            signal.side,
            size,
            price,
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
        exit_price_f = safe_float(exit_price)
        if exit_price_f <= 0.0:
            logger.error("close_position: invalid exit price %.4f for %s", exit_price_f, position.symbol)
            raise ValueError(f"Invalid exit price for {position.symbol}: {exit_price_f}")

        async with self._lock:
            open_trade = self._open_trades.pop(position.symbol, None)

        if open_trade is None:
            logger.warning("close_position: no open trade found for %s", position.symbol)
            # Build a synthetic result so callers don't crash
            pnl_usd = self._compute_pnl(position, exit_price_f)
            pnl_pct = safe_divide(pnl_usd, position.entry_price * position.size, 0.0)
            return TradeResult(
                trade_id=-1,
                symbol=position.symbol,
                side=position.side,
                entry_price=position.entry_price,
                exit_price=exit_price_f,
                size=position.size,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                status="closed",
                reason=reason,
                timestamp_ms=now_ms,
            )

        pnl_usd = self._compute_pnl(position, exit_price_f)
        notional = position.entry_price * position.size
        pnl_pct = safe_divide(pnl_usd, notional, 0.0)

        # Update DB
        exit_record = TradeExit(
            trade_id=open_trade.trade_id,
            exit_price=exit_price_f,
            exit_time=now_ms,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            status="closed",
        )
        self._db.update_trade_exit(exit_record)

        if self._mode in ("testnet", "mainnet"):
            await self._submit_live_close(position, exit_price_f)

        result = TradeResult(
            trade_id=open_trade.trade_id,
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=exit_price_f,
            size=position.size,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            status="closed",
            reason=reason,
            timestamp_ms=now_ms,
        )

        logger.info(
            "EXIT   mode=%s id=%d %s %s pnl=%.2f (%.2f%%) reason=%s",
            self._mode,
            result.trade_id,
            position.symbol,
            position.side,
            pnl_usd,
            pnl_pct * 100.0,
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
    ) -> None:
        """Submit an order to Hyperliquid (testnet or mainnet).

        This is a thin wrapper around the REST client.  Full signing and
        order construction are delegated to the exchange layer because
        Hyperliquid requires EIP-712 signatures.

        For now we log the intent; the signing infrastructure can be
        plugged in here without changing the ExecutionEngine contract.
        """
        if self._rest_client is None:
            logger.error("REST client not available in live mode")
            return

        logger.info(
            "LIVE ORDER (submit) %s %s size=%.6f @ %.4f",
            signal.symbol,
            signal.side,
            size,
            price,
        )
        # TODO: integrate vault.py signing + nonce management

    async def _submit_live_close(
        self,
        position: Position,
        exit_price: float,
    ) -> None:
        """Submit a closing order to Hyperliquid."""
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
        # TODO: integrate vault.py signing + nonce management

    # ------------------------------------------------------------------
    # State recovery
    # ------------------------------------------------------------------

    async def load_open_trades(self) -> None:
        """Load open trades from the DB into the in-memory index.

        Called once by the engine during startup so that positions opened
        in a previous session are tracked correctly.
        """
        rows = self._db.get_open_trades()
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
                    reason="restored_from_db",
                    timestamp_ms=safe_float(row["entry_time"]),
                )
                self._open_trades[symbol] = result
        logger.info("Loaded %d open trades from DB", len(rows))
