"""Strategy: SpotPerpCarry — true delta-neutral funding-rate carry trade.

Edge
----
Hyperliquid perp funding is the *rate* at which longs pay shorts (when
funding > 0) or vice versa. When funding is extreme positive (longs
crowded, paying shorts), the cleanest edge is to *short the perp* and
simultaneously *go long the same asset on spot* (delta-neutral). The
position collects the funding rate on every hourly settlement; the
delta-neutral hedge removes directional price risk.

Unlike :class:`FundingArbitrage` (which pairs two different perp
symbols and is exposed to basis risk between them), SpotPerpCarry
hedges the same asset on the same exchange's perp vs Binance spot —
basis risk is bounded by the spot–perp dislocation of one asset.

Execution model
---------------
The bot only places the **HL perp short** leg directly. The spot leg
is tracked synthetically in ``position.metadata`` (paper mode) or sent
to Binance via the live Binance REST client (TODO; current paper
implementation logs the synthetic leg for the dashboard).

The synthetic spot leg is **not** registered with the portfolio's
``PortfolioState`` because it is offset by the perp short — net delta
is zero and the cash impact is captured as the *funding cashflow*
credited on each hourly settlement. Realised PnL on close is therefore
just the accumulated funding cashflow minus the basis-stop cost (if
the hedge was forcibly unwound by a 2% basis blowout).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Tuple

from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.utils.helpers import safe_divide, safe_float

logger = logging.getLogger(__name__)


@dataclass
class _SpotPerpCarryState:
    """Per-symbol funding history for the rolling sample."""
    funding_history: Deque[Tuple[int, float]] = field(
        default_factory=lambda: deque(maxlen=64),
    )
    last_signal_ms: int = 0
    last_exit_ms: int = 0


class SpotPerpCarry(Strategy):
    """Delta-neutral funding carry: short HL perp, long spot (synthetic)."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # Entry thresholds (hourly rates)
        self.MIN_FUNDING_HOURLY = float(cfg.get("min_funding_hourly", 0.0005))
        self.EXIT_FUNDING_HOURLY = float(cfg.get("exit_funding_hourly", 0.0001))
        # Sizing
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.02))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.05))
        # Risk
        self.BASIS_STOP_PCT = float(cfg.get("basis_stop_pct", 0.02))
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 24.0))
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        # Funding payment interval (Hyperliquid is hourly)
        self.FUNDING_INTERVAL_HOURS = 1.0
        # Confidence
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.60))
        # Throttle: don't re-enter too soon after a flat
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 30 * 60_000))
        # Toggle
        self.MANUAL_ENABLED = bool(cfg.get("enabled", True))

        self._state: Dict[str, _SpotPerpCarryState] = {}

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
    def _resolve_funding(event: MarketEvent) -> Optional[float]:
        """Prefer predicted (next-payment) funding; fall back to current."""
        v = event.predicted_funding
        if v is not None:
            return float(v)
        v = event.funding
        if v is not None:
            return float(v)
        v = event.predicted_funding_avg
        if v is not None:
            return float(v)
        v = event.funding_avg
        if v is not None:
            return float(v)
        return None

    def _append_sample(
        self,
        state: _SpotPerpCarryState,
        ts_ms: int,
        rate: float,
    ) -> None:
        if not state.funding_history or state.funding_history[-1][0] != ts_ms:
            state.funding_history.append((ts_ms, rate))

    @staticmethod
    def _basis_pct(hl_perp_mid: float, bn_spot_mid: float) -> float:
        """Spot-vs-perp basis as a fraction of spot."""
        return safe_divide(hl_perp_mid - bn_spot_mid, bn_spot_mid, 0.0)

    def _confidence(self, funding_hourly: float) -> float:
        """Linear map from funding magnitude to confidence 0.5..0.9."""
        excess = max(funding_hourly - self.MIN_FUNDING_HOURLY, 0.0)
        # 1× threshold → 0.6, 4× threshold → 0.9
        span = 0.30
        step = 0.10  # per 1× threshold of excess
        score = 0.60 + min(excess / self.MIN_FUNDING_HOURLY * step, span)
        return min(max(score, self.MIN_CONFIDENCE), 0.90)

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        if not self.MANUAL_ENABLED:
            return None

        funding = self._resolve_funding(event)
        if funding is None:
            return None

        state = self._get_state(event.symbol)
        now_ms = event.timestamp_ms
        self._append_sample(state, now_ms, funding)

        if (
            state.last_signal_ms > 0
            and now_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS
        ):
            return None

        # SpotPerpCarry only enters on positive funding (shorts collect from
        # longs). Symmetric "short-spot, long-perp" on negative funding is
        # possible but introduces borrow-cost risk on spot shorts in many
        # venues; for the HL-only execution path, we only farm the longs-pay
        # direction. Flip side is left as future work.
        if funding < self.MIN_FUNDING_HOURLY:
            return None

        # Optional basis sanity check: only enter if basis is within
        # half the basis-stop distance (so the synthetic spot leg is
        # not already stretched).
        if (
            event.binance_mid is not None
            and event.binance_mid > 0
        ):
            basis = self._basis_pct(event.price, event.binance_mid)
            if abs(basis) > self.BASIS_STOP_PCT * 0.5:
                logger.debug(
                    "SpotPerpCarry %s SKIP — basis %.4f%% > %.4f%% (half-stop)",
                    event.symbol,
                    basis * 100.0,
                    self.BASIS_STOP_PCT * 0.5 * 100.0,
                )
                return None

        confidence = self._confidence(funding)
        if confidence < self.MIN_CONFIDENCE:
            return None

        size_pct = min(self.BASE_SIZE_PCT, self.MAX_SIZE_PCT)

        # Stop on the basis-stop distance (1R); funding cashflow over
        # MAX_HOLD_HOURS at the current rate is the expected gross.
        # R:R = (hourly_funding * max_hold / basis_stop) - 1 ; require >= 1.0
        expected_gross = funding * self.MAX_HOLD_HOURS
        if self.BASIS_STOP_PCT > 0:
            rr = safe_divide(expected_gross, self.BASIS_STOP_PCT, 0.0)
            if rr < 1.0:
                logger.debug(
                    "SpotPerpCarry %s SKIP — R:R=%.2f < 1.0 (funding=%.5f/h stop=%.4f)",
                    event.symbol, rr, funding, self.BASIS_STOP_PCT,
                )
                return None

        state.last_signal_ms = now_ms

        logger.info(
            "SpotPerpCarry %s signal — funding=%.5f/h rr=%.2f conf=%.2f size=%.2f%%",
            event.symbol,
            funding,
            rr,
            confidence,
            size_pct * 100.0,
        )

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side="short",  # we short the perp; spot leg is synthetic long
            confidence=confidence,
            size_pct=size_pct,
            entry_price=event.price,
            # Stop is denominated on the perp leg as the basis-stop distance
            stop_loss_pct=self.BASIS_STOP_PCT,
            # TP is on the funding-collection horizon
            take_profit_pct=self.BASIS_STOP_PCT * max(rr, 1.0),
            reason=f"spot_perp_carry_short_f{funding:.5f}",
            metadata={
                "funding_hourly": funding,
                "expected_gross_funding": expected_gross,
                "rr": rr,
                "basis_stop_pct": self.BASIS_STOP_PCT,
                "synthetic_spot_leg": "long",
                "leg_setup": "perp_short + spot_long (delta-neutral)",
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

        # 1. Max hold
        if hold_ms >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.85,
                reason=f"max_hold_{self.MAX_HOLD_HOURS:g}h",
                metadata={"hold_ms": hold_ms},
            )

        # 2. Funding reversion
        funding = self._resolve_funding(event)
        if funding is not None and abs(funding) < self.EXIT_FUNDING_HOURLY:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.9,
                reason=f"funding_reverted_{funding:.5f}",
                metadata={"funding_hourly": funding},
            )

        # 3. Basis stop — the synthetic spot leg cannot perfectly hedge
        # if the spot–perp dislocation exceeds the stop distance.
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
