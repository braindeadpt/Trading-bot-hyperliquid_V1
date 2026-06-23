"""Strategy: FundingMomentum — follow funding rate flips with rising OI.

Edge
----
:class:`MeanReversion` (FundingExtreme) fades extreme funding on the
assumption that crowded positioning will revert. The opposite view is
that *flips* in funding — particularly flips with rising OI — signal
new positions entering with momentum, not positioning that will
revert.

Entry (LONG)
------------
* Previous funding was negative (< -funding_flip_threshold)
* Current funding is now positive (> +funding_flip_threshold)
* OI is increasing over the last ``oi_lookback_hours`` bars
  (``oi_delta > 0``) — new longs are entering
* Price is above EMA50 (trend alignment)
* ADX > min_adx (momentum building, not a chop flip)

Entry (SHORT) — mirror
-----------------------
* Previous funding positive → current negative
* OI decreasing (new shorts entering)
* Price below EMA50
* ADX > min_adx

Exit
----
* Funding reverses sign again (flip back)
* Max hold ``max_hold_hours`` (12h default)
* Stop: 2×ATR from entry
* TP: 3×ATR from entry (1.5R)
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.strategies.indicators import (
    Candle,
    calculate_atr,
    calculate_ema,
)
from src.utils.helpers import safe_divide, safe_float

logger = logging.getLogger(__name__)


@dataclass
class _FundingMomentumState:
    """Per-symbol funding + OI history for flip detection."""
    funding_history: Deque[Tuple[int, float]] = field(
        default_factory=lambda: deque(maxlen=64),
    )
    oi_history: Deque[Tuple[int, float]] = field(
        default_factory=lambda: deque(maxlen=32),
    )
    candles_15m: Deque[Candle] = field(default_factory=lambda: deque(maxlen=80))
    candles_1h: Deque[Candle] = field(default_factory=lambda: deque(maxlen=80))
    last_signal_ms: int = 0


class FundingMomentum(Strategy):
    """Follow funding rate flips (positive OI divergence from FundingExtreme)."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # Funding flip thresholds
        self.FUNDING_FLIP_THRESHOLD = float(cfg.get("funding_flip_threshold", 0.0001))
        # OI filter
        self.OI_LOOKBACK_HOURS = int(cfg.get("oi_lookback_hours", 4))
        # Trend filter
        self.EMA_SLOW = int(cfg.get("ema_slow", 50))
        self.MIN_ADX = float(cfg.get("min_adx", 20.0))
        # Risk
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 12.0))
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        self.STOP_ATR_MULT = float(cfg.get("stop_atr_mult", 2.0))
        self.TP_ATR_MULT = float(cfg.get("tp_atr_mult", 3.0))
        self.ATR_PERIOD = int(cfg.get("atr_period", 14))
        # Sizing
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.015))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.025))
        # Confidence
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.50))
        # Throttle
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 30 * 60_000))

        self.MANUAL_ENABLED = bool(cfg.get("enabled", True))
        self._state: Dict[str, _FundingMomentumState] = {}

    @property
    def name(self) -> str:
        return "FundingMomentum"

    def is_active(self) -> bool:
        return self.MANUAL_ENABLED

    def _get_state(self, symbol: str) -> _FundingMomentumState:
        if symbol not in self._state:
            self._state[symbol] = _FundingMomentumState()
        return self._state[symbol]

    @staticmethod
    def _update_history(
        state: _FundingMomentumState,
        candle_15m: Optional[Candle],
        candle_1h: Optional[Candle],
        ts_ms: int,
        funding: Optional[float],
        oi_delta: Optional[float],
    ) -> None:
        if candle_15m is not None:
            if not state.candles_15m or state.candles_15m[-1].timestamp_ms != candle_15m.timestamp_ms:
                state.candles_15m.append(candle_15m)
        if candle_1h is not None:
            if not state.candles_1h or state.candles_1h[-1].timestamp_ms != candle_1h.timestamp_ms:
                state.candles_1h.append(candle_1h)
        if funding is not None:
            if not state.funding_history or state.funding_history[-1][0] != ts_ms:
                state.funding_history.append((ts_ms, float(funding)))
        if oi_delta is not None:
            if not state.oi_history or state.oi_history[-1][0] != ts_ms:
                state.oi_history.append((ts_ms, float(oi_delta)))

    @staticmethod
    def _previous_flip(
        funding_history: Deque[Tuple[int, float]],
        threshold: float,
    ) -> Optional[str]:
        """Return the sign of the previous funding sample.

        ``"long"`` (positive), ``"short"`` (negative), or ``None`` if
        there is no previous sample distinct from the current one.
        """
        if len(funding_history) < 2:
            return None
        prev = funding_history[-2][1]
        if prev > threshold:
            return "long"
        if prev < -threshold:
            return "short"
        return "flat"

    def _oi_direction(
        self,
        oi_history: Deque[Tuple[int, float]],
        lookback_hours: int,
    ) -> Optional[str]:
        """Determine the OI direction over the last ``lookback_hours`` samples.

        We assume 1 OI delta sample per hour (the engine emits
        ``event.oi_delta`` per tick). The function uses the *last*
        ``lookback_hours`` samples (not a sum) and returns ``"up"`` if
        the most recent sample is positive, ``"down"`` if negative.
        ``None`` is returned if there is insufficient history.

        Returns ``"up"``, ``"down"`` or ``None`` (insufficient data).
        """
        if len(oi_history) < 2:
            return None
        recent = list(oi_history)[-lookback_hours:]
        if not recent:
            return None
        # Use the *last* sample as the direction signal — it represents
        # the most recent OI delta which is the freshest evidence of
        # whether new positions are being added or removed.
        last = recent[-1][1]
        if last > 0:
            return "up"
        if last < 0:
            return "down"
        return None

    def _confidence(self, adx: float, funding_magnitude: float) -> float:
        # ADX score: 0 at min_adx, 1 at min_adx + 20
        adx_score = max(0.0, min(1.0, (adx - self.MIN_ADX) / 20.0))
        # Funding magnitude score: more extreme = stronger signal
        f_score = max(
            0.0,
            min(1.0, funding_magnitude / (self.FUNDING_FLIP_THRESHOLD * 10.0)),
        )
        raw = (adx_score + f_score) / 2.0
        return min(max(self.MIN_CONFIDENCE + 0.30 * raw, self.MIN_CONFIDENCE), 0.85)

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        if not self.MANUAL_ENABLED:
            return None

        state = self._get_state(event.symbol)
        funding = event.predicted_funding
        if funding is None:
            funding = event.funding
        if funding is None:
            funding = event.predicted_funding_avg
        if funding is None:
            funding = event.funding_avg

        self._update_history(
            state, event.candle_15m, event.candle_1h,
            event.timestamp_ms, funding, event.oi_delta,
        )

        now_ms = event.timestamp_ms
        if (
            state.last_signal_ms > 0
            and now_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS
        ):
            return None

        if funding is None:
            return None

        # Need at least two funding samples to detect a flip
        prev_side = self._previous_flip(
            state.funding_history, self.FUNDING_FLIP_THRESHOLD,
        )
        if prev_side is None or prev_side == "flat":
            return None

        # Current funding must be in the opposite sign of previous
        if funding > self.FUNDING_FLIP_THRESHOLD:
            current_side = "long"
        elif funding < -self.FUNDING_FLIP_THRESHOLD:
            current_side = "short"
        else:
            return None
        if current_side == prev_side:
            return None

        # OI direction
        oi_dir = self._oi_direction(state.oi_history, self.OI_LOOKBACK_HOURS)
        if oi_dir is None or oi_dir == "flat":
            return None
        # For long signal we want OI up; for short we want OI down
        if current_side == "long" and oi_dir != "up":
            return None
        if current_side == "short" and oi_dir != "down":
            return None

        # Need enough 1h candles for EMA50 + ATR
        if len(state.candles_1h) < self.EMA_SLOW + 1 \
                or len(state.candles_1h) < self.ATR_PERIOD + 1:
            return None

        closes_1h = [c.close for c in state.candles_1h]
        ema_slow = calculate_ema(closes_1h, self.EMA_SLOW)
        atr = calculate_atr(state.candles_1h, period=self.ATR_PERIOD)
        if ema_slow is None or atr is None or atr <= 0 or event.price <= 0:
            return None

        # Trend alignment: long needs price > ema50, short needs price < ema50
        if current_side == "long" and event.price <= ema_slow:
            return None
        if current_side == "short" and event.price >= ema_slow:
            return None

        # ADX
        adx = safe_float(event.adx_14, 0.0)
        if adx < self.MIN_ADX:
            return None

        # Stop and TP
        atr_pct = safe_divide(atr, event.price, 0.0)
        stop_loss_pct = atr_pct * self.STOP_ATR_MULT
        take_profit_pct = atr_pct * self.TP_ATR_MULT

        confidence = self._confidence(adx, abs(funding))
        if confidence < self.MIN_CONFIDENCE:
            return None

        size_pct = min(self.BASE_SIZE_PCT, self.MAX_SIZE_PCT)
        state.last_signal_ms = now_ms

        logger.info(
            "FundingMomentum %s signal %s — prev=%s cur=%.5f oi=%s adx=%.1f "
            "R=%.2f conf=%.2f",
            event.symbol, current_side, prev_side, funding, oi_dir, adx,
            self.TP_ATR_MULT / max(self.STOP_ATR_MULT, 1e-9), confidence,
        )

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=current_side,
            confidence=confidence,
            size_pct=size_pct,
            entry_price=event.price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            reason=f"funding_momentum_{current_side}_from_{prev_side}_f{funding:.5f}",
            metadata={
                "prev_funding_side": prev_side,
                "current_funding": funding,
                "oi_direction": oi_dir,
                "adx": adx,
                "ema_slow": ema_slow,
                "atr": atr,
            },
        )

    def on_position(
        self,
        position: Position,
        event: MarketEvent,
    ) -> Optional[ExitSignal]:
        meta = position.metadata or {}
        if meta.get("original_strategy") not in (None, self.name) \
                and meta.get("strategy") != self.name \
                and meta.get("sub_strategy") != self.name:
            return None

        now_ms = event.timestamp_ms
        hold_ms = now_ms - position.entry_time_ms

        # Max hold
        if hold_ms >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.8,
                reason=f"max_hold_{self.MAX_HOLD_HOURS:g}h",
                metadata={"hold_ms": hold_ms},
            )

        # Funding sign reversal
        funding = event.predicted_funding
        if funding is None:
            funding = event.funding
        if funding is not None and position.side == "long" and funding < -self.FUNDING_FLIP_THRESHOLD:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.85,
                reason=f"funding_reversed_long_{funding:.5f}",
                metadata={"funding": funding, "hold_ms": hold_ms},
            )
        if funding is not None and position.side == "short" and funding > self.FUNDING_FLIP_THRESHOLD:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.85,
                reason=f"funding_reversed_short_{funding:.5f}",
                metadata={"funding": funding, "hold_ms": hold_ms},
            )

        return None
