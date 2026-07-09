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
      - Max trades per day (0 = unlimited; resets at 00:00 UTC)
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
    MAX_SECTOR_EXPOSURE_PCT: float = 1.0       # 4.2: disabled by default (crypto-only bot); set < 1.0 for multi-asset
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
        # v3.1.16 C6: honour risk.max_position_size_pct from config (5% default,
        # 3% mainnet override) instead of the hard-coded 20% ceiling. Cap at
        # the class-level constant so a misconfigured YAML can never allow
        # positions larger than 20% of capital.
        config_max_pos_pct = safe_float(
            config.get("risk.max_position_size_pct", 5.0)
        ) / 100.0
        self._max_position_size_pct = min(config_max_pos_pct, self.MAX_POSITION_SIZE_PCT)

        self._max_daily_trades = int(
            config.get("risk.max_daily_trades", self.MAX_DAILY_TRADES)
        )
        self._max_daily_stop_losses = int(
            config.get("risk.max_daily_stop_losses", 4)
        )

        # Daily stop-loss streak circuit (blocks entries after N stop_loss exits/day)
        self._daily_stop_loss_count: int = 0
        self._daily_stop_streak_date: str = ""
        self._daily_stop_streak_tripped: bool = False

        # v3.1.22: read leverage from config. Capped at 20x as an
        # absolute safety floor — a misconfigured 1000x leverage would
        # liquidate on the first 0.1% move.
        config_leverage = safe_float(config.get("risk.leverage_max", 1.0))
        self._leverage_max = max(1.0, min(config_leverage, 20.0))

        # v3.1.22: per-trade risk budget is 1/3 of the daily loss
        # budget, so a worst-case 3-loss streak on the same day
        # still keeps us under the daily CB. Configurable via
        # risk.per_trade_risk_fraction_of_daily_loss (default 0.33).
        daily_loss_pct = self._max_daily_loss_pct
        per_trade_frac = safe_float(
            config.get("risk.per_trade_risk_fraction_of_daily_loss", 33.0)
        ) / 100.0
        per_trade_frac = clamp(per_trade_frac, 0.05, 1.0)
        if daily_loss_pct > 0.0:
            capped_per_trade = daily_loss_pct * per_trade_frac
            # Don't override a larger per_trade_risk_pct in config, but
            # do clamp it down if the per-day budget can't support it.
            max_per_trade = min(self._per_trade_risk_pct, capped_per_trade)
            self._per_trade_risk_pct_effective = max_per_trade
        else:
            self._per_trade_risk_pct_effective = self._per_trade_risk_pct

        gov = config.get("strategy.portfolio_governance", {})
        self._max_directional_exposure_pct = safe_float(
            gov.get("max_directional_exposure_pct", self.MAX_DIRECTIONAL_EXPOSURE_PCT * 100.0)
        ) / 100.0
        self._max_sector_exposure_pct = safe_float(
            gov.get("max_sector_exposure_pct", self.MAX_SECTOR_EXPOSURE_PCT * 100.0)
        ) / 100.0
        self._daily_drawdown_circuit_pct = safe_float(
            gov.get("daily_drawdown_circuit_pct", self.DAILY_DRAWDOWN_CIRCUIT_PCT * 100.0)
        ) / 100.0

        # Mutable circuit-breaker state
        self._circuit_breaker_tripped: bool = False
        self._circuit_breaker_reason: str = ""
        self._circuit_breaker_date: str = ""
        self._last_drawdown_pct: float = 0.0
        self._circuit_breaker_recovery_pct = safe_float(
            config.get("risk.circuit_breaker_recovery_pct", 50.0)
        ) / 100.0

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

        # --- 1b. Daily stop-loss streak circuit ---
        if self._is_daily_stop_streak_blocked():
            return False, (
                f"daily_stop_streak_circuit "
                f"({self._daily_stop_loss_count}/{self._max_daily_stop_losses} stops today)"
            )

        # --- 2. Daily reset check (embedded in portfolio) ---
        # PortfolioState auto-resets on date rollover, so we read current values.

        # --- 3. Max trades per day (0 = disabled) ---
        if self._max_daily_trades > 0:
            daily_trades = portfolio.daily_trades  # type: ignore
            if daily_trades >= self._max_daily_trades:
                return False, (
                    f"Daily trade limit reached ({daily_trades}/{self._max_daily_trades})"
                )

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
            if daily_dd >= self._daily_drawdown_circuit_pct:
                self._daily_drawdown_circuit_tripped = True
                logger.error(
                    "DAILY DRAWDOWN CIRCUIT: %.2f%% (limit %.2f%%). "
                    "Closing positions and stopping entries.",
                    daily_dd * 100,
                    self._daily_drawdown_circuit_pct * 100,
                )
                return (
                    False,
                    f"Daily drawdown circuit: {daily_dd * 100:.2f}% >= "
                    f"{self._daily_drawdown_circuit_pct * 100:.2f}%",
                )

        # --- 9. Directional exposure limit (FASE 4.1) ---
        atr_pct = self._derive_atr_pct(signal)
        new_notional_pct = safe_divide(
            self.estimate_entry_notional(signal, capital, atr_pct),
            capital,
            0.0,
        ) * 100.0
        long_exposure, short_exposure = self._calculate_directional_exposure(
            positions, capital
        )
        if signal.side == "long":
            if long_exposure + new_notional_pct > self._max_directional_exposure_pct * 100:
                return (
                    False,
                    f"Directional exposure limit: long={long_exposure:.1f}% + "
                    f"{new_notional_pct:.1f}% > {self._max_directional_exposure_pct * 100:.0f}%",
                )
        else:
            if short_exposure + new_notional_pct > self._max_directional_exposure_pct * 100:
                return (
                    False,
                    f"Directional exposure limit: short={short_exposure:.1f}% + "
                    f"{new_notional_pct:.1f}% > {self._max_directional_exposure_pct * 100:.0f}%",
                )

        # --- 10. Sector exposure cap (FASE 4.2) ---
        total_exposure = long_exposure + short_exposure
        if total_exposure + new_notional_pct > self._max_sector_exposure_pct * 100:
            return (
                False,
                f"Sector exposure limit: total={total_exposure:.1f}% + "
                f"{new_notional_pct:.1f}% > {self._max_sector_exposure_pct * 100:.0f}%",
            )

        # --- 11. Portfolio leverage limit ---
        open_notional = self.get_portfolio_total_notional(positions)
        new_notional = self.estimate_entry_notional(signal, capital, atr_pct)
        projected_leverage = safe_divide(open_notional + new_notional, capital, 0.0)
        if projected_leverage > self._leverage_max + 1e-9:
            return (
                False,
                f"Portfolio leverage limit: {projected_leverage:.1f}x > "
                f"{self._leverage_max:.1f}x",
            )

        # --- 12. Confidence after overcrowded penalty ---
        effective_confidence = self._apply_overcrowded_penalty(signal)
        if effective_confidence < 0.5:
            return (
                False,
                f"Confidence too low after overcrowding penalty ({effective_confidence:.2f})",
            )

        # --- 13. Minimum capital sanity check ---
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
    # Sizing helpers
    # ------------------------------------------------------------------

    def _derive_atr_pct(self, signal: Signal) -> float:
        """Estimate ATR% from signal metadata or stop distance."""
        atr_pct = safe_float(signal.metadata.get("atr_pct"))
        if atr_pct <= 0.0:
            atr_pct = safe_divide(safe_float(signal.stop_loss_pct), 2.0, 0.0)
        if atr_pct <= 0.0:
            atr_pct = self.MIN_STOP_DISTANCE_PCT / 2.0
        return atr_pct

    def _resolve_stop_distance_pct(self, signal: Signal, atr_pct: float) -> float:
        """Stop distance used for risk→notional conversion (matches engine exits)."""
        sl_pct = safe_float(signal.stop_loss_pct)
        if sl_pct > 0.0:
            return max(sl_pct, self.MIN_STOP_DISTANCE_PCT)
        return max(2.0 * safe_float(atr_pct), self.MIN_STOP_DISTANCE_PCT)

    def _compute_notional_bounds(
        self,
        signal: Signal,
        capital: float,
        atr_pct: float,
    ) -> Dict[str, float]:
        """Return notional caps and the binding final notional for a signal."""
        capital_f = safe_float(capital)
        if capital_f <= 0.0:
            return {
                "notional_risk": 0.0,
                "notional_conviction": 0.0,
                "max_notional": 0.0,
                "notional_final": 0.0,
                "stop_distance": self.MIN_STOP_DISTANCE_PCT,
                "effective_risk_pct": 0.0,
                "implicit_leverage": 0.0,
            }

        stop_distance = self._resolve_stop_distance_pct(signal, atr_pct)
        size_pct = max(0.0, safe_float(signal.size_pct))

        risk_amount = capital_f * self._per_trade_risk_pct_effective
        notional_risk = safe_divide(risk_amount, stop_distance, 0.0)

        if size_pct > 0.0:
            notional_conviction = safe_divide(capital_f * size_pct, stop_distance, 0.0)
        else:
            notional_conviction = notional_risk

        max_notional = capital_f * self._max_position_size_pct * self._leverage_max
        notional_final = min(notional_risk, notional_conviction, max_notional)

        effective_risk_pct = safe_divide(
            notional_final * stop_distance,
            capital_f,
            0.0,
        )
        implicit_leverage = safe_divide(notional_final, capital_f, 0.0)

        return {
            "notional_risk": notional_risk,
            "notional_conviction": notional_conviction,
            "max_notional": max_notional,
            "notional_final": notional_final,
            "stop_distance": stop_distance,
            "effective_risk_pct": effective_risk_pct,
            "implicit_leverage": implicit_leverage,
        }

    def estimate_entry_notional(
        self,
        signal: Signal,
        capital: float,
        atr_pct: Optional[float] = None,
    ) -> float:
        """Estimate USD notional for a proposed entry (pre-trade gate checks)."""
        atr = safe_float(atr_pct) if atr_pct is not None else self._derive_atr_pct(signal)
        return self._compute_notional_bounds(signal, capital, atr)["notional_final"]

    @staticmethod
    def get_portfolio_total_notional(
        positions: Dict[str, Any],
        mark_prices: Optional[Dict[str, float]] = None,
    ) -> float:
        """Sum open-position notionals (USD), mark-to-market when prices available."""
        total = 0.0
        mark_prices = mark_prices or {}
        for sym, pos in positions.items():
            size = safe_float(getattr(pos, "size", 0))
            if size <= 0.0:
                continue
            mark = safe_float(mark_prices.get(sym), 0.0)
            if mark <= 0.0:
                mark = safe_float(getattr(pos, "current_price", 0.0), 0.0)
            if mark <= 0.0:
                mark = safe_float(getattr(pos, "entry_price", 0.0), 0.0)
            total += mark * size
        return total

    def get_portfolio_leverage(
        self,
        positions: Dict[str, Any],
        capital: float,
        mark_prices: Optional[Dict[str, float]] = None,
    ) -> float:
        """Portfolio leverage = total open notional / equity."""
        capital_f = safe_float(capital)
        if capital_f <= 0.0:
            return 0.0
        return safe_divide(
            self.get_portfolio_total_notional(positions, mark_prices),
            capital_f,
            0.0,
        )

    @staticmethod
    def get_open_risk_pct(
        positions: Dict[str, Any],
        capital: float,
    ) -> float:
        """Capital at risk to stop-loss across open positions (% of equity)."""
        capital_f = safe_float(capital)
        if capital_f <= 0.0:
            return 0.0
        total_risk = 0.0
        for pos in positions.values():
            entry = safe_float(getattr(pos, "entry_price", 0.0))
            size = safe_float(getattr(pos, "size", 0.0))
            if entry <= 0.0 or size <= 0.0:
                continue
            sl = getattr(pos, "stop_loss_price", None)
            side = str(getattr(pos, "side", "long")).lower()
            if sl is not None and safe_float(sl) > 0.0:
                sl_f = safe_float(sl)
                if side == "long":
                    total_risk += max(0.0, (entry - sl_f) * size)
                else:
                    total_risk += max(0.0, (sl_f - entry) * size)
            else:
                meta = getattr(pos, "metadata", None) or {}
                sl_pct = safe_float(meta.get("stop_loss_pct", 0.0))
                if sl_pct > 0.0:
                    total_risk += entry * size * sl_pct
        return safe_divide(total_risk, capital_f, 0.0) * 100.0

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

        ``size_pct`` is the fraction of capital to **risk** (per Signal docstring),
        not the target notional. Kelly adjustments are applied upstream in the
        engine before this call.

        Formula:
          stop_distance       = max(signal.stop_loss_pct, 2×ATR, min_stop_distance_pct)
          notional_risk       = (capital × per_trade_risk_pct_effective) / stop_distance
          notional_conviction = (capital × size_pct) / stop_distance
          max_notional        = capital × max_position_size_pct × leverage_max
          notional_final      = min(notional_risk, notional_conviction, max_notional)
          size                = notional_final / entry_price

        Effective risk ≈ notional_final × stop_distance / capital.
        Implicit leverage per position = notional_final / capital.

        Example (capital $10k, size_pct 1%, stop 1.2%):
          conviction notional ≈ $8,333 → capped to $5,000 (5%×10x) → risk ≈ 0.6%.

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
            atr_pct_f = self._derive_atr_pct(signal)

        bounds = self._compute_notional_bounds(signal, capital_f, atr_pct_f)
        notional_final = bounds["notional_final"]
        stop_distance = bounds["stop_distance"]
        size_pct = max(0.0, safe_float(signal.size_pct))

        size = safe_divide(notional_final, price, 0.0)

        logger.info(
            "Sizing %s %s: capital=%.0f risk_pct=%.3f%% stop=%.3f%% "
            "notional=%.0f (risk_cap=%.0f conviction=%.0f max=%.0f) "
            "implicit_lev=%.2fx size=%.6f",
            signal.symbol,
            signal.side,
            capital_f,
            bounds["effective_risk_pct"] * 100.0,
            stop_distance * 100.0,
            notional_final,
            bounds["notional_risk"],
            bounds["notional_conviction"],
            bounds["max_notional"],
            bounds["implicit_leverage"],
            size,
        )
        return size

    # ------------------------------------------------------------------
    # Post-trade
    # ------------------------------------------------------------------

    def on_trade_closed(self, trade: Any) -> None:
        """Update internal metrics when a trade closes.

        *trade* is expected to have attributes: pnl_usd, pnl_pct, symbol, reason.
        """
        pnl = safe_float(getattr(trade, "pnl_usd", 0.0))
        self._total_trades_closed += 1
        self._total_pnl += pnl
        if pnl > 0.0:
            self._winning_trades += 1

        reason = str(getattr(trade, "reason", "") or "")
        if reason.startswith("stop_loss") and self._max_daily_stop_losses > 0:
            self._record_daily_stop_loss()

    def _reset_daily_stop_streak_if_new_day(self) -> None:
        today = utc_now().strftime("%Y-%m-%d")
        if self._daily_stop_streak_date != today:
            self._daily_stop_streak_date = today
            self._daily_stop_loss_count = 0
            self._daily_stop_streak_tripped = False

    def _is_daily_stop_streak_blocked(self) -> bool:
        if self._max_daily_stop_losses <= 0:
            return False
        self._reset_daily_stop_streak_if_new_day()
        return self._daily_stop_streak_tripped

    def _record_daily_stop_loss(self) -> None:
        self._reset_daily_stop_streak_if_new_day()
        self._daily_stop_loss_count += 1
        if self._daily_stop_loss_count >= self._max_daily_stop_losses:
            self._daily_stop_streak_tripped = True
            msg = (
                f"daily_stop_streak_circuit: {self._daily_stop_loss_count} "
                f"stop_loss exits today (limit {self._max_daily_stop_losses})"
            )
            logger.error("DAILY STOP STREAK CIRCUIT: %s", msg)
            if self._notifier is not None:
                try:
                    import asyncio
                    asyncio.create_task(
                        self._notifier.circuit_breaker(
                            reason=msg,
                            action="New entries blocked until 00:00 UTC",
                        )
                    )
                except Exception:
                    pass

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

        Auto-resets at 00:00 UTC or when drawdown recovers below recovery band.
        """
        if not self._circuit_breaker_tripped:
            return False
        today = utc_now().strftime("%Y-%m-%d")
        if today != self._circuit_breaker_date:
            logger.info("Circuit breaker auto-reset — new UTC day: %s", today)
            self.reset_circuit_breaker()
            return False
        recovery_level = (
            self._circuit_breaker_drawdown_pct * self._circuit_breaker_recovery_pct
        )
        if self._last_drawdown_pct < recovery_level:
            logger.info(
                "Circuit breaker auto-reset — drawdown %.2f%% below recovery %.2f%%",
                self._last_drawdown_pct * 100.0,
                recovery_level * 100.0,
            )
            self.reset_circuit_breaker()
            return False
        return True

    def check_drawdown(self, drawdown_pct: float) -> bool:
        """Evaluate drawdown and trip circuit breaker if threshold breached.

        Returns True if the breaker was tripped by this call.
        """
        self._last_drawdown_pct = max(0.0, safe_float(drawdown_pct))
        if self.is_circuit_breaker_tripped():
            return False
        if drawdown_pct >= self._circuit_breaker_drawdown_pct:
            self._trip_circuit_breaker(
                f"Max drawdown breached ({drawdown_pct * 100:.2f}%)",
            )
            return True
        return False

    def reset_circuit_breaker(self) -> None:
        """Reset the circuit breaker (manual or auto-recovery)."""
        self._circuit_breaker_tripped = False
        self._circuit_breaker_reason = ""
        self._circuit_breaker_date = ""

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_metrics(self, portfolio: Any = None) -> Dict[str, Any]:
        """Return a snapshot of current risk metrics."""
        win_rate = safe_divide(self._winning_trades, self._total_trades_closed, 0.0)
        metrics: Dict[str, Any] = {
            "total_trades_closed": self._total_trades_closed,
            "winning_trades": self._winning_trades,
            "win_rate": win_rate,
            "total_pnl": self._total_pnl,
            "circuit_breaker_tripped": self._circuit_breaker_tripped,
            "circuit_breaker_reason": self._circuit_breaker_reason,
            "max_positions": self._max_total_positions,
            "per_trade_risk_pct": self._per_trade_risk_pct,
            "per_trade_risk_pct_effective": self._per_trade_risk_pct_effective,
            "max_daily_loss_pct": self._max_daily_loss_pct,
            "circuit_breaker_drawdown_pct": self._circuit_breaker_drawdown_pct,
            "leverage_max": self._leverage_max,
            "max_position_size_pct": self._max_position_size_pct,
        }
        if portfolio is not None:
            capital = portfolio.current_capital  # type: ignore
            positions = portfolio.positions  # type: ignore
            total_notional = self.get_portfolio_total_notional(positions)
            lev = self.get_portfolio_leverage(positions, capital)
            long_exp, short_exp = self._calculate_directional_exposure(positions, capital)
            metrics.update({
                "portfolio_leverage": lev,
                "total_notional_usd": total_notional,
                "long_exposure_pct": long_exp,
                "short_exposure_pct": short_exp,
            })
        return metrics

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
        return self._max_daily_trades
