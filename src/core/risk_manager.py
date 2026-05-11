"""Risk management engine for the Hyperliquid trading bot.

Enforces position limits, daily loss thresholds, drawdown circuit breakers,
and computes position sizes using ATR-based volatility sizing.  All rules
are applied deterministically so that live and backtest behavior match
exactly.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from src.strategies.base import Position, Signal
from src.utils.config import Config
from src.utils.helpers import safe_float, safe_divide, clamp, utc_now

logger = logging.getLogger(__name__)


class RiskManager:
    """Central risk gate — every entry signal must pass through here.

    Hard limits:
      - Max 1 position per asset (both strategies share the same pool)
      - Max 5 total open positions
      - Max 5 trades per day (resets at 00:00 UTC)
      - Max 3% daily loss → circuit breaker until next day
      - Max 10% drawdown from peak → circuit breaker until next day
      - Per trade: risk 1% of capital (sized by ATR distance)
      - Max position size = 20% of capital
      - Stop distance = 2×ATR (minimum 0.5% of price)
      - Overcrowded penalty: confidence reduced by 20% if overcrowded_score > 0.7
    """

    # Hard-coded safety constants
    MAX_POSITIONS_PER_ASSET: int = 1
    MAX_TOTAL_POSITIONS: int = 5
    MAX_DAILY_TRADES: int = 5
    MAX_DAILY_LOSS_PCT: float = 0.03
    PER_TRADE_RISK_PCT: float = 0.01
    MAX_POSITION_SIZE_PCT: float = 0.20
    MIN_STOP_DISTANCE_PCT: float = 0.005
    CIRCUIT_BREAKER_DRAWDOWN_PCT: float = 0.10
    OVERCROWDED_CONFIDENCE_PENALTY: float = 0.20
    OVERCROWDED_THRESHOLD: float = 0.70
    # FASE 4: Portfolio Heat & Governance
    MAX_DIRECTIONAL_EXPOSURE_PCT: float = 0.60  # 4.1: max 60% book same direction
    MAX_SECTOR_EXPOSURE_PCT: float = 0.30      # 4.2: max 30% in crypto (if mixed assets)
    DAILY_DRAWDOWN_CIRCUIT_PCT: float = 0.05   # 4.3: daily drawdown > 5% = stop

    def __init__(self, config: Config, db: Any, notifier: Any = None) -> None:
        """Initialise with config overrides and DB reference.

        The *db* reference is kept for future persistence of risk metrics;
