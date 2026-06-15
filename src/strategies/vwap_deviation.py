"""Strategy 3.2: VWAP Deviation

Mean reversion based on price deviation from VWAP(1h):
  - Price > 2.5σ ABOVE VWAP + volume > 150% average → SHORT
  - Price > 2.5σ BELOW VWAP + volume > 150% average → LONG
  - Rationale: extreme deviations from VWAP tend to revert

Logic:
  1. Maintain 1h candle history per symbol
  2. Every tick, calculate VWAP(24h) + Z-score of current price
  3. If |Z| > threshold and volume surge > 1.5x:
     - Signal in the OPPOSITE direction (mean reversion)
  4. Exit when price crosses VWAP (Z changes sign) or max hold

Filters:
  - ADX < 25 (only trade in non-trending markets — VWAP deviation works
    better in range-bound conditions)
  - OIR confirms the deviation direction (e.g., OIR > 0.6 confirms price
    is pushed by one side, making reversion more likely)
  - Funding not extreme in the SAME direction as the signal

Timeframe: 1h candles for VWAP, 15m for confirmation.
Max hold: 4 hours (mean reversion is quick or it's wrong).
"""

from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple
import collections
import logging

from src.strategies.base import MarketEvent, Signal, ExitSignal, Position, Strategy
from src.strategies.indicators import (
    Candle,
    calculate_vwap_zscore,
    calculate_volume_ratio,
    calculate_atr,
)

logger = logging.getLogger(__name__)


@dataclass
class _VWAPDevState:
    """Per-symbol state for VWAP deviation tracking."""
    candles_1h: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=48))
    last_signal_ms: int = 0


