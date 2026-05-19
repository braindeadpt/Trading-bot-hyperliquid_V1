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
from typing import Any, Dict, Optional

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

        self._state: Dict[str, _ScalperState] = {}

    @property
    def name(self) -> str:
        return "OrderBookScalper"

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
        ratio = event.orderbook_bid_ask_ratio
        if ratio is None:
            logger.debug("OrderBookScalper SKIP %s — no bid_ask_ratio data", event.symbol)
            return None

        state = self._get_state(event.symbol)

        # Throttle signals per symbol
        if event.timestamp_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS:
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
