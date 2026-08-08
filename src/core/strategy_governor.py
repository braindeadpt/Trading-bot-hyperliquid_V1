"""Rolling performance governance — auto-disable weak sub-strategies."""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, Optional, Set

from src.data.database import Database
from src.utils.config import get_strategy_section
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)


class StrategyGovernor:
    """Disable sub-strategies with negative rolling Sharpe after enough trades.

    v3.1.48: never disable the last protected execution strategy — apply a size
    multiplier instead so the bot cannot become structurally inoperable.
    """

    def __init__(self, config: Any, db: Database) -> None:
        gov = config.get("strategy.strategy_governance", {}) or {}
        self._enabled = bool(gov.get("enabled", True))
        self._lookback_days = int(gov.get("lookback_days", 30))
        self._min_trades = int(gov.get("min_trades", 30))
        self._min_sharpe = safe_float(gov.get("min_sharpe", -1.0))
        self._eval_interval_ms = int(gov.get("eval_interval_ms", 3_600_000))
        self._min_active = max(1, int(gov.get("min_active_strategies", 1)))
        self._last_size_mult = safe_float(
            gov.get("last_strategy_size_multiplier", 0.5),
            default=0.5,
        )
        if self._last_size_mult <= 0.0 or self._last_size_mult > 1.0:
            self._last_size_mult = 0.5
        # Protected set = Phase08 execution strategies (never leave bot with zero).
        p08 = get_strategy_section(config, "phase08")
        self._protected: Set[str] = {
            str(s) for s in (p08.get("execution_strategies") or []) if s
        }
        # Fase 10 frozen-window floor: trades entered before this epoch-ms are
        # invisible to the governor.
        self._ignore_trades_before_ms = int(gov.get("ignore_trades_before_ms", 0))
        self._db = db
        self._disabled: Set[str] = set()
        self._size_multipliers: Dict[str, float] = {}
        self._last_eval_ms: int = 0
        self._last_metrics: Dict[str, Dict[str, float]] = {}

    @property
    def disabled_strategies(self) -> Set[str]:
        return set(self._disabled)

    @property
    def last_metrics(self) -> Dict[str, Dict[str, float]]:
        return dict(self._last_metrics)

    @property
    def protected_strategies(self) -> Set[str]:
        return set(self._protected)

    def is_enabled(self, strategy_name: str) -> bool:
        if not self._enabled:
            return True
        return strategy_name not in self._disabled

    def size_multiplier(self, strategy_name: str) -> float:
        """Return size scale for strategies kept alive under the last-exec floor."""
        if not self._enabled:
            return 1.0
        return float(self._size_multipliers.get(strategy_name, 1.0))

    def evaluate(self, now_ms: int) -> None:
        """Recompute disabled set from closed trades in the lookback window."""
        if not self._enabled:
            return
        if now_ms - self._last_eval_ms < self._eval_interval_ms:
            return
        self._last_eval_ms = now_ms

        since_ms = max(
            now_ms - self._lookback_days * 86_400_000,
            self._ignore_trades_before_ms,
        )
        by_strategy = self._db.get_metrics_by_strategy(since_ms)

        newly_disabled: Set[str] = set()
        newly_enabled: Set[str] = set()
        metrics_out: Dict[str, Dict[str, float]] = {}

        for name, m in by_strategy.items():
            if name in ("StrategyEnsemble", "unknown"):
                continue
            trades = int(m.get("total_trades", 0))
            sharpe = safe_float(m.get("sharpe_ratio", 0.0))
            metrics_out[name] = {
                "trades": float(trades),
                "sharpe": sharpe,
                "total_pnl": safe_float(m.get("total_pnl", 0.0)),
                "win_rate": safe_float(m.get("win_rate", 0.0)),
            }
            if trades < self._min_trades:
                if name in self._disabled:
                    newly_enabled.add(name)
                continue
            if sharpe < self._min_sharpe:
                newly_disabled.add(name)
            elif name in self._disabled:
                newly_enabled.add(name)

        # Apply enables first so the active-floor check sees the post-enable set.
        for name in newly_enabled:
            if name in self._disabled:
                self._disabled.discard(name)
                self._size_multipliers.pop(name, None)
                logger.info(
                    "StrategyGovernor RE-ENABLED %s — Sharpe %.3f (%d trades)",
                    name,
                    metrics_out.get(name, {}).get("sharpe", 0.0),
                    int(metrics_out.get(name, {}).get("trades", 0)),
                )

        for name in newly_disabled:
            if name in self._disabled:
                continue
            if self._would_breach_min_active(name):
                self._size_multipliers[name] = self._last_size_mult
                logger.warning(
                    "StrategyGovernor PROTECTED %s — would disable last "
                    "execution strategy (min_active=%d); applying size×%.2f "
                    "instead (Sharpe %.3f < %.3f over %d trades)",
                    name,
                    self._min_active,
                    self._last_size_mult,
                    metrics_out.get(name, {}).get("sharpe", 0.0),
                    self._min_sharpe,
                    int(metrics_out.get(name, {}).get("trades", 0)),
                )
                continue
            self._disabled.add(name)
            self._size_multipliers.pop(name, None)
            logger.warning(
                "StrategyGovernor DISABLED %s — Sharpe %.3f < %.3f over %d trades (%dd)",
                name,
                metrics_out[name]["sharpe"],
                self._min_sharpe,
                int(metrics_out[name]["trades"]),
                self._lookback_days,
            )

        # Stuck-disabled fix: strategy absent from window → re-enable.
        for name in list(self._disabled):
            if name not in by_strategy:
                self._disabled.discard(name)
                self._size_multipliers.pop(name, None)
                logger.info(
                    "StrategyGovernor RE-ENABLED %s — no trades in window (%dd)",
                    name,
                    self._lookback_days,
                )

        self._last_metrics = metrics_out

    def _would_breach_min_active(self, candidate: str) -> bool:
        """True if disabling *candidate* would leave fewer than min_active protected.

        Only Phase08 ``execution_strategies`` are protected. Shadow / other
        strategies can still be fully disabled. When no execution set is
        configured, the floor does not apply (legacy disable behaviour).
        """
        if not self._protected or candidate not in self._protected:
            return False
        active_protected = {
            n for n in self._protected if n not in self._disabled
        }
        if candidate in active_protected:
            active_protected.discard(candidate)
        return len(active_protected) < self._min_active

    @staticmethod
    def trade_sharpe(returns_pct: list[float]) -> float:
        """Simplified Sharpe on per-trade return series (percent units)."""
        if len(returns_pct) < 2:
            return 0.0
        mean = sum(returns_pct) / len(returns_pct)
        var = sum((r - mean) ** 2 for r in returns_pct) / (len(returns_pct) - 1)
        stdev = math.sqrt(var) if var > 0 else 0.0
        if stdev <= 0:
            return 0.0
        return (mean / stdev) * math.sqrt(len(returns_pct))
