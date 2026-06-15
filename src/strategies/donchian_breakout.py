"""Strategy: DonchianBreakout — momentum breakout on 15m candles.

Entry logic:
  - Long: price breaks above the highest high of the last N 15m candles
  - Short: price breaks below the lowest low of the last N 15m candles
  - ADX(14) >= min_adx to confirm trending regime (using either event.adx_14
    or calculated from 1h open/high/low/close prices in MarketEvent)
  - Anti-chop: avg range (high-low) of the last N candles must be >= min_range_pct

Stop loss: ATR(14) on 15m candles * stop_atr_mult
Take profit: optional 2R, but preference is to defer to engine trailing stop
Exit (on_position): max_hold_hours and/or price re-entering the channel
Size: base_size_pct scaled by breakout strength (distance beyond channel / channel width), capped at max_size_pct
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.strategies.indicators import Candle, calculate_adx, calculate_atr
from src.utils.helpers import safe_divide

logger = logging.getLogger(__name__)


@dataclass
class _DonchianState:
    candles_15m: Deque[Candle] = field(
        default_factory=lambda: collections.deque(maxlen=100)
    )
    last_signal_ms: int = 0


class DonchianBreakout(Strategy):
    """Donchian channel breakout on 15m candles with ADX filter."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}

        self.LOOKBACK = int(cfg.get("lookback", 20))
        self.MIN_ADX = float(cfg.get("min_adx", 22.0))
        self.MIN_RANGE_PCT = float(cfg.get("min_range_pct", 0.001))
        self.STOP_ATR_MULT = float(cfg.get("stop_atr_mult", 2.0))
        self.TP_R_MULT = float(cfg.get("tp_r_mult", 2.0))
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.01))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.03))
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 1_800_000))
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 6))
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.60))

        self._state: Dict[str, _DonchianState] = {}

    @property
    def name(self) -> str:
        return "DonchianBreakout"

    # ----------------------------------------------------------------
    # Entry
    # ----------------------------------------------------------------

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        state = self._get_state(event.symbol)

        if event.candle_15m and (
            not state.candles_15m
            or state.candles_15m[-1].timestamp_ms != event.candle_15m.timestamp_ms
        ):
            state.candles_15m.append(event.candle_15m)

        if event.timestamp_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS:
            return None

        candles = list(state.candles_15m)
        min_bars = self.LOOKBACK + 3
        if len(candles) < min_bars:
            if event.timestamp_ms - getattr(
                state, "_last_warmup_log_ms", 0
            ) > 300_000:
                state._last_warmup_log_ms = event.timestamp_ms
                logger.info(
                    "DonchianBreakout %s WARM-UP: %d/%d candles",
                    event.symbol,
                    len(candles),
                    min_bars,
                )
            return None

        price = event.price

        # Donchian channel on completed candles (exclude current forming)
        prior = candles[:-1]
        channel_high, channel_low = self._donchian(prior)
        if channel_high is None or channel_low is None:
            return None

        long_break = price > channel_high
        short_break = price < channel_low
        if not long_break and not short_break:
            return None

        # --- Anti-chop: avg range must be meaningful ---
        if not self._passes_anti_chop(prior):
            logger.debug(
                "DonchianBreakout SKIP %s — avg range too tight (anti-chop)",
                event.symbol,
            )
            return None

        # --- ADX filter ---
        adx = self._resolve_adx(event, prior)
        if adx is None or adx < self.MIN_ADX:
            logger.debug(
                "DonchianBreakout SKIP %s — ADX=%.1f < %.1f",
                event.symbol,
                adx if adx is not None else -1,
                self.MIN_ADX,
            )
            return None

        side = "long" if long_break else "short"

        # --- ATR stop ---
        atr = calculate_atr(prior, period=14)
        if atr is None or atr <= 0:
            atr = price * 0.005

        stop_loss_pct = safe_divide(self.STOP_ATR_MULT * atr, price, 0.015)
        take_profit_pct = stop_loss_pct * self.TP_R_MULT

        # --- Confidence & size ---
        channel_width = (channel_high - channel_low) / (channel_high + channel_low) * 2
        if long_break:
            break_dist = (price - channel_high) / channel_width if channel_width > 0 else 0.03
        else:
            break_dist = (channel_low - price) / channel_width if channel_width > 0 else 0.03

        break_strength = min(1.0, max(0.0, abs(break_dist) * 10))

        # --- Hard gate: skip weak breakouts that will never pass ensemble ---
        if break_strength < 0.6:
            logger.debug(
                "DonchianBreakout SKIP %s — break_strength=%.2f < 0.6 (weak breakout)",
                event.symbol, break_strength,
            )
            return None

        adx_score = min(1.0, max(0.0, (adx - self.MIN_ADX) / 30.0))
        confidence = 0.6 * break_strength + 0.4 * adx_score
        confidence = min(0.95, max(self.MIN_CONFIDENCE, confidence))

        size_pct = min(
            self.MAX_SIZE_PCT,
            self.BASE_SIZE_PCT * (1.0 + break_strength),
        )

        state.last_signal_ms = event.timestamp_ms
        logger.info(
            "DonchianBreakout %s %s ch=%.2f/cl=%.2f atr=%.4f adx=%.1f "
            "break=%.3f conf=%.2f",
            event.symbol,
            side,
            channel_high,
            channel_low,
            atr,
            adx,
            break_dist,
            confidence,
        )

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=side,
            confidence=confidence,
            size_pct=size_pct,
            entry_price=price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            reason=f"donchian_{side}_adx{adx:.0f}",
            metadata={
                "channel_high": channel_high,
                "channel_low": channel_low,
                "atr": atr,
                "adx": adx,
                "break_dist_pct": break_dist * 100,
                "break_strength": break_strength,
                "lookback": self.LOOKBACK,
            },
        )

    # ----------------------------------------------------------------
    # Exit
    # ----------------------------------------------------------------

    def on_position(
        self, position: Position, event: MarketEvent
    ) -> Optional[ExitSignal]:
        state = self._get_state(position.symbol)

        if event.candle_15m and (
            not state.candles_15m
            or state.candles_15m[-1].timestamp_ms != event.candle_15m.timestamp_ms
        ):
            state.candles_15m.append(event.candle_15m)

        # --- Time limit ---
        hold_ms = event.timestamp_ms - position.entry_time_ms
        if hold_ms >= self.MAX_HOLD_MS:
            hold_hrs = hold_ms / 3_600_000
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.8,
                reason=f"max_hold_{self.MAX_HOLD_HOURS}h",
                metadata={"hold_hours": round(hold_hrs, 1)},
            )

        # --- Channel re-entry exit ---
        candles = list(state.candles_15m)
        if len(candles) < self.LOOKBACK + 1:
            return None

        channel_high, channel_low = self._donchian(candles)
        if channel_high is None or channel_low is None:
            return None

        price = event.price

        if position.side == "long" and price <= channel_low:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.75,
                reason="reentry_channel_low",
                metadata={"channel_low": channel_low},
            )

        if position.side == "short" and price >= channel_high:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.75,
                reason="reentry_channel_high",
                metadata={"channel_high": channel_high},
            )

        return None

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    def _donchian(
        self, candles: List[Candle]
    ) -> Tuple[Optional[float], Optional[float]]:
        """Return (highest_high, lowest_low) over lookback."""
        if len(candles) < 2:
            return None, None
        window = candles[-self.LOOKBACK :]
        if not window:
            return None, None
        highest = max(c.high for c in window)
        lowest = min(c.low for c in window)
        return highest, lowest

    def _passes_anti_chop(self, candles: List[Candle]) -> bool:
        """Return False if the average candle range is too narrow."""
        window = candles[-self.LOOKBACK:]
        if len(window) < 2:
            return False
        total_range = sum(c.high - c.low for c in window)
        avg_range = total_range / len(window)
        mid = (max(c.high for c in window) + min(c.low for c in window)) / 2.0
        if mid <= 0:
            return True
        avg_range_pct = avg_range / mid
        return avg_range_pct >= self.MIN_RANGE_PCT

    def _resolve_adx(
        self, event: MarketEvent, candles_15m: List[Candle]
    ) -> Optional[float]:
        """Return ADX from event if available, otherwise compute from 15m candles."""
        if event.adx_14 is not None:
            return event.adx_14
        return calculate_adx(candles_15m, period=14)

    def _get_state(self, symbol: str) -> _DonchianState:
        if symbol not in self._state:
            self._state[symbol] = _DonchianState()
        return self._state[symbol]
