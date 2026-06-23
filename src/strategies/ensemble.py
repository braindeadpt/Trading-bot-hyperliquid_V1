"""
Professional Ensemble Strategy for Hyperliquid Trading Bot.

Combines multiple sub-strategies using weighted scoring to produce
higher-quality entry signals. Only generates a signal when the combined
confidence score exceeds a configurable threshold.

Inspired by: CTA (Commodity Trading Advisor) multi-factor models.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.strategies.base import MarketEvent, Signal
from src.strategies.base import ExitSignal, Position
from src.utils.helpers import safe_float, safe_divide

logger = logging.getLogger(__name__)


@dataclass
class StrategyWeight:
    """Configuration for a sub-strategy's weight in the ensemble."""
    name: str
    weight: float  # 0.0 - 1.0
    min_confidence: float = 0.0  # minimum confidence from this strategy to count


# v3.1.18: classify every sub-strategy by the type of edge it trades
# so the ensemble can detect when all "agreeing" signals are really
# the same market event counted 3x (e.g. SmartMoneyFlow + Donchian +
# VolatilityBreakout all fire on a single breakout candle).
STRATEGY_CLASS: Dict[str, str] = {
    "SmartMoneyFlow":     "trend",
    "VolatilityBreakout": "trend",
    "DonchianBreakout":   "trend",
    "VWAPDeviation":      "revert",
    "CVDOrderFlow":       "revert",
    "LiquidationCatcher": "revert",
    "FundingArbitrage":   "carry",
    "LeadLag":            "carry",
    "OrderBookScalper":   "micro",
}


