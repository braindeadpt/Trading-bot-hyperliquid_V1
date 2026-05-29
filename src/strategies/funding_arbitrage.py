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

from src.exchanges.funding_normalize import (
    funding_data_usable,
    is_valid_funding,
    resolve_effective_funding,
    venue_funding_spread,
)
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
        self.OI_DELTA_MAX = cfg.get("oi_delta_max", 1000.0)
        self.MIN_NET_SPREAD = cfg.get("min_net_spread", 0.0007)  # spread must exceed ~0.07% RT costs

        self.MANUAL_ENABLED = bool(cfg.get("enabled", True))
        self.AUTO_ENABLE = bool(cfg.get("auto_enable", False))
        self.AUTO_ENABLE_MIN_NET_SPREAD = cfg.get("auto_enable_min_net_spread", 0.0008)
        self.AUTO_DISABLE_NET_SPREAD = cfg.get("auto_disable_net_spread", 0.0005)
        self.AUTO_ENABLE_MIN_SYMBOLS = int(cfg.get("auto_enable_min_symbols", 2))
        self._auto_active = self.MANUAL_ENABLED
        self._auto_activate_logged = False
        self._auto_deactivate_logged = False
        self._last_spread_check_ms: int = 0
        self._spread_check_interval_ms: int = int(cfg.get("spread_check_interval_ms", 60_000))
        self._last_observed_spread: Optional[float] = None

        self._state: Dict[str, _FundingArbState] = {}
        self._active_pair: Optional[Tuple[str, str]] = None  # (long_symbol, short_symbol)
        self._pair_entry_ms: int = 0
        
        # CRIT-008 FIX: Cache latest funding data across all symbols so
        # on_data can trigger pair scans when enough symbols are known.
        # This replaces the broken engine integration that never called scan_pair_opportunity.
        self._latest_funding: Dict[str, float] = {}
        self._latest_oi_delta: Dict[str, Optional[float]] = {}
        self._latest_timestamp: int = 0
        self._last_scan_ms: int = 0
        self._scan_interval_ms: int = 60_000  # scan every 60 seconds
        self._pending_signals: List[Signal] = []  # queue of signals to emit
        self._pending_exit: Optional[ExitSignal] = None
        self.REQUIRE_FEED_HEALTH = bool(cfg.get("require_feed_health", False))
        self.MAX_VENUE_SPREAD = float(cfg.get("max_venue_spread", 0.001))
        self.USE_MULTI_VENUE = bool(cfg.get("use_multi_venue_predicted", True))

    @property
    def name(self) -> str:
        return "FundingArbitrage"

    def is_active(self) -> bool:
        """True when the strategy may scan for pairs and emit entry signals."""
        return self._auto_active

    def _best_spread(
        self,
        funding_map: Dict[str, float],
    ) -> Optional[Tuple[float, str, str, float, float]]:
        """Return (spread, long_sym, short_sym, long_funding, short_funding)."""
        if len(funding_map) < self.AUTO_ENABLE_MIN_SYMBOLS:
            return None
        sorted_by_funding = sorted(funding_map.items(), key=lambda x: x[1])
        long_sym, long_funding = sorted_by_funding[0]
        short_sym, short_funding = sorted_by_funding[-1]
        spread = short_funding - long_funding
        return spread, long_sym, short_sym, long_funding, short_funding

    def _update_auto_activation(self, event: MarketEvent) -> None:
        """Enable when cross-asset funding spread covers round-trip costs; disable with hysteresis."""
        if self.MANUAL_ENABLED:
            self._auto_active = True
            return
        if not self.AUTO_ENABLE:
            self._auto_active = False
            return

        if event.timestamp_ms - self._last_spread_check_ms < self._spread_check_interval_ms:
            return
        self._last_spread_check_ms = event.timestamp_ms

        spread_info = self._best_spread(self._latest_funding)
        if spread_info is None:
            return

        spread, long_sym, short_sym, long_funding, short_funding = spread_info
        self._last_observed_spread = spread

        if not self._auto_active and spread >= self.AUTO_ENABLE_MIN_NET_SPREAD:
            self._auto_active = True
            if not self._auto_activate_logged:
                self._auto_activate_logged = True
                self._auto_deactivate_logged = False
                logger.info(
                    "FundingArbitrage AUTO-ENABLED — net spread %.4f%% >= threshold %.4f%% "
                    "(long=%s %.4f%%, short=%s %.4f%%)",
                    spread * 100,
                    self.AUTO_ENABLE_MIN_NET_SPREAD * 100,
                    long_sym,
                    long_funding * 100,
                    short_sym,
                    short_funding * 100,
                )
            return

        if (
            self._auto_active
            and self._active_pair is None
            and spread < self.AUTO_DISABLE_NET_SPREAD
        ):
            self._auto_active = False
            if not self._auto_deactivate_logged:
                self._auto_deactivate_logged = True
                self._auto_activate_logged = False
                logger.info(
                    "FundingArbitrage AUTO-DISABLED — net spread %.4f%% < threshold %.4f%% "
                    "(long=%s %.4f%%, short=%s %.4f%%)",
                    spread * 100,
                    self.AUTO_DISABLE_NET_SPREAD * 100,
                    long_sym,
                    long_funding * 100,
                    short_sym,
                    short_funding * 100,
                )

    def _operational_gate(self, event: MarketEvent) -> bool:
        """Return False if strategy should stay dormant (monitoring only)."""
        self._update_auto_activation(event)
        return self._auto_active

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        """Evaluate funding arbitrage opportunity.

        CRIT-008 FIX: Accumulate funding data in a cross-symbol cache.
        Every scan_interval_ms, trigger a pair scan across all known symbols.
        If a pair is found, queue both leg signals and emit them on subsequent
        on_data calls for the respective symbols.
        """
        if not funding_data_usable(
            event,
            require_feed_health=self.REQUIRE_FEED_HEALTH,
            max_venue_spread=self.MAX_VENUE_SPREAD,
        ):
            return None

        funding = resolve_effective_funding(event, prefer_predicted=True)
        if not is_valid_funding(funding):
            return None

        if self.USE_MULTI_VENUE and event.predicted_funding_by_venue:
            spread_v = venue_funding_spread(event.predicted_funding_by_venue)
            if spread_v > self.MAX_VENUE_SPREAD:
                logger.debug(
                    "FundingArbitrage %s skip — venue spread %.4f%% > max",
                    event.symbol,
                    spread_v * 100,
                )
                return None

        # Update cross-symbol cache (always — powers spread monitoring when dormant)
        self._latest_funding[event.symbol] = funding
        self._latest_oi_delta[event.symbol] = event.oi_delta
        self._latest_timestamp = event.timestamp_ms

        if not self._operational_gate(event):
            return None

        state = self._get_state(event.symbol)
        state.funding_history.append(funding)
        state.last_signal_ms = event.timestamp_ms

        # --- Emit queued signals for this symbol ---
        # If we have pending signals from a previous pair scan, emit when
        # the matching symbol's event arrives.
        for i, sig in enumerate(self._pending_signals):
            if sig.symbol == event.symbol:
                self._pending_signals.pop(i)
                logger.info(
                    "FundingArbitrage EMIT %s signal for %s (funding=%.4f%%)",
                    sig.side, event.symbol, funding * 100,
                )
                return sig

        # --- Periodic pair scan ---
        if event.timestamp_ms - self._last_scan_ms < self._scan_interval_ms:
            return None
        self._last_scan_ms = event.timestamp_ms

        # Only scan if no active pair
        if self._active_pair is not None:
            logger.debug(
                "FundingArbitrage scan skipped — active pair %s still open",
                self._active_pair,
            )
            return None

        # Need at least 2 symbols with funding data
        if len(self._latest_funding) < 2:
            logger.info(
                "FundingArbitrage scan skipped — only %d symbol(s) with funding data",
                len(self._latest_funding),
            )
            return None

        # Run pair scan
        result = self.scan_pair_opportunity(
            funding_map=self._latest_funding,
            oi_delta_map=self._latest_oi_delta,
            timestamp_ms=event.timestamp_ms,
        )
        if result is None:
            return None

        long_sig, short_sig = result
        # Queue both signals — they will be emitted when their respective
        # symbol events arrive.
        self._pending_signals = [long_sig, short_sig]
        logger.info(
            "FundingArbitrage PAIR FOUND — long=%s (%.4f%%) short=%s (%.4f%%) "
            "spread=%.4f%%",
            long_sig.symbol, self._latest_funding.get(long_sig.symbol, 0) * 100,
            short_sig.symbol, self._latest_funding.get(short_sig.symbol, 0) * 100,
            (self._latest_funding.get(short_sig.symbol, 0) - self._latest_funding.get(long_sig.symbol, 0)) * 100,
        )

        # Try to emit immediately if the current symbol matches one of the pair
        for i, sig in enumerate(self._pending_signals):
            if sig.symbol == event.symbol:
                self._pending_signals.pop(i)
                return sig

        return None

    # ------------------------------------------------------------------
    # Exit logic
    # ------------------------------------------------------------------

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        """Exit when funding normalizes or max hold reached."""
        funding = resolve_effective_funding(event, prefer_predicted=True)
        if not is_valid_funding(funding):
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

        # Check spread (must exceed transaction costs when enabled)
        spread = short_funding - long_funding
        if spread < self.MIN_FUNDING_SPREAD:
            logger.info(
                "FundingArbitrage scan — spread %.4f%% < threshold %.4f%% (long=%s %.4f%%, short=%s %.4f%%)",
                spread * 100, self.MIN_FUNDING_SPREAD * 100,
                long_sym, long_funding * 100, short_sym, short_funding * 100,
            )
            return None
        if spread < self.MIN_NET_SPREAD:
            logger.info(
                "FundingArbitrage scan — spread %.4f%% < min net %.4f%% after fees",
                spread * 100, self.MIN_NET_SPREAD * 100,
            )
            return None

        # Check individual extremes (duplicate spread check removed below)
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
            take_profit_pct=self.STOP_LOSS_PCT * 1.5,  # 1.5R take-profit
            reason=f"funding_arb_long_{long_funding:.4f}",
            metadata={
                "pair": "funding_arb",
                "leg": "long",
                "partner": short_sym,
                "funding": long_funding,
                "spread": spread,
                "funding_source": "hl_predicted_cex_8h",
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
            take_profit_pct=self.STOP_LOSS_PCT * 1.5,  # 1.5R take-profit
            reason=f"funding_arb_short_{short_funding:.4f}",
            metadata={
                "pair": "funding_arb",
                "leg": "short",
                "partner": long_sym,
                "funding": short_funding,
                "spread": spread,
                "funding_source": "hl_predicted_cex_8h",
            },
        )

        logger.info(
            "FundingArbitrage PAIR: LONG %s (%.4f%%) + SHORT %s (%.4f%%) | spread=%.4f%%",
            long_sym, long_funding * 100,
            short_sym, short_funding * 100,
            spread * 100,
        )
        return long_sig, short_sig
