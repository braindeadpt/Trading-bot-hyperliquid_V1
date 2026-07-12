"""Engine-owned runtime risk state (Fase 09 extraction).

Groups the shutdown-flatten policy, Kelly-sizer DB bootstrap, and
cooldown/risk persistence wiring that previously lived directly on
``TradingEngine``. The cooldown-state dict itself (``engine._cooldown_state``)
stays a plain attribute on ``TradingEngine`` — several tests build the engine
via ``TradingEngine.__new__(TradingEngine)`` and assign
``engine._cooldown_state = {}`` directly, and other tests read/write it via
that exact attribute path. This class only extracts the *logic* around it,
reading/writing ``engine._cooldown_state`` through the engine reference.

Zero behavior change vs. the code previously inlined in engine.py — this is
a straight extraction with ``self`` renamed to ``engine`` throughout.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.utils.config import get_strategy_section

from .runtime_state import persist_runtime_state

if TYPE_CHECKING:
    from src.core.engine import TradingEngine

logger = logging.getLogger(__name__)


class RiskState:
    """Shutdown policy, Kelly seeding, and runtime-state persistence."""

    def __init__(self, engine: "TradingEngine") -> None:
        self._engine = engine

    def should_close_positions_on_shutdown(self) -> bool:
        """True when graceful stop should flatten all open positions.

        Paper/testnet default is false (restore from DB on next start).
        Mainnet override is true until SL/TP are placed as native trigger
        orders on the exchange via the Hyperliquid SDK at entry time.
        """
        engine = self._engine
        explicit = engine._config.get("engine.close_positions_on_shutdown")
        if explicit is not None:
            return bool(explicit)
        return bool(engine._config.get("execution.flatten_on_stop", True))

    def seed_kelly_from_db(self) -> None:
        """Pre-load KellySizer from recent closed trades in SQLite."""
        engine = self._engine
        if not engine._kelly_enabled:
            return
        kelly_cfg = get_strategy_section(engine._config, "kelly")
        lookback = int(kelly_cfg.get("lookback_trades", 50))
        try:
            pnl_pcts = engine._db.get_recent_closed_trade_pnl_pcts(limit=lookback)
        except Exception as exc:  # noqa: BLE001
            logger.warning("KellySizer seed skipped — DB read failed: %s", exc)
            return
        if not pnl_pcts:
            logger.info("KellySizer seed: no closed trades in DB yet")
            return
        n = engine._kelly_sizer.seed_history(pnl_pcts)
        mult = engine._kelly_sizer.get_size_multiplier()
        logger.info(
            "KellySizer seeded with %d historical trades (multiplier=%.3f)",
            n,
            mult,
        )

    def persist_runtime_state(self) -> None:
        engine = self._engine
        persist_runtime_state(
            engine._db,
            cooldown_state=engine._cooldown_state,
            risk_snapshot=engine._risk.snapshot_for_persist(),
        )
