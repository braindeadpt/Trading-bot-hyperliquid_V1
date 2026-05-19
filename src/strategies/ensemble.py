"""
Professional Ensemble Strategy for Hyperliquid Trading Bot.

Combines multiple sub-strategies using weighted scoring to produce
higher-quality entry signals. Only generates a signal when the combined
confidence score exceeds a configurable threshold.

Inspired by: CTA (Commodity Trading Advisor) multi-factor models.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

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
    ):
        self._strategies = {s.name: s for s in strategies}
        self._weights = {w.name: w for w in weights}
        self._threshold = threshold
        self._min_strategies_agreeing = min_strategies_agreeing
        self._last_decision_log: List[Dict] = []

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

        # Determine winning side
        if long_score > short_score and long_score >= self._threshold:
            winning_side = "long"
            winning_score = long_score
            winning_count = long_count
        elif short_score > long_score and short_score >= self._threshold:
            winning_side = "short"
            winning_score = short_score
            winning_count = short_count
        else:
            logger.info(
                "Ensemble NO SIGNAL %s: best_score=%.3f < threshold=%.2f",
                event.symbol, max(long_score, short_score), self._threshold
            )
            return None

        # Require minimum number of strategies agreeing
        if winning_count < self._min_strategies_agreeing:
            logger.info(
                "Ensemble NO SIGNAL %s: only %d strategy(s) agree, need %d",
                event.symbol, winning_count, self._min_strategies_agreeing
            )
            return None

        # Build composite signal from the agreeing strategies
        agreeing_signals = [s for s in signals if s.side == winning_side]

        # Average confidence
        avg_confidence = sum(s.confidence for s in agreeing_signals) / len(agreeing_signals)

        # Use the most conservative (smallest) size_pct
        min_size_pct = min(s.size_pct for s in agreeing_signals)

        # Use the tightest stop loss (largest stop_loss_pct)
        max_stop = max((s.stop_loss_pct for s in agreeing_signals), default=0.02)

        # Combine reasons
        reasons = " | ".join(f"{s.strategy}: {s.reason}" for s in agreeing_signals)

        # Combine metadata
        combined_meta: Dict = {}
        for s in agreeing_signals:
            combined_meta.update(s.metadata)
        combined_meta["ensemble_score"] = winning_score
        combined_meta["strategies_agreeing"] = [s.strategy for s in agreeing_signals]

        signal = Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=winning_side,
            confidence=avg_confidence,
            size_pct=min_size_pct,
            entry_price=event.price,
            stop_loss_pct=max_stop,
            take_profit_pct=min((s.take_profit_pct for s in agreeing_signals if s.take_profit_pct is not None), default=None),  # most conservative TP
            reason=f"Ensemble [{winning_score:.2f}]: {reasons}",
            metadata=combined_meta,
        )

        logger.info(
            "Ensemble SIGNAL %s %s | score=%.3f | conf=%.2f | size=%.2f%% | strategies=%s",
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
        """Evaluate exits for an ensemble-opened position."""
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
