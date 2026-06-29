"""Strategy 3.3: Liquidation Catcher

Fade liquidation cascades:
  - Monitor $ notional liquidated in 5min windows per symbol
  - If > threshold in one direction (longs or shorts liquidated; default $5M paper):
      → Enter OPPOSITE direction immediately
  - Rationale: liquidation cascades are panic-driven, tend to overshoot
    and revert quickly

Entry:
  - liquidation_notional_5m > threshold (``min_notional_usd`` in config)
  - All from the same side (longs liquidated → price drop → go LONG)
  - OI decreasing (confirms positions were wiped out, not just closed)

Exit:
  - 2R take profit (2x the risk = 2% move)
  - VWAP(1h) reversion
  - Max hold 30min (liquidation cascades resolve quickly)

Risk:
  - Stop: 1% ATR — liquidations can keep cascading
  - Size: small (0.5-1% capital) — this is catching a falling knife
  - Cooldown: 2h after any liquidation catch (avoid chasing)

Note: Requires liquidation data from exchange WebSocket.
  If unavailable, falls back to volume+price-spike detection.
"""

from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple
import collections
import logging

from src.strategies.base import MarketEvent, Signal, ExitSignal, Position, Strategy
from src.strategies.indicators import Candle, calculate_atr, calculate_vwap_zscore

logger = logging.getLogger(__name__)


@dataclass
class _LiqCatcherState:
    """Per-symbol state for liquidation tracking."""
    candles_1h: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=48))
    last_signal_ms: int = 0
    last_liq_notional: float = 0.0
    last_liq_side: Optional[str] = None


