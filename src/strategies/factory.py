"""Shared strategy construction for live trading and backtests."""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from src.strategies.base import Strategy, MarketEvent, Signal, Position, ExitSignal
from src.strategies.ensemble import StrategyEnsemble, StrategyWeight
from src.strategies.cvd_orderflow import CVDOrderFlow
from src.strategies.donchian_breakout import DonchianBreakout
from src.strategies.funding_arbitrage import FundingArbitrage
from src.strategies.funding_momentum import FundingMomentum
from src.strategies.lead_lag import LeadLag
from src.strategies.liquidation_catcher import LiquidationCatcher
from src.strategies.mean_reversion import MeanReversion
from src.strategies.orderbook_scalper import OrderBookScalper
from src.strategies.range_grid import RangeGrid
from src.strategies.spot_perp_carry import SpotPerpCarry
from src.strategies.trend_follow import TrendFollow
from src.strategies.trend_pyramid import TrendPyramid
from src.strategies.volatility_breakout import VolatilityBreakout
from src.strategies.vwap_deviation import VWAPDeviation

logger = logging.getLogger(__name__)

_STRATEGY_REGISTRY = (
    ("strategy.trend_follow", TrendFollow),
    ("strategy.volatility_breakout", VolatilityBreakout),
    ("strategy.mean_reversion", MeanReversion),
    ("strategy.donchian_breakout", DonchianBreakout),
    ("strategy.funding_arbitrage", FundingArbitrage),
    ("strategy.vwap_deviation", VWAPDeviation),
    ("strategy.liquidation_catcher", LiquidationCatcher),
    ("strategy.orderbook_scalper", OrderBookScalper),
    ("strategy.cvd_orderflow", CVDOrderFlow),
    ("strategy.lead_lag", LeadLag),
    # v3.1.20: 4 new strategies
    ("strategy.spot_perp_carry", SpotPerpCarry),
    ("strategy.range_grid", RangeGrid),
    ("strategy.trend_pyramid", TrendPyramid),
    ("strategy.funding_momentum", FundingMomentum),
)


def default_ensemble_weights() -> List[StrategyWeight]:
    """Default ensemble weight table.

    v3.1.14: removed dead reference to ``FundingExtreme`` (removed earlier
    due to Sharpe -37). The ``SmartMoneyFlow`` entry is *intentional* — it
    is the display name returned by the :class:`TrendFollow` strategy class
    (see ``src/strategies/trend_follow.py:name`` property). Renaming would
    require updates across the regime-weights dict, settings.yaml, ~30
    test assertions, and a docs file. Keeping the legacy alias is the
    lower-risk choice for now.

    v3.1.20: added 4 new strategies with explicit weights — TrendPyramid
    replaces SmartMoneyFlow as the primary trend strategy; SpotPerpCarry
    and RangeGrid are the new low-edge / mean-reversion entries.
    """
    return [
        StrategyWeight("SmartMoneyFlow", 0.12, min_confidence=0.40),
        StrategyWeight("TrendPyramid", 0.20, min_confidence=0.50),
        StrategyWeight("VolatilityBreakout", 0.12, min_confidence=0.50),
        StrategyWeight("DonchianBreakout", 0.08, min_confidence=0.50),
        StrategyWeight("VWAPDeviation", 0.08, min_confidence=0.65),
        StrategyWeight("RangeGrid", 0.10, min_confidence=0.50),
        StrategyWeight("FundingArbitrage", 0.05, min_confidence=0.35),
        StrategyWeight("SpotPerpCarry", 0.12, min_confidence=0.60),
        StrategyWeight("FundingMomentum", 0.10, min_confidence=0.50),
        StrategyWeight("LiquidationCatcher", 0.08, min_confidence=0.60),
        StrategyWeight("CVDOrderFlow", 0.08, min_confidence=0.55),
        StrategyWeight("LeadLag", 0.07, min_confidence=0.45),
    ]


def _enrich_funding_strategy_config(cfg: Any, section: dict) -> dict:
    """Inject market-data gates for funding-sensitive strategies."""
    out = dict(section)
    md = cfg.get("market_data", {}) or {}
    if md.get("block_funding_strategies_on_red", True):
        out.setdefault("require_feed_health", True)
    out.setdefault("max_venue_spread", md.get("max_venue_spread", 0.001))
    return out


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
        if hasattr(cfg, "has_path") and not cfg.has_path(path):
            logger.warning(
                "Strategy section '%s' missing from config — using built-in defaults "
                "(add the section explicitly to silence this warning).",
                path,
            )
        section = cfg.get(path, {}) or {}
        if path in ("strategy.mean_reversion", "strategy.funding_arbitrage"):
            section = _enrich_funding_strategy_config(cfg, section)
        if _should_load_strategy(section):
            strategies.append(cls(section))
    return strategies


