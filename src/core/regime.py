"""Shared ADX regime weighting for live engine and backtest."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.strategies.base import Signal


def regime_strategy_name(sig: Signal) -> str:
    """Resolve sub-strategy name for ADX regime weighting."""
    meta = sig.metadata or {}
    if sig.strategy == "StrategyEnsemble":
        original = meta.get("original_strategy")
        if original:
            return str(original)
        agreeing = meta.get("strategies_agreeing")
        if agreeing:
            return str(agreeing[0])
    return sig.strategy


def apply_regime_weights(
    signals: List[Signal],
    adx: Optional[float],
    regime_weights: Dict[str, Dict[str, float]],
    adx_trend_threshold: float = 25.0,
    adx_range_threshold: float = 20.0,
) -> List[Signal]:
    """Adjust signal confidence based on ADX regime."""
    if adx is None or not signals:
        return signals

    if adx > adx_trend_threshold:
        weights = regime_weights.get("trend", {})
    elif adx < adx_range_threshold:
        weights = regime_weights.get("range", {})
    else:
        return signals

    adjusted: List[Signal] = []
    for sig in signals:
        name = regime_strategy_name(sig)
        mult = weights.get(name, 1.0)
        if mult == 1.0:
            adjusted.append(sig)
            continue
        adjusted.append(
            Signal(
                strategy=sig.strategy,
                symbol=sig.symbol,
                side=sig.side,
                confidence=min(sig.confidence * mult, 1.0),
                size_pct=sig.size_pct,
                entry_price=sig.entry_price,
                stop_loss_pct=sig.stop_loss_pct,
                take_profit_pct=sig.take_profit_pct,
                reason=sig.reason,
                metadata={
                    **(sig.metadata or {}),
                    "regime_multiplier": mult,
                    "confidence_raw": sig.confidence,
                },
            )
        )
    return adjusted
