"""Hyperliquid public leaderboard (stats-data) — consistent durable top wallets.

Source: ``https://stats-data.hyperliquid.xyz/Mainnet/leaderboard``
(same feed the official app leaderboard uses). No API key.

Goal: **consistent winners**, not one-off allTime lottery whales.
Default filters require positive PnL on week + month + allTime, then rank by a
multi-horizon consistency score (month-weighted). We cannot see historical
“always ranked top-10” without a time-series archive; multi-window positivity
is the best public proxy for staying profitable across regimes.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp

from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
VALID_WINDOWS = frozenset({"day", "week", "month", "allTime"})
DEFAULT_POSITIVE_WINDOWS: Tuple[str, ...] = ("week", "month", "allTime")


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
    consistency_score: float = 0.0
    week_pnl: float = 0.0
    month_pnl: float = 0.0
    all_time_pnl: float = 0.0


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


def _consistency_score(
    *,
    week_pnl: float,
    month_pnl: float,
    all_pnl: float,
    month_roi: float,
    all_vlm: float,
) -> float:
    """Higher = more sustained edge across horizons.

    Month dominates (regime persistence); week confirms recent edge; allTime
    is a soft tie-break via log so early lucky whales don't dominate.
    ROI is clamped — HL can report extreme ratios that would otherwise swamp PnL.
    """
    week_term = math.log1p(max(0.0, week_pnl))
    month_term = math.log1p(max(0.0, month_pnl))
    all_term = math.log1p(max(0.0, all_pnl))
    roi_term = min(max(0.0, float(month_roi)), 1.0)  # cap at 100% month ROI
    vol_term = math.log1p(max(0.0, all_vlm)) / 30.0
    return (
        (0.40 * month_term)
        + (0.30 * week_term)
        + (0.25 * all_term)
        + (0.03 * roi_term)
        + (0.02 * vol_term)
    )


def select_durable_top(
    rows: Sequence[Dict[str, Any]],
    *,
    top_n: int = 10,
    window: str = "allTime",
    min_account_value: float = 100_000.0,
    min_volume: float = 5_000_000.0,
    min_pnl: float = 0.0,
    min_all_time_pnl: float = 1_000_000.0,
    require_month_positive: bool = True,
    require_consistent_windows: bool = True,
    positive_windows: Optional[Sequence[str]] = None,
    min_month_volume: float = 1_000_000.0,
) -> List[LeaderboardWallet]:
    """Filter + rank for multi-horizon consistent winners.

    ``require_consistent_windows`` (default True): PnL > 0 on week, month and
    allTime. Ranking uses ``_consistency_score`` (month-weighted), not raw
    allTime PnL alone. ``min_all_time_pnl`` keeps the set in top-trader scale.
    """
    win = window if window in VALID_WINDOWS else "allTime"
    need = tuple(positive_windows) if positive_windows else DEFAULT_POSITIVE_WINDOWS

    candidates: List[Tuple[Any, ...]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        addr = str(row.get("ethAddress") or "").strip().lower()
        if not (addr.startswith("0x") and len(addr) >= 42):
            continue
        av = safe_float(row.get("accountValue"))
        if av < min_account_value:
            continue

        week = _window_perf(row, "week")
        month = _window_perf(row, "month")
        all_t = _window_perf(row, "allTime")
        primary = _window_perf(row, win)

        if primary["pnl"] < min_pnl:
            continue
        if all_t["pnl"] < min_all_time_pnl:
            continue
        if all_t["vlm"] < min_volume:
            continue
        if month["vlm"] < min_month_volume:
            continue

        perfs = {
            "week": week,
            "month": month,
            "allTime": all_t,
            "day": _window_perf(row, "day"),
        }
        if require_consistent_windows:
            if any(perfs.get(wname, {}).get("pnl", 0.0) <= 0 for wname in need):
                continue
        elif require_month_positive and month["pnl"] <= 0:
            continue

        score = _consistency_score(
            week_pnl=week["pnl"],
            month_pnl=month["pnl"],
            all_pnl=all_t["pnl"],
            month_roi=month["roi"],
            all_vlm=all_t["vlm"],
        )
        name = str(row.get("displayName") or "")
        candidates.append(
            (
                score,
                month["pnl"],
                all_t["pnl"],
                addr,
                av,
                all_t,
                name,
                week["pnl"],
                month["pnl"],
                all_t["pnl"],
            )
        )

    candidates.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    out: List[LeaderboardWallet] = []
    for i, row_t in enumerate(candidates[: max(1, int(top_n))]):
        score, _m, _a, addr, av, all_t, name, week_pnl, month_pnl, all_pnl = row_t
        out.append(
            LeaderboardWallet(
                address=addr,
                rank=i + 1,
                account_value=av,
                window="consistent" if require_consistent_windows else win,
                pnl=float(all_pnl),
                roi=float(all_t.get("roi", 0.0)),
                volume=float(all_t.get("vlm", 0.0)),
                display_name=name,
                consistency_score=float(score),
                week_pnl=float(week_pnl),
                month_pnl=float(month_pnl),
                all_time_pnl=float(all_pnl),
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
        "selection": "consistent_multi_window",
        "notes": notes
        or (
            "Consistent winners: week+month+allTime PnL > 0, ranked by "
            "month-weighted consistency score (not allTime lottery)."
        ),
        "wallets": [
            {
                "address": w.address,
                "rank": w.rank,
                "label": w.display_name or f"hl_lb_{w.rank}",
                "account_value": w.account_value,
                "window": w.window,
                "pnl": w.pnl,
                "all_time_pnl": w.all_time_pnl,
                "month_pnl": w.month_pnl,
                "week_pnl": w.week_pnl,
                "consistency_score": w.consistency_score,
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
    min_all_time_pnl: float = 1_000_000.0,
    require_month_positive: bool = True,
    require_consistent_windows: bool = True,
    min_month_volume: float = 1_000_000.0,
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
        min_all_time_pnl=min_all_time_pnl,
        require_month_positive=require_month_positive,
        require_consistent_windows=require_consistent_windows,
        min_month_volume=min_month_volume,
    )
    logger.info(
        "HL leaderboard consistent top: n=%d (from %d rows, multi-window filter)",
        len(selected),
        len(rows),
    )
    return selected