class DirectStrategyRouter(Strategy):
    """Run sub-strategies directly without ensemble consensus (backtest parity)."""

    def __init__(self, strategies: List[Strategy]) -> None:
        self._strategies = list(strategies)
        self._by_name = {s.name: s for s in strategies}

    @property
    def name(self) -> str:
        return "DirectRouter"

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        signals: List[Signal] = []
        for strat in self._strategies:
            if hasattr(strat, "is_active") and not strat.is_active():
                continue
            sig = strat.on_data(event)
            if sig is not None:
                signals.append(sig)
        if not signals:
            return None
        return max(signals, key=lambda s: s.confidence)

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        meta = position.metadata or {}
        owner = meta.get("strategy") or position.strategy
        strat = self._by_name.get(owner)
        if strat is None:
            return None
        return strat.on_position(position, event)


def build_strategy_list(cfg: Any) -> List[Strategy]:
    """Top-level strategies for live TradingEngine.

    When ``strategy.ensemble.enabled`` is false, each enabled sub-strategy
    runs directly (no consensus wrapper).
    """
    ens_cfg = cfg.get("strategy.ensemble", {}) or {}
    subs = build_sub_strategies(cfg)
    if ens_cfg.get("enabled", True) is False:
        if not subs:
            logger.warning("ensemble disabled but no sub-strategies are enabled")
        return subs
    return [build_ensemble(cfg)]


def build_backtest_strategy(cfg: Any) -> Strategy:
    """Single strategy object for BacktestEngine (router when ensemble is off)."""
    ens_cfg = cfg.get("strategy.ensemble", {}) or {}
    if ens_cfg.get("enabled", True) is False:
        return DirectStrategyRouter(build_sub_strategies(cfg))
    return build_ensemble(cfg)


def build_ensemble(cfg: Any) -> StrategyEnsemble:
    """Build StrategyEnsemble with weights renormalized for enabled strategies.

    v3.1.14: ``min_confidence`` for each strategy is now read from the
    live strategy instance's ``MIN_CONFIDENCE`` attribute (which itself was
    populated from ``settings.yaml: strategy.<name>.min_confidence``). This
    way, QW5 (and any future per-strategy threshold tweaks) flow through to
    the ensemble's min_confidence filter instead of being shadowed by the
    hardcoded table defaults.
    """
    subs = build_sub_strategies(cfg)
    enabled_names = {s.name for s in subs}
    active_weights = [w for w in default_ensemble_weights() if w.name in enabled_names]
    total = sum(w.weight for w in active_weights)

    # Lookup helper: min_confidence from the actual strategy instance if present.
    sub_by_name = {s.name: s for s in subs}

    if total > 0:
        active_weights = []
        for w in default_ensemble_weights():
            if w.name not in enabled_names:
                continue
            # Prefer the live strategy's MIN_CONFIDENCE (already set from settings.yaml).
            inst = sub_by_name.get(w.name)
            mc = getattr(inst, "MIN_CONFIDENCE", w.min_confidence) if inst else w.min_confidence
            active_weights.append(
                StrategyWeight(w.name, w.weight / total, float(mc))
            )

    ens = cfg.get("strategy.ensemble", {}) or {}
    exclude = ens.get("high_conviction_exclude")
    if exclude is None:
        exclude = ["VWAPDeviation"]
    return StrategyEnsemble(
        strategies=subs,
        weights=active_weights,
        threshold=float(ens.get("threshold", cfg.get("strategy.ensemble.threshold", 0.18))),
        min_strategies_agreeing=int(
            ens.get("min_agreeing", cfg.get("strategy.ensemble.min_agreeing", 1))
        ),
        high_conviction_threshold=float(
            ens.get(
                "high_conviction_threshold",
                cfg.get("strategy.ensemble.high_conviction_threshold", 0.65),
            )
        ),
        high_conviction_enabled=bool(ens.get("high_conviction_enabled", True)),
        high_conviction_exclude=list(exclude) if exclude else [],
    )
