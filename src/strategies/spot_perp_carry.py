"""Strategy: SpotPerpCarry — funding-rate carry (research / shadow only).

Edge (intended)
---------------
When HL perp funding is extreme positive (longs crowded), short the perp
and long the same asset on spot (delta-neutral) to collect funding.

IMPORTANT — current limitations (do NOT treat as live-ready arb)
----------------------------------------------------------------
1. ``MarketEvent.funding`` / ``predicted_funding`` from the live engine are
   **8h-equivalent** rates (see ``normalize_funding_to_8h``). Hyperliquid
   pays ``rate_8h / 8`` each hour. Cashflow math MUST divide by 8.
2. Config keys ``min_funding_hourly`` / ``exit_funding_hourly`` are
   historically misnamed: YAML values (e.g. 0.0005 ≈ 55% APR) match the
   **8h-equivalent** convention used on MarketEvent, not a true hourly rate.
3. The spot hedge is **synthetic only** (metadata). Live execution of this
   strategy as a naked perp short is directional risk — not arbitrage.

Unlike :class:`FundingArbitrage` (cross-asset carry), this module targets
same-asset perp vs spot basis.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Tuple

from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.utils.helpers import safe_divide, safe_float

logger = logging.getLogger(__name__)

# HL settles hourly at 1/8 of the quoted 8h-equivalent rate.
_HL_FUNDING_QUOTE_HOURS = 8.0


@dataclass
class _SpotPerpCarryState:
    """Per-symbol funding history for the rolling sample."""
    funding_history: Deque[Tuple[int, float]] = field(
        default_factory=lambda: deque(maxlen=64),
    )
    last_signal_ms: int = 0
    last_exit_ms: int = 0


class SpotPerpCarry(Strategy):
    """Funding carry: short HL perp (+ synthetic spot long). Shadow/research."""

    # Explicit flag for auditors / factory — spot leg is not live-hedged.
    SYNTHETIC_SPOT_ONLY = True

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # Thresholds compare against MarketEvent 8h-equivalent rates.
        # Key names retain ``*_hourly`` for YAML compatibility (frozen Fase 10).
        self.MIN_FUNDING_8H = float(cfg.get("min_funding_hourly", 0.0005))
        self.EXIT_FUNDING_8H = float(cfg.get("exit_funding_hourly", 0.0001))
        # Back-compat aliases (tests / callers)
        self.MIN_FUNDING_HOURLY = self.MIN_FUNDING_8H
        self.EXIT_FUNDING_HOURLY = self.EXIT_FUNDING_8H
        # Sizing
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.02))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.05))
        # Risk
        self.BASIS_STOP_PCT = float(cfg.get("basis_stop_pct", 0.02))
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 24.0))
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        self.FUNDING_QUOTE_HOURS = float(
            cfg.get("funding_quote_hours", _HL_FUNDING_QUOTE_HOURS)
        )
        # Confidence
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.60))
        # Throttle: don't re-enter too soon after a flat
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 30 * 60_000))
        # Toggle
        self.MANUAL_ENABLED = bool(cfg.get("enabled", True))

        self._state: Dict[str, _SpotPerpCarryState] = {}
        self._warned_synthetic = False

    @property
    def name(self) -> str:
        return "SpotPerpCarry"

    def is_active(self) -> bool:
        return self.MANUAL_ENABLED

    def _get_state(self, symbol: str) -> _SpotPerpCarryState:
        if symbol not in self._state:
            self._state[symbol] = _SpotPerpCarryState()
        return self._state[symbol]

    @staticmethod
    def _resolve_funding_8h(event: MarketEvent) -> Optional[float]:
        """Prefer predicted (next-payment) funding; values are 8h-equivalent."""
        v = event.predicted_funding
        if v is not None:
            return float(v)
        v = event.funding
        if v is not None:
            return float(v)
        return None

    def _hourly_from_8h(self, funding_8h: float) -> float:
        """Convert 8h-equivalent quote to the per-hour settlement rate."""
        hours = self.FUNDING_QUOTE_HOURS if self.FUNDING_QUOTE_HOURS > 0 else _HL_FUNDING_QUOTE_HOURS
        return funding_8h / hours

    def _confidence(self, funding_8h: float) -> float:
        """Linear map from 8h funding magnitude to confidence 0.5..0.9."""
        excess = max(funding_8h - self.MIN_FUNDING_8H, 0.0)
        span = 0.30
        step = 0.10
        score = 0.60 + min(excess / self.MIN_FUNDING_8H * step, span)
        return min(max(score, self.MIN_CONFIDENCE), 0.90)

    @staticmethod
    def _basis_pct(hl_perp_mid: float, bn_spot_mid: float) -> float:
        """Spot-vs-perp basis as a fraction of spot."""
        return safe_divide(hl_perp_mid - bn_spot_mid, bn_spot_mid, 0.0)

    def _append_sample(
        self,
        state: _SpotPerpCarryState,
        ts_ms: int,
        rate_8h: float,
    ) -> None:
        if not state.funding_history or state.funding_history[-1][0] != ts_ms:
            state.funding_history.append((ts_ms, rate_8h))

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        if not self.MANUAL_ENABLED:
            return None

        funding_8h = self._resolve_funding_8h(event)
        if funding_8h is None:
            return None

        state = self._get_state(event.symbol)
        now_ms = event.timestamp_ms
        self._append_sample(state, now_ms, funding_8h)

        if (
            state.last_signal_ms > 0
            and now_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS
        ):
            return None

        # SpotPerpCarry only enters on positive funding (shorts collect from
        # longs). Threshold is 8h-equivalent (YAML key name is historical).
        if funding_8h < self.MIN_FUNDING_8H:
            return None

        if (
            event.binance_mid is not None
            and event.binance_mid > 0
        ):
            basis = self._basis_pct(event.price, event.binance_mid)
            if abs(basis) > self.BASIS_STOP_PCT * 0.5:
                logger.info(
                    "SpotPerpCarry %s SKIP — basis %.4f%% > %.4f%% (half-stop)",
                    event.symbol,
                    basis * 100.0,
                    self.BASIS_STOP_PCT * 0.5 * 100.0,
                )
                return None

        confidence = self._confidence(funding_8h)
        if confidence < self.MIN_CONFIDENCE:
            return None

        size_pct = min(self.BASE_SIZE_PCT, self.MAX_SIZE_PCT)

        # Cashflow: HL pays funding_8h/8 each hour → over max_hold hours
        funding_hourly = self._hourly_from_8h(funding_8h)
        expected_gross = funding_hourly * self.MAX_HOLD_HOURS
        if self.BASIS_STOP_PCT > 0:
            rr = safe_divide(expected_gross, self.BASIS_STOP_PCT, 0.0)
            if rr < 1.0:
                logger.info(
                    "SpotPerpCarry %s SKIP — R:R=%.2f < 1.0 "
                    "(funding_8h=%.5f → %.5f/h × %.0fh stop=%.4f)",
                    event.symbol,
                    rr,
                    funding_8h,
                    funding_hourly,
                    self.MAX_HOLD_HOURS,
                    self.BASIS_STOP_PCT,
                )
                return None

        state.last_signal_ms = now_ms

        if not self._warned_synthetic:
            self._warned_synthetic = True
            logger.warning(
                "SpotPerpCarry emitting signals with SYNTHETIC spot hedge only "
                "— not delta-neutral in live execution; research/shadow use."
            )

        logger.info(
            "SpotPerpCarry %s signal — funding_8h=%.5f (%.5f/h) rr=%.2f conf=%.2f size=%.2f%%",
            event.symbol,
            funding_8h,
            funding_hourly,
            rr,
            confidence,
            size_pct * 100.0,
        )

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side="short",
            confidence=confidence,
            size_pct=size_pct,
            entry_price=event.price,
            stop_loss_pct=self.BASIS_STOP_PCT,
            take_profit_pct=self.BASIS_STOP_PCT * max(rr, 1.0),
            reason=f"spot_perp_carry_short_f8h{funding_8h:.5f}",
            metadata={
                "funding_8h": funding_8h,
                "funding_hourly": funding_hourly,
                "expected_gross_funding": expected_gross,
                "rr": rr,
                "basis_stop_pct": self.BASIS_STOP_PCT,
                "synthetic_spot_leg": "long",
                "synthetic_spot_only": True,
                "leg_setup": "perp_short + spot_long (SYNTHETIC — not live-hedged)",
            },
        )

    def on_position(
        self,
        position: Position,
        event: MarketEvent,
    ) -> Optional[ExitSignal]:
        meta = position.metadata or {}
        if meta.get("original_strategy") not in (None, self.name) \
                and meta.get("strategy") != self.name \
                and meta.get("sub_strategy") != self.name:
            return None

        now_ms = event.timestamp_ms
        hold_ms = now_ms - position.entry_time_ms

        if hold_ms >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.85,
                reason=f"max_hold_{self.MAX_HOLD_HOURS:g}h",
                metadata={"hold_ms": hold_ms},
            )

        funding_8h = self._resolve_funding_8h(event)
        if funding_8h is not None and abs(funding_8h) < self.EXIT_FUNDING_8H:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.9,
                reason=f"funding_reverted_{funding_8h:.5f}",
                metadata={
                    "funding_8h": funding_8h,
                    "funding_hourly": self._hourly_from_8h(funding_8h),
                },
            )

        if event.binance_mid is not None and event.binance_mid > 0:
            basis = self._basis_pct(event.price, event.binance_mid)
            if abs(basis) > self.BASIS_STOP_PCT:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.95,
                    reason=f"basis_stop_{basis:.4f}",
                    metadata={"basis_pct": basis, "stop_pct": self.BASIS_STOP_PCT},
                )

        return None
