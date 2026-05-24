"""Strategy 6: OrderBook Imbalance Scalper

Scalps micro-inefficiencies in the orderbook:
  - bid_ask_ratio > 1.5 → excess bids → go LONG
  - bid_ask_ratio < 0.67 → excess asks → go SHORT

Designed for near-zero funding regimes where directional bias
is absent and market-making bots create micro-patterns.

Timeframe: tick-level (orderbook updates).
Max hold: 5 minutes (quick scalp or wrong).
Risk: 0.3% stop, 0.15% take-profit, 0.5% capital.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from src.core.tca import round_trip_cost_pct
from src.strategies.base import MarketEvent, Signal, ExitSignal, Position, Strategy

logger = logging.getLogger(__name__)


@dataclass
class _ScalperState:
    """Per-symbol state for scalper tracking."""
    last_signal_ms: int = 0
    entry_ratio: float = 0.0  # bid_ask_ratio at entry (for exit reference)


class OrderBookScalper(Strategy):
    """OrderBook Imbalance Scalper — fades orderbook micro-imbalances.

    Enters when bid/ask ratio deviates from 1.0 significantly,
    betting on mean reversion of the book.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        self.BID_ASK_LONG = cfg.get("bid_ask_ratio_long", 1.5)
        self.BID_ASK_SHORT = cfg.get("bid_ask_ratio_short", 0.67)
        self.TAKE_PROFIT_PCT = cfg.get("take_profit_pct", 0.0015)
        self.STOP_LOSS_PCT = cfg.get("stop_loss_pct", 0.003)
        self.BASE_SIZE_PCT = cfg.get("base_size_pct", 0.005)
        self.MAX_SIZE_PCT = cfg.get("max_size_pct", 0.01)
        self.MAX_HOLD_MS = cfg.get("max_hold_seconds", 300) * 1000
        self.MIN_CONFIDENCE = cfg.get("min_confidence", 0.55)
        self.SIGNAL_THROTTLE_MS = cfg.get("signal_throttle_ms", 60_000)

        self.MANUAL_ENABLED = bool(cfg.get("enabled", True))
        self.AUTO_ENABLE = bool(cfg.get("auto_enable", False))
        self.AUTO_ENABLE_TAKER_FEE = cfg.get("auto_enable_taker_fee_pct", 0.00035)
        self.AUTO_ENABLE_SLIPPAGE = cfg.get("auto_enable_slippage_pct", 0.0005)
        self.AUTO_ENABLE_MIN_EDGE_BUFFER = cfg.get("auto_enable_min_edge_buffer_pct", 0.0005)
        self.AUTO_ENABLE_USE_BOOK_SPREAD = bool(cfg.get("auto_enable_use_book_spread", True))
        self.AUTO_ENABLE_MAX_BOOK_SPREAD = cfg.get("auto_enable_max_book_spread_pct", 0.0004)
        self.AUTO_DISABLE_MAX_BOOK_SPREAD = cfg.get("auto_disable_max_book_spread_pct", 0.0008)
        self.AUTO_ENABLE_MIN_SYMBOLS_WITH_BOOK = int(
            cfg.get("auto_enable_min_symbols_with_book", 1)
        )
        self._viability_check_interval_ms = int(cfg.get("viability_check_interval_ms", 60_000))
        self._auto_active = self.MANUAL_ENABLED
        self._auto_activate_logged = False
        self._auto_deactivate_logged = False
        self._last_viability_check_ms: int = 0
        self._hold_until_ms: int = 0
        self._latest_book_spread: Dict[str, float] = {}

        self._state: Dict[str, _ScalperState] = {}

    @property
    def name(self) -> str:
        return "OrderBookScalper"

    def is_active(self) -> bool:
        """True when expected scalp edge exceeds round-trip costs on a tight book."""
        return self._auto_active

    def _effective_slippage_per_side(self, book_spread_pct: Optional[float]) -> float:
        """Estimate one-side slippage; prefer observed half-spread when tighter."""
        if not self.AUTO_ENABLE_USE_BOOK_SPREAD or book_spread_pct is None:
            return self.AUTO_ENABLE_SLIPPAGE
        spread_slip = book_spread_pct / 2.0
        if spread_slip <= 0:
            return self.AUTO_ENABLE_SLIPPAGE
        return min(self.AUTO_ENABLE_SLIPPAGE, spread_slip)

    def _edge_viability(
        self,
        book_spread_pct: Optional[float],
    ) -> Tuple[bool, float, float, float]:
        """Return (viable, edge, required, round_trip_cost)."""
        slip = self._effective_slippage_per_side(book_spread_pct)
        cost = round_trip_cost_pct(self.AUTO_ENABLE_TAKER_FEE, slip)
        required = cost + self.AUTO_ENABLE_MIN_EDGE_BUFFER
        edge = self.TAKE_PROFIT_PCT
        return edge > required, edge, required, cost

    def _best_book_spread(self) -> Optional[float]:
        if not self._latest_book_spread:
            return None
        return min(self._latest_book_spread.values())

    def _update_auto_activation(self, event: MarketEvent) -> None:
        """Enable when TP edge clears fees on a tight book; disable with hysteresis."""
        if self.MANUAL_ENABLED:
            self._auto_active = True
            return
        if not self.AUTO_ENABLE:
            self._auto_active = False
            return

        if event.orderbook_spread_pct is not None:
            self._latest_book_spread[event.symbol] = event.orderbook_spread_pct

        if event.timestamp_ms - self._last_viability_check_ms < self._viability_check_interval_ms:
            return
        self._last_viability_check_ms = event.timestamp_ms

        if len(self._latest_book_spread) < self.AUTO_ENABLE_MIN_SYMBOLS_WITH_BOOK:
            return

        best_spread = self._best_book_spread()
        if best_spread is None:
            return

        viable, edge, required, cost = self._edge_viability(best_spread)

        if not self._auto_active:
            if viable and best_spread <= self.AUTO_ENABLE_MAX_BOOK_SPREAD:
                self._auto_active = True
                if not self._auto_activate_logged:
                    self._auto_activate_logged = True
                    self._auto_deactivate_logged = False
                    logger.info(
                        "OrderBookScalper AUTO-ENABLED — edge %.4f%% > required %.4f%% "
                        "(RT cost %.4f%%, best book spread %.4f%%)",
                        edge * 100,
                        required * 100,
                        cost * 100,
                        best_spread * 100,
                    )
            return

        if event.timestamp_ms < self._hold_until_ms:
            return

        if not viable or best_spread > self.AUTO_DISABLE_MAX_BOOK_SPREAD:
            self._auto_active = False
            if not self._auto_deactivate_logged:
                self._auto_deactivate_logged = True
                self._auto_activate_logged = False
                reason = (
                    f"edge {edge * 100:.4f}% <= required {required * 100:.4f}%"
                    if not viable
                    else f"book spread {best_spread * 100:.4f}% > "
                    f"{self.AUTO_DISABLE_MAX_BOOK_SPREAD * 100:.4f}%"
                )
                logger.info(
                    "OrderBookScalper AUTO-DISABLED — %s (RT cost %.4f%%)",
                    reason,
                    cost * 100,
                )

    def _operational_gate(self, event: MarketEvent) -> bool:
        """Return False if strategy should stay dormant (monitoring only)."""
        self._update_auto_activation(event)
        return self._auto_active

    def _get_state(self, symbol: str) -> _ScalperState:
        if symbol not in self._state:
            self._state[symbol] = _ScalperState()
        return self._state[symbol]

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        """Check bid/ask ratio for scalping opportunity."""
        if event is None:
            return None

        if not self._operational_gate(event):
            return None

        ratio = event.orderbook_bid_ask_ratio
        if ratio is None:
            logger.debug("OrderBookScalper SKIP %s — no bid_ask_ratio data", event.symbol)
            return None

        state = self._get_state(event.symbol)

        # Throttle signals per symbol
        if (
            state.last_signal_ms > 0
            and event.timestamp_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS
        ):
            return None

        # Check for imbalance
        if ratio >= self.BID_ASK_LONG:
            target_side = "long"
            deviation = ratio - 1.0
        elif ratio <= self.BID_ASK_SHORT:
            target_side = "short"
            deviation = 1.0 - ratio
        else:
            logger.debug(
                "OrderBookScalper %s NO SIGNAL — ratio=%.3f (need > %.1f or < %.2f)",
                event.symbol, ratio, self.BID_ASK_LONG, self.BID_ASK_SHORT,
            )
            return None

        state.last_signal_ms = event.timestamp_ms
        state.entry_ratio = ratio
        self._hold_until_ms = max(
            self._hold_until_ms,
            event.timestamp_ms + self.MAX_HOLD_MS,
        )

        # Confidence scales with deviation magnitude
        confidence = min(self.MIN_CONFIDENCE + deviation * 0.2, 0.85)
        confidence = max(confidence, self.MIN_CONFIDENCE)

        # Scale size with confidence
        size_pct = min(self.BASE_SIZE_PCT * (1.0 + deviation), self.MAX_SIZE_PCT)

        logger.info(
            "OrderBookScalper %s signal for %s — ratio=%.3f, deviation=%.3f, "
            "confidence=%.2f, size=%.2f%%",
            target_side, event.symbol, ratio, deviation, confidence, size_pct * 100,
        )

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=target_side,
            confidence=confidence,
            size_pct=size_pct,
            entry_price=event.price,
            stop_loss_pct=self.STOP_LOSS_PCT,
            take_profit_pct=self.TAKE_PROFIT_PCT,
            reason=f"ob_scalp_{target_side}_ratio{ratio:.2f}",
            metadata={
                "bid_ask_ratio": ratio,
                "deviation": deviation,
                "stop_loss_pct": self.STOP_LOSS_PCT,
                "take_profit_pct": self.TAKE_PROFIT_PCT,
            },
        )

    # ------------------------------------------------------------------
    # Exit logic
    # ------------------------------------------------------------------

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Exit on time limit, take-profit, stop-loss, or book normalization."""
        hold_time = event.timestamp_ms - position.entry_time_ms

        # Max hold time
        if hold_time >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.8,
                reason=f"max_hold_{self.MAX_HOLD_MS//1000}s",
            )

        entry = position.entry_price
        current = event.price
        if entry <= 0 or current <= 0:
            return None

        if position.side == "long":
            pnl_pct = (current - entry) / entry
        else:
            pnl_pct = (entry - current) / entry

        # Take-profit
        if pnl_pct >= self.TAKE_PROFIT_PCT:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.85,
                reason=f"take_profit_{pnl_pct*100:.2f}%",
            )

        # Stop-loss
        if pnl_pct <= -self.STOP_LOSS_PCT:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.95,
                reason=f"stop_loss_{pnl_pct*100:.2f}%",
            )

        # Book normalization — exit if bid_ask_ratio returns to ~1.0
        ratio = event.orderbook_bid_ask_ratio
        if ratio is not None and 0.9 < ratio < 1.1:
            state = self._get_state(position.symbol)
            if abs(ratio - 1.0) < abs(state.entry_ratio - 1.0) * 0.5:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.7,
                    reason=f"book_normalized_ratio{ratio:.2f}",
                )

        return None