class StrategyEnsemble:
    """Combines signals from multiple strategies into a single ensemble decision.

    Usage:
        ensemble = StrategyEnsemble(
            strategies=[liquidation_catcher, trend_follow, ...],
            weights=[
                StrategyWeight("LiquidationCatcher", 0.40),
                StrategyWeight("TrendFollow", 0.30),
                StrategyWeight("MeanReversion", 0.15),
                StrategyWeight("VWAPDeviation", 0.15),
            ],
            threshold=0.65,  # minimum combined score to enter
        )
        signal = ensemble.on_market_event(event)
    """

    def __init__(
        self,
        strategies: List,
        weights: List[StrategyWeight],
        threshold: float = 0.65,
        min_strategies_agreeing: int = 2,
        high_conviction_threshold: float = 0.80,
        high_conviction_enabled: bool = True,
        high_conviction_exclude: Optional[List[str]] = None,
    ):
        self._strategies = {s.name: s for s in strategies}
        self._weights = {w.name: w for w in weights}
        self._threshold = threshold
        self._min_strategies_agreeing = min_strategies_agreeing
        self._high_conviction_threshold = high_conviction_threshold
        self._high_conviction_enabled = high_conviction_enabled
        self._high_conviction_exclude = frozenset(high_conviction_exclude or ())
        self._last_decision_log: List[Dict] = []
        self._governor: Optional[Any] = None

    def set_governor(self, governor: Any) -> None:
        """Attach rolling performance governor (Phase 3)."""
        self._governor = governor

    @property
    def name(self) -> str:
        return "StrategyEnsemble"

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        """Alias for on_market_event — engine calls on_data, not on_market_event."""
        return self.on_market_event(event)

    def on_market_event(self, event: MarketEvent) -> Optional[Signal]:
        """Evaluate all sub-strategies and return an ensemble signal if threshold met."""
        signals: List[Signal] = []
        scores: Dict[str, float] = {}

        # Collect signals from all sub-strategies
        for name, strategy in self._strategies.items():
            if self._governor is not None and not self._governor.is_enabled(name):
                logger.debug("Ensemble: %s — disabled by strategy governor", name)
                continue
            if hasattr(strategy, "is_active") and not strategy.is_active():
                logger.debug("Ensemble: %s — dormant (auto-enable)", name)
                continue
            try:
                # Sub-strategies use on_data, not on_market_event
                sig = strategy.on_data(event)
                if sig is not None:
                    weight_cfg = self._weights.get(name)
                    if weight_cfg and sig.confidence >= weight_cfg.min_confidence:
                        weighted_score = sig.confidence * weight_cfg.weight
                        scores[name] = weighted_score
                        signals.append(sig)
                        logger.debug(
                            "Ensemble: %s signal | side=%s conf=%.2f weight=%.2f score=%.3f",
                            name, sig.side, sig.confidence, weight_cfg.weight, weighted_score
                        )
                    else:
                        logger.debug(
                            "Ensemble: %s signal below min_confidence (%.2f < %.2f)",
                            name, sig.confidence, weight_cfg.min_confidence if weight_cfg else 0
                        )
                else:
                    logger.debug("Ensemble: %s — no signal", name)
            except Exception:
                logger.exception("Ensemble: %s strategy crashed", name)
                continue

        if not signals:
            return None

        # --- Ensemble scoring ---
        # 1. Directional consensus: at least N strategies must agree on side
        long_score = sum(scores.get(s.strategy, 0) for s in signals if s.side == "long")
        short_score = sum(scores.get(s.strategy, 0) for s in signals if s.side == "short")

        long_count = sum(1 for s in signals if s.side == "long")
        short_count = sum(1 for s in signals if s.side == "short")

        logger.info(
            "Ensemble scores for %s: long=%.3f (%d strategies) | short=%.3f (%d strategies) | threshold=%.2f",
            event.symbol, long_score, long_count, short_score, short_count, self._threshold
        )

        # Determine winning side (without threshold check yet)
        if long_score > short_score:
            winning_side = "long"
            winning_score = long_score
            winning_count = long_count
        elif short_score > long_score:
            winning_side = "short"
            winning_score = short_score
            winning_count = short_count
        else:
            logger.info(
                "Ensemble NO SIGNAL %s: no clear side (long=%.3f short=%.3f)",
                event.symbol, long_score, short_score
            )
            return None

        agreeing_signals = [s for s in signals if s.side == winning_side]

        # v3.1.18: cross-class agreement gate. A single breakout candle
        # can fire SmartMoneyFlow + DonchianBreakout + VolatilityBreakout
        # simultaneously — they are 1 market event counted 3x, not 3
        # independent votes. Require the agreeing set to span at least
        # two different strategy classes (e.g. trend + revert) OR a
        # single strategy to clear the high-conviction bypass.
        agreeing_classes = {
            STRATEGY_CLASS.get(s.strategy, "other")
            for s in agreeing_signals
        }
        if len(agreeing_classes) < 2 and winning_count >= 2:
            # Same-class multi-strategy agreement is correlated noise,
            # not independent confirmation. Reject unless bypassed below.
            logger.info(
                "Ensemble NO SIGNAL %s | path=rejected | reason=single_class_agreement | "
                "agreeing=%d | classes=%s | strategies=%s",
                event.symbol,
                winning_count,
                sorted(agreeing_classes),
                [s.strategy for s in agreeing_signals],
            )
            # High-conviction bypass can still rescue a single high-
            # confidence signal. The winning_count >= 2 branch here
            # means the bypass is not applicable (we have multiple
            # strategies but all in one class) — return None.
            return None

        # High-conviction bypass: single strategy with raw confidence >= threshold
        single_sig = agreeing_signals[0] if len(agreeing_signals) == 1 else None
        high_conviction = (
            self._high_conviction_enabled
            and winning_count == 1
            and single_sig is not None
            and single_sig.confidence >= self._high_conviction_threshold
            and single_sig.strategy not in self._high_conviction_exclude
        )
        if high_conviction:
            single_sig = agreeing_signals[0]
            logger.info(
                "Ensemble DECISION %s %s | path=high_conviction | agreeing=1 | "
                "score=%.3f | hc_threshold=%.2f | strategy=%s conf=%.2f",
                event.symbol,
                winning_side,
                winning_score,
                self._high_conviction_threshold,
                single_sig.strategy,
                single_sig.confidence,
            )
            signal = Signal(
                strategy=self.name,
                symbol=event.symbol,
                side=winning_side,
                confidence=single_sig.confidence,
                size_pct=single_sig.size_pct,
                entry_price=event.price,
                stop_loss_pct=single_sig.stop_loss_pct,
                take_profit_pct=single_sig.take_profit_pct,
                reason=f"Ensemble [HighConviction] [{single_sig.strategy}]: {single_sig.reason}",
                metadata={**single_sig.metadata, "ensemble_score": winning_score, "high_conviction_bypass": True, "original_strategy": single_sig.strategy},
            )
            logger.info(
                "Ensemble SIGNAL %s %s | path=high_conviction | score=%.3f | conf=%.2f | size=%.2f%% | strategy=%s",
                event.symbol, winning_side, winning_score, signal.confidence,
                signal.size_pct * 100, single_sig.strategy,
            )
            return signal

        # Normal threshold check
        if winning_score < self._threshold:
            logger.info(
                "Ensemble NO SIGNAL %s | path=rejected | reason=below_threshold | "
                "agreeing=%d | score=%.3f | threshold=%.2f | min_agreeing=%d",
                event.symbol,
                winning_count,
                winning_score,
                self._threshold,
                self._min_strategies_agreeing,
            )
            return None

        # Require minimum number of strategies agreeing
        if winning_count < self._min_strategies_agreeing:
            logger.info(
                "Ensemble NO SIGNAL %s | path=rejected | reason=insufficient_agreement | "
                "agreeing=%d | need=%d | score=%.3f | threshold=%.2f",
                event.symbol,
                winning_count,
                self._min_strategies_agreeing,
                winning_score,
                self._threshold,
            )
            return None

        # Average confidence
        avg_confidence = sum(s.confidence for s in agreeing_signals) / len(agreeing_signals)

        # Use the most conservative (smallest) size_pct
        min_size_pct = min(s.size_pct for s in agreeing_signals)

        # Use highest-confidence contributor's stop/TP to preserve R:R.
        # v3.1.16 C2 fix: previously the ensemble took max(stop_loss) (widest
        # stop) and min(take_profit) (tightest TP) — the WORST R:R combination
        # of any contributor. If each strategy offered 2R, the ensemble would
        # deliver ~1R, guaranteeing negative expectancy after fees. Use the
        # highest-confidence contributor's full stop/TP pair instead.
        agreeing_sorted = sorted(agreeing_signals, key=lambda s: s.confidence, reverse=True)
        primary = agreeing_sorted[0]
        primary_stop = primary.stop_loss_pct if primary.stop_loss_pct is not None else 0.02
        primary_tp = primary.take_profit_pct

        # Combine reasons
        reasons = " | ".join(f"{s.strategy}: {s.reason}" for s in agreeing_signals)

        # Combine metadata
        combined_meta: Dict = {}
        for s in agreeing_signals:
            combined_meta.update(s.metadata)
        combined_meta["ensemble_score"] = winning_score
        combined_meta["strategies_agreeing"] = [s.strategy for s in agreeing_signals]

        logger.info(
            "Ensemble DECISION %s %s | path=confluence | agreeing=%d | need=%d | "
            "score=%.3f | threshold=%.2f | conf=%.2f | strategies=%s",
            event.symbol,
            winning_side,
            winning_count,
            self._min_strategies_agreeing,
            winning_score,
            self._threshold,
            avg_confidence,
            [s.strategy for s in agreeing_signals],
        )

        signal = Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=winning_side,
            confidence=avg_confidence,
            size_pct=min_size_pct,
            entry_price=event.price,
            stop_loss_pct=primary_stop,
            take_profit_pct=primary_tp,
            reason=f"Ensemble [{winning_score:.2f}]: {reasons}",
            metadata=combined_meta,
        )

        logger.info(
            "Ensemble SIGNAL %s %s | path=confluence | score=%.3f | conf=%.2f | size=%.2f%% | strategies=%s",
            event.symbol, winning_side, winning_score, avg_confidence,
            min_size_pct * 100,
            [s.strategy for s in agreeing_signals],
        )

        return signal

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Engine entry point for exit signals.

        Delegates to the sub-strategy that opened the position.
        """
        strategy_name = position.metadata.get("strategy", "unknown")
        if strategy_name == self.name:
            return self._on_ensemble_position(position, event)
        strategy = self._strategies.get(strategy_name)
        if strategy and hasattr(strategy, 'on_position'):
            return strategy.on_position(position, event)
        return None

    def _on_ensemble_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Evaluate exits for an ensemble-opened position.

        Delegates to the original sub-strategy that triggered the entry,
        falling back to the ensemble flip check.
        """
        meta = position.metadata or {}
        original = meta.get("original_strategy") or meta.get("sub_strategy")
        agreeing = meta.get("strategies_agreeing", [])

        # Try original sub-strategy first
        candidates = []
        if original and original in self._strategies:
            candidates.append(original)
        candidates.extend(agreeing)

        # If no candidates known (e.g. restored from DB without metadata),
        # try ALL sub-strategies so positions can still be closed.
        if not candidates:
            candidates = list(self._strategies.keys())

        for name in candidates:
            sub = self._strategies.get(name)
            if sub and hasattr(sub, "on_position"):
                try:
                    exit_sig = sub.on_position(position, event)
                    if exit_sig is not None:
                        return exit_sig
                except Exception:
                    logger.exception("Sub-strategy %s exit error", name)

        # Fallback: ensemble flip check
        sig = self.on_market_event(event)
        if sig and sig.side != position.side:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=sig.confidence,
                reason=f"Ensemble flipped: {sig.reason}",
            )
        return None

    def on_position_update(self, event: MarketEvent, position: "Position") -> Optional["ExitSignal"]:
        """Deprecated — kept for backward compatibility."""
        return self.on_position(position, event)
