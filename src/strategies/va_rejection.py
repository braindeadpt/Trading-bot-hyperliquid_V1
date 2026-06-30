"""Strategy: Volume Profile Value Area Rejection (v3.1.34).

Builds a Volume Profile over a rolling 24h window of 15m candles,
computes the 70% Value Area (VAH/VAL/POC), and trades rejections at
the VA boundaries.

Entry logic (long, mirrored for short):
  1. Price was inside or above VAL
  2. Current candle wicks below VAL (sweeps the boundary)
  3. Current candle closes back above VAL (rejection)
  4. Wick rejection: close in upper half of candle
  5. Volume surge: volume > volume_surge * 24-bar average
  6. NOT in D-shape regime (is_balanced=False)

Exit:
  - Take profit: opposite VA extreme (VAH for long, VAL for short)
  - Stop loss: beyond the rejection wick + buffer
  - Max hold: configurable (default 6h)
  - Optional trailing stop after 1R
"""
from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.strategies.indicators import (
    Candle,
    calculate_atr,
    calculate_ema,
    calculate_volume_profile_va,
    calculate_volume_ratio,
)
from src.strategies.time_filters import is_weekday_blocked, parse_weekday_blocks
from src.utils.helpers import safe_divide

logger = logging.getLogger(__name__)


@dataclass
class _VAState:
    candles_15m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=200))
    last_signal_ms: int = 0
    last_entry_time_ms: int = 0
    trail_active: bool = False
    current_trail: float = 0.0