class VWAPDeviation(Strategy):
    """VWAP Deviation — mean reversion when price strays too far from VWAP.

    Scans for extreme price deviations from the 24h VWAP,
    combined with volume confirmation. Fades the move.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # Z-score threshold
        self.Z_THRESHOLD = cfg.get("z_threshold", 2.5)
        # Volume confirmation
        self.VOLUME_SURGE = cfg.get("volume_surge", 1.5)  # 150% of avg
        # ADX filter: only trade when ADX < 25 (non-trending)
        self.MAX_ADX = cfg.get("max_adx", 25.0)
        # OIR filter: require OIR to confirm the move direction
        self.REQUIRE_OIR_CONFIRM = cfg.get("require_oir_confirm", True)
        self.OIR_THRESHOLD = cfg.get("oir_threshold", 0.4)
        # Funding filter: avoid if funding is extreme in signal direction
        self.MAX_FUNDING_OPPOSITE = cfg.get("max_funding_opposite", 0.005)
        # Position sizing
        self.BASE_SIZE_PCT = cfg.get("base_size_pct", 0.01)
        self.MAX_SIZE_PCT = cfg.get("max_size_pct", 0.03)
        # Risk
        self.STOP_ATR_MULT = cfg.get("stop_loss_atr_multiplier", 2.0)
        self.MAX_HOLD_HOURS = cfg.get("max_hold_hours", 4)
        self.MAX_HOLD_MS = self.MAX_HOLD_HOURS * 3_600_000
        # Confidence
        self.MIN_CONFIDENCE = cfg.get("min_confidence", 0.65)
        self.CONFIDENCE_EXTREME = cfg.get("confidence_extreme", 0.85)
        # Throttle
        self.SIGNAL_THROTTLE_MS = cfg.get("signal_throttle_ms", 300_000)  # 5 min
        self.TAKE_PROFIT_R_MULT = float(cfg.get("take_profit_r_multiple", 2.0))

        self._state: Dict[str, _VWAPDevState] = {}

    @property
    def name(self) -> str:
        return "VWAPDeviation"

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        """Evaluate VWAP deviation opportunity."""
        state = self._get_state(event.symbol)

        # Update candles (deduplicate by timestamp)
        if event.candle_1h and (not state.candles_1h or state.candles_1h[-1].timestamp_ms != event.candle_1h.timestamp_ms):
            state.candles_1h.append(event.candle_1h)

        # Throttle signals
        if event.timestamp_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS:
            return None

        # Need 1h candles
        candles_1h = list(state.candles_1h)
        if len(candles_1h) < 24:
            # Periodic warm-up logging
            if event.timestamp_ms - getattr(state, "_last_warmup_log_ms", 0) > 300_000:
                state._last_warmup_log_ms = event.timestamp_ms
                logger.info(
                    "VWAPDeviation %s WARM-UP: %d/24 1h candles collected",
                    event.symbol, len(candles_1h),
                )
            return None  # Need 24h of 1h data

        # --- Calculate VWAP + Z-score ---
        vwap, stddev, zscore = calculate_vwap_zscore(
            candles_1h, event.price, lookback=24,
        )
        if vwap is None or zscore is None:
            return None

        # --- Check volume surge ---
        _, vol_ratio = calculate_volume_ratio(candles_1h, lookback=24)
        if vol_ratio is None or vol_ratio < self.VOLUME_SURGE:
            if event.timestamp_ms - getattr(state, "_last_skip_log_ms", 0) > 300_000:
                state._last_skip_log_ms = event.timestamp_ms
                logger.info(
                    "VWAPDeviation %s SKIP — vol_ratio=%s (need >= %.1f)",
                    event.symbol,
                    f"{vol_ratio:.2f}" if vol_ratio is not None else "N/A",
                    self.VOLUME_SURGE,
                )
            return None

        # --- ADX filter (only trade in non-trending markets) ---
        adx = event.adx_14
        if adx is not None and adx > self.MAX_ADX:
            logger.debug(
                "VWAPDeviation SKIP %s — ADX=%.1f > %.1f (trending market)",
                event.symbol, adx, self.MAX_ADX,
            )
            return None

        # --- Determine signal direction based on Z-score ---
        # Z > 2.5 → price way above VWAP → SHORT (mean reversion down)
        # Z < -2.5 → price way below VWAP → LONG (mean reversion up)
        if zscore >= self.Z_THRESHOLD:
            target_side = "short"
            deviation = zscore
        elif zscore <= -self.Z_THRESHOLD:
            target_side = "long"
            deviation = abs(zscore)
        else:
            # Throttled logging for debugging silent strategies
            if event.timestamp_ms - getattr(state, "_last_no_signal_log_ms", 0) > 300_000:
                state._last_no_signal_log_ms = event.timestamp_ms
                logger.info(
                    "VWAPDeviation %s NO SIGNAL — zscore=%.2f (threshold=%.2f), adx=%s, vol_ratio=%s",
                    event.symbol, zscore, self.Z_THRESHOLD,
                    f"{adx:.1f}" if adx is not None else "N/A",
                    f"{vol_ratio:.2f}" if vol_ratio is not None else "N/A",
                )
            return None  # Not extreme enough

        # --- OIR filter: confirm the move is one-sided ---
        if self.REQUIRE_OIR_CONFIRM:
            oir = event.orderbook_oir
            if oir is not None:
                if target_side == "short" and oir < self.OIR_THRESHOLD:
                    # We want to short, but OIR shows weak buying pressure
                    # (the move up might not be one-sided enough)
                    logger.debug(
                        "VWAPDeviation SKIP %s short — OIR=%.2f < %.2f",
                        event.symbol, oir, self.OIR_THRESHOLD,
                    )
                    return None
                if target_side == "long" and oir > -self.OIR_THRESHOLD:
                    # We want to long, but OIR shows weak selling pressure
                    logger.debug(
                        "VWAPDeviation SKIP %s long — OIR=%.2f > %.2f",
                        event.symbol, oir, -self.OIR_THRESHOLD,
                    )
                    return None

        # --- Funding filter: avoid if funding extreme in opposite direction ---
        # If we're going LONG, funding should not be very negative (shorts pay)
        # If we're going SHORT, funding should not be very positive (longs pay)
        funding = event.predicted_funding or event.funding
        if funding is not None:
            if target_side == "long" and funding < -self.MAX_FUNDING_OPPOSITE:
                logger.info(
                    "VWAPDeviation SKIP %s long — funding=%.4f too negative "
                    "(shorts pay, crowd already short)",
                    event.symbol, funding,
                )
                return None
            if target_side == "short" and funding > self.MAX_FUNDING_OPPOSITE:
                logger.info(
                    "VWAPDeviation SKIP %s short — funding=%.4f too positive "
                    "(longs pay, crowd already long)",
                    event.symbol, funding,
                )
                return None

        # --- Calculate ATR for stop loss ---
        atr = calculate_atr(candles_1h[-30:], 14) if len(candles_1h) >= 30 else None
        if atr is not None and event.price > 0:
            stop_loss_pct = (atr * self.STOP_ATR_MULT) / event.price
        else:
            stop_loss_pct = 0.02  # 2% default

        # --- Confidence based on deviation magnitude ---
        # Z=2.5 → 0.65, Z=3.5 → 0.85, Z=4.0 → 0.95
        confidence = min(
            self.MIN_CONFIDENCE + (deviation - self.Z_THRESHOLD) * 0.15,
            self.CONFIDENCE_EXTREME,
        )
        confidence = max(confidence, self.MIN_CONFIDENCE)

        # --- Size: increase with deviation confidence ---
        size_pct = min(
            self.BASE_SIZE_PCT * (1.0 + (deviation - self.Z_THRESHOLD) * 0.3),
            self.MAX_SIZE_PCT,
        )

        # Record signal time
        state.last_signal_ms = event.timestamp_ms

        logger.info(
            "VWAPDeviation %s signal for %s — "
            "price=%.2f, vwap=%.2f, zscore=%.2fσ, vol_ratio=%.1fx, "
            "confidence=%.2f, size=%.2f%%",
            target_side, event.symbol, event.price, vwap, zscore,
            vol_ratio, confidence, size_pct * 100,
        )

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=target_side,
            confidence=confidence,
            size_pct=size_pct,
            entry_price=event.price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=stop_loss_pct * self.TAKE_PROFIT_R_MULT,
            reason=f"vwap_dev_{target_side}_z{zscore:.1f}_v{vol_ratio:.1f}",
            metadata={
                "vwap": vwap,
                "zscore": zscore,
                "stddev": stddev,
                "vol_ratio": vol_ratio,
                "adx": adx,
                "oir": event.orderbook_oir,
                "funding": funding,
                "atr": atr,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_r": self.TAKE_PROFIT_R_MULT,
            },
        )

    # ------------------------------------------------------------------
    # Exit logic
    # ------------------------------------------------------------------

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Exit when price reverts to VWAP or max hold reached."""
        state = self._get_state(position.symbol)
        candles_1h = list(state.candles_1h)
        if len(candles_1h) < 24:
            return None

        # Time-based exit
        hold_time = event.timestamp_ms - position.entry_time_ms
        if hold_time >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.8,
                reason=f"max_hold_{self.MAX_HOLD_HOURS}h_reached",
            )

        # VWAP reversion exit
        vwap, _, zscore = calculate_vwap_zscore(
            candles_1h, event.price, lookback=24,
        )
        if vwap is None or zscore is None:
            return None

        # Exit when price crosses VWAP (Z changes sign from entry)
        # If we entered SHORT (Z > 2.5), exit when Z < 0 (price below VWAP)
        # If we entered LONG (Z < -2.5), exit when Z > 0 (price above VWAP)
        if position.side == "short" and zscore < 0:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.75,
                reason=f"vwap_cross_below_z{zscore:.1f}",
            )
        if position.side == "long" and zscore > 0:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.75,
                reason=f"vwap_cross_above_z{zscore:.1f}",
            )

        # Exit when deviation normalizes (|Z| < 1.0)
        if abs(zscore) < 1.0:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.7,
                reason=f"deviation_normalized_z{zscore:.1f}",
            )

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_state(self, symbol: str) -> _VWAPDevState:
        if symbol not in self._state:
            self._state[symbol] = _VWAPDevState()
        return self._state[symbol]

    def on_candle(self, candle: Candle, symbol: str) -> None:
        """Callback for completed candles."""
        state = self._get_state(symbol)
        state.candles_1h.append(candle)
