"""Strategy 2: Funding Extreme (Mean Reversion / Contrarian)

Bets against extreme funding rates when the market is overcrowded.
The idea: when funding is extreme and OI is concentrated on one side,
the majority is already positioned that way — reversion is likely.
"""

from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple
import collections
import logging

from src.exchanges.funding_normalize import (
    funding_data_usable,
    is_valid_funding,
    resolve_effective_funding,
)
from src.utils.helpers import safe_float
from src.strategies.base import MarketEvent, Signal, ExitSignal, Position, Strategy
from src.strategies.indicators import (
    Candle,
    calculate_atr,
    calculate_realized_volatility,
    volatility_target_size,
    detect_support_resistance,
    calculate_oi_concentration,
    calculate_overcrowded_score,
)

logger = logging.getLogger(__name__)


@dataclass
class _MeanRevState:
    """Internal state for mean reversion strategy."""
    candles_1h: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=60))
    candles_15m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=40))
    # Rolling funding history for dynamic percentile calculation
    funding_history: Deque[float] = field(default_factory=lambda: collections.deque(maxlen=500))
    predicted_funding_history: Deque[float] = field(default_factory=lambda: collections.deque(maxlen=500))
    last_atr: Optional[float] = None
    last_signal_side: Optional[str] = None
    last_signal_ts: int = 0
    last_funding_hist_ms: int = 0
    last_exit_ms: int = 0


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
        self.FUNDING_EXTREME = cfg.get("extreme_threshold", 0.0003)
        self.FUNDING_STRONG = cfg.get("strong_threshold", 0.0002)
        self.FUNDING_REVERTED = cfg.get("funding_reverted", 0.00002)
        self.FUNDING_REVERT_FRACTION = float(cfg.get("funding_revert_fraction", 0.5))
        self.MIN_FUNDING_EXIT_HOLD_MS = int(cfg.get("min_funding_exit_hold_ms", 900_000))
        self.MIN_PROFIT_BEFORE_FUNDING_EXIT_PCT = float(
            cfg.get("min_profit_before_funding_exit_pct", 0.001)
        )
        self.MIN_STRONG_THRESHOLD_ABS = float(cfg.get("min_strong_threshold_abs", 0.00006))
        self.MIN_EXTREME_THRESHOLD_ABS = float(cfg.get("min_extreme_threshold_abs", 0.00009))
        self.OI_CONCENTRATION = cfg.get("overcrowded_oi_pct", 65) / 100.0
        self.MAX_HOLD_MINUTES = cfg.get("max_hold_minutes", 60)
        # ATR-based stop loss
        self.ATR_PERIOD = cfg.get("atr_period", 14)
        self.STOP_ATR_MULT = cfg.get("stop_loss_atr_multiplier", 2.0)
        # Volatility targeting
        self.TARGET_VOL_ANNUAL = cfg.get("target_vol_annual", 0.20)  # 20% target
        self.VOLATILITY_PERIOD = cfg.get("volatility_period", 480)   # 480 1h candles
        self.VOL_MIN_MULT = cfg.get("vol_min_mult", 0.25)
        self.VOL_MAX_MULT = cfg.get("vol_max_mult", 3.0)
        self.BASE_SIZE_PCT = cfg.get("base_size_pct", 0.01)  # 1% base
        self.TAKE_PROFIT_R_MULT = float(cfg.get("take_profit_r_multiple", 2.0))
        self.SR_LOOKBACK = cfg.get("sr_lookback", 30)
        self.MIN_CONFIDENCE_EXTREME = cfg.get("min_confidence_extreme", 0.85)
        self.MIN_CONFIDENCE_STRONG = cfg.get("min_confidence_strong", 0.65)
        # ── Task 2.3: Dynamic upgrades ──
        self.USE_DYNAMIC_PERCENTILE = cfg.get("use_dynamic_percentile", True)
        self.FUNDING_PERCENTILE_LOOKBACK = cfg.get("funding_percentile_lookback", 90)  # 90 obs ~ 30 days
        self.FUNDING_PERCENTILE = cfg.get("funding_percentile", 90)  # p90
        self.USE_CROSS_EXCHANGE_CONFIRM = cfg.get("use_cross_exchange_confirm", True)
        self.CROSS_EXCHANGE_DEVIATION_MAX = cfg.get("cross_exchange_deviation_max", 0.003)  # 0.3%
        self.REQUIRE_OI_DECREASING = cfg.get("require_oi_decreasing", True)
        self.OI_DELTA_THRESHOLD = cfg.get("oi_delta_threshold", 0.0)  # OI must be decreasing
        self.USE_PREDICTED_PRIMARY = cfg.get("use_predicted_primary", True)
        self.REQUIRE_REAL_OI_RATIO = cfg.get("require_real_oi_ratio", False)
        self.REQUIRE_FEED_HEALTH = bool(cfg.get("require_feed_health", False))
        self.MAX_VENUE_SPREAD = float(cfg.get("max_venue_spread", 0.001))
        self.REENTRY_COOLDOWN_MS = int(cfg.get("reentry_cooldown_ms", 600_000))

    @property
    def name(self) -> str:
        return "FundingExtreme"

    def _get_state(self, symbol: str) -> _MeanRevState:
        if symbol not in self._state:
            self._state[symbol] = _MeanRevState()
        return self._state[symbol]

    def _compute_dynamic_thresholds(
        self,
        state: _MeanRevState,
    ) -> Tuple[float, float]:
        """Compute dynamic funding thresholds from rolling history.

        Returns (extreme_threshold, strong_threshold) based on historical
        funding percentiles. Falls back to fixed config if insufficient history.
        """
        hist = list(state.funding_history)
        if len(hist) < self.FUNDING_PERCENTILE_LOOKBACK // 2:
            # Not enough history — use fixed thresholds
            return self.FUNDING_EXTREME, self.FUNDING_STRONG

        # Use the last N observations for percentile calculation
        sample = hist[-self.FUNDING_PERCENTILE_LOOKBACK:]
        sample_abs = sorted([abs(v) for v in sample])
        n = len(sample_abs)
        # Linear interpolation for percentile
        idx = int((self.FUNDING_PERCENTILE / 100.0) * (n - 1))
        p90 = sample_abs[idx]
        # Strong threshold = 70th percentile of the same sample
        idx70 = int(0.70 * (n - 1))
        p70 = sample_abs[idx70]

        # Sanity caps for low-funding regimes (8h-normalized rates)
        p90 = min(max(p90, self.MIN_EXTREME_THRESHOLD_ABS), 0.02)
        p70 = min(max(p70, self.MIN_STRONG_THRESHOLD_ABS), 0.01)
        if len(hist) < self.FUNDING_PERCENTILE_LOOKBACK // 2:
            p90 = max(p90, self.FUNDING_EXTREME, self.MIN_EXTREME_THRESHOLD_ABS)
            p70 = max(p70, self.FUNDING_STRONG, self.MIN_STRONG_THRESHOLD_ABS)

        return p90, p70

    def _cross_exchange_confirms(
        self,
        event: MarketEvent,
        funding: float,
    ) -> bool:
        """Confirm that funding extremity is not just a HL outlier.

        Checks that cross-exchange average funding is also extreme
        and that HL funding isn't wildly divergent from others.
        """
        if not self.USE_CROSS_EXCHANGE_CONFIRM:
            return True  # skip check

        # Need cross-exchange aggregated data
        avg_funding = event.funding_avg
        if avg_funding is None:
            return True  # no data, pass through

        # HL funding and average should point in the same direction
        if (funding > 0 and avg_funding <= 0) or (funding < 0 and avg_funding >= 0):
            logger.debug(
                "Cross-exchange mismatch: HL funding=%.6f vs avg=%.6f",
                funding, avg_funding,
            )
            return False

        # HL funding shouldn't be more than 0.3% away from cross-exchange average
        deviation = abs(funding - avg_funding)
        if deviation > self.CROSS_EXCHANGE_DEVIATION_MAX:
            logger.debug(
                "HL funding outlier: %.6f vs avg %.6f (deviation %.4f%%)",
                funding, avg_funding, deviation * 100,
            )
            return False

        # Average funding should also be "strong" (same sign, reasonable magnitude)
        if abs(avg_funding) < self.FUNDING_STRONG * 0.5:
            logger.debug(
                "Cross-exchange funding too mild: avg=%.6f", avg_funding,
            )
            return False

        return True

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        """Evaluate contrarian entry on extreme funding + overcrowded OI."""
        state = self._get_state(event.symbol)

        if (
            state.last_exit_ms > 0
            and event.timestamp_ms - state.last_exit_ms < self.REENTRY_COOLDOWN_MS
        ):
            return None

        # Update candle histories (deduplicate by timestamp)
        if event.candle_1h and (not state.candles_1h or state.candles_1h[-1].timestamp_ms != event.candle_1h.timestamp_ms):
            state.candles_1h.append(event.candle_1h)
        if event.candle_15m and (not state.candles_15m or state.candles_15m[-1].timestamp_ms != event.candle_15m.timestamp_ms):
            state.candles_15m.append(event.candle_15m)

        # Calculate ATR from 15m candles for stop sizing
        if len(state.candles_15m) >= self.ATR_PERIOD + 1:
            atr = calculate_atr(list(state.candles_15m)[-self.ATR_PERIOD-1:], self.ATR_PERIOD)
            state.last_atr = atr
        else:
            atr = None
            # Periodic warm-up logging
            n_15m = len(state.candles_15m)
            n_1h = len(state.candles_1h)
            if event.timestamp_ms - getattr(state, "_last_warmup_log_ms", 0) > 300_000:
                state._last_warmup_log_ms = event.timestamp_ms
                logger.info(
                    "FundingExtreme %s WARM-UP: %d/%d 15m candles, %d/%d 1h candles",
                    event.symbol, n_15m, self.ATR_PERIOD + 1,
                    n_1h, self.VOLATILITY_PERIOD + 1,
                )

        # Calculate realized volatility from 1h candles (20 days)
        if len(state.candles_1h) >= self.VOLATILITY_PERIOD + 1:
            rv = calculate_realized_volatility(list(state.candles_1h), self.VOLATILITY_PERIOD)
            # state doesn't track last_realized_vol, but we calculate it here
        else:
            rv = None

        if not funding_data_usable(
            event,
            require_feed_health=self.REQUIRE_FEED_HEALTH,
            max_venue_spread=self.MAX_VENUE_SPREAD,
        ):
            return None

        funding = resolve_effective_funding(event, prefer_predicted=self.USE_PREDICTED_PRIMARY)
        if not is_valid_funding(funding):
            return None

        pred_funding = event.predicted_funding

        if event.timestamp_ms != state.last_funding_hist_ms:
            state.last_funding_hist_ms = event.timestamp_ms
            state.funding_history.append(funding)
            if is_valid_funding(pred_funding):
                state.predicted_funding_history.append(pred_funding)

        # Compute dynamic thresholds from history
        extreme_threshold, strong_threshold = self._compute_dynamic_thresholds(state)

        # --- Check funding extremity ---
        funding_abs = abs(funding)
        is_extreme = funding_abs >= extreme_threshold
        is_strong = funding_abs >= strong_threshold

        if not is_strong:
            # Throttled logging for debugging silent strategies
            if event.timestamp_ms - getattr(state, "_last_no_signal_log_ms", 0) > 300_000:
                state._last_no_signal_log_ms = event.timestamp_ms
                logger.info(
                    "FundingExtreme %s NO SIGNAL — funding_abs=%.4f < strong_threshold=%.4f",
                    event.symbol, funding_abs, strong_threshold,
                )
            return None  # Not extreme enough for contrarian play

        # Cross-exchange confirmation (not just a HL outlier)
        if not self._cross_exchange_confirms(event, funding):
            logger.info(
                "FundingExtreme SKIP %s — cross-exchange not confirming (funding=%.6f)",
                event.symbol, funding,
            )
            return None

        # Determine expected contrarian side
        # Extreme negative funding → shorts pay longs → overcrowded shorts → go LONG
        # Extreme positive funding → longs pay shorts → overcrowded longs → go SHORT
        if funding < 0:
            target_side = "long"
        else:
            target_side = "short"

        # --- OI concentration check ---
        oi_ratio, oi_ratio_is_real = self._estimate_oi_ratio(event)
        if oi_ratio is None:
            return None  # Can't assess overcrowding without OI

        # oi_ratio > 0.65 = overcrowded longs; < 0.35 = overcrowded shorts
        if target_side == "long":
            oi_overcrowded = oi_ratio <= (1.0 - self.OI_CONCENTRATION)
        else:
            oi_overcrowded = oi_ratio >= self.OI_CONCENTRATION

        if not oi_overcrowded:
            return None  # OI doesn't confirm overcrowding

        # ── Task 2.3: OI decreasing filter ──
        # Only enter if the crowd is already starting to leave (OI_delta < 0).
        # This means the overheated market is beginning to cool off — better
        # timing for mean reversion than entering while OI is still surging.
        if self.REQUIRE_OI_DECREASING:
            oi_delta = event.oi_delta
            if oi_delta is None:
                logger.debug(
                    "FundingExtreme SKIP %s — no OI_delta available", event.symbol,
                )
                return None
            if oi_delta >= self.OI_DELTA_THRESHOLD:
                logger.info(
                    "FundingExtreme SKIP %s — OI_delta=%.0f not decreasing "
                    "(crowd still entering)",
                    event.symbol, oi_delta,
                )
                return None

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
            base_confidence = self.MIN_CONFIDENCE_EXTREME
        elif is_strong:
            base_confidence = self.MIN_CONFIDENCE_STRONG
        else:
            base_confidence = 0.5

        # Boost confidence if OI is very concentrated
        oi_extremity = abs(oi_ratio - 0.5)
        if oi_extremity > 0.2:
            confidence = min(base_confidence + 0.1, 0.95)
        else:
            confidence = base_confidence

        # Boost if cross-exchange is strongly confirming
        if self.USE_CROSS_EXCHANGE_CONFIRM and event.funding_avg is not None:
            cross_strength = abs(event.funding_avg) / strong_threshold if strong_threshold > 0 else 0
            if cross_strength > 1.2:
                confidence = min(confidence + 0.05, 0.95)

        # Minimum confidence threshold
        if confidence < 0.6:
            return None

        # --- Position sizing: volatility targeting ---
        # Base size adjusted by realized volatility
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

        # More extreme funding = additional scaling (more conviction)
        if is_extreme:
            risk_pct = min(risk_pct * 1.5, self.BASE_SIZE_PCT * self.VOL_MAX_MULT)

        # ATR-based stop loss (replaces fixed 2%)
        if current_price > 0 and atr is not None and atr > 0:
            stop_loss_pct = (self.STOP_ATR_MULT * atr) / current_price
        else:
            stop_loss_pct = 0.02  # fallback 2% if ATR unavailable

        # Build metadata for transparency
        overcrowded_score = calculate_overcrowded_score(funding, oi_ratio)

        logger.info(
            "MeanReversion %s signal for %s "
            "(funding=%.4f, dyn_extreme=%.4f, dyn_strong=%.4f, "
            "confidence=%.2f, size=%.4f%%, atr_stop=%.4f%%)",
            target_side, event.symbol, funding,
            extreme_threshold, strong_threshold,
            confidence, risk_pct * 100, stop_loss_pct * 100,
        )
        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=target_side,
            confidence=confidence,
            size_pct=risk_pct,
            entry_price=current_price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=stop_loss_pct * self.TAKE_PROFIT_R_MULT,
            reason=f"funding_extreme_{target_side}_f{funding:.4f}",
            metadata={
                "entry_funding": funding,
                "funding": event.funding,
                "predicted_funding": event.predicted_funding,
                "funding_avg": event.funding_avg,
                "funding_weighted": event.funding_weighted,
                "oi_ratio": oi_ratio,
                "oi_ratio_is_real": oi_ratio_is_real,
                "oi_delta": event.oi_delta,
                "overcrowded_score": overcrowded_score,
                "support": support,
                "resistance": resistance,
                "is_extreme": is_extreme,
                "is_strong": is_strong,
                "extreme_threshold": extreme_threshold,
                "strong_threshold": strong_threshold,
                "atr": atr,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_r": self.TAKE_PROFIT_R_MULT,
                "realized_vol_annual": rv,
                "size_pct": risk_pct,
            },
        )

    def _mark_exit(self, state: _MeanRevState, now_ms: int) -> None:
        """Record exit time for per-symbol re-entry cooldown."""
        state.last_exit_ms = now_ms

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Evaluate exit conditions for a contrarian position."""
        state = self._get_state(position.symbol)
        current_price = event.price
        now = event.timestamp_ms

        # 1. Time limit: max 60 minutes (before next funding payment)
        hold_ms = now - position.entry_time_ms
        if hold_ms >= self.MAX_HOLD_MINUTES * 60 * 1000:
            self._mark_exit(state, now)
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=1.0,
                reason="time_limit_funding_window",
                metadata={"hold_minutes": hold_ms / (60 * 1000)},
            )

        # 2. Stop loss: ATR-based (replaces fixed 2%)
        # Use the stop_loss_price set at entry (based on ATR), or recalculate from state
        atr = state.last_atr
        if position.stop_loss_price is not None:
            # Entry-calculated ATR stop
            if position.side == "long" and current_price <= position.stop_loss_price:
                self._mark_exit(state, now)
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=1.0,
                    reason="stop_loss_atr",
                    metadata={"stop_price": position.stop_loss_price},
                )
            if position.side == "short" and current_price >= position.stop_loss_price:
                self._mark_exit(state, now)
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=1.0,
                    reason="stop_loss_atr",
                    metadata={"stop_price": position.stop_loss_price},
                )
        elif atr is not None and position.entry_price > 0:
            # Fallback: recalculate from current ATR
            stop_pct = (self.STOP_ATR_MULT * atr) / position.entry_price
            move_pct = (current_price - position.entry_price) / position.entry_price
            if position.side == "long" and move_pct <= -stop_pct:
                self._mark_exit(state, now)
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=1.0,
                    reason="stop_loss_atr_fallback",
                    metadata={"move_pct": move_pct, "atr_stop_pct": stop_pct},
                )
            if position.side == "short" and move_pct >= stop_pct:
                self._mark_exit(state, now)
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=1.0,
                    reason="stop_loss_atr_fallback",
                    metadata={"move_pct": move_pct, "atr_stop_pct": stop_pct},
                )

        # 3. Funding reverted — only after min hold, strong entry, and min profit
        if hold_ms >= self.MIN_FUNDING_EXIT_HOLD_MS:
            funding = resolve_effective_funding(event, prefer_predicted=True)
            entry_funding = safe_float((position.metadata or {}).get("entry_funding"))
            entry_abs = abs(entry_funding) if entry_funding else 0.0
            entry_was_strong = entry_abs >= self.MIN_STRONG_THRESHOLD_ABS

            if entry_was_strong and is_valid_funding(funding):
                revert_level = max(
                    self.FUNDING_REVERTED,
                    entry_abs * self.FUNDING_REVERT_FRACTION,
                )
                funding_reverted = abs(funding) < revert_level
                if funding_reverted:
                    if position.entry_price > 0:
                        if position.side == "long":
                            move_pct = (current_price - position.entry_price) / position.entry_price
                        else:
                            move_pct = (position.entry_price - current_price) / position.entry_price
                    else:
                        move_pct = 0.0
                    if move_pct >= self.MIN_PROFIT_BEFORE_FUNDING_EXIT_PCT:
                        self._mark_exit(state, now)
                        return ExitSignal(
                            strategy=self.name,
                            symbol=position.symbol,
                            side="close",
                            confidence=0.9,
                            reason="funding_reverted",
                            metadata={
                                "funding": funding,
                                "entry_funding": entry_funding,
                                "revert_level": revert_level,
                                "move_pct": move_pct,
                            },
                        )
                    logger.debug(
                        "FundingExtreme %s hold funding exit — move %.4f%% < min %.4f%%",
                        position.symbol,
                        move_pct * 100,
                        self.MIN_PROFIT_BEFORE_FUNDING_EXIT_PCT * 100,
                    )
            elif not entry_was_strong:
                logger.debug(
                    "FundingExtreme %s skip funding exit — |entry_funding|=%.6f < min_strong %.6f",
                    position.symbol,
                    entry_abs,
                    self.MIN_STRONG_THRESHOLD_ABS,
                )

        # 4. OI starts normalizing (crowd is leaving) — real LS ratio only
        oi_ratio, oi_ratio_is_real = self._estimate_oi_ratio(event)
        if oi_ratio is not None and oi_ratio_is_real:
            if position.side == "long" and oi_ratio > 0.4:
                self._mark_exit(state, now)
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.7,
                    reason="oi_normalizing_after_shorts",
                    metadata={"oi_ratio": oi_ratio, "oi_ratio_is_real": True},
                )
            if position.side == "short" and oi_ratio < 0.6:
                self._mark_exit(state, now)
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.7,
                    reason="oi_normalizing_after_longs",
                    metadata={"oi_ratio": oi_ratio, "oi_ratio_is_real": True},
                )

        return None

    def _estimate_oi_ratio(self, event: MarketEvent) -> Tuple[Optional[float], bool]:
        """Estimate long/(long+short) positioning ratio.

        Returns ``(ratio, is_real)`` where ``is_real`` is True only when the
        value comes from ``event.oi_long_ratio`` (Binance global LS ratio).

        Priority:
        1. Binance global long/short account ratio (real data)
        2. Heuristic from OI delta + price direction (fallback, is_real=False)
        """
        if event.oi_long_ratio is not None:
            return float(event.oi_long_ratio), True

        if self.REQUIRE_REAL_OI_RATIO:
            return None, False

        if event.oi_total is None:
            return None, False

        # Heuristic fallback — must not drive oi_normalizing exits
        if event.oi_delta is not None:
            oi_delta = event.oi_delta

            if oi_delta > 0:
                if event.candle_15m:
                    price_change = event.candle_15m.close - event.candle_15m.open
                    if price_change > 0:
                        return 0.65, False
                    elif price_change < 0:
                        return 0.35, False
                return 0.55, False
            elif oi_delta < 0:
                if event.candle_15m:
                    price_change = event.candle_15m.close - event.candle_15m.open
                    if price_change > 0:
                        return 0.35, False
                    elif price_change < 0:
                        return 0.65, False
                return 0.5, False

        return 0.5, False

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
