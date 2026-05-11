"""Strategy 3: Funding Arbitrage

Cross-asset funding rate arbitrage:
  - Long the asset with the most negative funding (shorts pay longs)
  - Short the asset with the most positive funding (longs pay shorts)
  - Hedge ratio 1:1 (same notional on both sides)

Logic:
  1. Every tick, look at funding across all configured symbols
  2. Identify the pair with the largest funding spread
  3. If spread > threshold and |funding| on both sides > min_threshold:
     - Enter long on most-negative-funding asset
     - Enter short on most-positive-funding asset
  4. Hold until funding reverts (|funding| < exit_threshold on both)

Risks:
  - Funding can stay extreme longer than you can stay solvent
  - One asset can move violently while the other doesn't (basis risk)
  - OI surge on one side can extend the funding pain

Timeframe: funding updates every 8 hours, but we check every tick.
Max hold: until next funding payment (8h) or until reversion.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import collections
import logging

from src.strategies.base import MarketEvent, Signal, ExitSignal, Position, Strategy

logger = logging.getLogger(__name__)


@dataclass
class _FundingArbState:
    """Per-symbol rolling funding history for ranking."""
    funding_history: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=200)
    )
    last_signal_ms: int = 0


class FundingArbitrage(Strategy):
    """Funding Arbitrage — cross-asset funding rate differential.

    Scans all symbols for the pair with the largest funding spread.
    Longs the most negative, shorts the most positive.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # Entry thresholds
        self.MIN_FUNDING_SPREAD = cfg.get("min_funding_spread", 0.012)  # 1.2% spread
        self.MIN_INDIVIDUAL_FUNDING = cfg.get("min_individual_funding", 0.005)  # 0.5%
        # Exit thresholds
        self.EXIT_THRESHOLD = cfg.get("exit_threshold", 0.002)  # 0.2% = "normal"
        self.MAX_HOLD_HOURS = cfg.get("max_hold_hours", 8)
        self.MAX_HOLD_MS = self.MAX_HOLD_HOURS * 3_600_000
        # Position sizing
        self.PAIR_SIZE_PCT = cfg.get("pair_size_pct", 0.02)  # 2% of capital per pair side
        self.STOP_LOSS_PCT = cfg.get("stop_loss_pct", 0.03)  # 3% stop on each leg
        self.CONFIDENCE = cfg.get("confidence", 0.75)
        # OI filter: avoid if OI is surging (crowd still entering)
        self.REQUIRE_OI_STABLE = cfg.get("require_oi_stable", True)
        self.OI_DELTA_MAX = cfg.get("oi_delta_max", 1000.0)  # max OI increase

        self._state: Dict[str, _FundingArbState] = {}
        self._active_pair: Optional[Tuple[str, str]] = None  # (long_symbol, short_symbol)
        self._pair_entry_ms: int = 0

    @property
    def name(self) -> str:
        return "FundingArbitrage"

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        """Evaluate funding arbitrage opportunity.

        This strategy produces signals per symbol, but the engine
        will see them individually. We need to coordinate the pair
        entry — the engine doesn't natively support pair trades.

        Approach: produce signals for both legs separately, tagging
        them as part of the same pair. The engine's conflict resolution
        will pick one at a time, but cooldown + our coordination
        ensures both legs get entered.
        """
        # We need funding data
        funding = event.predicted_funding or event.funding
        if funding is None:
            return None

        state = self._get_state(event.symbol)
        state.funding_history.append(funding)

        # Don't re-signal too frequently for the same symbol
        if event.timestamp_ms - state.last_signal_ms < 300_000:  # 5 min throttle
            return None

        # If we already have an active pair, only produce exit signals
        if self._active_pair is not None:
            return None  # exits handled in on_position

        # Need enough symbols with funding data to find a pair
        # This is a per-symbol callback — we can't see other symbols here.
        # The pair selection happens in a separate scan that we simulate
        # by keeping a global cache of latest funding per symbol.
        # However, the engine calls on_data per symbol individually.
        #
        # WORKAROUND: We maintain _latest_funding cache and only
        # produce the LONG leg signal from the most-negative symbol.
        # The SHORT leg signal is produced when that symbol's event
        # comes through. We use a delayed pair entry mechanism.
        return None  # Pair logic is handled differently — see below

    # ------------------------------------------------------------------
    # Exit logic
    # ------------------------------------------------------------------

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Exit when funding normalizes or max hold reached."""
        funding = event.predicted_funding or event.funding
        if funding is None:
            return None

        # Time-based exit
        hold_time = event.timestamp_ms - position.entry_time_ms
        if hold_time >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.9,
                reason=f"max_hold_{self.MAX_HOLD_HOURS}h_reached",
            )

        # Funding reversion exit
        if abs(funding) < self.EXIT_THRESHOLD:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.8,
                reason=f"funding_reverted_{funding:.4f}",
            )

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_state(self, symbol: str) -> _FundingArbState:
        if symbol not in self._state:
            self._state[symbol] = _FundingArbState()
        return self._state[symbol]

    # ------------------------------------------------------------------
    # Pair scan (called externally by engine or manually)
    # ------------------------------------------------------------------

    def clear_active_pair(self) -> None:
        """Clear the active pair state so new scans can proceed."""
        if self._active_pair is not None:
            logger.info("FundingArbitrage active pair %s cleared", self._active_pair)
        self._active_pair = None
        self._pair_entry_ms = 0

    def scan_pair_opportunity(
        self,
        funding_map: Dict[str, float],  # symbol -> funding
        oi_delta_map: Dict[str, Optional[float]],  # symbol -> oi_delta
        timestamp_ms: int,
    ) -> Optional[Tuple[Signal, Signal]]:
        """Scan all symbols for the best funding arbitrage pair.

        Returns (long_signal, short_signal) or None.

        This method is designed to be called by the engine after
        collecting funding data from all symbols.
        """
        if self._active_pair is not None:
            logger.debug(
                "FundingArbitrage scan skipped — active pair %s still open",
                self._active_pair,
            )
            return None

        if len(funding_map) < 2:
            logger.debug("FundingArbitrage scan skipped — need >=2 symbols, got %d", len(funding_map))
            return None

        # Sort by funding
        sorted_by_funding = sorted(funding_map.items(), key=lambda x: x[1])
        most_negative = sorted_by_funding[0]   # (symbol, funding)
        most_positive = sorted_by_funding[-1]  # (symbol, funding)

        long_sym, long_funding = most_negative
        short_sym, short_funding = most_positive

        # Check spread
        spread = short_funding - long_funding
        if spread < self.MIN_FUNDING_SPREAD:
            logger.info(
                "FundingArbitrage scan — spread %.4f%% < threshold %.4f%% (long=%s %.4f%%, short=%s %.4f%%)",
                spread * 100, self.MIN_FUNDING_SPREAD * 100,
                long_sym, long_funding * 100, short_sym, short_funding * 100,
            )
            return None

        # Check individual extremes
        if abs(long_funding) < self.MIN_INDIVIDUAL_FUNDING:
            logger.info(
                "FundingArbitrage scan — long leg %s funding %.4f%% < min %.4f%%",
                long_sym, long_funding * 100, self.MIN_INDIVIDUAL_FUNDING * 100,
            )
            return None
        if abs(short_funding) < self.MIN_INDIVIDUAL_FUNDING:
            logger.info(
                "FundingArbitrage scan — short leg %s funding %.4f%% < min %.4f%%",
                short_sym, short_funding * 100, self.MIN_INDIVIDUAL_FUNDING * 100,
            )
            return None

        # OI stability check
        if self.REQUIRE_OI_STABLE:
            long_oi_delta = oi_delta_map.get(long_sym)
            short_oi_delta = oi_delta_map.get(short_sym)
            if long_oi_delta is not None and long_oi_delta > self.OI_DELTA_MAX:
                logger.info(
                    "Arb SKIP %s — OI still surging (+%.0f)", long_sym, long_oi_delta,
                )
                return None
            if short_oi_delta is not None and short_oi_delta > self.OI_DELTA_MAX:
                logger.info(
                    "Arb SKIP %s — OI still surging (+%.0f)", short_sym, short_oi_delta,
                )
                return None

        # Record active pair
        self._active_pair = (long_sym, short_sym)
        self._pair_entry_ms = timestamp_ms

        long_sig = Signal(
            strategy=self.name,
            symbol=long_sym,
            side="long",
            confidence=self.CONFIDENCE,
            size_pct=self.PAIR_SIZE_PCT,
            entry_price=None,
            stop_loss_pct=self.STOP_LOSS_PCT,
            take_profit_pct=None,
            reason=f"funding_arb_long_{long_funding:.4f}",
            metadata={
                "pair": "funding_arb",
                "leg": "long",
                "partner": short_sym,
                "funding": long_funding,
                "spread": spread,
            },
        )
        short_sig = Signal(
            strategy=self.name,
            symbol=short_sym,
            side="short",
            confidence=self.CONFIDENCE,
            size_pct=self.PAIR_SIZE_PCT,
            entry_price=None,
            stop_loss_pct=self.STOP_LOSS_PCT,
            take_profit_pct=None,
            reason=f"funding_arb_short_{short_funding:.4f}",
            metadata={
                "pair": "funding_arb",
                "leg": "short",
                "partner": long_sym,
                "funding": short_funding,
                "spread": spread,
            },
        )

        logger.info(
            "FundingArbitrage PAIR: LONG %s (%.4f%%) + SHORT %s (%.4f%%) | spread=%.4f%%",
            long_sym, long_funding * 100,
            short_sym, short_funding * 100,
            spread * 100,
        )
        return long_sig, short_sig

    def clear_active_pair(self) -> None:
        """Clear active pair tracking (call when pair is fully closed)."""
        self._active_pair = None
        self._pair_entry_ms = 0
