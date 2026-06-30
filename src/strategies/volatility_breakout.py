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
from src.strategies.time_filters import is_weekday_blocked, parse_weekday_blocks
from src.utils.helpers import safe_divide

logger = logging.getLogger(__name__)


@dataclass
class _VolBreakoutState:
    candles_15m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=80))
    last_signal_ms: int = 0
    squeeze_active: bool = False
    # Trailing-stop state (reset on new position entry)
    last_entry_time_ms: int = 0
    trail_active: bool = False
    current_trail: float = 0.0
    # v3.1.39: SL-to-BE state
    sl_moved_to_be: bool = False
    # v3.1.35: retest entry mode state
    pending_break_side: Optional[str] = None
    pending_break_band_price: float = 0.0
    pending_break_time_ms: int = 0
    pending_break_atr: float = 0.0
    pending_break_bbw_pct: float = 0.0
    pending_break_confidence: float = 0.0
    pending_break_oi_delta: Optional[float] = None
    pending_break_adx: Optional[float] = None


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

        # --- v3.1.26 optimisation: trailing stop (opt-in) ---
        self.USE_TRAILING_STOP = bool(cfg.get("use_trailing_stop", False))
        self.TRAILING_METHOD = str(cfg.get("trailing_method", "ema9"))  # ema9|swing|atr
        self.TRAILING_START_R = float(cfg.get("trailing_start_r", 1.0))
        self.TRAILING_EMA_PERIOD = int(cfg.get("trailing_ema_period", 9))
        self.TRAILING_ATR_MULT = float(cfg.get("trailing_atr_mult", 1.5))
        self.TRAILING_SWING_LOOKBACK = int(cfg.get("trailing_swing_lookback", 5))

        # --- v3.1.26 optimisation: time-scaled TP (opt-in) ---
        # If hold > TIME_TP_FIRST_HOURS and original TP not hit yet,
        # exit at the lower TIME_TP_AFTER_R multiple instead of waiting full MAX_HOLD.
        self.USE_TIME_SCALED_TP = bool(cfg.get("use_time_scaled_tp", False))
        self.TIME_TP_FIRST_HOURS = float(cfg.get("time_tp_first_hours", 3.0))
        self.TIME_TP_AFTER_R = float(cfg.get("time_tp_after_r", 2.5))

        # --- v3.1.35: retest entry mode (opt-in) ---
        # On break, instead of entering immediately, store the break and wait
        # for price to pull back to the broken band ("retest"). Enter only if
        # the retest candle rejects (closes back on the breakout side).
        # Filters fake-outs — the retest is the trade, not the break.
        self.USE_RETEST_ENTRY = bool(cfg.get("use_retest_entry", False))
        self.RETEST_MAX_BARS = int(cfg.get("retest_max_bars", 8))      # ~2h on 15m
        self.RETEST_BUFFER_PCT = float(cfg.get("retest_buffer_pct", 0.002))  # 0.2% of price
        self.RETEST_MIN_HOLD_MS = int(cfg.get("retest_min_hold_ms", 900_000))  # wait >=1 bar

        # --- v3.1.36: weekday filter (opt-in) ---
        self._weekday_blocks = parse_weekday_blocks(cfg) if cfg.get("use_weekday_filter", False) else []

        # --- v3.1.39: SL-to-BE after N R profit (cut losers early once green) ---
        self.USE_SL_TO_BE_AFTER_R = bool(cfg.get("use_sl_to_be_after_1r", False))
        self.SL_TO_BE_R_TRIGGER = float(cfg.get("sl_to_be_r_trigger", 1.0))
        self.SL_TO_BE_BUFFER_PCT = float(cfg.get("sl_to_be_buffer_pct", 0.001))

        self._state: Dict[str, _VolBreakoutState] = {}

    @property
    def name(self) -> str:
        return "VolatilityBreakout"

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        state = self._get_state(event.symbol)

        if self._weekday_blocks and is_weekday_blocked(event.timestamp_ms, self._weekday_blocks):
            return None

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

        # --- v3.1.35: retest entry mode ---
        # If we have a pending breakout, check for retest entry FIRST
        # (the retest is the actual trade entry, not the break).
        if self.USE_RETEST_ENTRY and state.pending_break_side is not None:
            retest_signal = self._check_retest_entry(
                state, price, last, event.timestamp_ms, event,
            )
            if retest_signal is not None:
                state.last_signal_ms = event.timestamp_ms
                return retest_signal
            # If retest timed out, pending_break_side has been cleared by _check_retest_entry

        if not broke_up and not broke_down:
            # If no new break and no pending retest, nothing to do
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

        # --- v3.1.35: in retest mode, store the breakout and wait for retest ---
        if self.USE_RETEST_ENTRY:
            band_price = upper if side == "long" else lower
            state.pending_break_side = side
            state.pending_break_band_price = band_price
            state.pending_break_time_ms = event.timestamp_ms
            state.pending_break_atr = atr
            state.pending_break_bbw_pct = bbw_pct
            state.pending_break_confidence = confidence
            state.pending_break_oi_delta = event.oi_delta
            state.pending_break_adx = adx
            logger.info(
                "VolatilityBreakout PENDING-RETEST %s %s band=%.4f atr=%.4f conf=%.2f — waiting retest",
                event.symbol, side, band_price, atr, confidence,
            )
            return None

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
        price = event.price
        if event.candle_15m and (
            not state.candles_15m
            or state.candles_15m[-1].timestamp_ms != event.candle_15m.timestamp_ms
        ):
            state.candles_15m.append(event.candle_15m)

        # Reset trailing state on a fresh position
        if position.entry_time_ms != state.last_entry_time_ms:
            state.last_entry_time_ms = position.entry_time_ms
            state.trail_active = False
            state.current_trail = 0.0
            state.sl_moved_to_be = False

        hold_ms = event.timestamp_ms - position.entry_time_ms
        if hold_ms >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.8,
                reason=f"max_hold_{self.MAX_HOLD_HOURS}h",
            )

        # v3.1.39: SL-to-BE after N R profit — cut losers early once green
        if (
            self.USE_SL_TO_BE_AFTER_R
            and not state.sl_moved_to_be
            and position.stop_loss_price is not None
        ):
            r_dist = abs(position.entry_price - position.stop_loss_price)
            if r_dist > 0:
                if position.side == "long":
                    profit_r = (price - position.entry_price) / r_dist
                else:
                    profit_r = (position.entry_price - price) / r_dist
                if profit_r >= self.SL_TO_BE_R_TRIGGER:
                    state.sl_moved_to_be = True
                    logger.info(
                        "VolatilityBreakout SL-TO-BE %s %s profit_r=%.2f >= %.2f",
                        position.symbol, position.side, profit_r, self.SL_TO_BE_R_TRIGGER,
                    )

        # If SL has been moved to BE, exit on touch (no further loss)
        if self.USE_SL_TO_BE_AFTER_R and state.sl_moved_to_be:
            buf = self.SL_TO_BE_BUFFER_PCT
            if position.side == "long":
                be_price = position.entry_price * (1.0 + buf)
                if price <= be_price:
                    return ExitSignal(
                        strategy=self.name, symbol=position.symbol, side=position.side,
                        confidence=0.7, reason=f"sl_to_be_hit_r{self.SL_TO_BE_R_TRIGGER}",
                    )
            else:
                be_price = position.entry_price * (1.0 - buf)
                if price >= be_price:
                    return ExitSignal(
                        strategy=self.name, symbol=position.symbol, side=position.side,
                        confidence=0.7, reason=f"sl_to_be_hit_r{self.SL_TO_BE_R_TRIGGER}",
                    )

        candles = list(state.candles_15m)
        if len(candles) < self.BB_PERIOD + 2:
            return None

        price = event.price  # already defined above, kept for compat with older code below

        # --- v3.1.26: trailing stop (only after TRAILING_START_R profit) ---
        if self.USE_TRAILING_STOP and position.stop_loss_price is not None:
            r_dist = abs(position.entry_price - position.stop_loss_price)
            if r_dist > 0:
                if position.side == "long":
                    profit_r = (price - position.entry_price) / r_dist
                else:
                    profit_r = (position.entry_price - price) / r_dist

                if profit_r >= self.TRAILING_START_R:
                    trail = self._compute_trail(candles, position.side)
                    if trail is not None:
                        # Trail only moves in profit direction
                        if not state.trail_active:
                            state.trail_active = True
                            state.current_trail = trail
                        else:
                            if position.side == "long":
                                state.current_trail = max(state.current_trail, trail)
                            else:
                                state.current_trail = min(state.current_trail, trail)

                        ct = state.current_trail
                        if position.side == "long" and price <= ct:
                            return ExitSignal(
                                strategy=self.name,
                                symbol=position.symbol,
                                side=position.side,
                                confidence=0.8,
                                reason=f"trailing_stop_{self.TRAILING_METHOD}_r{profit_r:.1f}",
                            )
                        if position.side == "short" and price >= ct:
                            return ExitSignal(
                                strategy=self.name,
                                symbol=position.symbol,
                                side=position.side,
                                confidence=0.8,
                                reason=f"trailing_stop_{self.TRAILING_METHOD}_r{profit_r:.1f}",
                            )

        # --- v3.1.26: time-scaled TP (exit at lower R if first_hours elapsed) ---
        if self.USE_TIME_SCALED_TP and position.stop_loss_price is not None:
            first_ms = int(self.TIME_TP_FIRST_HOURS * 3_600_000)
            if hold_ms >= first_ms:
                r_dist = abs(position.entry_price - position.stop_loss_price)
                if r_dist > 0:
                    if position.side == "long":
                        eff_tp = position.entry_price + self.TIME_TP_AFTER_R * r_dist
                        if price >= eff_tp:
                            return ExitSignal(
                                strategy=self.name,
                                symbol=position.symbol,
                                side=position.side,
                                confidence=0.8,
                                reason=f"time_scaled_tp_{self.TIME_TP_AFTER_R}r",
                            )
                    else:
                        eff_tp = position.entry_price - self.TIME_TP_AFTER_R * r_dist
                        if price <= eff_tp:
                            return ExitSignal(
                                strategy=self.name,
                                symbol=position.symbol,
                                side=position.side,
                                confidence=0.8,
                                reason=f"time_scaled_tp_{self.TIME_TP_AFTER_R}r",
                            )

        closes = [c.close for c in candles]
        lower, middle, upper = self._bands(candles)
        if lower is None or upper is None:
            return None

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

    def _compute_trail(self, candles: List[Candle], side: str) -> Optional[float]:
        """Compute trailing-stop level for the given side."""
        if self.TRAILING_METHOD == "ema9":
            closes = [c.close for c in candles]
            ema = calculate_ema(closes, self.TRAILING_EMA_PERIOD)
            return ema
        if self.TRAILING_METHOD == "swing":
            lookback = self.TRAILING_SWING_LOOKBACK
            window = candles[-lookback:] if len(candles) >= lookback else candles
            if side == "long":
                return min(c.low for c in window)
            return max(c.high for c in window)
        if self.TRAILING_METHOD == "atr":
            atr = calculate_atr(candles, period=14)
            if atr is None:
                return None
            last_close = candles[-1].close
            if side == "long":
                return last_close - self.TRAILING_ATR_MULT * atr
            return last_close + self.TRAILING_ATR_MULT * atr
        return None

    def _get_state(self, symbol: str) -> _VolBreakoutState:
        if symbol not in self._state:
            self._state[symbol] = _VolBreakoutState()
        return self._state[symbol]

    def _check_retest_entry(
        self,
        state: _VolBreakoutState,
        price: float,
        last_candle: Candle,
        now_ms: int,
        event: MarketEvent,
    ) -> Optional[Signal]:
        """Check whether the latest closed candle is a valid retest entry.

        A valid retest:
          - At least ``RETEST_MIN_HOLD_MS`` has elapsed since the break
          - Price has come back within ``RETEST_BUFFER_PCT`` of the broken band
          - The latest closed candle closed on the breakout side of the band
            (rejection of the retest from the opposite side)
          - The retest happens within ``RETEST_MAX_BARS`` bars of the break
        """
        if state.pending_break_side is None:
            return None

        elapsed_ms = now_ms - state.pending_break_time_ms
        if elapsed_ms < self.RETEST_MIN_HOLD_MS:
            return None

        # Timeout: clear pending state if too many bars have passed
        max_wait_ms = self.RETEST_MAX_BARS * 15 * 60_000  # 15m bars
        if elapsed_ms > max_wait_ms:
            logger.info(
                "VolatilityBreakout RETEST-TIMEOUT %s %s — no retest within %d bars",
                event.symbol, state.pending_break_side, self.RETEST_MAX_BARS,
            )
            self._clear_pending_break(state)
            return None

        band = state.pending_break_band_price
        side = state.pending_break_side
        buffer = abs(band) * self.RETEST_BUFFER_PCT

        # Distance from the broken band
        dist_to_band = abs(price - band)

        # Has price come back to the band?
        if dist_to_band > buffer:
            return None

        # Did the latest closed candle close on the breakout side of the band?
        # For long: close > band (rejection from below)
        # For short: close < band (rejection from above)
        if side == "long" and last_candle.close <= band:
            return None
        if side == "short" and last_candle.close >= band:
            return None

        # Valid retest entry — emit signal using stored break-time metrics
        atr = state.pending_break_atr
        confidence = state.pending_break_confidence
        stop_loss_pct = safe_divide(self.STOP_ATR_MULT * atr, price, 0.015)
        take_profit_pct = safe_divide(self.TAKE_PROFIT_ATR_MULT * atr, price, 0.03)
        size_pct = min(
            self.MAX_SIZE_PCT,
            self.BASE_SIZE_PCT * (1.0 + (confidence - self.MIN_CONFIDENCE)),
        )

        logger.info(
            "VolatilityBreakout RETEST-SIGNAL %s %s band=%.4f price=%.4f dist=%.4f conf=%.2f",
            event.symbol, side, band, price, dist_to_band, confidence,
        )

        sig = Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=side,
            confidence=confidence,
            size_pct=size_pct,
            entry_price=price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            reason=f"retest_{side}_bbw_p{state.pending_break_bbw_pct:.0f}",
            metadata={
                "bb_upper": band if side == "long" else None,
                "bb_lower": band if side == "short" else None,
                "bb_width_pct": state.pending_break_bbw_pct,
                "atr": atr,
                "adx": state.pending_break_adx,
                "oi_delta": state.pending_break_oi_delta,
                "retest_dist_to_band": dist_to_band,
                "retest_wait_ms": elapsed_ms,
            },
        )
        self._clear_pending_break(state)
        return sig

    def _clear_pending_break(self, state: _VolBreakoutState) -> None:
        state.pending_break_side = None
        state.pending_break_band_price = 0.0
        state.pending_break_time_ms = 0
        state.pending_break_atr = 0.0
        state.pending_break_bbw_pct = 0.0
        state.pending_break_confidence = 0.0
        state.pending_break_oi_delta = None
        state.pending_break_adx = None
