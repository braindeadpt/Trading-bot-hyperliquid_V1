"""Kelly Criterion position sizing (Task 4.4).

Uses historical win rate + risk/reward to compute optimal position size.
Applies Half-Kelly for safety (more conservative than full Kelly).

Formula:
  f* = (win_rate * avg_win - loss_rate * avg_loss) / avg_win
  size_multiplier = 0.5 * f*  (Half-Kelly)

Where:
  win_rate = winning_trades / total_trades
  loss_rate = 1 - win_rate
  avg_win = average profit of winning trades (as % of capital)
  avg_loss = average loss of losing trades (as % of capital)

v3.1.17 C10: ``record_trade`` now expects ``pnl_pct`` as a fraction of
**total capital** (e.g. 0.02 = +2% of capital), not a fraction of
position notional. The engine feeds ``result.pnl_pct_capital`` from
``ExecutionEngine.close_position``. Using the old PnL/notional value
for a 20%-of-capital trade would over-state the return by 5x and
mis-calibrate the Kelly multiplier.

If insufficient history, falls back to base size.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from src.utils.helpers import safe_float, safe_divide, clamp

logger = logging.getLogger(__name__)


class KellySizer:
    """Half-Kelly position size adjuster.

    Maintains a rolling window of trade results and computes the Kelly
    fraction.  The engine calls ``get_size_multiplier()`` before
    sizing each trade; the result is multiplied by the base signal
    ``size_pct``.
    """

    def __init__(
        self,
        min_trades: int = 20,           # minimum history before Kelly activates
        half_kelly: bool = True,        # use 0.5 * f* for safety
        max_multiplier: float = 2.0,    # cap Kelly boost
        min_multiplier: float = 0.25,   # floor Kelly reduction
        lookback_trades: int = 50,      # rolling window for stats
    ) -> None:
        self._min_trades = min_trades
        self._half_kelly = half_kelly
        self._max_multiplier = max_multiplier
        self._min_multiplier = min_multiplier
        self._lookback_trades = lookback_trades
        self._trade_history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_trade(self, pnl_pct: float) -> None:
        """Record a closed trade result for Kelly calculation.

        Args:
            pnl_pct: Profit/loss as a fraction of capital
                     (e.g. 0.02 = +2%, -0.01 = -1%)
        """
        self._trade_history.append({"pnl_pct": pnl_pct})
        # Keep rolling window
        if len(self._trade_history) > self._lookback_trades:
            self._trade_history.pop(0)

    def get_size_multiplier(self) -> float:
        """Return the Kelly-adjusted size multiplier.

        Returns 1.0 (no adjustment) if insufficient trade history.
        Otherwise returns a value between ``min_multiplier`` and
        ``max_multiplier``.
        """
        if len(self._trade_history) < self._min_trades:
            return 1.0

        wins = [t["pnl_pct"] for t in self._trade_history if t["pnl_pct"] > 0]
        losses = [t["pnl_pct"] for t in self._trade_history if t["pnl_pct"] <= 0]
        total = len(self._trade_history)

        if not wins or not losses:
            return 1.0

        win_rate = len(wins) / total
        loss_rate = 1.0 - win_rate
        avg_win = sum(wins) / len(wins)
        avg_loss = abs(sum(losses) / len(losses))  # positive number

        # Kelly formula: f* = (W * avg_win - L * avg_loss) / avg_win
        # This simplifies to: win_rate - (loss_rate * avg_loss / avg_win)
        if avg_win <= 0.0:
            return self._min_multiplier

        kelly = win_rate - (loss_rate * avg_loss / avg_win)

        if self._half_kelly:
            kelly = kelly * 0.5

        # Convert Kelly fraction to a safe size multiplier.
        # Kelly fraction is the optimal % of capital to risk.
        # We map it to a multiplier on the base signal size:
        #   Kelly <= 0     → min_multiplier (edge is negative)
        #   Kelly 0.05     → 0.5x
        #   Kelly 0.10     → 0.75x
        #   Kelly 0.20     → 1.0x (base)
        #   Kelly 0.40     → 1.5x
        #   Kelly >= 0.60  → max_multiplier (2.0x)
        if kelly <= 0.0:
            multiplier = self._min_multiplier
        elif kelly >= 0.60:
            multiplier = self._max_multiplier
        else:
            # Linear interpolation between (0.0, min) and (0.60, max)
            # At 0.20 → 1.0 (base)
            slope = (self._max_multiplier - self._min_multiplier) / 0.60
            multiplier = self._min_multiplier + kelly * slope

        logger.info(
            "Kelly sizing: win_rate=%.2f, avg_win=%.3f%%, avg_loss=%.3f%%, "
            "kelly=%.3f, multiplier=%.3f",
            win_rate,
            avg_win * 100,
            avg_loss * 100,
            kelly,
            multiplier,
        )

        return multiplier

    def get_stats(self) -> Dict[str, Any]:
        """Return current Kelly statistics for dashboard / logging."""
        wins = [t["pnl_pct"] for t in self._trade_history if t["pnl_pct"] > 0]
        losses = [t["pnl_pct"] for t in self._trade_history if t["pnl_pct"] <= 0]
        total = len(self._trade_history)

        return {
            "total_trades": total,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": len(wins) / total if total > 0 else 0.0,
            "avg_win_pct": (sum(wins) / len(wins) * 100) if wins else 0.0,
            "avg_loss_pct": (abs(sum(losses) / len(losses)) * 100) if losses else 0.0,
            "kelly_multiplier": self.get_size_multiplier(),
            "half_kelly": self._half_kelly,
            "sufficient_history": total >= self._min_trades,
        }
