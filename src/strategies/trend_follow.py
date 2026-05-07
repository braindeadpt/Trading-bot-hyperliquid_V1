"""Strategy 1: Smart Money Flow (Trend Following)

Rides directional momentum confirmed by volume, open interest, and
microstructure. Avoids entering when funding is extreme (crowded).
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple
import collections
import time

from src.strategies.base import MarketEvent, Signal, ExitSignal, Position, Strategy
from src.strategies.indicators import (
    Candle,
    calculate_vwap,
    calculate_ema,
    calculate_atr,
    calculate_volume_profile,
    calculate_realized_volatility,
    volatility_target_size,
    calculate_rsi,
)

logger = logging.getLogger(__name__)


@dataclass
class _TrendState:
    """Internal state for trend following calculations."""
    candles_15m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=40))
    candles_5m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=40))
    candles_1h: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=500))  # 20+ days for vol
    last_vwap: Optional[float] = None
    last_ema20: Optional[float] = None
    last_volume_avg: Optional[float] = None
    last_atr: Optional[float] = None
    last_oi: Optional[float] = None
    last_realized_vol: Optional[float] = None
    # Track if we already signaled long/short to avoid spam on same candle
    last_signal_side: Optional[str] = None
    last_signal_ts: int = 0


class TrendFollow(Strategy):
    """Smart Money Flow — trend following with volume + OI confirmation.

    Timeframe: 15m primary, 5m confirmation.
    Risk: 1% per trade, sized by ATR.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._state: Dict[str, _TrendState] = {}
        cfg = config or {}
        # Constants (overridable via config)
        self.MAX_HOLD_HOURS = cfg.get("max_hold_hours", 4)
        self.STOP_ATR_MULT = cfg.get("stop_loss_atr_multiplier", 2.0)
        self.VOLUME_SURGE = cfg.get("volume_surge_multiplier", 1.5)
        self.VOLUME_LOOKBACK = cfg.get("volume_lookback", 20)
        self.EMA_PERIOD = cfg.get("ema_period", 20)
        self.ATR_PERIOD = cfg.get("atr_period", 14)
        self.FUNDING_EXTREME = cfg.get("extreme_threshold", 0.008)
        self.IMBALANCE_THRESHOLD = cfg.get("imbalance_threshold", 0.02)
        self.OVERCROWDED_PENALTY = cfg.get("overcrowded_penalty", 0.2)
        # Microstructure filters (Task 2.2)
        self.OIR_LONG_THRESHOLD = cfg.get("oir_long_threshold", 0.6)      # OIR > 0.6 confirms long
        self.OIR_SHORT_THRESHOLD = cfg.get("oir_short_threshold", -0.6)  # OIR < -0.6 confirms short
        self.WALL_PROXIMITY_PCT = cfg.get("wall_proximity_pct", 0.005)    # avoid walls within 0.5%
        self.RSI_MIN = cfg.get("rsi_min", 40.0)                           # no long if RSI < 40
        self.RSI_MAX = cfg.get("rsi_max", 70.0)                           # no short if RSI > 70
        # Volatility targeting
        self.TARGET_VOL_ANNUAL = cfg.get("target_vol_annual", 0.20)  # 20% target volatility
        self.VOLATILITY_PERIOD = cfg.get("volatility_period", 480)   # 480 1h candles = 20 days
        self.VOL_MIN_MULT = cfg.get("vol_min_mult", 0.25)            # min 0.25x base size
        self.VOL_MAX_MULT = cfg.get("vol_max_mult", 3.0)             # max 3.0x base size
        self.BASE_SIZE_PCT = cfg.get("base_size_pct", 0.01)          # 1% base

    @property
    def name(self) -> str:
        return "SmartMoneyFlow"

    def _get_state(self, symbol: str) -> _TrendState:
        if symbol not in self._state:
            self._state[symbol] = _TrendState()
        return self._state[symbol]

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        """Evaluate entry conditions on every market event."""
        state = self._get_state(event.symbol)

        # Update candle histories
        if event.candle_15m:
            state.candles_15m.append(event.candle_15m)
        if event.candle_5m:
            state.candles_5m.append(event.candle_5m)
        if event.candle_1h:
            state.candles_1h.append(event.candle_1h)

        # Calculate realized volatility from 1h candles (20 days)
        if len(state.candles_1h) >= self.VOLATILITY_PERIOD + 1:
            rv = calculate_realized_volatility(list(state.candles_1h), self.VOLATILITY_PERIOD)
            state.last_realized_vol = rv

        # Need at least enough 15m candles for indicators
        if len(state.candles_15m) < self.EMA_PERIOD:
            return None

        candles_15m = list(state.candles_15m)
        closes = [c.close for c in candles_15m]
        current_price = event.price

        # --- Calculate indicators ---
        vwap = calculate_vwap(candles_15m)
        ema20 = calculate_ema(closes, self.EMA_PERIOD)
        atr = calculate_atr(candles_15m, self.ATR_PERIOD)

        # Volume average (last 20 candles, excluding current)
        if len(candles_15m) >= self.VOLUME_LOOKBACK + 1:
            vol_history = [c.volume for c in candles_15m[-(self.VOLUME_LOOKBACK + 1):-1]]
            volume_avg = sum(vol_history) / len(vol_history)
        else:
            volume_avg = None

        # Save for exit logic
        state.last_vwap = vwap
        state.last_ema20 = ema20
        state.last_volume_avg = volume_avg
        state.last_atr = atr
        if event.oi_total is not None:
            state.last_oi = event.oi_total

        # Need all key indicators
        if vwap is None or ema20 is None or atr is None or volume_avg is None:
            return None
        if atr == 0.0:
            return None  # Can't size a position without volatility

        current_candle = candles_15m[-1]
        current_volume = current_candle.volume

        # --- Funding check: avoid entering against extreme funding ---
        funding = event.funding
        predicted_funding = event.predicted_funding
        # Use predicted if available, else current
        funding_effective = predicted_funding if predicted_funding is not None else funding

        # OI delta: smart money entering?
        oi_increasing = False
        if event.oi_delta is not None:
            oi_increasing = event.oi_delta > 0.0
        elif len(candles_15m) >= 2:
            # Derive from candle OI if available
            oi_series = [c.open_interest for c in candles_15m[-5:] if c.open_interest is not None]
            if len(oi_series) >= 2 and oi_series[-1] > oi_series[0]:
                oi_increasing = True

        # Bid/ask imbalance
        imbalance = event.bid_ask_imbalance
        buying_pressure = imbalance is not None and imbalance > self.IMBALANCE_THRESHOLD
        selling_pressure = imbalance is not None and imbalance < -self.IMBALANCE_THRESHOLD
        imbalance_present = imbalance is not None

        # --- Volume surge check ---
        volume_surge = current_volume > (volume_avg * self.VOLUME_SURGE)

        # --- Microstructure filters (Task 2.2) ---
        # 1. RSI(14) — avoid overbought/oversold
        rsi = calculate_rsi(closes, 14)
        rsi_ok_long = rsi is not None and rsi >= self.RSI_MIN
        rsi_ok_short = rsi is not None and rsi <= self.RSI_MAX

        # 2. OIR filter — orderbook imbalance ratio confirms direction
        oir = event.orderbook_oir
        oir_confirms_long = oir is not None and oir > self.OIR_LONG_THRESHOLD
        oir_confirms_short = oir is not None and oir < self.OIR_SHORT_THRESHOLD
        oir_present = oir is not None

        # 3. Wall detection — avoid entering near large walls
        wall_blocks_long = False
        wall_blocks_short = False
        if current_price > 0:
            if event.orderbook_largest_ask_wall is not None:
                # Long: avoid if large ask wall is just above price (resistance)
                ask_wall_dist = abs(event.orderbook_largest_ask_wall - current_price) / current_price
                if ask_wall_dist < self.WALL_PROXIMITY_PCT:
                    wall_blocks_long = True
            if event.orderbook_largest_bid_wall is not None:
                # Short: avoid if large bid wall is just below price (support)
                bid_wall_dist = abs(current_price - event.orderbook_largest_bid_wall) / current_price
                if bid_wall_dist < self.WALL_PROXIMITY_PCT:
                    wall_blocks_short = True

        # --- LONG entry conditions ---
        long_conditions: List[Tuple[str, bool]] = []
        long_conditions.append(("price_above_vwap", current_price > vwap))
        long_conditions.append(("price_above_ema20", current_price > ema20))
        long_conditions.append(("volume_surge", volume_surge))
        long_conditions.append(("oi_increasing", oi_increasing))
        # Funding not extreme negative (not overcrowded short)
        not_overcrowded_short = (
            funding_effective is None or funding_effective > -self.FUNDING_EXTREME
        )
        long_conditions.append(("not_overcrowded_short", not_overcrowded_short))
        # Buying pressure in imbalance OR we don't have imbalance data
        long_conditions.append(
            ("buying_pressure", buying_pressure or not imbalance_present)
        )
        # OIR confirms long OR no OIR data
        long_conditions.append(
            ("oir_confirms_long", oir_confirms_long or not oir_present)
        )
        # RSI not oversold
        long_conditions.append(("rsi_ok", rsi_ok_long))
        # No ask wall blocking just above
        long_conditions.append(("no_wall_blocking", not wall_blocks_long))

        # --- SHORT entry conditions ---
        short_conditions: List[Tuple[str, bool]] = []
        short_conditions.append(("price_below_vwap", current_price < vwap))
        short_conditions.append(("price_below_ema20", current_price < ema20))
        short_conditions.append(("volume_surge", volume_surge))
        short_conditions.append(("oi_increasing", oi_increasing))
        not_overcrowded_long = (
            funding_effective is None or funding_effective < self.FUNDING_EXTREME
        )
        short_conditions.append(("not_overcrowded_long", not_overcrowded_long))
        short_conditions.append(
            ("selling_pressure", selling_pressure or not imbalance_present)
        )
        # OIR confirms short OR no OIR data
        short_conditions.append(
            ("oir_confirms_short", oir_confirms_short or not oir_present)
        )
        # RSI not overbought
        short_conditions.append(("rsi_ok", rsi_ok_short))
        # No bid wall blocking just below
        short_conditions.append(("no_wall_blocking", not wall_blocks_short))

        # --- Evaluate confluence and generate signal ---
        long_met = sum(1 for _, v in long_conditions if v)
        short_met = sum(1 for _, v in short_conditions if v)
        total_conditions = len(long_conditions)  # same for both

        # Minimum 6 of 9 conditions for entry (raised bar with microstructure filters)
        MIN_CONFLUENCE = 6

        signal: Optional[Signal] = None

        if long_met >= MIN_CONFLUENCE and long_met > short_met:
            confidence = self._calculate_confidence(long_conditions)
            signal = self._build_signal(event, "long", confidence, atr, long_conditions)
            state.last_signal_side = "long"
            state.last_signal_ts = event.timestamp_ms
            logger.info("TrendFollow LONG signal for %s (confidence=%.2f)", event.symbol, confidence)

        elif short_met >= MIN_CONFLUENCE and short_met > long_met:
            confidence = self._calculate_confidence(short_conditions)
            signal = self._build_signal(event, "short", confidence, atr, short_conditions)
            state.last_signal_side = "short"
            state.last_signal_ts = event.timestamp_ms
            logger.info("TrendFollow SHORT signal for %s (confidence=%.2f)", event.symbol, confidence)

        return signal

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Evaluate exit conditions for an open position."""
        state = self._get_state(position.symbol)
        current_price = event.price
        now = event.timestamp_ms

        # 1. Time limit: max hold 4 hours
        hold_ms = now - position.entry_time_ms
        if hold_ms >= self.MAX_HOLD_HOURS * 3600 * 1000:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=1.0,
                reason="time_limit_max_hold",
                metadata={"hold_hours": hold_ms / (3600 * 1000)},
            )

        # 2. Stop loss: 2x ATR from entry
        if position.stop_loss_price is not None:
            if position.side == "long" and current_price <= position.stop_loss_price:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=1.0,
                    reason="stop_loss_atr",
                    metadata={"stop_price": position.stop_loss_price},
                )
            if position.side == "short" and current_price >= position.stop_loss_price:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=1.0,
                    reason="stop_loss_atr",
                    metadata={"stop_price": position.stop_loss_price},
                )

        # 3. Price crosses back over VWAP (opposite direction)
        vwap = state.last_vwap
        if vwap is not None:
            if position.side == "long" and current_price < vwap:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.85,
                    reason="vwap_reversal_long",
                    metadata={"vwap": vwap, "price": current_price},
                )
            if position.side == "short" and current_price > vwap:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.85,
                    reason="vwap_reversal_short",
                    metadata={"vwap": vwap, "price": current_price},
                )

        # 4. Funding flips extreme against position
        funding = event.predicted_funding if event.predicted_funding is not None else event.funding
        if funding is not None:
            if position.side == "long" and funding < -self.FUNDING_EXTREME:
                # Funding went extremely negative — shorts are now overcrowded,
                # but we are long and getting paid. Actually, extreme negative funding
                # means shorts pay longs, which is *good* for longs. But per spec:
                # "Funding flips extreme against position" means funding goes extreme
                # in the direction that hurts us. For longs, that's extreme POSITIVE.
                pass  # Will check below
            if position.side == "long" and funding > self.FUNDING_EXTREME:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.9,
                    reason="funding_extreme_against_long",
                    metadata={"funding": funding},
                )
            if position.side == "short" and funding < -self.FUNDING_EXTREME:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.9,
                    reason="funding_extreme_against_short",
                    metadata={"funding": funding},
                )

        return None

    def _calculate_confidence(self, conditions: List[Tuple[str, bool]]) -> float:
        """Map confluence (number of met conditions) to confidence 0.5-1.0.

        6/9 met → 0.50
        7/9 met → 0.75
        8/9 met → 1.00
        """
        met = sum(1 for _, v in conditions if v)
        total = len(conditions)
        if met < 6:
            return 0.0
        # Linear interpolation: 6 → 0.5, 8 → 1.0
        confidence = 0.5 + 0.25 * (met - 6)
        return min(confidence, 1.0)

    def _build_signal(
        self,
        event: MarketEvent,
        side: str,
        confidence: float,
        atr: float,
        conditions: List[Tuple[str, bool]],
    ) -> Signal:
        """Construct a Signal with proper sizing and stop loss."""
        state = self._get_state(event.symbol)

        # --- Volatility targeting sizing ---
        # Base size adjusted by realized volatility
        rv = state.last_realized_vol
        if rv is not None and rv > 0:
            risk_pct = volatility_target_size(
                base_size_pct=self.BASE_SIZE_PCT,
                realized_vol_annual=rv,
                target_vol_annual=self.TARGET_VOL_ANNUAL,
                min_size_mult=self.VOL_MIN_MULT,
                max_size_mult=self.VOL_MAX_MULT,
            )
        else:
            risk_pct = self.BASE_SIZE_PCT  # fallback to base size

        # Stop loss: 2x ATR from entry (as a percentage of price)
        if event.price > 0:
            stop_loss_pct = (self.STOP_ATR_MULT * atr) / event.price
        else:
            stop_loss_pct = 0.02  # fallback 2%

        met_reasons = [name for name, v in conditions if v]
        unmet_reasons = [name for name, v in conditions if not v]

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=side,
            confidence=confidence,
            size_pct=risk_pct,
            entry_price=event.price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=None,  # Trend follow rides until exit rule hits
            reason=f"trend_{side}_" + "_".join(met_reasons),
            metadata={
                "met_conditions": met_reasons,
                "unmet_conditions": unmet_reasons,
                "vwap": event.vwap_15m,
                "atr": atr,
                "volume_surge": self.VOLUME_SURGE,
                "funding": event.funding,
                "predicted_funding": event.predicted_funding,
                "oi_delta": event.oi_delta,
                "imbalance": event.bid_ask_imbalance,
                "realized_vol_annual": rv,
                "size_pct": risk_pct,
                "rsi": event.rsi_14,
                "oir": event.orderbook_oir,
                "ask_wall": event.orderbook_largest_ask_wall,
                "bid_wall": event.orderbook_largest_bid_wall,
            },
        )
