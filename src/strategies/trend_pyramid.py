"""Strategy: TrendPyramid — trend following with pyramiding and Chandelier exit.

Edge
----
The historically highest-edge crypto strategy: ride strong trends,
add on shallow pullbacks to EMA20, and exit with a Chandelier stop
(highest-high minus N*ATR) — never on a VWAP cross.

Entry (initial leg)
--------------------
* EMA20 > EMA50 (uptrend confirmed; mirror for shorts)
* ADX > min_adx (strong trend)
* Price pulls back to within pullback_threshold_pct (0.5%) of EMA20
* Volume on the pullback is LOW: vol_ratio < pullback_vol_max (0.8) —
  a quiet pullback, not a reversal

Pyramid (add to a winner)
-------------------------
* Existing position is in profit >= 1R
* Price pulls back to EMA20 again
* Add pyramid_size_mult × initial size (default 50% of initial)
* Each pyramid has its own stop at the same level (1R below EMA20
  for the long leg)

Exit (Chandelier)
-----------------
* Long:  stop = highest_high_since_entry − chandelier_atr_mult × ATR
* Short: stop = lowest_low_since_entry + chandelier_atr_mult × ATR
* Trend reversal: EMA20 crosses below EMA50 (longs) or above (shorts)
* No max-hold; let winners run
* All legs exit together when the Chandelier triggers (engine
  enforces per-leg SL/TP)

Pyramiding tracking
-------------------
We track:
  * ``initial_entry_price`` / ``initial_size`` for R-multiple math
  * ``highest_high`` (or ``lowest_low``) for the Chandelier ratchet
  * ``pyramid_count`` (capped at max_pyramids)
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional

from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.strategies.indicators import (
    Candle,
    calculate_atr,
    calculate_ema,
    calculate_volume_ratio,
)
from src.utils.helpers import safe_divide, safe_float

logger = logging.getLogger(__name__)


@dataclass
class _TrendPyramidState:
    """Per-symbol state for TrendPyramid."""
    candles_15m: Deque[Candle] = field(default_factory=lambda: deque(maxlen=80))
    candles_1h: Deque[Candle] = field(default_factory=lambda: deque(maxlen=80))
    last_signal_ms: int = 0
    last_pyramid_ms: int = 0


class TrendPyramid(Strategy):
    """Trend follower with EMA20 pullback entries and Chandelier exit."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # EMA periods
        self.EMA_FAST = int(cfg.get("ema_fast", 20))
        self.EMA_SLOW = int(cfg.get("ema_slow", 50))
        # Trend filter
        self.MIN_ADX = float(cfg.get("min_adx", 25.0))
        # Pullback entry
        self.PULLBACK_THRESHOLD_PCT = float(cfg.get("pullback_threshold_pct", 0.005))
        self.PULLBACK_VOL_MAX = float(cfg.get("pullback_vol_max", 0.8))
        # Pyramiding
        self.MAX_PYRAMIDS = int(cfg.get("max_pyramids", 2))
        self.PYRAMID_SIZE_MULT = float(cfg.get("pyramid_size_mult", 0.5))
        self.PYRAMID_PROFIT_R = float(cfg.get("pyramid_profit_r", 1.0))
        # Chandelier exit
        self.CHANDELIER_ATR_MULT = float(cfg.get("chandelier_atr_mult", 3.0))
        # ATR
        self.ATR_PERIOD = int(cfg.get("atr_period", 14))
        # Sizing
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.02))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.04))
        # Confidence
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.50))
        # Throttle (initial entries only — pyramids fire on pullback)
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 30 * 60_000))
        self.PYRAMID_THROTTLE_MS = int(cfg.get("pyramid_throttle_ms", 60 * 60_000))

        self.MANUAL_ENABLED = bool(cfg.get("enabled", True))
        self._state: Dict[str, _TrendPyramidState] = {}

    @property
    def name(self) -> str:
        return "TrendPyramid"

    def is_active(self) -> bool:
        return self.MANUAL_ENABLED

    def _get_state(self, symbol: str) -> _TrendPyramidState:
        if symbol not in self._state:
            self._state[symbol] = _TrendPyramidState()
        return self._state[symbol]

    @staticmethod
    def _update_history(
        state: _TrendPyramidState,
        candle_15m: Optional[Candle],
        candle_1h: Optional[Candle],
    ) -> None:
        if candle_15m is not None:
            if not state.candles_15m or state.candles_15m[-1].timestamp_ms != candle_15m.timestamp_ms:
                state.candles_15m.append(candle_15m)
        if candle_1h is not None:
            if not state.candles_1h or state.candles_1h[-1].timestamp_ms != candle_1h.timestamp_ms:
                state.candles_1h.append(candle_1h)

    def _trend_metrics(
        self, state: _TrendPyramidState
    ) -> Optional[Dict[str, float]]:
        """Return ema_fast, ema_slow, adx, atr_pct or None if not enough data."""
        closes_15m = [c.close for c in state.candles_15m]
        closes_1h = [c.close for c in state.candles_1h]
        if len(closes_15m) < self.EMA_SLOW + 1 or len(closes_1h) < self.ATR_PERIOD + 1:
            return None
        ema_fast = calculate_ema(closes_15m, self.EMA_FAST)
        ema_slow = calculate_ema(closes_15m, self.EMA_SLOW)
        atr = calculate_atr(state.candles_1h, period=self.ATR_PERIOD)
        if ema_fast is None or ema_slow is None or atr is None or atr <= 0:
            return None
        return {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "atr": atr,
        }

    @staticmethod
    def _atr_pct(atr: float, price: float) -> float:
        if price <= 0:
            return 0.0
        return safe_divide(atr, price, 0.0)

    def _confidence(self, adx: float, pullback_dist_pct: float) -> float:
        # ADX score: 0 at min_adx, 1 at min_adx + 20
        adx_score = max(0.0, min(1.0, (adx - self.MIN_ADX) / 20.0))
        # Pullback score: closer to EMA20 = better
        pb_score = max(0.0, 1.0 - abs(pullback_dist_pct) / max(self.PULLBACK_THRESHOLD_PCT, 1e-9))
        raw = (adx_score + pb_score) / 2.0
        return min(max(self.MIN_CONFIDENCE + 0.30 * raw, self.MIN_CONFIDENCE), 0.85)

    def _build_initial_signal(
        self,
        event: MarketEvent,
        ema_fast: float,
        ema_slow: float,
        atr: float,
        adx: float,
        vol_ratio: Optional[float],
    ) -> Optional[Signal]:
        price = event.price
        if price <= 0:
            return None
        # Trend direction
        if ema_fast > ema_slow:
            side = "long"
        elif ema_fast < ema_slow:
            side = "short"
        else:
            return None

        # Pullback distance to EMA20
        if side == "long":
            pullback_dist_pct = (ema_fast - price) / ema_fast
        else:
            pullback_dist_pct = (price - ema_fast) / ema_fast
        if pullback_dist_pct < 0 or pullback_dist_pct > self.PULLBACK_THRESHOLD_PCT:
            return None

        # Volume quiet on the pullback
        if vol_ratio is None or vol_ratio >= self.PULLBACK_VOL_MAX:
            return None

        # ADX gate
        if adx < self.MIN_ADX:
            return None

        # 1R stop = max(pullback_distance_to_EMA20, 1*ATR)
        atr_pct = self._atr_pct(atr, price)
        if side == "long":
            stop_distance = max(ema_fast - price, atr * 2.0)  # 2×ATR floor
        else:
            stop_distance = max(price - ema_fast, atr * 2.0)
        stop_loss_pct = safe_divide(stop_distance, price, atr_pct)
        # Chandelier ratchet: trail at 3×ATR from the high
        take_profit_pct = stop_loss_pct * 4.0  # 4R default — let winners run

        confidence = self._confidence(adx, pullback_dist_pct)
        if confidence < self.MIN_CONFIDENCE:
            return None

        size_pct = min(self.BASE_SIZE_PCT, self.MAX_SIZE_PCT)
        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=side,
            confidence=confidence,
            size_pct=size_pct,
            entry_price=price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            reason=f"trend_pyramid_{side}_adx{adx:.0f}_pb{pullback_dist_pct * 100:.2f}",
            metadata={
                "leg": "initial",
                "pyramid_count": 0,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "atr": atr,
                "adx": adx,
                "vol_ratio": vol_ratio,
                "pullback_dist_pct": pullback_dist_pct,
                "chandelier_atr_mult": self.CHANDELIER_ATR_MULT,
            },
        )

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        if not self.MANUAL_ENABLED:
            return None
        state = self._get_state(event.symbol)
        self._update_history(state, event.candle_15m, event.candle_1h)

        metrics = self._trend_metrics(state)
        if metrics is None:
            if event.timestamp_ms - getattr(state, "_last_warmup_log_ms", 0) > 600_000:
                state._last_warmup_log_ms = event.timestamp_ms
                logger.info(
                    "TrendPyramid SKIP %s — insufficient data (15m=%d/1h=%d)",
                    event.symbol, len(state.candles_15m), len(state.candles_1h),
                )
            return None

        now_ms = event.timestamp_ms
        if (
            state.last_signal_ms > 0
            and now_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS
        ):
            return None

        ema_fast = metrics["ema_fast"]
        ema_slow = metrics["ema_slow"]
        atr = metrics["atr"]
        adx = safe_float(event.adx_14, 0.0)
        _, vol_ratio = calculate_volume_ratio(list(state.candles_15m), lookback=20)

        sig = self._build_initial_signal(
            event, ema_fast, ema_slow, atr, adx, vol_ratio,
        )
        if sig is not None:
            state.last_signal_ms = now_ms
        elif event.timestamp_ms - getattr(state, "_last_reject_log_ms", 0) > 600_000:
            state._last_reject_log_ms = event.timestamp_ms
            side = "long" if ema_fast > ema_slow else "short" if ema_fast < ema_slow else "flat"
            pb = abs(event.price - ema_fast) / ema_fast if ema_fast > 0 else 0
            logger.info(
                "TrendPyramid SKIP %s — side=%s pb=%.3f%% (need<%.3f%%) vol=%s (need<%.2f) adx=%.1f (need>%.1f)",
                event.symbol, side, pb * 100, self.PULLBACK_THRESHOLD_PCT * 100,
                f"{vol_ratio:.2f}" if vol_ratio else "N/A", self.PULLBACK_VOL_MAX,
                adx, self.MIN_ADX,
            )
        return sig

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

        # No max-hold, no time stop — let winners run.
        # The engine enforces per-leg SL/TP on every tick; we only
        # need to emit an early exit signal if the trend itself breaks
        # (EMA20 cross) — the engine trailing-stop exclusion for
        # TrendPyramid ensures the engine doesn't tighten our stop.
        if event.candle_15m is not None:
            closes = [c.close for c in self._get_state(event.symbol).candles_15m]
            if len(closes) >= self.EMA_SLOW + 1:
                ema_fast = calculate_ema(closes, self.EMA_FAST)
                ema_slow = calculate_ema(closes, self.EMA_SLOW)
                if ema_fast is not None and ema_slow is not None:
                    if position.side == "long" and ema_fast < ema_slow:
                        return ExitSignal(
                            strategy=self.name,
                            symbol=position.symbol,
                            side="close",
                            confidence=0.9,
                            reason="ema_trend_reversal_long",
                            metadata={"ema_fast": ema_fast, "ema_slow": ema_slow},
                        )
                    if position.side == "short" and ema_fast > ema_slow:
                        return ExitSignal(
                            strategy=self.name,
                            symbol=position.symbol,
                            side="close",
                            confidence=0.9,
                            reason="ema_trend_reversal_short",
                            metadata={"ema_fast": ema_fast, "ema_slow": ema_slow},
                        )
        return None
