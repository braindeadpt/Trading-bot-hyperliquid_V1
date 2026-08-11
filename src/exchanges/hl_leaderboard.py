"""Hyperliquid public leaderboard (stats-data) — durable top-wallet selection.

Source: ``https://stats-data.hyperliquid.xyz/Mainnet/leaderboard``
(same feed the official app leaderboard uses). No API key.

Default ranking prefers **allTime** PnL with account-value / volume floors so
day-lottery wallets and thin-volume outliers are filtered out.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import aiohttp

from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
VALID_WINDOWS = frozenset({"day", "week", "month", "allTime"})


@dataclass(frozen=True)
class LeaderboardWallet:
    """One ranked durable trader from the HL stats leaderboard."""

    address: str
    rank: int
    account_value: float
    window: str
    pnl: float
    roi: float
    volume: float
    display_name: str = ""


def _window_perf(row: Dict[str, Any], window: str) -> Dict[str, float]:
    for item in row.get("windowPerformances") or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        if str(item[0]) != window:
            continue
        perf = item[1] if isinstance(item[1], dict) else {}
        return {
            "pnl": safe_float(perf.get("pnl")),
            "roi": safe_float(perf.get("roi")),
            "vlm": safe_float(perf.get("vlm")),
        }
    return {"pnl": 0.0, "roi": 0.0, "vlm": 0.0}


def select_durable_top(
    rows: Sequence[Dict[str, Any]],
    *,
    top_n: int = 10,
    window: str = "allTime",
    min_account_value: float = 100_000.0,
    min_volume: float = 5_000_000.0,
    min_pnl: float = 0.0,
    require_month_positive: bool = True,
) -> List[LeaderboardWallet]:
    """Filter + rank leaderboard rows for long-horizon reliability."""
    win = window if window in VALID_WINDOWS else "allTime"
    candidates: List[tuple] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        addr = str(row.get("ethAddress") or "").strip().lower()
        if not (addr.startswith("0x") and len(addr) >= 42):
            continue
        av = safe_float(row.get("accountValue"))
        if av < min_account_value:
            continue
        perf = _window_perf(row, win)
        if perf["pnl"] < min_pnl:
            continue
        if perf["vlm"] < min_volume:
            continue
        if require_month_positive and win == "allTime":
            month = _window_perf(row, "month")
            if month["pnl"] <= 0:
                continue
        name = str(row.get("displayName") or "")
        candidates.append((perf["pnl"], addr, av, perf, name))

    candidates.sort(key=lambda t: t[0], reverse=True)
    out: List[LeaderboardWallet] = []
    for i, (pnl, addr, av, perf, name) in enumerate(candidates[: max(1, int(top_n))]):
        out.append(
            LeaderboardWallet(
                address=addr,
                rank=i + 1,
                account_value=av,
                window=win,
                pnl=float(pnl),
                roi=float(perf["roi"]),
                volume=float(perf["vlm"]),
                display_name=name,
            )
        )
    return out


def wallets_payload(
    wallets: Sequence[LeaderboardWallet],
    *,
    notes: str = "",
) -> Dict[str, Any]:
    """JSON shape written to ``data/research/top_traders.json``."""
    return {
        "updated_ms": int(time.time() * 1000),
        "source": "stats-data.hyperliquid.xyz/Mainnet/leaderboard",
        "notes": notes
        or "Auto-selected durable top wallets (allTime PnL + filters). Shadow only.",
        "wallets": [
            {
                "address": w.address,
                "rank": w.rank,
                "label": w.display_name or f"hl_lb_{w.rank}",
                "account_value": w.account_value,
                "window": w.window,
                "pnl": w.pnl,
                "roi": w.roi,
                "volume": w.volume,
            }
            for w in wallets
        ],
    }


async def fetch_leaderboard_rows(
    session: Optional[aiohttp.ClientSession] = None,
    *,
    url: str = LEADERBOARD_URL,
    timeout_sec: float = 60.0,
) -> List[Dict[str, Any]]:
    """Download full leaderboard JSON (can be tens of thousands of rows)."""
    own_session = session is None
    sess = session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout_sec),
        headers={"User-Agent": "hl-premium-bot/top-trader-leaderboard"},
    )
    try:
        async with sess.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    finally:
        if own_session:
            await sess.close()
    if not isinstance(data, dict):
        return []
    rows = data.get("leaderboardRows") or []
    return list(rows) if isinstance(rows, list) else []


async def fetch_durable_top_wallets(
    *,
    top_n: int = 10,
    window: str = "allTime",
    min_account_value: float = 100_000.0,
    min_volume: float = 5_000_000.0,
    min_pnl: float = 0.0,
    require_month_positive: bool = True,
    session: Optional[aiohttp.ClientSession] = None,
) -> List[LeaderboardWallet]:
    rows = await fetch_leaderboard_rows(session=session)
    selected = select_durable_top(
        rows,
        top_n=top_n,
        window=window,
        min_account_value=min_account_value,
        min_volume=min_volume,
        min_pnl=min_pnl,
        require_month_positive=require_month_positive,
    )
    logger.info(
        "HL leaderboard durable top: n=%d window=%s (from %d rows)",
        len(selected),
        window,
        len(rows),
    )
    return selected
