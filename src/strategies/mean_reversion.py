"""Strategy 2: Funding Extreme (Mean Reversion / Contrarian)

Bets against extreme funding rates when the market is overcrowded.
The idea: when funding is extreme and OI is concentrated on one side,
the majority is already positioned that way — reversion is likely.
"""

from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple
import collections

from strategies.base import MarketEvent, Signal, ExitSignal, Position, Strategy
from strategies.indicators import (
    Candle,
    detect_support_resistance,
    calculate_oi_concentration,
    calculate_overcrowded_score,
)


@dataclass
class _MeanRevState:
    """Internal state for mean reversion strategy."""
    candles_1h: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=60))
    last_signal_side: Optional[str] = None
    last_signal_ts: int = 0


class MeanReversion(Strategy):
    """Funding Extreme — contrarian strategy.

    Timeframe: 15m (funding check), 1h (price context).
    Risk: 1% per trade, sized by funding magnitude.
    Max hold: 60 minutes (before next funding payment).
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._state: Dict[str, _MeanRevState] = {}
        cfg = config or {}
        # Thresholds (overridable via config)
        self.FUNDING_EXTREME = cfg.get("extreme_threshold", 0.008)
        self.FUNDING_STRONG = cfg.get("strong_threshold", 0.005)
        self.FUNDING_REVERTED = cfg.get("funding_reverted", 0.003)
        self.OI_CONCENTRATION = cfg.get("overcrowded_oi_pct", 65) / 100.0
        self.MAX_HOLD_MINUTES = cfg.get("max_hold_minutes", 60)
        self.STOP_PCT = cfg.get("stop_loss_pct", 2.0) / 100.0
        self.SR_LOOKBACK = cfg.get("sr_lookback", 30)
        self.MIN_CONFIDENCE_EXTREME = cfg.get("min_confidence_extreme", 0.85)
        self.MIN_CONFIDENCE_STRONG = cfg.get("min_confidence_strong", 0.65)

    @property
    def name(self) -> str:
        return "FundingExtreme"

    def _get_state(self, symbol: str) -> _MeanRevState:
        if symbol not in self._state:
            self._state[symbol] = _MeanRevState()
        return self._state[symbol]

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        """Evaluate contrarian entry on extreme funding + overcrowded OI."""
        state = self._get_state(event.symbol)

        # Update 1h candle history for price context
        if event.candle_1h:
            state.candles_1h.append(event.candle_1h)

        # We need predicted funding (primary) or current funding
        funding = event.predicted_funding
        if funding is None:
            funding = event.funding
        if funding is None:
            return None  # Can't trade without funding data

        # --- Check funding extremity ---
        funding_abs = abs(funding)
        is_extreme = funding_abs >= self.FUNDING_EXTREME
        is_strong = funding_abs >= self.FUNDING_STRONG

        if not is_strong:
            return None  # Not extreme enough for contrarian play

        # Determine expected contrarian side
        # Extreme negative funding → shorts pay longs → overcrowded shorts → go LONG
        # Extreme positive funding → longs pay shorts → overcrowded longs → go SHORT
        if funding < 0:
            target_side = "long"
        else:
            target_side = "short"

        # --- OI concentration check ---
        oi_ratio = self._estimate_oi_ratio(event)
        if oi_ratio is None:
            return None  # Can't assess overcrowding without OI

        # oi_ratio > 0.65 = overcrowded longs; < 0.35 = overcrowded shorts
        if target_side == "long":
            oi_overcrowded = oi_ratio <= (1.0 - self.OI_CONCENTRATION)
        else:
            oi_overcrowded = oi_ratio >= self.OI_CONCENTRATION

        if not oi_overcrowded:
            return None  # OI doesn't confirm overcrowding

        # --- Price context: not in freefall / not parabolic ---
        # Need 1h candles for support/resistance
        candles_1h = list(state.candles_1h)
        if len(candles_1h) < self.SR_LOOKBACK:
            return None  # Need enough price history

        support, resistance = detect_support_resistance(candles_1h, self.SR_LOOKBACK)
        if support is None or resistance is None:
            return None

        current_price = event.price
        # Check we're not entering into a blow-off move
        not_blowoff = self._check_not_blowoff(
            target_side, current_price, candles_1h, support, resistance
        )
        if not not_blowoff:
            return None

        # --- Calculate confidence ---
        if is_extreme:
            base_confidence = 0.85
        elif is_strong:
            base_confidence = 0.65
        else:
            base_confidence = 0.5

        # Boost confidence if OI is very concentrated
        oi_extremity = abs(oi_ratio - 0.5)
        if oi_extremity > 0.2:
            confidence = min(base_confidence + 0.1, 0.95)
        else:
            confidence = base_confidence

        # Minimum confidence threshold
        if confidence < 0.6:
            return None

        # --- Position sizing: risk 1%, scaled by funding magnitude ---
        # More extreme funding = slightly larger position (more conviction)
        risk_pct = 0.01
        if is_extreme:
            risk_pct = 0.015  # 1.5% at extreme

        # Build metadata for transparency
        overcrowded_score = calculate_overcrowded_score(funding, oi_ratio)

        logger.info("MeanReversion %s signal for %s (funding=%.4f, confidence=%.2f)", target_side, event.symbol, funding, confidence)
        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=target_side,
            confidence=confidence,
            size_pct=risk_pct,
            entry_price=current_price,
            stop_loss_pct=self.STOP_PCT,
            take_profit_pct=None,  # Exit on funding reversion
            reason=f"funding_extreme_{target_side}_f{funding:.4f}",
            metadata={
                "funding": event.funding,
                "predicted_funding": event.predicted_funding,
                "oi_ratio": oi_ratio,
                "overcrowded_score": overcrowded_score,
                "support": support,
                "resistance": resistance,
                "is_extreme": is_extreme,
                "is_strong": is_strong,
            },
        )

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Evaluate exit conditions for a contrarian position."""
        state = self._get_state(position.symbol)
        current_price = event.price
        now = event.timestamp_ms

        # 1. Time limit: max 60 minutes (before next funding payment)
        hold_ms = now - position.entry_time_ms
        if hold_ms >= self.MAX_HOLD_MINUTES * 60 * 1000:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=1.0,
                reason="time_limit_funding_window",
                metadata={"hold_minutes": hold_ms / (60 * 1000)},
            )

        # 2. Stop loss: price moved 2% against position
        if position.entry_price > 0:
            move_pct = (current_price - position.entry_price) / position.entry_price
            if position.side == "long" and move_pct <= -self.STOP_PCT:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=1.0,
                    reason="stop_loss_price_2pct",
                    metadata={"move_pct": move_pct},
                )
            if position.side == "short" and move_pct >= self.STOP_PCT:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=1.0,
                    reason="stop_loss_price_2pct",
                    metadata={"move_pct": move_pct},
                )

        # 3. Funding reverted to < ±0.3%
        funding = event.predicted_funding if event.predicted_funding is not None else event.funding
        if funding is not None and abs(funding) < self.FUNDING_REVERTED:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.9,
                reason="funding_reverted",
                metadata={"funding": funding, "threshold": self.FUNDING_REVERTED},
            )

        # 4. OI starts normalizing (crowd is leaving)
        oi_ratio = self._estimate_oi_ratio(event)
        if oi_ratio is not None:
            # Was it very concentrated? Check if it's now back toward 0.5
            # For long position: we entered when shorts were crowded (oi_ratio low).
            # Exit when OI ratio returns above ~0.4 (normalizing).
            # For short position: we entered when longs were crowded (oi_ratio high).
            # Exit when OI ratio returns below ~0.6.
            if position.side == "long" and oi_ratio > 0.4:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.7,
                    reason="oi_normalizing_after_shorts",
                    metadata={"oi_ratio": oi_ratio},
                )
            if position.side == "short" and oi_ratio < 0.6:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.7,
                    reason="oi_normalizing_after_longs",
                    metadata={"oi_ratio": oi_ratio},
                )

        return None

    def _estimate_oi_ratio(self, event: MarketEvent) -> Optional[float]:
        """Estimate long/short ratio from available OI data.

        Priority:
        1. Use oi_total + oi_delta + price direction for proxy estimate
        2. Fallback to calculate_oi_concentration if we have history
        """
        if event.oi_total is None:
            return None

        # If we have oi_delta, we can estimate direction from price + OI change
        # Price up + OI up = longs entering → long ratio higher
        # Price down + OI up = shorts entering → short ratio higher
        if event.oi_delta is not None:
            # This is a rough heuristic. In a real system, the exchange API
            # might provide direct long/short OI split.
            oi_total = event.oi_total
            oi_delta = event.oi_delta

            if oi_delta > 0:
                # OI increasing: new positions being opened
                # Assume the direction of new positions matches recent price move
                if event.candle_15m:
                    price_change = event.candle_15m.close - event.candle_15m.open
                    if price_change > 0:
                        return 0.65  # Longs dominant
                    elif price_change < 0:
                        return 0.35  # Shorts dominant
                # No candle data — neutral with mild long bias (crypto tends long)
                return 0.55
            elif oi_delta < 0:
                # OI decreasing: positions being closed
                # If price is up and OI down = shorts covering (shorts were dominant)
                if event.candle_15m:
                    price_change = event.candle_15m.close - event.candle_15m.open
                    if price_change > 0:
                        return 0.35  # Shorts were dominant, now covering
                    elif price_change < 0:
                        return 0.65  # Longs were dominant, now selling
                return 0.5

        # No delta — return neutral
        return 0.5

    def _check_not_blowoff(
        self,
        target_side: str,
        current_price: float,
        candles_1h: List[Candle],
        support: float,
        resistance: float,
    ) -> bool:
        """Check that price is not in a parabolic/freefall state.

        - For LONG entry: price should be near support, not in freefall
        - For SHORT entry: price should be near resistance, not parabolic
        Also checks recent 1h momentum isn't extreme.
        """
        if len(candles_1h) < 3:
            return False

        # Price proximity to support/resistance
        range_size = resistance - support
        if range_size <= 0:
            range_size = current_price * 0.05  # 5% fallback

        if target_side == "long":
            dist_to_support = current_price - support
            # Price should be within 1.5x ATR-equivalent of support
            # (roughly: within ~15% of the range from support)
            near_support = dist_to_support <= range_size * 0.25
            if not near_support:
                return False

            # Check not in freefall: last 3 1h candles shouldn't all be strong down
            recent = candles_1h[-3:]
            down_candles = sum(
                1 for c in recent if (c.close - c.open) / (c.open or 1e-9) < -0.005
            )
            if down_candles >= 3:
                return False  # Three consecutive down hours = likely freefall

        elif target_side == "short":
            dist_to_resistance = resistance - current_price
            near_resistance = dist_to_resistance <= range_size * 0.25
            if not near_resistance:
                return False

            # Check not parabolic: last 3 1h candles shouldn't all be strong up
            recent = candles_1h[-3:]
            up_candles = sum(
                1 for c in recent if (c.close - c.open) / (c.open or 1e-9) > 0.005
            )
            if up_candles >= 3:
                return False  # Three consecutive up hours = likely parabolic

        return True
