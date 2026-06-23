"""Strategy: RangeGrid — ping-pong limit orders at support / resistance.

Edge
----
In confirmed ranging markets (ADX below a band threshold, Bollinger
width stable) crypto spends the majority of time oscillating between
established support and resistance. We place maker limit orders at the
band edges and exit at the opposite edge with a stop beyond the band
— a mean-reversion grid with 2:1 R:R and explicit invalidation.

Unlike a pure Bollinger-band fade (which fades every bar that touches
the band), this strategy only enters when:

  * ADX(14) < max_adx (confirmed range, not a transition)
  * Bollinger width percentile is in the bottom bb_width_percentile_max
    of its recent history (low volatility — the range is mature, not
    about to expand)
  * detect_support_resistance returns valid support / resistance
  * Price is within the band by a configurable buffer

This is a *conservative* range strategy: it waits for S/R confirmation
before firing, so signal frequency is much lower than a BB-fade.

Exits
-----
  * TP at the opposite band (resistance - offset for longs)
  * SL beyond the band (support - 1% for longs)
  * Max hold 4h (force exit if range breaks)
  * Min R:R = 2:1

Maker routing
-------------
The signal uses ``order_type='limit_maker'`` so the execution layer
picks up the maker fee tier (0.01% vs 0.035% taker). Limit price is
set just inside the band offset (long: support - 0.3%).
"""

from __future__ import annotations

import collections
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.strategies.indicators import (
    Candle,
    calculate_adx,
    calculate_bollinger_bands,
    detect_support_resistance,
)
from src.utils.helpers import safe_divide, safe_float

logger = logging.getLogger(__name__)


@dataclass
class _RangeGridState:
    """Per-symbol state for the RangeGrid strategy."""
    candles_15m: Deque[Candle] = field(default_factory=lambda: deque(maxlen=120))
    bb_width_history: Deque[float] = field(default_factory=lambda: deque(maxlen=100))
    last_signal_ms: int = 0


