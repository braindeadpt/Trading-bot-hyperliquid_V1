"""Dashboard payload for Top Traders panel (research / virtual swing)."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from src.exchanges.top_trader_tracker import get_tracker
from src.research.top_trader_store import TopTraderStore
from src.research.top_trader_virtual_book import get_virtual_book
from src.utils.config import Config, get_strategy_section


def build_top_traders_panel_payload(
    *,
    config: Optional[Config] = None,
    engine: Any = None,
) -> Dict[str, Any]:
    """Assemble live bias + virtual book + recent closed trades."""
    now_ms = int(time.time() * 1000)
    tracker = get_tracker()
    book = get_virtual_book()
    if book is None and engine is not None:
        book = getattr(engine, "_top_trader_virtual_book", None)
    if tracker is None and engine is not None:
        tracker = getattr(engine, "_top_trader_tracker", None)

    store = TopTraderStore()
    cfg_section: Dict[str, Any] = {}
    if config is not None:
        try:
            cfg_section = get_strategy_section(config, "top_trader_flow")
        except Exception:  # noqa: BLE001
            cfg_section = {}

    wallets_n = len(tracker.wallets) if tracker is not None else 0
    last_poll_ms = int(getattr(tracker, "_last_poll_ms", 0) or 0) if tracker else 0
    last_error = getattr(tracker, "_last_error", None) if tracker else None
    lb_source = getattr(tracker, "_leaderboard_source", None) if tracker else None
    auto_lb = bool(getattr(tracker, "_auto_from_leaderboard", False)) if tracker else False
    enabled = bool(cfg_section.get("enabled", True))

    snapshots: List[Dict[str, Any]] = []
    live_snaps = tracker.all_snapshots() if tracker is not None else {}
    if live_snaps:
        for sym, snap in sorted(live_snaps.items()):
            snapshots.append(
                {
                    "symbol": snap.symbol,
                    "n_long": snap.n_long,
                    "n_short": snap.n_short,
                    "n_wallets": snap.n_wallets,
                    "long_notional_usd": snap.long_notional_usd,
                    "short_notional_usd": snap.short_notional_usd,
                    "net_bias": snap.net_bias,
                    "long_frac": snap.long_frac,
                    "updated_ms": snap.updated_ms,
                    "age_ms": max(0, now_ms - int(snap.updated_ms)),
                }
            )
    else:
        for sym, row in sorted(store.latest_bias_by_symbol().items()):
            ts = int(row.get("timestamp_ms") or 0)
            snapshots.append(
                {
                    "symbol": sym,
                    "n_long": int(row.get("n_long") or 0),
                    "n_short": int(row.get("n_short") or 0),
                    "n_wallets": int(row.get("n_long") or 0) + int(row.get("n_short") or 0),
                    "long_notional_usd": float(row.get("long_notional") or 0),
                    "short_notional_usd": float(row.get("short_notional") or 0),
                    "net_bias": float(row.get("net_bias") or 0),
                    "long_frac": float(row.get("long_frac") or 0),
                    "updated_ms": ts,
                    "age_ms": max(0, now_ms - ts) if ts else None,
                    "source": "db",
                }
            )

    open_positions = book.open_positions() if book is not None else []
    closed = book.recent_closed(limit=25) if book is not None else store.list_closed_trades(limit=25)

    empty_reason = None
    if wallets_n == 0:
        if auto_lb:
            empty_reason = "auto leaderboard pending first refresh (allTime durable top-N)"
        else:
            empty_reason = "fill data/research/top_traders.json with HL wallet addresses"
    elif not snapshots and not open_positions:
        empty_reason = "waiting for first clearinghouse poll"

    return {
        "generated_ms": now_ms,
        "enabled": enabled,
        "wallets_configured": wallets_n,
        "auto_from_leaderboard": auto_lb,
        "leaderboard_source": lb_source,
        "last_poll_ms": last_poll_ms or None,
        "last_error": last_error,
        "bias_threshold": float(cfg_section.get("bias_threshold", 0.55)),
        "max_hold_hours": float(cfg_section.get("max_hold_hours", 120)),
        "stop_loss_pct": float(cfg_section.get("stop_loss_pct", 0.04)),
        "take_profit_pct": float(cfg_section.get("take_profit_pct", 0.10)),
        "exit_style": "hybrid_flip_or_hold",
        "disclaimer": (
            "Research / virtual swing only — aggregate wallet bias, not copy-trade. "
            "No OMS execution. Wallets = durable HL leaderboard top-N when auto enabled."
        ),
        "empty_reason": empty_reason,
        "snapshots": snapshots,
        "open_positions": open_positions,
        "closed_trades": closed,
    }
