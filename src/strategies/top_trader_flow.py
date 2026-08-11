"""TopTraderFlow — shadow strategy on aggregated HL top-wallet positioning.

Research / Phase08 shadow only. Does **not** copy individual wallets.
Signals when tracked top-N wallets show strong net long/short bias on a coin.

Requires ``TopTraderTracker`` background poller to populate snapshots.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.exchanges.top_trader_tracker import get_top_trader_snapshot
from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)


class TopTraderFlow(Strategy):
    """Aggregate top-trader bias → directional shadow signal."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", True))
        self.MODE = str(cfg.get("mode", "aggregate")).lower()  # aggregate | single
        self.BIAS_THRESHOLD = float(cfg.get("bias_threshold", 0.55))
        self.MIN_WALLETS = int(cfg.get("min_wallets_with_position", 3))
        self.MIN_NOTIONAL_USD = float(cfg.get("min_aggregate_notional_usd", 50_000.0))
        self.SIZE_PCT = float(cfg.get("size_pct", 0.01))
        self.STOP_LOSS_PCT = float(cfg.get("stop_loss_pct", 0.04))
        self.TAKE_PROFIT_PCT = float(cfg.get("take_profit_pct", 0.10))
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 120.0))
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 300_000))
        self.MAX_SNAPSHOT_AGE_MS = int(cfg.get("max_snapshot_age_ms", 180_000))
        self._last_signal_ms: Dict[str, int] = {}

    @property
    def name(self) -> str:
        return "TopTraderFlow"

    def is_active(self) -> bool:
        return self._enabled

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        if not self._enabled:
            return None
        if self.MODE not in ("aggregate", "single"):
            return None

        snap = get_top_trader_snapshot(event.symbol)
        if snap is None:
            return None
        age = int(event.timestamp_ms) - int(snap.updated_ms)
        if age < 0:
            age = 0
        if age > self.MAX_SNAPSHOT_AGE_MS:
            return None

        total_notional = snap.long_notional_usd + snap.short_notional_usd
        if total_notional < self.MIN_NOTIONAL_USD:
            return None
        if snap.n_wallets < self.MIN_WALLETS and self.MODE == "aggregate":
            return None

        bias = float(snap.net_bias)
        if abs(bias) < self.BIAS_THRESHOLD:
            return None

        side = "long" if bias > 0 else "short"
        last = self._last_signal_ms.get(event.symbol, 0)
        if event.timestamp_ms - last < self.SIGNAL_THROTTLE_MS:
            return None
        self._last_signal_ms[event.symbol] = int(event.timestamp_ms)

        conf = min(0.95, 0.45 + abs(bias) * 0.5)
        reason = (
            f"top_trader_{self.MODE} bias={bias:.2f} "
            f"long={snap.n_long} short={snap.n_short} "
            f"notional={total_notional:.0f}"
        )
        logger.info(
            "TopTraderFlow %s %s — %s",
            side,
            event.symbol,
            reason,
        )
        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=side,
            confidence=conf,
            size_pct=self.SIZE_PCT,
            stop_loss_pct=self.STOP_LOSS_PCT,
            take_profit_pct=self.TAKE_PROFIT_PCT,
            reason=reason,
            metadata={
                "net_bias": bias,
                "long_frac": snap.long_frac,
                "n_long": snap.n_long,
                "n_short": snap.n_short,
                "notional_usd": total_notional,
                "mode": self.MODE,
                "tracker_updated_ms": snap.updated_ms,
                "max_hold_hours": self.MAX_HOLD_HOURS,
                "exit_style": "hybrid_flip_or_hold",
            },
        )

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Exit when aggregate bias flips against the position."""
        snap = get_top_trader_snapshot(event.symbol)
        if snap is None:
            return None
        bias = float(snap.net_bias)
        # Flip: long position but strong short bias (or mirror)
        if position.side == "long" and bias <= -self.BIAS_THRESHOLD:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.7,
                reason=f"top_trader_bias_flip bias={bias:.2f}",
            )
        if position.side == "short" and bias >= self.BIAS_THRESHOLD:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.7,
                reason=f"top_trader_bias_flip bias={bias:.2f}",
            )
        return None