class RangeGrid(Strategy):
    """Range-bound maker grid: limit orders at S/R, exit at the opposite band."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # Range filters
        self.MAX_ADX = float(cfg.get("max_adx", 18.0))
        self.BB_PERIOD = int(cfg.get("bb_period", 20))
        self.BB_STD = float(cfg.get("bb_std", 2.0))
        self.BB_WIDTH_PERCENTILE_MAX = float(cfg.get("bb_width_percentile_max", 40.0))
        self.SR_LOOKBACK = int(cfg.get("sr_lookback", 50))
        self.MAX_BAND_WIDTH_PCT = float(cfg.get("max_band_width_pct", 0.05))
        # Order placement
        self.BAND_OFFSET_PCT = float(cfg.get("band_offset_pct", 0.003))
        self.STOP_OFFSET_PCT = float(cfg.get("stop_offset_pct", 0.01))
        # Risk
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 4.0))
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        # Sizing
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.015))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.025))
        # Confidence
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.50))
        # Throttle
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 10 * 60_000))

        self.MANUAL_ENABLED = bool(cfg.get("enabled", True))
        self._state: Dict[str, _RangeGridState] = {}

    @property
    def name(self) -> str:
        return "RangeGrid"

    def is_active(self) -> bool:
        return self.MANUAL_ENABLED

    def _get_state(self, symbol: str) -> _RangeGridState:
        if symbol not in self._state:
            self._state[symbol] = _RangeGridState()
        return self._state[symbol]

    @staticmethod
    def _update_candle_history(
        state: _RangeGridState,
        candle: Optional[Candle],
    ) -> None:
        if candle is None:
            return
        if state.candles_15m and state.candles_15m[-1].timestamp_ms == candle.timestamp_ms:
            return
        state.candles_15m.append(candle)

    def _bb_width_pct(self, closes: List[float]) -> Optional[float]:
        lower, middle, upper = calculate_bollinger_bands(
            closes, period=self.BB_PERIOD, std=self.BB_STD,
        )
        if lower is None or middle is None or middle <= 0:
            return None
        return (upper - lower) / middle

    @staticmethod
    def _percentile_rank(history: Deque[float], value: float) -> Optional[float]:
        """Return the percentile rank of *value* within *history* (0..100)."""
        if not history or value is None:
            return None
        below = sum(1 for v in history if v < value)
        return (below / len(history)) * 100.0

    def _confidence(
        self,
        adx: float,
        bb_pct_rank: float,
        band_width_pct: float,
    ) -> float:
        """0.5..0.85 confidence based on range strength and band tightness."""
        # ADX score: 0.0 at MAX_ADX, 1.0 at MAX_ADX - 10 (deeper range)
        adx_score = max(0.0, min(1.0, (self.MAX_ADX - adx) / 10.0))
        # BB percentile score: 1.0 at 0% (tightest), 0.0 at threshold
        bb_score = max(
            0.0,
            min(1.0, (self.BB_WIDTH_PERCENTILE_MAX - bb_pct_rank) / self.BB_WIDTH_PERCENTILE_MAX),
        )
        # Band width score: tighter is better
        bw_score = max(0.0, min(1.0, 1.0 - band_width_pct / self.MAX_BAND_WIDTH_PCT))
        raw = (adx_score + bb_score + bw_score) / 3.0
        return min(max(self.MIN_CONFIDENCE + 0.20 * raw, self.MIN_CONFIDENCE), 0.85)

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        if not self.MANUAL_ENABLED:
            return None

        state = self._get_state(event.symbol)
        self._update_candle_history(state, event.candle_15m)

        candles_15m = list(state.candles_15m)
        if len(candles_15m) < max(self.SR_LOOKBACK + 3, self.BB_PERIOD + 1):
            return None

        now_ms = event.timestamp_ms
        if (
            state.last_signal_ms > 0
            and now_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS
        ):
            return None

        # --- 1. ADX filter (must be in range regime) ---
        adx = event.adx_14
        if adx is None:
            adx = calculate_adx(candles_15m, period=14) or 0.0
        if adx >= self.MAX_ADX:
            return None

        # --- 2. Bollinger width filter (low-vol regime) ---
        closes = [c.close for c in candles_15m]
        bb_width = self._bb_width_pct(closes)
        if bb_width is None:
            return None
        state.bb_width_history.append(bb_width)
        bb_pct_rank = self._percentile_rank(state.bb_width_history, bb_width)
        if bb_pct_rank is None or bb_pct_rank > self.BB_WIDTH_PERCENTILE_MAX:
            return None

        # --- 3. Support / resistance (causal) ---
        support, resistance = detect_support_resistance(
            candles_15m, lookback=self.SR_LOOKBACK,
        )
        if support is None or resistance is None or resistance <= support:
            return None
        mid = (support + resistance) / 2.0
        band_width_pct = (resistance - support) / mid
        if band_width_pct > self.MAX_BAND_WIDTH_PCT:
            return None

        # --- 4. Determine side from price ---
        price = event.price
        long_limit = support * (1.0 - self.BAND_OFFSET_PCT)
        short_limit = resistance * (1.0 + self.BAND_OFFSET_PCT)
        if price <= long_limit:
            side = "long"
        elif price >= short_limit:
            side = "short"
        else:
            return None  # Price is in the middle — no edge

        # --- 5. Compute stop and TP from the band ---
        if side == "long":
            entry_target = long_limit
            stop = support * (1.0 - self.STOP_OFFSET_PCT)
            tp = resistance * (1.0 - self.BAND_OFFSET_PCT)
        else:
            entry_target = short_limit
            stop = resistance * (1.0 + self.STOP_OFFSET_PCT)
            tp = support * (1.0 + self.BAND_OFFSET_PCT)

        risk = abs(entry_target - stop) / entry_target
        reward = abs(tp - entry_target) / entry_target
        if risk <= 0 or reward <= 0:
            return None
        rr = safe_divide(reward, risk, 0.0)
        if rr < 2.0:
            return None

        # --- 6. Confidence ---
        confidence = self._confidence(adx, bb_pct_rank, band_width_pct)
        if confidence < self.MIN_CONFIDENCE:
            return None

        size_pct = min(self.BASE_SIZE_PCT, self.MAX_SIZE_PCT)
        state.last_signal_ms = now_ms

        logger.info(
            "RangeGrid %s signal %s — entry=%.2f stop=%.2f tp=%.2f adx=%.1f "
            "bb_pct=%.0f bw=%.4f R=%.2f conf=%.2f",
            event.symbol, side, entry_target, stop, tp, adx,
            bb_pct_rank, band_width_pct, rr, confidence,
        )

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=side,
            confidence=confidence,
            size_pct=size_pct,
            entry_price=entry_target,
            stop_loss_pct=risk,
            take_profit_pct=reward,
            reason=f"range_grid_{side}_adx{adx:.0f}_R{rr:.1f}",
            metadata={
                "support": support,
                "resistance": resistance,
                "adx": adx,
                "bb_width_pct": bb_width,
                "bb_pct_rank": bb_pct_rank,
                "band_width_pct": band_width_pct,
                "rr": rr,
                "order_type": "limit_maker",
                "limit_price": entry_target,
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

        # 1. Time exit
        hold_ms = event.timestamp_ms - position.entry_time_ms
        if hold_ms >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.85,
                reason=f"max_hold_{self.MAX_HOLD_HOURS:g}h",
                metadata={"hold_ms": hold_ms},
            )

        # 2. SL / TP via position-level stop_loss_price / take_profit_price
        # (set by execution engine from stop_loss_pct / take_profit_pct).
        # The strategy-level on_position is mostly for time exit; price
        # SL/TP are enforced by the engine on every tick.
        return None
