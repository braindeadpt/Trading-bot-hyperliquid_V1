"""Shared strategy construction for live trading and backtests."""

from __future__ import annotations

from typing import Any, List

from src.strategies.base import Strategy
from src.strategies.ensemble import StrategyEnsemble, StrategyWeight
from src.strategies.funding_arbitrage import FundingArbitrage
from src.strategies.liquidation_catcher import LiquidationCatcher
from src.strategies.mean_reversion import MeanReversion
from src.strategies.orderbook_scalper import OrderBookScalper
from src.strategies.trend_follow import TrendFollow
from src.strategies.vwap_deviation import VWAPDeviation

_STRATEGY_REGISTRY = (
    ("strategy.trend_follow", TrendFollow),
    ("strategy.mean_reversion", MeanReversion),
    ("strategy.funding_arbitrage", FundingArbitrage),
    ("strategy.vwap_deviation", VWAPDeviation),
    ("strategy.liquidation_catcher", LiquidationCatcher),
    ("strategy.orderbook_scalper", OrderBookScalper),
)


def default_ensemble_weights() -> List[StrategyWeight]:
    """Default ensemble weight table."""
    return [
        StrategyWeight("SmartMoneyFlow", 0.20, min_confidence=0.40),
        StrategyWeight("FundingExtreme", 0.20, min_confidence=0.40),
        StrategyWeight("VWAPDeviation", 0.15, min_confidence=0.40),
        StrategyWeight("FundingArbitrage", 0.10, min_confidence=0.35),
        StrategyWeight("LiquidationCatcher", 0.15, min_confidence=0.40),
        StrategyWeight("OrderBookScalper", 0.10, min_confidence=0.50),
    ]


def _should_load_strategy(section: dict) -> bool:
    """Load strategy if manually enabled or configured for auto-enable."""
    if section.get("enabled", True):
        return True
    if section.get("auto_enable", False):
        return True
    return False


def build_sub_strategies(cfg: Any) -> List[Strategy]:
    """Instantiate enabled (or auto-enable) sub-strategies from config."""
    strategies: List[Strategy] = []
    for path, cls in _STRATEGY_REGISTRY:
        section = cfg.get(path, {}) or {}
        if _should_load_strategy(section):
            strategies.append(cls(section))
    return strategies


def build_ensemble(cfg: Any) -> StrategyEnsemble:
    """Build StrategyEnsemble with weights renormalized for enabled strategies."""
    subs = build_sub_strategies(cfg)
    enabled_names = {s.name for s in subs}
    active_weights = [w for w in default_ensemble_weights() if w.name in enabled_names]
    total = sum(w.weight for w in active_weights)
    if total > 0:
        active_weights = [
            StrategyWeight(w.name, w.weight / total, w.min_confidence)
            for w in active_weights
        ]

    return StrategyEnsemble(
        strategies=subs,
        weights=active_weights,
        threshold=float(cfg.get("strategy.ensemble.threshold", 0.40)),
        min_strategies_agreeing=int(cfg.get("strategy.ensemble.min_agreeing", 1)),
        high_conviction_threshold=float(
            cfg.get("strategy.ensemble.high_conviction_threshold", 0.70)
        ),
    )
