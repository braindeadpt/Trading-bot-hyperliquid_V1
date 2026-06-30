"""Strategy: SFP Reversion (Swing Failure Pattern + 75% retracement magnet).

Detects liquidity sweeps at established swing lows/highs followed by a
close back inside the range. This is the "finished low" pattern: trapped
sellers are stopped out, fuel is created for the opposite move.

Entry logic (long SFP at support, mirrored for short at resistance):
  1. Identify a confirmed pivot low in the last `lookback` bars
  2. A subsequent candle sweeps below the pivot low (low < pivot_low)
  3. That same candle closes back above the pivot low (close > pivot_low)
  4. Wick rejection: close in upper half of the candle range
  5. Volume surge: volume > volume_surge * 24-bar average
  6. Optional: OI rising (longs entering)

Exit:
  - Take profit: 75% retracement of the range from sweep low to recent high
    (capped at take_profit_r_multiple * R for safety)
  - Stop loss: sweep low - buffer_pct * price (or ATR-based)
  - Max hold: configurable (default 6h)
  - Optional trailing stop after 1R (same as VolatilityBreakout)

Timeframe: 15m candles (enough swings per day without too much noise).
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
    calculate_ema,
    calculate_volume_ratio,
    detect_recent_pivots,
)
from src.strategies.time_filters import is_weekday_blocked, parse_weekday_blocks
from src.utils.helpers import safe_divide

logger = logging.getLogger(__name__)


@dataclass
class _SFPState:
    candles_15m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=200))
    last_signal_ms: int = 0
    # Trailing-stop state (reset on new position)
    last_entry_time_ms: int = 0
    trail_active: bool = False
    current_trail: float = 0.0


class SFPReversion(Strategy):
    """Swing Failure Pattern reversion strategy with 75% retracement target."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # Pivot detection
        self.PIVOT_LOOKBACK = int(cfg.get("pivot_lookback", 100))
        self.PIVOT_MAX_AGE_BARS = int(cfg.get("pivot_max_age_bars", 30))
        self.PIVOT_MIN_SEPARATION_BARS = int(cfg.get("pivot_min_separation_bars", 3))
        # Sweep / confirmation
        self.SWEEP_BUFFER_PCT = float(cfg.get("sweep_buffer_pct", 0.001))  # 0.1% below pivot
        self.WICK_REJECTION_MIN = float(cfg.get("wick_rejection_min", 0.5))  # close in upper half
        self.VOLUME_SURGE = float(cfg.get("volume_surge", 1.5))
        self.REQUIRE_OI_CONFIRM = bool(cfg.get("require_oi_confirm", False))
        # Range for 75% magnet
        self.RANGE_LOOKBACK = int(cfg.get("range_lookback", 100))
        # Sizing / risk
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.01))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.025))
        self.STOP_BUFFER_PCT = float(cfg.get("stop_buffer_pct", 0.003))  # 0.3% beyond sweep
        self.STOP_ATR_MULT = float(cfg.get("stop_loss_atr_multiplier", 1.5))
        self.TAKE_PROFIT_R_MULT = float(cfg.get("take_profit_r_multiple", 2.5))
        self.MAGNET_RETRACEMENT = float(cfg.get("magnet_retracement", 0.75))
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 6))
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.60))
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 1_800_000))
        # ADX filter: don't fade strong trends
        self.MAX_ADX = float(cfg.get("max_adx", 40.0))
        # Trailing stop (opt-in)
        self.USE_TRAILING_STOP = bool(cfg.get("use_trailing_stop", False))
        self.TRAILING_METHOD = str(cfg.get("trailing_method", "ema9"))
        self.TRAILING_START_R = float(cfg.get("trailing_start_r", 1.0))
        self.TRAILING_EMA_PERIOD = int(cfg.get("trailing_ema_period", 9))
        self.TRAILING_ATR_MULT = float(cfg.get("trailing_atr_mult", 1.5))
        self.TRAILING_SWING_LOOKBACK = int(cfg.get("trailing_swing_lookback", 5))

        # v3.1.36: weekday filter (opt-in)
        self._weekday_blocks = parse_weekday_blocks(cfg) if cfg.get("use_weekday_filter", False) else []

        self._state: Dict[str, _SFPState] = {}

    @property
    def name(self) -> str:
        return "SFPReversion"

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------
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
        if len(candles) < self.PIVOT_LOOKBACK + 3:
            if event.timestamp_ms - getattr(state, "_last_warmup_log_ms", 0) > 300_000:
                state._last_warmup_log_ms = event.timestamp_ms
                logger.info(
                    "SFPReversion %s WARM-UP: %d/%d 15m candles",
                    event.symbol, len(candles), self.PIVOT_LOOKBACK + 3,
                )
            return None

        # SFP is decided on CLOSED candles. The latest closed candle is candles[-2]
        # (candles[-1] is the still-forming bar). We need candles[-2] to be the
        # sweep+close candle, and candles[-3..-pivot_lookback] to contain the pivot.
        prior = candles[:-1]  # all closed candles except the forming one
        if len(prior) < self.PIVOT_LOOKBACK + 3:
            return None
        sweep_candle = prior[-1]
        before_sweep = prior[:-1]

        # Detect pivots in candles up to (but excluding) the sweep candle.
        # Pivots confirmed at bar i need bars i+1 and i+2 to be present.
        # So we pass `before_sweep` (which ends 1 bar before the sweep).
        pivot_highs, pivot_lows = detect_recent_pivots(
            before_sweep, lookback=self.PIVOT_LOOKBACK, max_pivots=10,
        )

        price = event.price
        side, pivot_price, sweep_price = self._detect_sfp(
            pivot_highs, pivot_lows, sweep_candle, before_sweep,
        )
        if side is None:
            return None

        # ADX filter
        adx = event.adx_14
        if adx is not None and adx > self.MAX_ADX:
            logger.info(
                "SFPReversion SKIP %s — ADX=%.1f > %.1f (strong trend, don't fade)",
                event.symbol, adx, self.MAX_ADX,
            )
            return None

        # Volume surge on the sweep candle
        _, vol_ratio = calculate_volume_ratio(prior, lookback=24)
        if vol_ratio is None or vol_ratio < self.VOLUME_SURGE:
            if event.timestamp_ms - getattr(state, "_last_skip_log_ms", 0) > 600_000:
                state._last_skip_log_ms = event.timestamp_ms
                logger.info(
                    "SFPReversion SKIP %s — vol_ratio=%s (need >= %.2f)",
                    event.symbol,
                    f"{vol_ratio:.2f}" if vol_ratio is not None else "N/A",
                    self.VOLUME_SURGE,
                )
            return None

        # Wick rejection quality
        if sweep_candle.high > sweep_candle.low:
            close_pos = (sweep_candle.close - sweep_candle.low) / (sweep_candle.high - sweep_candle.low)
        else:
            close_pos = 0.5
        # For long SFP we want close in upper half; for short SFP, lower half
        wick_ok = (close_pos >= self.WICK_REJECTION_MIN) if side == "long" else (close_pos <= 1.0 - self.WICK_REJECTION_MIN)
        if not wick_ok:
            logger.info(
                "SFPReversion SKIP %s %s — wick close_pos=%.2f (need %s %.2f)",
                event.symbol, side, close_pos,
                ">=" if side == "long" else "<=",
                self.WICK_REJECTION_MIN if side == "long" else 1.0 - self.WICK_REJECTION_MIN,
            )
            return None

        # OI confirmation (optional)
        if self.REQUIRE_OI_CONFIRM and event.oi_delta is not None:
            if side == "long" and event.oi_delta <= 0:
                return None
            if side == "short" and event.oi_delta >= 0:
                return None

        # Compute range for 75% magnet target
        range_window = candles[-self.RANGE_LOOKBACK:] if len(candles) >= self.RANGE_LOOKBACK else candles
        highest_high = max(c.high for c in range_window)
        lowest_low = min(c.low for c in range_window)

        # Stop: beyond the sweep extreme + small buffer (or ATR-based)
        atr = calculate_atr(prior, period=14)
        if side == "long":
            stop_price = sweep_price * (1.0 - self.STOP_BUFFER_PCT)
            stop_alt = price - self.STOP_ATR_MULT * (atr or price * 0.01)
            stop_price = min(stop_price, stop_alt)  # wider of the two
            magnet = sweep_price + self.MAGNET_RETRACEMENT * (highest_high - sweep_price)
        else:
            stop_price = sweep_price * (1.0 + self.STOP_BUFFER_PCT)
            stop_alt = price + self.STOP_ATR_MULT * (atr or price * 0.01)
            stop_price = max(stop_price, stop_alt)
            magnet = sweep_price - self.MAGNET_RETRACEMENT * (sweep_price - lowest_low)

        r_dist = abs(price - stop_price)
        if r_dist <= 0:
            return None
        # Cap TP at TAKE_PROFIT_R_MULT * R (avoid ridiculous magnet targets)
        tp_r_price = price + self.TAKE_PROFIT_R_MULT * r_dist if side == "long" else price - self.TAKE_PROFIT_R_MULT * r_dist
        if side == "long":
            tp_price = min(magnet, tp_r_price)
        else:
            tp_price = max(magnet, tp_r_price)

        stop_loss_pct = abs(price - stop_price) / price
        take_profit_pct = abs(tp_price - price) / price

        # Confidence: wick quality + volume + sweep depth
        wick_score = min(1.0, abs(close_pos - 0.5) * 2.0)  # 0..1, 1 = perfect wick
        vol_score = min(1.0, (vol_ratio - self.VOLUME_SURGE) / self.VOLUME_SURGE + 0.5)
        sweep_depth = abs(sweep_price - pivot_price) / pivot_price if pivot_price > 0 else 0
        sweep_score = min(1.0, sweep_depth / 0.005)  # 0.5% sweep = max score
        confidence = 0.4 * wick_score + 0.35 * vol_score + 0.25 * sweep_score
        confidence = max(self.MIN_CONFIDENCE, min(0.95, confidence))

        size_pct = min(
            self.MAX_SIZE_PCT,
            self.BASE_SIZE_PCT * (1.0 + (confidence - self.MIN_CONFIDENCE)),
        )

        state.last_signal_ms = event.timestamp_ms
        logger.info(
            "SFPReversion SIGNAL %s %s sweep=%.4f pivot=%.4f vol=%.2f wick=%.2f conf=%.2f TP=%.4f SL=%.4f",
            event.symbol, side, sweep_price, pivot_price, vol_ratio, close_pos,
            confidence, tp_price, stop_price,
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
            reason=f"sfp_{side}_sweep{side[0]}{sweep_depth:.3f}",
            metadata={
                "pivot_price": pivot_price,
                "sweep_price": sweep_price,
                "magnet_target": magnet,
                "tp_price": tp_price,
                "stop_price": stop_price,
                "vol_ratio": vol_ratio,
                "wick_close_pos": close_pos,
                "atr": atr,
                "adx": adx,
                "oi_delta": event.oi_delta,
            },
        )

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------
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

        # Trailing stop (opt-in)
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

        # Take profit (engine handles TP/SL but we mirror for safety)
        if position.take_profit_price is not None:
            if position.side == "long" and price >= position.take_profit_price:
                return ExitSignal(
                    strategy=self.name, symbol=position.symbol, side=position.side,
                    confidence=0.85, reason="magnet_tp_hit",
                )
            if position.side == "short" and price <= position.take_profit_price:
                return ExitSignal(
                    strategy=self.name, symbol=position.symbol, side=position.side,
                    confidence=0.85, reason="magnet_tp_hit",
                )

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _detect_sfp(
        self,
        pivot_highs: List[Tuple[int, float]],
        pivot_lows: List[Tuple[int, float]],
        sweep_candle: Candle,
        before_sweep: List[Candle],
    ) -> Tuple[Optional[str], Optional[float], Optional[float]]:
        """Return (side, pivot_price, sweep_price) or (None, None, None).

        Long SFP: a recent pivot low is swept (low < pivot_low) but the
        sweep candle closes back above it (close > pivot_low).
        Short SFP: mirror at pivot high.
        """
        # Pivot must be at least PIVOT_MIN_SEPARATION_BARS before the sweep
        # candle, and not older than PIVOT_MAX_AGE_BARS bars.
        sweep_ts = sweep_candle.timestamp_ms
        # Find timestamps of bars in before_sweep to compute age
        # We use bar count proxy: count bars between pivot bar and sweep bar.
        # Easier: use the pivot's timestamp_ms and find its index in before_sweep.
        ts_to_idx = {c.timestamp_ms: i for i, c in enumerate(before_sweep)}
        sweep_idx_in_before = len(before_sweep)  # sweep is the next bar

        # Check long SFP (sweep below a pivot low)
        for p_ts, p_low in pivot_lows:
            p_idx = ts_to_idx.get(p_ts)
            if p_idx is None:
                continue
            age_bars = sweep_idx_in_before - p_idx
            if age_bars < self.PIVOT_MIN_SEPARATION_BARS:
                continue
            if age_bars > self.PIVOT_MAX_AGE_BARS:
                break  # sorted most-recent-first; older pivots will all be too old
            if sweep_candle.low < p_low and sweep_candle.close > p_low:
                return "long", p_low, sweep_candle.low

        # Check short SFP (sweep above a pivot high)
        for p_ts, p_high in pivot_highs:
            p_idx = ts_to_idx.get(p_ts)
            if p_idx is None:
                continue
            age_bars = sweep_idx_in_before - p_idx
            if age_bars < self.PIVOT_MIN_SEPARATION_BARS:
                continue
            if age_bars > self.PIVOT_MAX_AGE_BARS:
                break
            if sweep_candle.high > p_high and sweep_candle.close < p_high:
                return "short", p_high, sweep_candle.high

        return None, None, None

    def _compute_trail(self, candles: List[Candle], side: str) -> Optional[float]:
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

    def _get_state(self, symbol: str) -> _SFPState:
        if symbol not in self._state:
            self._state[symbol] = _SFPState()
        return self._state[symbol]