it is not used directly in the current implementation.
        """
        self._config = config
        self._db = db
        self._notifier = notifier

        # Allow config overrides (e.g. backtest tuning)
        self._max_total_positions = int(
            config.get("risk.max_positions", self.MAX_TOTAL_POSITIONS)
        )
        self._max_daily_loss_pct = safe_float(
            config.get("risk.max_daily_loss_pct", self.MAX_DAILY_LOSS_PCT * 100.0)
        ) / 100.0
        self._per_trade_risk_pct = safe_float(
            config.get("risk.per_trade_risk_pct", self.PER_TRADE_RISK_PCT * 100.0)
        ) / 100.0
        self._circuit_breaker_drawdown_pct = safe_float(
            config.get("risk.circuit_breaker_drawdown_pct", self.CIRCUIT_BREAKER_DRAWDOWN_PCT * 100.0)
        ) / 100.0

        # Mutable circuit-breaker state
        self._circuit_breaker_tripped: bool = False
        self._circuit_breaker_reason: str = ""
        self._circuit_breaker_date: str = ""

        # FASE 4: Daily drawdown circuit (4.3)
        self._daily_peak_capital: float = 0.0  # set on first trade of day
        self._daily_drawdown_circuit_tripped: bool = False
        self._daily_drawdown_circuit_date: str = ""

        # Accumulated metrics for reporting
        self._total_trades_closed: int = 0
        self._winning_trades: int = 0
        self._total_pnl: float = 0.0
        self._max_drawdown_observed: float = 0.0

    # ------------------------------------------------------------------
    # Entry gate
    # ------------------------------------------------------------------

    def can_enter(
        self,
        signal: Signal,
        portfolio: Any,  # PortfolioState
    ) -> Tuple[bool, str]:
        """Return (approved, reason) for a proposed entry signal.

        This is the single choke-point that enforces **all** risk rules.
        """
        # --- 1. Circuit breaker ---
        if self.is_circuit_breaker_tripped():
            return False, f"Circuit breaker active: {self._circuit_breaker_reason}"

        # --- 2. Daily reset check (embedded in portfolio) ---
        # PortfolioState auto-resets on date rollover, so we read current values.

        # --- 3. Max trades per day ---
        daily_trades = portfolio.daily_trades  # type: ignore
        if daily_trades >= self.MAX_DAILY_TRADES:
            return False, f"Daily trade limit reached ({daily_trades}/{self.MAX_DAILY_TRADES})"

        # --- 4. Max positions per asset ---
        positions = portfolio.positions  # type: ignore
        if signal.symbol in positions:
            return False, f"Already have a position in {signal.symbol}"

        # --- 5. Max total positions ---
        if len(positions) >= self._max_total_positions:
            return (
                False,
                f"Max total positions reached ({len(positions)}/{self._max_total_positions})",
            )

        # --- 6. Daily loss limit ---
        daily_pnl = portfolio.daily_pnl  # type: ignore
        capital = portfolio.current_capital  # type: ignore
        if capital > 0.0:
            daily_loss_pct = abs(min(daily_pnl, 0.0)) / capital
            if daily_loss_pct >= self._max_daily_loss_pct:
                self._trip_circuit_breaker(
                    f"Daily loss limit breached ({daily_loss_pct * 100:.2f}%)",
                )
                return False, self._circuit_breaker_reason

        # --- 7. Drawdown circuit breaker (max from peak) ---
        drawdown = portfolio.get_max_drawdown()  # type: ignore
        if drawdown >= self._circuit_breaker_drawdown_pct:
            self._trip_circuit_breaker(
                f"Max drawdown breached ({drawdown * 100:.2f}%)",
            )
            return False, self._circuit_breaker_reason

        # --- 8. Daily drawdown circuit (FASE 4.3) ---
        # Check daily drawdown from today's peak capital
        today = utc_now().strftime("%Y-%m-%d")
        if self._daily_drawdown_circuit_date != today:
            # New day — reset daily tracker
            self._daily_drawdown_circuit_tripped = False
            self._daily_drawdown_circuit_date = today
            self._daily_peak_capital = capital
        else:
            # Update daily peak if capital grew
            if capital > self._daily_peak_capital:
                self._daily_peak_capital = capital
        # Calculate daily drawdown
        if self._daily_peak_capital > 0.0:
            daily_dd = (self._daily_peak_capital - capital) / self._daily_peak_capital
            if daily_dd >= self.DAILY_DRAWDOWN_CIRCUIT_PCT:
                self._daily_drawdown_circuit_tripped = True
                logger.error(
                    "DAILY DRAWDOWN CIRCUIT: %.2f%% (limit %.2f%%). "
                    "Closing positions and stopping entries.",
                    daily_dd * 100,
                    self.DAILY_DRAWDOWN_CIRCUIT_PCT * 100,
                )
                return (
                    False,
                    f"Daily drawdown circuit: {daily_dd * 100:.2f}% >= "
                    f"{self.DAILY_DRAWDOWN_CIRCUIT_PCT * 100:.2f}%",
                )

        # --- 9. Directional exposure limit (FASE 4.1) ---
        # Max 60% of book in same direction (long or short)
        long_exposure, short_exposure = self._calculate_directional_exposure(
            positions, capital
        )
        if signal.side == "long":
            if long_exposure + (signal.size_pct * 100) > self.MAX_DIRECTIONAL_EXPOSURE_PCT * 100:
                return (
                    False,
                    f"Directional exposure limit: long={long_exposure:.1f}% + "
                    f"{signal.size_pct * 100:.1f}% > {self.MAX_DIRECTIONAL_EXPOSURE_PCT * 100:.0f}%",
                )
        else:
            if short_exposure + (signal.size_pct * 100) > self.MAX_DIRECTIONAL_EXPOSURE_PCT * 100:
                return (
                    False,
                    f"Directional exposure limit: short={short_exposure:.1f}% + "
                    f"{signal.size_pct * 100:.1f}% > {self.MAX_DIRECTIONAL_EXPOSURE_PCT * 100:.0f}%",
                )

        # --- 10. Sector exposure cap (FASE 4.2) ---
        # Max 30% of capital in crypto (if we had other asset classes)
        # For now, all positions are crypto so this is a total book cap
        total_exposure = long_exposure + short_exposure
        if total_exposure + (signal.size_pct * 100) > self.MAX_SECTOR_EXPOSURE_PCT * 100:
            return (
                False,
                f"Sector exposure limit: total={total_exposure:.1f}% + "
                f"{signal.size_pct * 100:.1f}% > {self.MAX_SECTOR_EXPOSURE_PCT * 100:.0f}%",
            )

        # --- 11. Confidence after overcrowded penalty ---
        effective_confidence = self._apply_overcrowded_penalty(signal)
        if effective_confidence < 0.5:
            return (
                False,
                f"Confidence too low after overcrowding penalty ({effective_confidence:.2f})",
            )

        # --- 12. Minimum capital sanity check ---
        if capital <= 0.0:
            return False, "Zero or negative capital — cannot trade"

        return True, "approved"

    def _apply_overcrowded_penalty(self, signal: Signal) -> float:
        """Reduce confidence if the asset is overcrowded."""
        overcrowded_score = safe_float(signal.metadata.get("overcrowded_score"), 0.0)
        if overcrowded_score > self.OVERCROWDED_THRESHOLD:
            return max(0.0, signal.confidence - self.OVERCROWDED_CONFIDENCE_PENALTY)
        return signal.confidence

    # ------------------------------------------------------------------
    # Directional exposure (FASE 4.1)
    # ------------------------------------------------------------------

    def _calculate_directional_exposure(
        self,
        positions: Dict[str, Any],
        capital: float,
    ) -> Tuple[float, float]:
        """Return (long_exposure_pct, short_exposure_pct) of capital.

        Each position's exposure = position notional / capital * 100
        """
        if capital <= 0.0:
            return 0.0, 0.0
        long_exposure = 0.0
        short_exposure = 0.0
        for pos in positions.values():
            notional = pos.entry_price * pos.size
            pct = (notional / capital) * 100.0
            if pos.side == "long":
                long_exposure += pct
            else:
                short_exposure += pct
        return long_exposure, short_exposure

    def get_directional_exposure(self, portfolio: Any) -> Tuple[float, float]:
        """Public accessor for directional exposure."""
        positions = portfolio.positions  # type: ignore
        capital = portfolio.current_capital  # type: ignore
        return self._calculate_directional_exposure(positions, capital)

    # ------------------------------------------------------------------
    # Daily drawdown circuit (FASE 4.3)
    # ------------------------------------------------------------------

    def is_daily_drawdown_circuit_tripped(self) -> bool:
        """Return True if the daily drawdown circuit is active.

        Auto-resets at 00:00 UTC.
        """
        if not self._daily_drawdown_circuit_tripped:
            return False
        today = utc_now().strftime("%Y-%m-%d")
        if today != self._daily_drawdown_circuit_date:
            logger.info("Daily drawdown circuit auto-reset — new UTC day: %s", today)
            self._daily_drawdown_circuit_tripped = False
            self._daily_drawdown_circuit_date = ""
            self._daily_peak_capital = 0.0
            return False
        return True

    def get_daily_drawdown(self, portfolio: Any) -> float:
        """Return current daily drawdown as fraction of today's peak."""
        today = utc_now().strftime("%Y-%m-%d")
        capital = portfolio.current_capital  # type: ignore
        if self._daily_drawdown_circuit_date != today:
            return 0.0
        if self._daily_peak_capital <= 0.0:
            return 0.0
        return (self._daily_peak_capital - capital) / self._daily_peak_capital

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calculate_position_size(
        self,
        signal: Signal,
        capital: float,
        atr_pct: float,
    ) -> float:
        """Return the base-asset position size (notional / price).

        Formula:
          risk_amount   = capital × per_trade_risk_pct
          stop_distance = max(2 × atr_pct, min_stop_distance_pct)
          notional      = risk_amount / stop_distance
          size          = notional / entry_price

        The result is clamped to max_position_size_pct of capital.

        Args:
            signal:  The entry signal (entry_price may be None → use current price)
            capital: Available trading capital
            atr_pct: ATR as a percentage of price (e.g. 0.02 = 2%)

        Returns:
            Position size in base-asset units (e.g. BTC amount).
        """
        price = safe_float(signal.entry_price)
        if price <= 0.0:
            logger.warning("calculate_position_size: invalid entry_price %s", signal.entry_price)
            return 0.0

        capital_f = safe_float(capital)
        if capital_f <= 0.0:
            return 0.0

        atr_pct_f = safe_float(atr_pct)
        if atr_pct_f <= 0.0:
            atr_pct_f = self.MIN_STOP_DISTANCE_PCT / 2.0

        # Risk amount in USD
        risk_amount = capital_f * self._per_trade_risk_pct

        # Stop distance = 2×ATR, floored at 0.5%
        stop_distance = max(2.0 * atr_pct_f, self.MIN_STOP_DISTANCE_PCT)

        # Notional position size in USD
        notional = safe_divide(risk_amount, stop_distance, 0.0)

        # Max notional = 20% of capital
        max_notional = capital_f * self.MAX_POSITION_SIZE_PCT
        notional = min(notional, max_notional)

        # Convert to base-asset size
        size = safe_divide(notional, price, 0.0)

        logger.debug(
            "Sizing: capital=%.2f risk=%.2f stop=%.4f notional=%.2f size=%.6f",
            capital_f,
            risk_amount,
            stop_distance,
            notional,
            size,
        )
        return size

    # ------------------------------------------------------------------
    # Post-trade
    # ------------------------------------------------------------------

    def on_trade_closed(self, trade: Any) -> None:
        """Update internal metrics when a trade closes.

        *trade* is expected to have attributes: pnl_usd, pnl_pct, symbol.
        """
        pnl = safe_float(getattr(trade, "pnl_usd", 0.0))
        self._total_trades_closed += 1
        self._total_pnl += pnl
        if pnl > 0.0:
            self._winning_trades += 1

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _trip_circuit_breaker(self, reason: str) -> None:
        """Trip the circuit breaker and record the reason."""
        self._circuit_breaker_tripped = True
        self._circuit_breaker_reason = reason
        self._circuit_breaker_date = utc_now().strftime("%Y-%m-%d")
        logger.error("CIRCUIT BREAKER TRIPPED: %s", reason)
        # Notify via alert system (best-effort)
        if self._notifier is not None:
            try:
                import asyncio
                asyncio.create_task(
                    self._notifier.circuit_breaker(reason=reason, action="Trading halted")
                )
            except Exception:
                pass

    def is_circuit_breaker_tripped(self) -> bool:
        """Return True if the circuit breaker is currently active.

        Auto-resets at 00:00 UTC so trading can resume the next day.
        """
        if not self._circuit_breaker_tripped:
            return False
        today = utc_now().strftime("%Y-%m-%d")
        if today != self._circuit_breaker_date:
            logger.info("Circuit breaker auto-reset — new UTC day: %s", today)
            self._circuit_breaker_tripped = False
            self._circuit_breaker_reason = ""
            self._circuit_breaker_date = ""
            return False
        return True

    def check_drawdown(self, drawdown_pct: float) -> bool:
        """Evaluate drawdown and trip circuit breaker if threshold breached.

        Returns True if the breaker was tripped by this call.
        """
        if self.is_circuit_breaker_tripped():
            return False
        if drawdown_pct >= self._circuit_breaker_drawdown_pct:
            self._trip_circuit_breaker(
                f"Max drawdown breached ({drawdown_pct * 100:.2f}%)",
            )
            return True
        return False

    def reset_circuit_breaker(self) -> None:
        """Manually reset the circuit breaker (owner override)."""
        logger.warning("Circuit breaker manually reset")
        self._circuit_breaker_tripped = False
        self._circuit_breaker_reason = ""
        self._circuit_breaker_date = ""

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_metrics(self) -> Dict[str, Any]:
        """Return a snapshot of current risk metrics."""
        win_rate = safe_divide(self._winning_trades, self._total_trades_closed, 0.0)
        return {
            "total_trades_closed": self._total_trades_closed,
            "winning_trades": self._winning_trades,
            "win_rate": win_rate,
            "total_pnl": self._total_pnl,
            "circuit_breaker_tripped": self._circuit_breaker_tripped,
            "circuit_breaker_reason": self._circuit_breaker_reason,
            "max_positions": self._max_total_positions,
            "per_trade_risk_pct": self._per_trade_risk_pct,
            "max_daily_loss_pct": self._max_daily_loss_pct,
            "circuit_breaker_drawdown_pct": self._circuit_breaker_drawdown_pct,
        }

    # ------------------------------------------------------------------
    # Dashboard-compatible properties (synchronous, read-only)
    # ------------------------------------------------------------------

    @property
    def daily_wins(self) -> int:
        """Total winning trades (dashboard metric)."""
        return self._winning_trades

    @property
    def daily_losses(self) -> int:
        """Total losing trades (dashboard metric)."""
        return max(0, self._total_trades_closed - self._winning_trades)

    @property
    def daily_pnl(self) -> float:
        """Total PnL across all closed trades (dashboard metric)."""
        return self._total_pnl

    @property
    def daily_trade_count(self) -> int:
        """Total trades closed (dashboard metric)."""
        return self._total_trades_closed

    @property
    def max_positions(self) -> int:
        """Maximum allowed open positions."""
        return self._max_total_positions

    @property
    def max_daily_trades(self) -> int:
        """Maximum allowed trades per day."""
        return self.MAX_DAILY_TRADES
