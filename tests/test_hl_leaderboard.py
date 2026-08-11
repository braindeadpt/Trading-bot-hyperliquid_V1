"""Unit tests for HL leaderboard durable/consistent top selection."""

from __future__ import annotations

import pytest

from src.exchanges.hl_leaderboard import select_durable_top, wallets_payload

pytestmark = pytest.mark.unit


def _row(
    addr: str,
    av: float,
    *,
    all_pnl: float,
    all_vlm: float,
    month_pnl: float,
    week_pnl: float = 1e5,
    month_vlm: float = 2e6,
) -> dict:
    return {
        "ethAddress": addr,
        "accountValue": str(av),
        "displayName": addr[:8],
        "windowPerformances": [
            ["day", {"pnl": "1000", "roi": "0.01", "vlm": "1e5"}],
            ["week", {"pnl": str(week_pnl), "roi": "0.05", "vlm": "5e5"}],
            ["month", {"pnl": str(month_pnl), "roi": "0.1", "vlm": str(month_vlm)}],
            ["allTime", {"pnl": str(all_pnl), "roi": "1.0", "vlm": str(all_vlm)}],
        ],
    }


def test_select_rejects_inconsistent_and_ranks_by_consistency() -> None:
    rows = [
        _row("0x" + "1" * 40, 50_000, all_pnl=1e9, all_vlm=1e9, month_pnl=1e6),  # av low
        _row("0x" + "2" * 40, 200_000, all_pnl=1e6, all_vlm=1e3, month_pnl=1e5),  # vlm low
        _row("0x" + "3" * 40, 200_000, all_pnl=5e6, all_vlm=1e8, month_pnl=-1e3),  # month neg
        _row(
            "0x" + "4" * 40,
            200_000,
            all_pnl=9e6,
            all_vlm=1e8,
            month_pnl=1e5,
            week_pnl=-1e3,
        ),  # week neg — inconsistent
        # Strong month+week (consistent) but lower allTime than lottery whale
        _row(
            "0x" + "b" * 40,
            400_000,
            all_pnl=8e6,
            all_vlm=2e8,
            month_pnl=5e5,
            week_pnl=2e5,
        ),
        _row(
            "0x" + "a" * 40,
            500_000,
            all_pnl=20e6,
            all_vlm=1e8,
            month_pnl=1e5,
            week_pnl=5e4,
        ),  # high allTime, weak recent — still passes min_all_time
        _row(
            "0x" + "e" * 40,
            200_000,
            all_pnl=50_000,
            all_vlm=1e8,
            month_pnl=2e4,
            week_pnl=1e4,
        ),  # allTime too small
    ]
    top = select_durable_top(
        rows,
        top_n=2,
        min_account_value=100_000,
        min_volume=5e6,
        min_all_time_pnl=1_000_000,
    )
    assert len(top) == 2
    # Month-weighted: 0xbbb (month 5e5) should beat 0xaaa (month 1e5) despite lower allTime
    assert top[0].address.startswith("0xbbbb")
    assert top[0].month_pnl == pytest.approx(5e5)
    assert top[0].consistency_score > top[1].consistency_score
    payload = wallets_payload(top)
    assert payload["selection"] == "consistent_multi_window"


def test_select_legacy_alltime_without_consistency() -> None:
    rows = [
        _row(
            "0x" + "d" * 40,
            200_000,
            all_pnl=5e6,
            all_vlm=1e8,
            month_pnl=-1e3,
            week_pnl=-1e3,
        ),
    ]
    top = select_durable_top(
        rows,
        top_n=5,
        min_account_value=100_000,
        min_volume=5e6,
        require_consistent_windows=False,
        require_month_positive=False,
    )
    assert len(top) == 1
