"""Strategy: Volatility Breakout (Bollinger squeeze + expansion)

Captures the transition from low-volatility compression to directional expansion:
  1. Detect squeeze — BB width in bottom percentile of recent history
  2. Enter on band break with volume confirmation
  3. Exit on failed breakout (back inside bands), target, or max hold

Complements SmartMoneyFlow (enters after trend is established) by targeting
the *start* of expansion moves.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.strategies.indicators import (
    Candle,
    calculate_atr,
    calculate_bollinger_bands,
    calculate_ema,
    calculate_volume_ratio,
)
from src.utils.helpers import safe_divide

logger = logging.getLogger(__name__)


@dataclass
class _VolBreakoutState:
    candles_15m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=60))
    last_signal_ms: int = 0
    squeeze_active: bool = False


class VolatilityBreakout(Strategy):
    """Bollinger squeeze breakout on 15m candles."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        self.BB_PERIOD = int(cfg.get("bb_period", 20))
        self.BB_STD = float(cfg.get("bb_std", 2.0))
        self.SQUEEZE_LOOKBACK = int(cfg.get("squeeze_lookback", 20))
        self.SQUEEZE_PERCENTILE = float(cfg.get("squeeze_percentile", 25.0))
        self.MIN_SQUEEZE_BARS = int(cfg.get("min_squeeze_bars", 3))
        self.VOLUME_SURGE = float(cfg.get("volume_surge", 1.3))
        self.MIN_ADX = float(cfg.get("min_adx", 12.0))
        self.MAX_ADX = float(cfg.get("max_adx", 38.0))
        self.REQUIRE_OI_CONFIRM = bool(cfg.get("require_oi_confirm", False))
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.01))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.025))
        self.STOP_ATR_MULT = float(cfg.get("stop_loss_atr_multiplier", 1.5))
        self.TAKE_PROFIT_ATR_MULT = float(cfg.get("take_profit_atr_multiplier", 3.0))
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 6))
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.55))
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 900_000))
        self.REQUIRE_TREND_ALIGNMENT = bool(cfg.get("require_trend_alignment", True))
        self.TREND_EMA_FAST = int(cfg.get("trend_ema_fast", 20))
        self.TREND_EMA_SLOW = int(cfg.get("trend_ema_slow", 50))
        self.FAILED_BREAKOUT_MIN_HOLD_MS = int(
            cfg.get("failed_breakout_min_hold_ms", 2_700_000)
        )
        self.FAILED_BREAKOUT_BUFFER_PCT = float(
            cfg.get("failed_breakout_buffer_pct", 0.001)
        )

        self._state: Dict[str, _VolBreakoutState] = {}

    @property
    def name(self) -> str:
        return "VolatilityBreakout"

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
        min_bars = self.BB_PERIOD + self.SQUEEZE_LOOKBACK + 3
        if len(candles) < min_bars:
            if event.timestamp_ms - getattr(state, "_last_warmup_log_ms", 0) > 300_000:
                state._last_warmup_log_ms = event.timestamp_ms
                logger.info(
                    "VolatilityBreakout %s WARM-UP: %d/%d 15m candles",
                    event.symbol,
                    len(candles),
                    min_bars,
                )
            return None

        # Squeeze on completed history *before* the latest bar (avoid breakout bar polluting BB width)
        prior = candles[:-1]
        last = candles[-1]
        prev = prior[-1]

        squeeze_ok, bbw_pct, lower, middle, upper = self._detect_squeeze(prior)
        if lower is None or upper is None:
            lower, middle, upper = self._bands(prior)

        state.squeeze_active = squeeze_ok
        if not squeeze_ok or lower is None or upper is None or middle is None:
            if event.timestamp_ms - getattr(state, "_last_squeeze_log_ms", 0) > 600_000:
                state._last_squeeze_log_ms = event.timestamp_ms
                logger.info(
                    "VolatilityBreakout SKIP %s — no squeeze (bbw_p=%.0f%%)",
                    event.symbol, bbw_pct,
                )
            return None

        price = event.price

        broke_up = price > upper and last.close > upper and prev.close <= upper
        broke_down = price < lower and last.close < lower and prev.close >= lower
        if not broke_up and not broke_down:
            return None

        side = "long" if broke_up else "short"

        if self.REQUIRE_TREND_ALIGNMENT:
            closes = [c.close for c in candles]
            ema_fast = calculate_ema(closes, self.TREND_EMA_FAST)
            ema_slow = calculate_ema(closes, self.TREND_EMA_SLOW)
            if ema_fast is None or ema_slow is None:
                return None
            if side == "long" and not (price > ema_fast and ema_fast > ema_slow):
                logger.info(
                    "VolatilityBreakout SKIP %s long — trend misaligned "
                    "(price=%.2f ema%d=%.2f ema%d=%.2f)",
                    event.symbol,
                    price,
                    self.TREND_EMA_FAST,
                    ema_fast,
                    self.TREND_EMA_SLOW,
                    ema_slow,
                )
                return None
            if side == "short" and not (price < ema_fast and ema_fast < ema_slow):
                logger.info(
                    "VolatilityBreakout SKIP %s short — trend misaligned "
                    "(price=%.2f ema%d=%.2f ema%d=%.2f)",
                    event.symbol,
                    price,
                    self.TREND_EMA_FAST,
                    ema_fast,
                    self.TREND_EMA_SLOW,
                    ema_slow,
                )
                return None

        adx = event.adx_14
        if adx is not None and (adx < self.MIN_ADX or adx > self.MAX_ADX):
            logger.info(
                "VolatilityBreakout SKIP %s — ADX=%.1f outside [%.1f, %.1f]",
                event.symbol,
                adx,
                self.MIN_ADX,
                self.MAX_ADX,
            )
            return None

        _, vol_ratio = calculate_volume_ratio(candles, lookback=24)
        if vol_ratio is None or vol_ratio < self.VOLUME_SURGE:
            logger.info(
                "VolatilityBreakout SKIP %s — vol_ratio=%s (need >= %.2f)",
                event.symbol,
                f"{vol_ratio:.2f}" if vol_ratio is not None else "N/A",
                self.VOLUME_SURGE,
            )
            return None

        if self.REQUIRE_OI_CONFIRM and event.oi_delta is not None:
            if side == "long" and event.oi_delta <= 0:
                return None
            if side == "short" and event.oi_delta >= 0:
                return None

        atr = calculate_atr(candles, period=14)
        if atr is None or atr <= 0:
            atr = middle * 0.01

        stop_loss_pct = safe_divide(self.STOP_ATR_MULT * atr, price, 0.015)
        take_profit_pct = safe_divide(self.TAKE_PROFIT_ATR_MULT * atr, price, 0.03)

        squeeze_score = max(0.0, min(1.0, (100.0 - bbw_pct) / 100.0))
        vol_score = min(1.0, (vol_ratio - self.VOLUME_SURGE) / self.VOLUME_SURGE + 0.5)
        oi_score = 1.0
        if event.oi_delta is not None:
            if side == "long" and event.oi_delta > 0:
                oi_score = 1.0
            elif side == "short" and event.oi_delta < 0:
                oi_score = 1.0
            else:
                oi_score = 0.7

        confidence = 0.35 * squeeze_score + 0.35 * vol_score + 0.30 * oi_score
        confidence = min(0.95, max(self.MIN_CONFIDENCE, confidence))

        size_pct = min(
            self.MAX_SIZE_PCT,
            self.BASE_SIZE_PCT * (1.0 + (confidence - self.MIN_CONFIDENCE)),
        )

        state.last_signal_ms = event.timestamp_ms
        logger.info(
            "VolatilityBreakout SIGNAL %s %s squeeze_p=%.0f vol=%.2f bbw=%.4f conf=%.2f",
            event.symbol,
            side,
            bbw_pct,
            vol_ratio,
            (upper - lower) / middle if middle else 0.0,
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
            reason=f"squeeze_breakout_{side}_bbw_p{bbw_pct:.0f}",
            metadata={
                "bb_upper": upper,
                "bb_lower": lower,
                "bb_middle": middle,
                "bb_width_pct": bbw_pct,
                "vol_ratio": vol_ratio,
                "atr": atr,
                "adx": adx,
                "oi_delta": event.oi_delta,
            },
        )

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        state = self._get_state(position.symbol)
        if event.candle_15m and (
            not state.candles_15m
            or state.candles_15m[-1].timestamp_ms != event.candle_15m.timestamp_ms
        ):
            state.candles_15m.append(event.candle_15m)

        hold_ms = event.timestamp_ms - position.entry_time_ms
        if hold_ms >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.8,
                reason=f"max_hold_{self.MAX_HOLD_HOURS}h",
            )

        candles = list(state.candles_15m)
        if len(candles) < self.BB_PERIOD + 2:
            return None

        closes = [c.close for c in candles]
        lower, middle, upper = self._bands(candles)
        if lower is None or upper is None:
            return None

        price = event.price
        if hold_ms < self.FAILED_BREAKOUT_MIN_HOLD_MS:
            return None

        buf = self.FAILED_BREAKOUT_BUFFER_PCT
        mid_long_exit = middle * (1.0 - buf) if middle else None
        mid_short_exit = middle * (1.0 + buf) if middle else None
        if (
            position.side == "long"
            and mid_long_exit is not None
            and price < mid_long_exit
        ):
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.75,
                reason="failed_breakout_below_mid",
            )
        if (
            position.side == "short"
            and mid_short_exit is not None
            and price > mid_short_exit
        ):
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.75,
                reason="failed_breakout_above_mid",
            )

        if position.take_profit_price is not None:
            if position.side == "long" and price >= position.take_profit_price:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side=position.side,
                    confidence=0.85,
                    reason="take_profit_hit",
                )
            if position.side == "short" and price <= position.take_profit_price:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side=position.side,
                    confidence=0.85,
                    reason="take_profit_hit",
                )

        return None

    def _detect_squeeze(
        self, candles: List[Candle]
    ) -> Tuple[bool, float, Optional[float], Optional[float], Optional[float]]:
        """Return (squeeze_ok, width_percentile, lower, middle, upper)."""
        widths: List[float] = []
        for end in range(self.BB_PERIOD, len(candles)):
            window = candles[: end + 1]
            closes = [c.close for c in window]
            lower, middle, upper = calculate_bollinger_bands(
                closes, self.BB_PERIOD, self.BB_STD
            )
            if middle is None or middle <= 0 or lower is None or upper is None:
                continue
            widths.append((upper - lower) / middle)

        if len(widths) < self.SQUEEZE_LOOKBACK + 1:
            return False, 100.0, None, None, None

        recent = widths[-self.SQUEEZE_LOOKBACK :]
        current_width = widths[-1]
        sorted_w = sorted(recent)
        rank = sum(1 for w in sorted_w if w <= current_width)
        percentile = 100.0 * rank / len(sorted_w)

        threshold_idx = max(0, int(len(sorted_w) * self.SQUEEZE_PERCENTILE / 100.0) - 1)
        threshold = sorted_w[threshold_idx]
        squeeze_bars = sum(1 for w in recent[-self.MIN_SQUEEZE_BARS :] if w <= threshold)
        squeeze_ok = (
            current_width <= threshold
            and squeeze_bars >= max(1, self.MIN_SQUEEZE_BARS - 1)
            and percentile <= self.SQUEEZE_PERCENTILE + 5.0
        )

        lower, middle, upper = self._bands(candles)
        return squeeze_ok, percentile, lower, middle, upper

    def _bands(
        self, candles: List[Candle]
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        closes = [c.close for c in candles]
        return calculate_bollinger_bands(closes, self.BB_PERIOD, self.BB_STD)

    def _get_state(self, symbol: str) -> _VolBreakoutState:
        if symbol not in self._state:
            self._state[symbol] = _VolBreakoutState()
        return self._state[symbol]
