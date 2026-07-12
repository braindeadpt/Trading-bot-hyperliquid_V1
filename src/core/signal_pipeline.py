"""Shared entry gate ordering for live engine and backtest replay.

Phase 05: single source of truth for feed/replay quality, debounce,
cooldown, Kelly, symbol multipliers, chase, correlation, volatility
circuit, funding blackout, risk, and TCA.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src.backtest.replay_data_quality import ReplayDataQualityGate, SymbolReplayAudit
from src.core.correlation_monitor import CorrelationMonitor
from src.core.funding_blackout import FundingBlackoutFilter
from src.core.kelly_sizer import KellySizer
from src.core.order_router import OrderSpec, resolve_order_routing
from src.core.regime import apply_regime_weights, regime_strategy_name
from src.core.risk_manager import RiskManager
from src.core.tca import passes_tca_check
from src.core.volatility_circuit import VolatilityCircuitBreaker
from src.strategies.base import MarketEvent, Signal
from src.utils.config import Config, get_strategy_section, resolve_kelly_enabled
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

GATE_PARITY_VERSION = "phase05-gates-v1"

_FUNDING_COOLDOWN_STRATEGIES = frozenset({
    "FundingExtreme",
    "FundingArbitrage",
    "FundingMomentum",
})

# Canonical gate order shared by live and backtest replay.
GATE_ORDER: Tuple[str, ...] = (
    "feed_health",
    "entry_debounce",
    "cooldown",
    "vol_circuit",
    "funding_blackout",
    "chase_filter",
    "correlation",
    "risk",
    "tca",
)

# Gates that remain live-only (documented in manifest).
LIVE_ONLY_GATES: Tuple[str, ...] = (
    "execution_block",
    "fill_ratio",
    "slippage_l2",
    "reconciliation_stale",
    "executor_debounce",
)

FeedBlockFn = Callable[[str], Optional[str]]


@dataclass
class GateDecision:
    """Result of running the shared entry pipeline."""

    approved: bool
    gate: str = ""
    reason: str = ""
    signal: Optional[Signal] = None


@dataclass
class PipelineContext:
    """Mutable replay/live state carried across bars/trades."""

    cooldown_state: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    candles_15m_history: Dict[str, List[Any]] = field(default_factory=dict)
    last_entry_ms: Dict[str, int] = field(default_factory=dict)
    last_bar_ts: Dict[str, int] = field(default_factory=dict)
    replay_audit: Dict[str, SymbolReplayAudit] = field(default_factory=dict)
    funding_ts_at: Dict[str, int] = field(default_factory=dict)
    oi_ts_at: Dict[str, int] = field(default_factory=dict)


class EntryDebounce:
    """Deterministic per-symbol entry debounce (engine-level; mirrors live)."""

    def __init__(self, debounce_ms: int, *, enabled: bool = True) -> None:
        self._debounce_ms = max(0, int(debounce_ms))
        self._enabled = enabled

    @classmethod
    def from_config(cls, config: Config, *, enabled: bool = True) -> "EntryDebounce":
        ms = int(config.get("engine.entry_signal_debounce_ms", 5_000))
        return cls(ms, enabled=enabled)

    def is_blocked(self, symbol: str, ts_ms: int, state: Dict[str, int]) -> Tuple[bool, str]:
        if not self._enabled or self._debounce_ms <= 0:
            return False, ""
        last = int(state.get(symbol, 0))
        if last <= 0:
            return False, ""
        elapsed = ts_ms - last
        if elapsed < self._debounce_ms:
            remaining = self._debounce_ms - elapsed
            return True, f"entry_debounce {remaining}ms remaining"
        return False, ""

    def record_entry(self, symbol: str, ts_ms: int, state: Dict[str, int]) -> None:
        if self._enabled:
            state[symbol] = ts_ms


class CooldownManager:
    """Per-(strategy,symbol) cooldown with loss doubling and funding/ADX resets."""

    def __init__(
        self,
        *,
        base_ms: int,
        max_ms: int,
        multiplier: float,
        funding_strong_threshold: float,
        adx_trend_threshold: float,
        adx_range_threshold: float,
        enabled: bool = True,
    ) -> None:
        self._base_ms = base_ms
        self._max_ms = max_ms
        self._multiplier = multiplier
        self._funding_strong_threshold = funding_strong_threshold
        self._adx_trend_threshold = adx_trend_threshold
        self._adx_range_threshold = adx_range_threshold
        self._enabled = enabled

    @classmethod
    def from_config(cls, config: Config, *, enabled: bool = True) -> "CooldownManager":
        cooldown_cfg = get_strategy_section(config, "cooldown")
        return cls(
            base_ms=int(safe_float(cooldown_cfg.get("base_minutes", 60)) * 60_000),
            max_ms=int(safe_float(cooldown_cfg.get("max_minutes", 240)) * 60_000),
            multiplier=safe_float(cooldown_cfg.get("multiplier", 2.0)),
            funding_strong_threshold=safe_float(
                config.get("strategy.mean_reversion.strong_threshold", 0.0001)
            ),
            adx_trend_threshold=safe_float(config.get("strategy.adx_trend_threshold", 25.0)),
            adx_range_threshold=safe_float(config.get("strategy.adx_range_threshold", 20.0)),
            enabled=enabled,
        )

    @staticmethod
    def _key(strategy: str, symbol: str) -> str:
        return f"{strategy}:{symbol}"

    def is_blocked(
        self,
        strategy: str,
        symbol: str,
        event: MarketEvent,
        state: Dict[str, Dict[str, Any]],
    ) -> Tuple[bool, str]:
        if not self._enabled:
            return False, ""
        key = self._key(strategy, symbol)
        cd = state.get(key)
        if cd is None:
            return False, ""

        now = event.timestamp_ms
        elapsed = now - int(cd.get("last_trade_ms", 0))
        duration = int(cd.get("duration_ms", self._base_ms))

        if elapsed >= duration:
            state.pop(key, None)
            return False, ""

        if strategy in _FUNDING_COOLDOWN_STRATEGIES:
            entry_funding = cd.get("funding")
            if entry_funding is not None and abs(safe_float(entry_funding, 0.0)) > self._funding_strong_threshold:
                funding = event.funding or event.predicted_funding
                normalize_th = self._funding_strong_threshold * 0.5
                if funding is not None and abs(funding) < normalize_th:
                    state.pop(key, None)
                    return False, ""

        current_adx = event.adx_14
        entry_adx = cd.get("adx")
        if current_adx is not None and entry_adx is not None:
            old_regime = (
                "trend" if entry_adx > self._adx_trend_threshold
                else "range" if entry_adx < self._adx_range_threshold
                else "neutral"
            )
            new_regime = (
                "trend" if current_adx > self._adx_trend_threshold
                else "range" if current_adx < self._adx_range_threshold
                else "neutral"
            )
            if old_regime != new_regime and new_regime != "neutral":
                state.pop(key, None)
                return False, ""

        remaining = (duration - elapsed) / 60_000
        losses = int(cd.get("consecutive_losses", 0))
        return True, f"cooldown {remaining:.1f}min remaining ({losses} losses)"

    def on_entry(
        self,
        strategy: str,
        symbol: str,
        event: MarketEvent,
        state: Dict[str, Dict[str, Any]],
    ) -> None:
        if not self._enabled:
            return
        key = self._key(strategy, symbol)
        prev = state.get(key, {})
        state[key] = {
            "last_trade_ms": event.timestamp_ms,
            "duration_ms": int(prev.get("duration_ms", self._base_ms)),
            "consecutive_losses": int(prev.get("consecutive_losses", 0)),
            "adx": event.adx_14,
            "funding": event.funding or event.predicted_funding,
        }

    def on_exit(
        self,
        strategy: str,
        symbol: str,
        pnl_pct: float,
        state: Dict[str, Dict[str, Any]],
    ) -> None:
        if not self._enabled:
            return
        key = self._key(strategy, symbol)
        cd = state.get(key)
        if cd is None:
            return
        if pnl_pct > 0:
            cd["consecutive_losses"] = 0
            cd["duration_ms"] = self._base_ms
        else:
            losses = int(cd.get("consecutive_losses", 0)) + 1
            cd["consecutive_losses"] = losses
            cd["duration_ms"] = int(min(
                self._base_ms * (self._multiplier ** losses),
                self._max_ms,
            ))


class ChaseFilter:
    """Reject entries that chase an extended directional move."""

    def __init__(
        self,
        *,
        enabled: bool,
        lookback_hours: float,
        max_runup_pct: float,
        exempt_strategies: Set[str],
    ) -> None:
        self._enabled = enabled
        self._lookback_hours = lookback_hours
        self._max_runup_pct = max_runup_pct
        self._exempt = exempt_strategies

    @classmethod
    def from_config(cls, config: Config) -> "ChaseFilter":
        chase = config.get("risk.chase_filter", {}) or {}
        exempt = chase.get("exempt_strategies", ["VolatilityBreakout", "DonchianBreakout"])
        return cls(
            enabled=bool(chase.get("enabled", True)),
            lookback_hours=safe_float(chase.get("lookback_hours", 3.0)),
            max_runup_pct=safe_float(chase.get("max_runup_pct", 0.008)),
            exempt_strategies=set(exempt) if isinstance(exempt, list) else set(),
        )

    def check(
        self,
        signal: Signal,
        history: Dict[str, List[Any]],
    ) -> Optional[str]:
        if not self._enabled:
            return None
        if signal.strategy in self._exempt:
            return None
        hist = list(history.get(signal.symbol, []))
        if len(hist) < 2:
            return None
        bars = max(2, int(self._lookback_hours * 4.0))
        window = hist[-bars:] if len(hist) >= bars else hist
        if len(window) < 2:
            return None
        start_px = safe_float(getattr(window[0], "close", 0.0), 0.0)
        end_px = safe_float(getattr(window[-1], "close", 0.0), 0.0)
        if start_px <= 0.0:
            return None
        ret = (end_px - start_px) / start_px
        runup = max(ret, 0.0) if signal.side == "long" else max(-ret, 0.0)
        if runup > self._max_runup_pct:
            return (
                f"chase runup={runup * 100:.2f}% "
                f"> {self._max_runup_pct * 100:.2f}% "
                f"over {self._lookback_hours:.1f}h"
            )
        return None


class SignalPipeline:
    """Shared preprocessing and gate evaluation for live and backtest."""

    def __init__(
        self,
        config: Config,
        risk_manager: RiskManager,
        *,
        kelly_sizer: Optional[KellySizer] = None,
        vol_circuit: Optional[VolatilityCircuitBreaker] = None,
        funding_blackout: Optional[FundingBlackoutFilter] = None,
        cooldown: Optional[CooldownManager] = None,
        chase: Optional[ChaseFilter] = None,
        correlation_monitor: Optional[CorrelationMonitor] = None,
        replay_quality: Optional[ReplayDataQualityGate] = None,
        feed_block_fn: Optional[FeedBlockFn] = None,
        entry_debounce: Optional[EntryDebounce] = None,
        use_regime_weights: bool = True,
        use_cooldown: bool = True,
        use_debounce: bool = True,
        kelly_enabled: Optional[bool] = None,
        tca_enabled: bool = True,
        for_backtest: bool = False,
    ) -> None:
        self._config = config
        self._risk = risk_manager
        self._for_backtest = for_backtest
        self._kelly = kelly_sizer or KellySizer(
            min_trades=int(get_strategy_section(config, "kelly").get("min_trades", 20)),
            half_kelly=bool(get_strategy_section(config, "kelly").get("half_kelly", True)),
            max_multiplier=safe_float(get_strategy_section(config, "kelly").get("max_multiplier", 2.0)),
            min_multiplier=safe_float(get_strategy_section(config, "kelly").get("min_multiplier", 0.25)),
            lookback_trades=int(get_strategy_section(config, "kelly").get("lookback_trades", 50)),
        )
        self._kelly_enabled = (
            resolve_kelly_enabled(config, for_backtest=for_backtest)
            if kelly_enabled is None
            else kelly_enabled
        )
        self._vol_circuit = vol_circuit
        self._funding_blackout = funding_blackout
        self._cooldown = cooldown or CooldownManager.from_config(config, enabled=use_cooldown)
        self._chase = chase or ChaseFilter.from_config(config)
        self._correlation = correlation_monitor or CorrelationMonitor(
            lookback=int(
                (config.get("strategy.portfolio_governance", {}) or {}).get(
                    "max_correlation_lookback", 60,
                )
            )
        )
        self._corr_threshold = safe_float(
            (config.get("strategy.portfolio_governance", {}) or {}).get(
                "max_correlation",
                config.get("portfolio.max_correlation", 0.70),
            )
        )
        self._replay_quality = replay_quality
        self._feed_block_fn = feed_block_fn
        self._debounce = entry_debounce or EntryDebounce.from_config(
            config, enabled=use_debounce,
        )
        self._use_regime_weights = use_regime_weights
        sym_mult = config.get("risk.symbol_risk_multiplier", {}) or {}
        self._symbol_multipliers: Dict[str, float] = {
            str(sym): safe_float(mult, 1.0)
            for sym, mult in sym_mult.items()
        } if isinstance(sym_mult, dict) else {}
        self._regime_weights = config.get("strategy.regime_weights", {})
        self._adx_trend = safe_float(config.get("strategy.adx_trend_threshold", 25.0))
        self._adx_range = safe_float(config.get("strategy.adx_range_threshold", 20.0))
        self._tca_enabled = tca_enabled
        self._taker_fee = safe_float(config.get("risk.taker_fee_pct", 0.035)) / 100.0
        self._paper_slip = safe_float(config.get("risk.paper_slippage_pct", 0.05)) / 100.0
        self._tca_buffer = safe_float(config.get("execution.min_edge_buffer_pct", 0.05)) / 100.0
        if for_backtest:
            self._tca_mode = str(config.get("backtest.tca_mode", "proxy")).lower()
        else:
            self._tca_mode = str(config.get("execution.tca_mode", "strict")).lower()

    @property
    def correlation_monitor(self) -> CorrelationMonitor:
        return self._correlation

    def preprocess_signal(
        self,
        signal: Signal,
        event: MarketEvent,
    ) -> Optional[Signal]:
        """Apply regime weights only (Kelly/sym mult run after cooldown in evaluate_gates)."""
        adjusted: List[Signal] = [signal]
        if self._use_regime_weights and self._regime_weights:
            adjusted = apply_regime_weights(
                adjusted,
                event.adx_14,
                self._regime_weights,
                self._adx_trend,
                self._adx_range,
            )
            if not adjusted:
                return None
            return adjusted[0]
        return signal

    def _apply_sizing_adjustments(self, signal: Signal) -> Signal:
        """Kelly + per-symbol risk multiplier (after cooldown, matching live)."""
        if self._kelly_enabled:
            mult = self._kelly.get_size_multiplier()
            if mult != 1.0:
                signal = Signal(
                    strategy=signal.strategy,
                    symbol=signal.symbol,
                    side=signal.side,
                    confidence=signal.confidence,
                    size_pct=signal.size_pct * mult,
                    entry_price=signal.entry_price,
                    stop_loss_pct=signal.stop_loss_pct,
                    take_profit_pct=signal.take_profit_pct,
                    reason=f"{signal.reason} (kelly:{mult:.2f}x)",
                    metadata={**signal.metadata, "kelly_multiplier": mult},
                )

        sym_mult = safe_float(self._symbol_multipliers.get(signal.symbol, 1.0), 1.0)
        if sym_mult != 1.0:
            base = signal.size_pct
            signal = Signal(
                strategy=signal.strategy,
                symbol=signal.symbol,
                side=signal.side,
                confidence=signal.confidence,
                size_pct=base * sym_mult,
                entry_price=signal.entry_price,
                stop_loss_pct=signal.stop_loss_pct,
                take_profit_pct=signal.take_profit_pct,
                reason=f"{signal.reason} (sym_risk:{sym_mult:.2f}x)",
                metadata={**signal.metadata, "symbol_risk_multiplier": sym_mult},
            )
        return signal

    def _check_feed_or_replay_quality(
        self,
        symbol: str,
        event: MarketEvent,
        ctx: PipelineContext,
    ) -> Optional[str]:
        if self._for_backtest and self._replay_quality is not None:
            audit = ctx.replay_audit.get(symbol)
            prev_ts = ctx.last_bar_ts.get(symbol)
            return self._replay_quality.check_entry(
                symbol,
                event,
                audit=audit,
                last_bar_ts=prev_ts,
                funding_ts_at=ctx.funding_ts_at.get(symbol),
                oi_ts_at=ctx.oi_ts_at.get(symbol),
            )
        if self._feed_block_fn is not None:
            return self._feed_block_fn(symbol)
        return None

    def _check_correlation(
        self,
        signal: Signal,
        portfolio: Any,
    ) -> Optional[str]:
        if self._corr_threshold <= 0:
            return None
        positions = getattr(portfolio, "positions", {}) or {}
        if not positions or signal.symbol in positions:
            return None
        existing = list(positions.keys())
        violated, conflict_sym, corr = self._correlation.would_violate(
            signal.symbol, existing, self._corr_threshold,
        )
        if violated and conflict_sym is not None and corr is not None:
            return (
                f"Correlation limit: |r({signal.symbol},{conflict_sym})|="
                f"{abs(corr):.2f} > {self._corr_threshold:.2f}"
            )
        return None

    def evaluate_gates(
        self,
        signal: Signal,
        event: MarketEvent,
        portfolio: Any,
        ctx: PipelineContext,
        *,
        order_spec: Optional[OrderSpec] = None,
        has_orderbook: bool = False,
        skip_tca: bool = False,
    ) -> GateDecision:
        """Run shared gates in canonical order; return first rejection or approval."""
        strat = signal.strategy

        feed_reason = self._check_feed_or_replay_quality(signal.symbol, event, ctx)
        if feed_reason:
            gate = "replay_data_quality" if self._for_backtest else "feed_health"
            return GateDecision(False, gate, feed_reason, signal)

        debounced, deb_reason = self._debounce.is_blocked(
            signal.symbol, event.timestamp_ms, ctx.last_entry_ms,
        )
        if debounced:
            return GateDecision(False, "entry_debounce", deb_reason, signal)

        blocked, reason = self._cooldown.is_blocked(
            strat, signal.symbol, event, ctx.cooldown_state,
        )
        if blocked:
            return GateDecision(False, "cooldown", reason, signal)

        signal = self._apply_sizing_adjustments(signal)

        if self._vol_circuit is not None and self._vol_circuit.is_blocked(
            signal.symbol, event.timestamp_ms,
        ):
            remaining = self._vol_circuit.block_remaining_sec(signal.symbol, event.timestamp_ms)
            return GateDecision(
                False, "vol_circuit", f"vol_circuit:{remaining}s", signal,
            )

        if (
            self._funding_blackout is not None
            and self._funding_blackout.is_blocked(event.timestamp_ms)
        ):
            return GateDecision(False, "funding_blackout", "funding_blackout", signal)

        chase_reason = self._chase.check(signal, ctx.candles_15m_history)
        if chase_reason:
            return GateDecision(False, "chase_filter", chase_reason, signal)

        corr_reason = self._check_correlation(signal, portfolio)
        if corr_reason:
            return GateDecision(False, "correlation", corr_reason, signal)

        ok, risk_reason = self._risk.can_enter(signal, portfolio)
        if not ok:
            return GateDecision(False, "risk", risk_reason, signal)

        if self._tca_enabled and not skip_tca:
            tca_decision = self.evaluate_tca_gate(
                signal, order_spec=order_spec, has_orderbook=has_orderbook,
            )
            if not tca_decision.approved:
                return tca_decision

        return GateDecision(True, "", "", signal)

    def evaluate_tca_gate(
        self,
        signal: Signal,
        *,
        order_spec: Optional[OrderSpec] = None,
        has_orderbook: bool = False,
    ) -> GateDecision:
        """TCA-only pass (after live sizing / order routing when L2 may exist)."""
        if not self._tca_enabled:
            return GateDecision(True, "", "", signal)
        if self._tca_mode == "strict" and not has_orderbook:
            return GateDecision(False, "tca", "tca_strict_no_l2_book", signal)
        slip = self._paper_slip
        entry_fee = self._taker_fee
        exit_fee = self._taker_fee
        entry_slip = slip
        exit_slip = slip
        if order_spec is not None:
            entry_fee = order_spec.entry_fee_pct
            exit_fee = order_spec.exit_fee_pct
            entry_slip = order_spec.entry_slippage_pct
            exit_slip = order_spec.exit_slippage_pct
            slip = order_spec.exit_slippage_pct
        tca_ok, tca_reason = passes_tca_check(
            signal,
            self._taker_fee,
            slip,
            self._tca_buffer,
            entry_fee_pct=entry_fee,
            exit_fee_pct=exit_fee,
            entry_slippage_pct=entry_slip,
            exit_slippage_pct=exit_slip,
        )
        if not tca_ok:
            return GateDecision(False, "tca", tca_reason, signal)
        return GateDecision(True, "", "", signal)

    def resolve_order_fees(
        self,
        signal: Signal,
        orderbook: Any = None,
    ) -> Tuple[Signal, OrderSpec]:
        """Apply maker/taker routing — same helper as live engine."""
        return resolve_order_routing(signal, self._config, orderbook)

    def record_trade_closed(
        self,
        strategy: str,
        symbol: str,
        pnl_pct: float,
        ctx: PipelineContext,
    ) -> None:
        self._cooldown.on_exit(strategy, symbol, pnl_pct, ctx.cooldown_state)
        if self._kelly_enabled:
            self._kelly.record_trade(pnl_pct)

    def record_trade_opened(
        self,
        strategy: str,
        symbol: str,
        event: MarketEvent,
        ctx: PipelineContext,
    ) -> None:
        self._cooldown.on_entry(strategy, symbol, event, ctx.cooldown_state)
        self._debounce.record_entry(symbol, event.timestamp_ms, ctx.last_entry_ms)

    def gate_manifest(self) -> Dict[str, Any]:
        """Document which gates are shared vs live-only."""
        return {
            "gate_parity_version": GATE_PARITY_VERSION,
            "shared_gate_order": list(GATE_ORDER),
            "live_only_gates": list(LIVE_ONLY_GATES),
            "replay_substitutes": {
                "feed_health": "replay_data_quality",
            },
            "intentional_exclusions": {
                "executor_debounce": (
                    "execution.entry_debounce_ms in ExecutionEngine is a second "
                    "live-only layer after engine entry debounce; not replayed."
                ),
            },
            "tca_mode": self._tca_mode,
            "tca_fidelity": (
                "strict rejects entries without L2 (tier_a); "
                "proxy allows paper slippage only (tier_b_tca_proxy)"
            ),
            "correlation_exposure": (
                "correlation gate + RiskManager directional/sector caps shared live/backtest"
            ),
            "entry_debounce_ms": self._debounce._debounce_ms,
        }