class LiquidationCatcher(Strategy):
    """Liquidation Catcher — fade liquidation cascades.

    Enters opposite to large liquidation clusters (configurable USD in 5min),
    betting on panic-driven overshoot reversion.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # Entry thresholds
        self.MIN_NOTIONAL_USD = cfg.get("min_notional_usd", 5_000_000)  # $5M paper default
        self.MIN_LIQUIDATION_COUNT = cfg.get("min_liquidation_count", 10)
        # OI filter
        self.REQUIRE_OI_DECREASING = cfg.get("require_oi_decreasing", True)
        self.OI_DELTA_MAX = cfg.get("oi_delta_max", 0.0)  # OI must decrease
        # ADX filter: avoid in strong trends (liquidations can cascade further)
        self.MAX_ADX = cfg.get("max_adx", 40.0)
        # Position sizing
        self.BASE_SIZE_PCT = cfg.get("base_size_pct", 0.005)  # 0.5% (small, catching knife)
        self.MAX_SIZE_PCT = cfg.get("max_size_pct", 0.01)     # 1% max
        # Risk
        self.STOP_ATR_MULT = cfg.get("stop_loss_atr_multiplier", 1.0)  # tight stop
        self.MAX_HOLD_MINUTES = cfg.get("max_hold_minutes", 60)  # 60min default (was 30)
        self.MAX_HOLD_MS = self.MAX_HOLD_MINUTES * 60_000
        self.TAKE_PROFIT_R = cfg.get("take_profit_r", 2.0)  # 2R = 2x risk
        # Confidence
        self.MIN_CONFIDENCE = cfg.get("min_confidence", 0.70)
        # Throttle
        self.SIGNAL_THROTTLE_MS = cfg.get("signal_throttle_ms", 7_200_000)  # 2h

        self._state: Dict[str, _LiqCatcherState] = {}
        self.REQUIRE_REAL_LIQUIDATION = cfg.get("require_real_liquidation_data", True)
        self.MANUAL_ENABLED = bool(cfg.get("enabled", True))
        self.AUTO_ENABLE = bool(cfg.get("auto_enable", False))
        self._auto_active = self.MANUAL_ENABLED
        self._auto_activate_logged = False

    @property
    def name(self) -> str:
        return "LiquidationCatcher"

    def is_active(self) -> bool:
        """True when the strategy is allowed to generate signals."""
        return self._auto_active

    def _update_auto_activation(self, event: MarketEvent) -> None:
        """Turn on automatically when the engine reports a validated Binance feed."""
        if self._auto_active:
            return
        if self.MANUAL_ENABLED:
            self._auto_active = True
            return
        if not self.AUTO_ENABLE:
            return
        if not event.liquidation_feed_ready:
            return
        self._auto_active = True
        if not self._auto_activate_logged:
            self._auto_activate_logged = True
            logger.info(
                "LiquidationCatcher AUTO-ENABLED — Binance liquidation feed validated "
                "(require_real=%s)",
                self.REQUIRE_REAL_LIQUIDATION,
            )

    def _operational_gate(self, event: MarketEvent) -> bool:
        """Return False if strategy should stay dormant."""
        self._update_auto_activation(event)
        if not self._auto_active:
            return False
        return True

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        """Evaluate liquidation cascade fade opportunity."""
        if not self._operational_gate(event):
            return None

        liq_notional = event.liquidation_notional_5m
        liq_side = event.liquidation_side_5m
        liq_count = event.liquidation_count_5m

        # Check if we have liquidation data
        if liq_notional is None or liq_side is None:
            logger.info(
                "LiquidationCatcher SKIP %s — no liquidation data available",
                event.symbol,
            )
            return None

        if self.REQUIRE_REAL_LIQUIDATION and event.liquidation_data_source != "binance":
            logger.info(
                "LiquidationCatcher SKIP %s — require binance liquidations (source=%s)",
                event.symbol,
                event.liquidation_data_source,
            )
            return None

        # Check minimum notional threshold
        if liq_notional < self.MIN_NOTIONAL_USD:
            return None

        # Check minimum count (avoid single large liquidation)
        if liq_count is not None and liq_count < self.MIN_LIQUIDATION_COUNT:
            logger.info(
                "LiquidationCatcher SKIP %s — count=%d < min=%d",
                event.symbol, liq_count, self.MIN_LIQUIDATION_COUNT,
            )
            return None

        state = self._get_state(event.symbol)

        # Throttle: avoid chasing multiple cascades
        if state.last_signal_ms > 0 and event.timestamp_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS:
            remaining_min = (self.SIGNAL_THROTTLE_MS - (event.timestamp_ms - state.last_signal_ms)) // 60_000
            logger.info(
                "LiquidationCatcher SKIP %s — throttle active (%d min remaining)",
                event.symbol, remaining_min,
            )
            return None

        # --- Determine signal direction (OPPOSITE to liquidated side) ---
        # Longs liquidated → price drops → go LONG
        # Shorts liquidated → price pumps → go SHORT
        if liq_side == "long":
            target_side = "long"
        elif liq_side == "short":
            target_side = "short"
        else:
            return None

        # --- OI filter: confirm positions were actually wiped out ---
        if self.REQUIRE_OI_DECREASING:
            oi_delta = event.oi_delta
            if oi_delta is None:
                logger.debug(
                    "LiquidationCatcher SKIP %s — no OI_delta data", event.symbol,
                )
                return None
            if oi_delta > self.OI_DELTA_MAX:
                logger.info(
                    "LiquidationCatcher SKIP %s — OI_delta=%.0f > %.0f "
                    "(OI still increasing, not a true liquidation)",
                    event.symbol, oi_delta, self.OI_DELTA_MAX,
                )
                return None

        # --- ADX filter: avoid in strong trends ---
        adx = event.adx_14
        if adx is not None and adx > self.MAX_ADX:
            logger.info(
                "LiquidationCatcher SKIP %s — ADX=%.1f > %.1f "
                "(trending market, liquidations may cascade further)",
                event.symbol, adx, self.MAX_ADX,
            )
            return None

        # --- Calculate ATR for stop loss ---
        candles_1h = list(state.candles_1h)
        atr = calculate_atr(candles_1h[-30:], 14) if len(candles_1h) >= 30 else None
        if atr is not None and event.price > 0:
            stop_loss_pct = (atr * self.STOP_ATR_MULT) / event.price
        else:
            stop_loss_pct = 0.01  # 1% default

        # --- Size: scale with liquidation magnitude ---
        # $50M → base size, $100M → max size
        scale = min(liq_notional / self.MIN_NOTIONAL_USD, 2.0)
        size_pct = min(self.BASE_SIZE_PCT * scale, self.MAX_SIZE_PCT)

        # --- Confidence: higher for larger cascades ---
        # $50M → 0.70, $100M → 0.85, $200M → 0.95
        confidence = min(
            self.MIN_CONFIDENCE + (scale - 1.0) * 0.15,
            0.95,
        )
        confidence = max(confidence, self.MIN_CONFIDENCE)

        # --- Record state ---
        state.last_signal_ms = event.timestamp_ms
        state.last_liq_notional = liq_notional
        state.last_liq_side = liq_side

        logger.info(
            "LiquidationCatcher %s signal for %s — "
            "cascade=%s, notional=$%.1fM, count=%d, "
            "confidence=%.2f, size=%.2f%%, stop=%.2f%%",
            target_side, event.symbol, liq_side,
            liq_notional / 1_000_000,
            liq_count or 0,
            confidence, size_pct * 100, stop_loss_pct * 100,
        )

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=target_side,
            confidence=confidence,
            size_pct=size_pct,
            entry_price=event.price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=stop_loss_pct * self.TAKE_PROFIT_R,  # 2R
            reason=f"liq_catch_{liq_side}_${liq_notional/1_000_000:.0f}M",
            metadata={
                "liq_notional": liq_notional,
                "liq_side": liq_side,
                "liq_count": liq_count,
                "adx": adx,
                "oi_delta": event.oi_delta,
                "atr": atr,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_r": self.TAKE_PROFIT_R,
            },
        )

    # ------------------------------------------------------------------
    # Exit logic
    # ------------------------------------------------------------------

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Exit on time, 2R, or VWAP reversion."""
        hold_time = event.timestamp_ms - position.entry_time_ms
        if hold_time >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.8,
                reason=f"max_hold_{self.MAX_HOLD_MINUTES}min",
            )

        # Use the ATR-based stop that was calculated at entry (stored in metadata)
        # Fallback to 1% default if not available
        sl_pct = position.metadata.get("stop_loss_pct", 0.01)
        if sl_pct is None or sl_pct <= 0:
            sl_pct = 0.01

        # 2R take profit check
        entry = position.entry_price
        current = event.price
        if entry > 0 and current > 0:
            tp_pct = sl_pct * self.TAKE_PROFIT_R
            if position.side == "long":
                pnl_pct = (current - entry) / entry
                if pnl_pct >= tp_pct:
                    return ExitSignal(
                        strategy=self.name,
                        symbol=position.symbol,
                        side=position.side,
                        confidence=0.85,
                        reason=f"take_profit_2R_{pnl_pct*100:.2f}%",
                    )
            else:
                pnl_pct = (entry - current) / entry
                if pnl_pct >= tp_pct:
                    return ExitSignal(
                        strategy=self.name,
                        symbol=position.symbol,
                        side=position.side,
                        confidence=0.85,
                        reason=f"take_profit_2R_{pnl_pct*100:.2f}%",
                    )

        # VWAP reversion exit
        state = self._get_state(position.symbol)
        candles_1h = list(state.candles_1h)
        if len(candles_1h) >= 24:
            vwap, _, zscore = calculate_vwap_zscore(candles_1h, current, lookback=24)
            if vwap is not None and zscore is not None:
                # Price reverted to VWAP (|Z| < 0.5)
                if abs(zscore) < 0.5:
                    return ExitSignal(
                        strategy=self.name,
                        symbol=position.symbol,
                        side=position.side,
                        confidence=0.7,
                        reason=f"vwap_revert_z{zscore:.1f}",
                    )

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_state(self, symbol: str) -> _LiqCatcherState:
        if symbol not in self._state:
            self._state[symbol] = _LiqCatcherState()
        return self._state[symbol]

    def on_candle(self, candle: Candle, symbol: str) -> None:
        """Callback for completed candles."""
        state = self._get_state(symbol)
        state.candles_1h.append(candle)
