"""Unit tests for HL leaderboard durable top selection."""

from __future__ import annotations

import pytest

from src.exchanges.hl_leaderboard import select_durable_top, wallets_payload

pytestmark = pytest.mark.unit


def _row(addr: str, av: float, *, all_pnl: float, all_vlm: float, month_pnl: float) -> dict:
    return {
        "ethAddress": addr,
        "accountValue": str(av),
        "displayName": addr[:8],
        "windowPerformances": [
            ["day", {"pnl": "0", "roi": "0", "vlm": "0"}],
            ["month", {"pnl": str(month_pnl), "roi": "0.1", "vlm": "1e6"}],
            ["allTime", {"pnl": str(all_pnl), "roi": "1.0", "vlm": str(all_vlm)}],
        ],
    }


def test_select_durable_top_filters_and_ranks() -> None:
    rows = [
        _row("0x" + "1" * 40, 50_000, all_pnl=1e9, all_vlm=1e9, month_pnl=1e6),  # av too low
        _row("0x" + "2" * 40, 200_000, all_pnl=1e6, all_vlm=1e3, month_pnl=1e5),  # vlm too low
        _row("0x" + "3" * 40, 200_000, all_pnl=5e6, all_vlm=1e8, month_pnl=-1e3),  # month neg
        _row("0x" + "a" * 40, 500_000, all_pnl=9e6, all_vlm=1e8, month_pnl=1e5),
        _row("0x" + "b" * 40, 400_000, all_pnl=8e6, all_vlm=2e8, month_pnl=2e5),
        _row("0x" + "c" * 40, 300_000, all_pnl=7e6, all_vlm=3e8, month_pnl=3e5),
    ]
    top = select_durable_top(rows, top_n=2, min_account_value=100_000, min_volume=5e6)
    assert len(top) == 2
    assert top[0].address.startswith("0xaaaa")
    assert top[0].rank == 1
    assert top[1].address.startswith("0xbbbb")
    payload = wallets_payload(top)
    assert len(payload["wallets"]) == 2
    assert payload["source"].startswith("stats-data")


def test_select_allows_skip_month_filter() -> None:
    rows = [
        _row("0x" + "d" * 40, 200_000, all_pnl=5e6, all_vlm=1e8, month_pnl=-1e3),
    ]
    top = select_durable_top(
        rows,
        top_n=5,
        min_account_value=100_000,
        min_volume=5e6,
        require_month_positive=False,
    )
    assert len(top) == 1
