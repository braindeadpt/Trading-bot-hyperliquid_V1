"""Persist and rebuild runtime governance state across restarts."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.utils.helpers import safe_float, utc_timestamp_ms

if TYPE_CHECKING:
    from src.core.risk_manager import RiskManager
    from src.data.database import Database

logger = logging.getLogger(__name__)

_RUNTIME_STATE_KEY = "engine_runtime_v1"


def _utc_midnight_ms() -> int:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def rebuild_cooldown_state(
    db: "Database",
    *,
    base_ms: int,
    max_ms: int,
    multiplier: float,
) -> Dict[str, Dict[str, Any]]:
    """Reconstruct per-(strategy,symbol) cooldown from today's closed trades."""
    since_ms = _utc_midnight_ms()
    rows = db.get_closed_trades_since(since_ms, limit=500)
    if not rows:
        return {}

    # Process oldest → newest so consecutive-loss streak is correct.
    rows = sorted(rows, key=lambda r: int(r.get("exit_time") or 0))
    state: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        strategy = str(row.get("strategy") or "unknown")
        symbol = str(row.get("symbol") or "?")
        key = f"{strategy}:{symbol}"
        pnl_pct = safe_float(row.get("pnl_pct"), 0.0)
        exit_ms = int(row.get("exit_time") or 0)
        prev = state.get(key, {})
        consecutive = int(prev.get("consecutive_losses", 0))
        if pnl_pct > 0:
            consecutive = 0
            duration_ms = base_ms
        else:
            consecutive += 1
            duration_ms = min(
                int(base_ms * (multiplier ** consecutive)),
                max_ms,
            )
        state[key] = {
            "last_trade_ms": exit_ms,
            "duration_ms": duration_ms,
            "consecutive_losses": consecutive,
            "adx": None,
            "funding": None,
        }
    return state


def persist_runtime_state(
    db: "Database",
    *,
    cooldown_state: Dict[str, Dict[str, Any]],
    risk_snapshot: Dict[str, Any],
) -> None:
    """Best-effort persistence of cooldown + risk circuit state."""
    payload = {
        "cooldown_state": cooldown_state,
        "risk": risk_snapshot,
        "saved_ms": utc_timestamp_ms(),
    }
    try:
        db.save_runtime_state(_RUNTIME_STATE_KEY, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to persist runtime state: %s", exc)


def restore_runtime_state(
    db: "Database",
    risk: "RiskManager",
    *,
    base_ms: int,
    max_ms: int,
    multiplier: float,
    portfolio_daily_peak: float = 0.0,
    portfolio_capital: float = 0.0,
) -> Dict[str, Dict[str, Any]]:
    """Load persisted state or rebuild cooldown/stop-streak from SQLite."""
    since_ms = _utc_midnight_ms()
    saved = db.load_runtime_state(_RUNTIME_STATE_KEY)
    cooldown_state: Dict[str, Dict[str, Any]] = {}

    if saved:
        raw_cd = saved.get("cooldown_state")
        if isinstance(raw_cd, dict):
            cooldown_state = {
                str(k): dict(v) for k, v in raw_cd.items() if isinstance(v, dict)
            }
        risk.restore_snapshot(saved.get("risk") or {}, since_ms=since_ms)

    if not cooldown_state:
        cooldown_state = rebuild_cooldown_state(
            db,
            base_ms=base_ms,
            max_ms=max_ms,
            multiplier=multiplier,
        )

    stop_count = db.count_stop_loss_exits_since(since_ms)
    risk.restore_daily_stop_streak(stop_count)

    peak = portfolio_daily_peak
    if peak <= 0.0:
        peak = portfolio_capital
    risk.seed_daily_drawdown(peak_capital=peak, current_capital=portfolio_capital)

    if cooldown_state:
        logger.info(
            "Runtime state restored: cooldown_pairs=%d stop_streak=%d daily_dd_tripped=%s cb=%s",
            len(cooldown_state),
            risk.daily_stop_loss_count,
            risk.is_daily_drawdown_circuit_tripped(),
            risk.is_circuit_breaker_tripped(),
        )
    return cooldown_state
