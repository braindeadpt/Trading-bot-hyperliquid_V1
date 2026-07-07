"""Strategy: Checklist Meta-Signal (v3.1.37).

Replaces the weighted ensemble with a transparent "trade decision
checklist" — bull points and bear points are scored separately, and
the side with more weight wins.

Bullish points (each adds to long bias):
  +1.5  SFP at support (sweep + close back inside recent swing low)
  +1.0  Price > VWAP (acceptance above value)
  +1.0  EMA20 > EMA50 on 15m (uptrend structure)
  +0.5  Price > EMA20 (short-term momentum up)
  +0.5  RSI in [40, 60] (room to run, not overbought)
  +0.5  OI_delta > 0 (longs entering)
  +0.5  ADX > 20 (trend strength)
  +0.5  Orderbook OIR > 0.3 (bid pressure)
  +0.5  Liquidations on short side (shorts squeezed = fuel for longs)

Bearish points mirror.

Entry: |score| >= SCORE_THRESHOLD AND bull/bear spread >= dominance_margin
       AND ADX >= min_adx_gate (when available) → directional trade.
Anti-flip: after a stop-loss on a symbol, block the opposite side for
       flip_block_minutes (long stop → no short; short stop → no long).

Exit: TP at ATR multiple, SL at ATR multiple, max hold, optional trailing.

This is intentionally transparent — every component weight is visible
in config and can be tuned per regime. It does NOT depend on other
strategies (no circular dependency); it recomputes the building blocks
from candle history + MarketEvent fields.
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
    calculate_vwap_multi_tf,
    detect_recent_pivots,
)
from src.strategies.time_filters import is_weekday_blocked, parse_weekday_blocks
from src.utils.helpers import safe_divide

logger = logging.getLogger(__name__)


@dataclass
class _ChecklistState:
    candles_15m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=200))
    candles_1h: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=100))
    last_signal_ms: int = 0
    last_entry_time_ms: int = 0
    trail_active: bool = False
    current_trail: float = 0.0
    # v3.1.39: SL-to-BE state
    sl_moved_to_be: bool = False
    # Anti-whipsaw: last stop-loss on this symbol (opposite side blocked briefly)
    last_stop_ms: int = 0
    last_stop_side: str = ""


class ChecklistMeta(Strategy):
    """Weighted bull/bear checklist meta-signal."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # Thresholds
        self.SCORE_THRESHOLD = float(cfg.get("score_threshold", 3.0))
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.60))
        self.MIN_ADX_GATE = float(cfg.get("min_adx_gate", 18.0))
        self.DOMINANCE_MARGIN = float(cfg.get("dominance_margin", 1.5))
        self.FLIP_BLOCK_MINUTES = float(cfg.get("flip_block_minutes", 60.0))
        self.FLIP_BLOCK_MS = int(self.FLIP_BLOCK_MINUTES * 60_000)
        # Component weights (overridable per regime)
        self.W_SFP = float(cfg.get("w_sfp", 1.5))
        self.W_VWAP = float(cfg.get("w_vwap", 1.0))
        self.W_TREND_STRUCTURE = float(cfg.get("w_trend_structure", 1.0))
        self.W_MOMENTUM = float(cfg.get("w_momentum", 0.5))
        self.W_RSI_ROOM = float(cfg.get("w_rsi_room", 0.5))
        self.W_OI_DELTA = float(cfg.get("w_oi_delta", 0.5))
        self.W_ADX = float(cfg.get("w_adx", 0.5))
        self.W_OIR = float(cfg.get("w_oir", 0.5))
        self.W_LIQUIDATION = float(cfg.get("w_liquidation", 0.5))
        # Indicator params
        self.EMA_FAST = int(cfg.get("ema_fast", 20))
        self.EMA_SLOW = int(cfg.get("ema_slow", 50))
        self.ADX_TREND_MIN = float(cfg.get("adx_trend_min", 20.0))
        self.OIR_THRESHOLD = float(cfg.get("oir_threshold", 0.3))
        self.RSI_ROOM_MIN = float(cfg.get("rsi_room_min", 40.0))
        self.RSI_ROOM_MAX = float(cfg.get("rsi_room_max", 60.0))
        self.SFP_LOOKBACK = int(cfg.get("sfp_lookback", 100))
        self.SFP_MAX_AGE_BARS = int(cfg.get("sfp_max_age_bars", 30))
        # Sizing / risk
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.01))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.025))
        self.STOP_ATR_MULT = float(cfg.get("stop_loss_atr_multiplier", 1.5))
        self.TAKE_PROFIT_ATR_MULT = float(cfg.get("take_profit_atr_multiplier", 3.0))
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 6))
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 1_800_000))
        # Trailing stop (opt-in)
        self.USE_TRAILING_STOP = bool(cfg.get("use_trailing_stop", False))
        self.TRAILING_METHOD = str(cfg.get("trailing_method", "ema9"))
        self.TRAILING_START_R = float(cfg.get("trailing_start_r", 1.0))
        self.TRAILING_EMA_PERIOD = int(cfg.get("trailing_ema_period", 9))
        self.TRAILING_ATR_MULT = float(cfg.get("trailing_atr_mult", 1.5))
        self.TRAILING_SWING_LOOKBACK = int(cfg.get("trailing_swing_lookback", 5))
        # v3.1.39: SL-to-BE after N R profit (cut losers early once green)
        self.USE_SL_TO_BE_AFTER_R = bool(cfg.get("use_sl_to_be_after_1r", False))
        self.SL_TO_BE_R_TRIGGER = float(cfg.get("sl_to_be_r_trigger", 1.0))
        self.SL_TO_BE_BUFFER_PCT = float(cfg.get("sl_to_be_buffer_pct", 0.001))  # 0.1% above entry for long
        # Weekday filter
        self._weekday_blocks = parse_weekday_blocks(cfg) if cfg.get("use_weekday_filter", False) else []

        self._state: Dict[str, _ChecklistState] = {}

    @property
    def name(self) -> str:
        return "ChecklistMeta"

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        state = self._get_state(event.symbol)

        if self._weekday_blocks and is_weekday_blocked(event.timestamp_ms, self._weekday_blocks):
            return None

        if event.candle_15m and (
            not state.candles_15m
            or state.candles_15m[-1].timestamp_ms != event.candle_15m.timestamp_ms
        ):
            state.candles_15m.append(event.candle_15m)
        if event.candle_1h and (
            not state.candles_1h
            or state.candles_1h[-1].timestamp_ms != event.candle_1h.timestamp_ms
        ):
            state.candles_1h.append(event.candle_1h)

        if event.timestamp_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS:
            return None

        adx = event.adx_14
        if adx is not None and adx < self.MIN_ADX_GATE:
            logger.info(
                "ChecklistMeta NO SIGNAL %s reason=chop_gate adx=%.1f < %.1f",
                event.symbol, adx, self.MIN_ADX_GATE,
            )
            return None

        candles_15m = list(state.candles_15m)
        if len(candles_15m) < max(self.EMA_SLOW + 3, self.SFP_LOOKBACK + 3):
            return None

        prior = candles_15m[:-1]
        if len(prior) < self.EMA_SLOW + 2:
            return None
        last_closed = prior[-1]

        # --- Compute components ---
        components: Dict[str, float] = {}

        # 1. SFP at swing low/high
        sfp_side = self._detect_sfp_side(prior)
        if sfp_side == "long":
            components["sfp_long"] = self.W_SFP
        elif sfp_side == "short":
            components["sfp_short"] = self.W_SFP

        # 2. VWAP position
        vwap = self._compute_vwap(prior)
        price = event.price
        if vwap is not None:
            if price > vwap:
                components["vwap_above"] = self.W_VWAP
            elif price < vwap:
                components["vwap_below"] = self.W_VWAP

        # 3. Trend structure: EMA20 vs EMA50 on 15m closes
        closes_15m = [c.close for c in prior]
        ema_fast = calculate_ema(closes_15m, self.EMA_FAST)
        ema_slow = calculate_ema(closes_15m, self.EMA_SLOW)
        if ema_fast is not None and ema_slow is not None:
            if ema_fast > ema_slow:
                components["trend_up"] = self.W_TREND_STRUCTURE
                if price > ema_fast:
                    components["mom_up"] = self.W_MOMENTUM
            elif ema_fast < ema_slow:
                components["trend_down"] = self.W_TREND_STRUCTURE
                if price < ema_fast:
                    components["mom_down"] = self.W_MOMENTUM

        # 4. RSI room (event.rsi_14)
        rsi = event.rsi_14
        if rsi is not None:
            if self.RSI_ROOM_MIN <= rsi <= self.RSI_ROOM_MAX:
                # Room both ways — neutral. Bias toward trend direction (already counted).
                pass
            elif rsi > 60:
                # Overbought → bearish point (mean reversion risk)
                components["rsi_overbought"] = self.W_RSI_ROOM
            elif rsi < 40:
                components["rsi_oversold"] = self.W_RSI_ROOM

        # 5. OI delta
        if event.oi_delta is not None:
            if event.oi_delta > 0:
                components["oi_rising"] = self.W_OI_DELTA
            elif event.oi_delta < 0:
                components["oi_falling"] = self.W_OI_DELTA

        # 6. ADX (trend strength, direction-neutral)
        if adx is not None and adx >= self.ADX_TREND_MIN:
            components["adx_trend"] = self.W_ADX

        # 7. Orderbook OIR
        oir = event.orderbook_oir
        if oir is not None:
            if oir > self.OIR_THRESHOLD:
                components["oir_bid"] = self.W_OIR
            elif oir < -self.OIR_THRESHOLD:
                components["oir_ask"] = self.W_OIR

        # 8. Liquidations (short squeeze fuel for longs, long squeeze fuel for shorts)
        liq_side = event.liquidation_side_5m
        if liq_side == "long":
            # Longs liquidated → fuel for further DOWN move
            components["liq_long_squeeze"] = self.W_LIQUIDATION
        elif liq_side == "short":
            components["liq_short_squeeze"] = self.W_LIQUIDATION

        # --- Aggregate bull vs bear ---
        bull_keys = {"sfp_long", "vwap_above", "trend_up", "mom_up", "rsi_oversold", "oi_rising", "adx_trend", "oir_bid", "liq_short_squeeze"}
        bear_keys = {"sfp_short", "vwap_below", "trend_down", "mom_down", "rsi_overbought", "oi_falling", "adx_trend", "oir_ask", "liq_long_squeeze"}
        # Note: adx_trend is direction-neutral — counts toward whichever side is dominant
        # We handle this by NOT including adx_trend in either set; instead, add it to the
        # side that has more directional components.
        directional_bull = sum(v for k, v in components.items() if k in bull_keys)
        directional_bear = sum(v for k, v in components.items() if k in bear_keys)

        # Add ADX to the winning side (trend strength amplifies the dominant direction)
        if "adx_trend" in components:
            if directional_bull > directional_bear:
                directional_bull += components["adx_trend"]
            elif directional_bear > directional_bull:
                directional_bear += components["adx_trend"]

        spread = abs(directional_bull - directional_bear)
        block = self._entry_gates_block_reason(
            event.symbol, directional_bull, directional_bear,
            adx, event.timestamp_ms, spread,
        )
        if block is not None:
            logger.info(
                "ChecklistMeta NO SIGNAL %s reason=%s",
                event.symbol, block,
            )
            return None

        score = directional_bull - directional_bear

        if abs(score) < self.SCORE_THRESHOLD:
            return None

        side = "long" if score > 0 else "short"

        flip_reason = self._flip_block_reason(state, side, event.timestamp_ms)
        if flip_reason is not None:
            logger.info(
                "ChecklistMeta NO SIGNAL %s reason=%s",
                event.symbol, flip_reason,
            )
            return None

        # ATR for sizing
        atr = calculate_atr(prior, period=14) or price * 0.01
        stop_loss_pct = safe_divide(self.STOP_ATR_MULT * atr, price, 0.015)
        take_profit_pct = safe_divide(self.TAKE_PROFIT_ATR_MULT * atr, price, 0.03)

        # Confidence scales with |score| relative to threshold
        confidence = min(0.95, self.MIN_CONFIDENCE + 0.05 * (abs(score) - self.SCORE_THRESHOLD))
        confidence = max(self.MIN_CONFIDENCE, confidence)

        size_pct = min(
            self.MAX_SIZE_PCT,
            self.BASE_SIZE_PCT * (1.0 + (confidence - self.MIN_CONFIDENCE)),
        )

        state.last_signal_ms = event.timestamp_ms
        logger.info(
            "ChecklistMeta SIGNAL %s %s score=%+.2f bull=%.2f bear=%.2f conf=%.2f components=%s",
            event.symbol, side, score, directional_bull, directional_bear, confidence,
            ",".join(f"{k}={v:.1f}" for k, v in components.items()),
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
            reason=f"checklist_{side}_score{score:+.1f}",
            metadata={
                "score": score,
                "bull_score": directional_bull,
                "bear_score": directional_bear,
                "components": components,
                "atr": atr,
                "adx": adx,
                "rsi": rsi,
                "vwap": vwap,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "oi_delta": event.oi_delta,
                "oir": oir,
                "liq_side": liq_side,
                "sfp_side": sfp_side,
            },
        )

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        state = self._get_state(position.symbol)
        price = event.price

        if position.stop_loss_price is not None:
            sl = position.stop_loss_price
            if position.side == "long" and price <= sl:
                state.last_stop_ms = event.timestamp_ms
                state.last_stop_side = "long"
            elif position.side == "short" and price >= sl:
                state.last_stop_ms = event.timestamp_ms
                state.last_stop_side = "short"

        if event.candle_15m and (
            not state.candles_15m
            or state.candles_15m[-1].timestamp_ms != event.candle_15m.timestamp_ms
        ):
            state.candles_15m.append(event.candle_15m)

        if position.entry_time_ms != state.last_entry_time_ms:
            state.last_entry_time_ms = position.entry_time_ms
            state.trail_active = False
            state.current_trail = 0.0
            state.sl_moved_to_be = False

        hold_ms = event.timestamp_ms - position.entry_time_ms
        if hold_ms >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name, symbol=position.symbol, side=position.side,
                confidence=0.8, reason=f"max_hold_{self.MAX_HOLD_HOURS}h",
            )

        # v3.1.39: SL-to-BE after N R profit — cut losers early once green
        # Returns an ExitSignal only if the position reverses back through BE.
        # The engine's stop_loss_price is not mutated here; we emit a defensive
        # exit when price touches BE after the move-to-BE trigger has fired.
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
                        "ChecklistMeta SL-TO-BE %s %s profit_r=%.2f >= %.2f — SL now at BE",
                        position.symbol, position.side, profit_r, self.SL_TO_BE_R_TRIGGER,
                    )

        # If SL has been moved to BE, exit when price touches BE (no further loss)
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
        if len(candles) < 5:
            return None

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
                    confidence=0.85, reason="checklist_tp_hit",
                )
            if position.side == "short" and price <= position.take_profit_price:
                return ExitSignal(
                    strategy=self.name, symbol=position.symbol, side=position.side,
                    confidence=0.85, reason="checklist_tp_hit",
                )

        return None

    def record_stop_exit(self, symbol: str, side: str, timestamp_ms: int) -> None:
        """Record a stop-loss exit for anti-flip gating (tests / external hooks)."""
        state = self._get_state(symbol)
        state.last_stop_ms = int(timestamp_ms)
        state.last_stop_side = str(side)

    def _entry_gates_block_reason(
        self,
        symbol: str,
        directional_bull: float,
        directional_bear: float,
        adx: Optional[float],
        timestamp_ms: int,
        spread: Optional[float] = None,
    ) -> Optional[str]:
        """Return block reason for chop/dominance gates (spread-only phase)."""
        if adx is not None and adx < self.MIN_ADX_GATE:
            return f"chop_gate adx={adx:.1f}"
        bull_bear_spread = (
            spread if spread is not None
            else abs(directional_bull - directional_bear)
        )
        if bull_bear_spread < self.DOMINANCE_MARGIN:
            return (
                f"dominance_gate bull={directional_bull:.2f} "
                f"bear={directional_bear:.2f}"
            )
        return None

    def _flip_block_reason(
        self,
        state: _ChecklistState,
        side: str,
        timestamp_ms: int,
    ) -> Optional[str]:
        """Return a block reason if opposite-side entry is forbidden after a stop."""
        if state.last_stop_ms <= 0 or not state.last_stop_side:
            return None
        elapsed = timestamp_ms - state.last_stop_ms
        if elapsed >= self.FLIP_BLOCK_MS:
            return None
        opposite = "short" if state.last_stop_side == "long" else "long"
        if side == opposite:
            remaining_min = (self.FLIP_BLOCK_MS - elapsed) / 60_000
            return (
                f"flip_block after {state.last_stop_side} stop "
                f"({remaining_min:.0f}min left)"
            )
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _detect_sfp_side(self, prior: List[Candle]) -> Optional[str]:
        """Detect SFP on the latest closed candle. Returns 'long', 'short', or None."""
        if len(prior) < self.SFP_LOOKBACK + 3:
            return None
        sweep_candle = prior[-1]
        before_sweep = prior[:-1]
        pivot_highs, pivot_lows = detect_recent_pivots(
            before_sweep, lookback=self.SFP_LOOKBACK, max_pivots=10,
        )
        ts_to_idx = {c.timestamp_ms: i for i, c in enumerate(before_sweep)}
        sweep_idx = len(before_sweep)

        for p_ts, p_low in pivot_lows:
            p_idx = ts_to_idx.get(p_ts)
            if p_idx is None:
                continue
            age = sweep_idx - p_idx
            if age < 3:
                continue
            if age > self.SFP_MAX_AGE_BARS:
                break
            if sweep_candle.low < p_low and sweep_candle.close > p_low:
                return "long"

        for p_ts, p_high in pivot_highs:
            p_idx = ts_to_idx.get(p_ts)
            if p_idx is None:
                continue
            age = sweep_idx - p_idx
            if age < 3:
                continue
            if age > self.SFP_MAX_AGE_BARS:
                break
            if sweep_candle.high > p_high and sweep_candle.close < p_high:
                return "short"
        return None

    def _compute_vwap(self, prior: List[Candle]) -> Optional[float]:
        """Compute VWAP over the last 24 bars (fallback to event.vwap_15m)."""
        if len(prior) < 14:
            return None
        window = prior[-96:] if len(prior) >= 96 else prior
        total_vol = sum(c.volume for c in window)
        if total_vol <= 0:
            return None
        return sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in window) / total_vol

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

    def _get_state(self, symbol: str) -> _ChecklistState:
        if symbol not in self._state:
            self._state[symbol] = _ChecklistState()
        return self._state[symbol]