class VARejection(Strategy):
    """Value Area rejection strategy — trades sweeps at VAH/VAL boundaries."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # Volume profile
        self.VP_LOOKBACK = int(cfg.get("vp_lookback", 96))  # 24h of 15m
        self.VP_BINS = int(cfg.get("vp_bins", 50))
        self.VA_PCT = float(cfg.get("va_pct", 0.70))
        self.BALANCED_THRESHOLD = float(cfg.get("balanced_threshold", 0.60))
        self.SKIP_BALANCED = bool(cfg.get("skip_balanced", True))
        # Rejection candle
        self.VOLUME_SURGE = float(cfg.get("volume_surge", 1.5))
        self.WICK_REJECTION_MIN = float(cfg.get("wick_rejection_min", 0.5))
        # ADX filter
        self.MAX_ADX = float(cfg.get("max_adx", 40.0))
        # Sizing / risk
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.01))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.025))
        self.STOP_BUFFER_PCT = float(cfg.get("stop_buffer_pct", 0.003))
        self.STOP_ATR_MULT = float(cfg.get("stop_loss_atr_multiplier", 1.5))
        self.TAKE_PROFIT_R_MULT = float(cfg.get("take_profit_r_multiple", 2.5))
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 6))
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.60))
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 1_800_000))
        # Trailing stop (opt-in)
        self.USE_TRAILING_STOP = bool(cfg.get("use_trailing_stop", False))
        self.TRAILING_METHOD = str(cfg.get("trailing_method", "ema9"))
        self.TRAILING_START_R = float(cfg.get("trailing_start_r", 1.0))
        self.TRAILING_EMA_PERIOD = int(cfg.get("trailing_ema_period", 9))
        self.TRAILING_ATR_MULT = float(cfg.get("trailing_atr_mult", 1.5))
        self.TRAILING_SWING_LOOKBACK = int(cfg.get("trailing_swing_lookback", 5))

        # v3.1.36: weekday filter (opt-in)
        self._weekday_blocks = parse_weekday_blocks(cfg) if cfg.get("use_weekday_filter", False) else []

        self._state: Dict[str, _VAState] = {}

    @property
    def name(self) -> str:
        return "VARejection"

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
        if len(candles) < self.VP_LOOKBACK + 3:
            if event.timestamp_ms - getattr(state, "_last_warmup_log_ms", 0) > 300_000:
                state._last_warmup_log_ms = event.timestamp_ms
                logger.info(
                    "VARejection %s WARM-UP: %d/%d 15m candles",
                    event.symbol, len(candles), self.VP_LOOKBACK + 3,
                )
            return None

        # Decide on CLOSED candles. candles[-1] is forming; candles[-2] is the
        # latest closed candle which we treat as the "rejection candle".
        prior = candles[:-1]
        if len(prior) < self.VP_LOOKBACK + 3:
            return None
        rej_candle = prior[-1]

        # Build VP on the window ending just before the rejection candle
        # (so the rejection candle itself is NOT in the profile — it's the
        # candle that reacts to the VA).
        vp_window = prior[-(self.VP_LOOKBACK + 1):-1]
        if len(vp_window) < self.VP_LOOKBACK:
            return None
        vp = calculate_volume_profile_va(
            vp_window, bins=self.VP_BINS, va_pct=self.VA_PCT,
            balanced_threshold=self.BALANCED_THRESHOLD,
        )
        if vp is None:
            return None

        if self.SKIP_BALANCED and vp.is_balanced:
            return None

        price = event.price
        side, sweep_price = self._detect_rejection(rej_candle, vp)
        if side is None:
            return None

        # ADX filter
        adx = event.adx_14
        if adx is not None and adx > self.MAX_ADX:
            logger.info(
                "VARejection SKIP %s — ADX=%.1f > %.1f",
                event.symbol, adx, self.MAX_ADX,
            )
            return None

        # Volume surge
        _, vol_ratio = calculate_volume_ratio(prior, lookback=24)
        if vol_ratio is None or vol_ratio < self.VOLUME_SURGE:
            return None

        # Wick quality
        if rej_candle.high > rej_candle.low:
            close_pos = (rej_candle.close - rej_candle.low) / (rej_candle.high - rej_candle.low)
        else:
            close_pos = 0.5
        wick_ok = (close_pos >= self.WICK_REJECTION_MIN) if side == "long" else (close_pos <= 1.0 - self.WICK_REJECTION_MIN)
        if not wick_ok:
            return None

        # Stop / TP
        atr = calculate_atr(prior, period=14)
        if side == "long":
            stop_price = sweep_price * (1.0 - self.STOP_BUFFER_PCT)
            stop_alt = price - self.STOP_ATR_MULT * (atr or price * 0.01)
            stop_price = min(stop_price, stop_alt)
            tp_price = vp.vah  # target the opposite VA extreme
            tp_r_price = price + self.TAKE_PROFIT_R_MULT * abs(price - stop_price)
            tp_price = min(tp_price, tp_r_price)
        else:
            stop_price = sweep_price * (1.0 + self.STOP_BUFFER_PCT)
            stop_alt = price + self.STOP_ATR_MULT * (atr or price * 0.01)
            stop_price = max(stop_price, stop_alt)
            tp_price = vp.val
            tp_r_price = price - self.TAKE_PROFIT_R_MULT * abs(price - stop_price)
            tp_price = max(tp_price, tp_r_price)

        r_dist = abs(price - stop_price)
        if r_dist <= 0:
            return None
        stop_loss_pct = abs(price - stop_price) / price
        take_profit_pct = abs(tp_price - price) / price

        # Confidence
        wick_score = min(1.0, abs(close_pos - 0.5) * 2.0)
        vol_score = min(1.0, (vol_ratio - self.VOLUME_SURGE) / self.VOLUME_SURGE + 0.5)
        # Distance from POC: deeper sweep = stronger rejection signal
        poc_dist = abs(sweep_price - vp.poc) / vp.poc if vp.poc > 0 else 0
        poc_score = min(1.0, poc_dist / 0.01)  # 1% from POC = max score
        # Unbalanced profile = stronger directional signal
        bal_score = 0.3 if vp.is_balanced else 0.8
        confidence = 0.35 * wick_score + 0.30 * vol_score + 0.20 * poc_score + 0.15 * bal_score
        confidence = max(self.MIN_CONFIDENCE, min(0.95, confidence))

        size_pct = min(
            self.MAX_SIZE_PCT,
            self.BASE_SIZE_PCT * (1.0 + (confidence - self.MIN_CONFIDENCE)),
        )

        state.last_signal_ms = event.timestamp_ms
        logger.info(
            "VARejection SIGNAL %s %s sweep=%.4f POC=%.4f VAH=%.4f VAL=%.4f vol=%.2f conf=%.2f",
            event.symbol, side, sweep_price, vp.poc, vp.vah, vp.val, vol_ratio, confidence,
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
            reason=f"va_{side}_rejection",
            metadata={
                "poc": vp.poc,
                "vah": vp.vah,
                "val": vp.val,
                "is_balanced": vp.is_balanced,
                "sweep_price": sweep_price,
                "tp_price": tp_price,
                "stop_price": stop_price,
                "vol_ratio": vol_ratio,
                "wick_close_pos": close_pos,
                "atr": atr,
                "adx": adx,
            },
        )

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        state = self._get_state(position.symbol)
        if event.candle_15m and (
            not state.candles_15m
            or state.candles_15m[-1].timestamp_ms != event.candle_15m.timestamp_ms
        ):
            state.candles_15m.append(event.candle_15m)

        if position.entry_time_ms != state.last_entry_time_ms:
            state.last_entry_time_ms = position.entry_time_ms
            state.trail_active = False
            state.current_trail = 0.0

        hold_ms = event.timestamp_ms - position.entry_time_ms
        if hold_ms >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name, symbol=position.symbol, side=position.side,
                confidence=0.8, reason=f"max_hold_{self.MAX_HOLD_HOURS}h",
            )

        candles = list(state.candles_15m)
        if len(candles) < 5:
            return None
        price = event.price

        # Trailing stop
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
                                strategy=self.name, symbol=position.symbol,
                                side=position.side, confidence=0.8,
                                reason=f"trailing_stop_{self.TRAILING_METHOD}_r{profit_r:.1f}",
                            )
                        if position.side == "short" and price >= ct:
                            return ExitSignal(
                                strategy=self.name, symbol=position.symbol,
                                side=position.side, confidence=0.8,
                                reason=f"trailing_stop_{self.TRAILING_METHOD}_r{profit_r:.1f}",
                            )

        # TP mirror
        if position.take_profit_price is not None:
            if position.side == "long" and price >= position.take_profit_price:
                return ExitSignal(
                    strategy=self.name, symbol=position.symbol, side=position.side,
                    confidence=0.85, reason="va_tp_hit",
                )
            if position.side == "short" and price <= position.take_profit_price:
                return ExitSignal(
                    strategy=self.name, symbol=position.symbol, side=position.side,
                    confidence=0.85, reason="va_tp_hit",
                )

        return None

    def _detect_rejection(self, rej_candle: Candle, vp: Any) -> tuple:
        """Return (side, sweep_price) or (None, None).

        Long: wick below VAL, close back above VAL.
        Short: wick above VAH, close back below VAH.
        """
        if rej_candle.low < vp.val and rej_candle.close > vp.val:
            return "long", rej_candle.low
        if rej_candle.high > vp.vah and rej_candle.close < vp.vah:
            return "short", rej_candle.high
        return None, None

    def _compute_trail(self, candles: List[Candle], side: str) -> Optional[float]:
        if self.TRAILING_METHOD == "ema9":
            closes = [c.close for c in candles]
            return calculate_ema(closes, self.TRAILING_EMA_PERIOD)
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

    def _get_state(self, symbol: str) -> _VAState:
        if symbol not in self._state:
            self._state[symbol] = _VAState()
        return self._state[symbol]
