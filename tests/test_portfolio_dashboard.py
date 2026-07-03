"""Dashboard portfolio math — restore, overnight positions, live marks."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.portfolio import PortfolioState
from src.strategies.base import Position


async def _open_btc(p: PortfolioState, entry: float = 100_000.0, size: float = 0.04) -> None:
    pos = Position(
        symbol="BTC",
        side="long",
        entry_price=entry,
        size=size,
        entry_time_ms=1,
        metadata={"strategy": "test"},
    )
    await p.add_position(pos, cost=entry * size)


async def test_legacy_restore_no_phantom_daily_pnl() -> None:
    p = PortfolioState(10_000.0)
    await _open_btc(p)
    total = await p.current_capital
    assert abs(total - 10_000.0) < 0.01

    p2 = PortfolioState(10_000.0)
    await p2.from_dict({
        "capital": total,
        "peak_capital": 10_000.0,
        "daily_pnl": 0.0,
        "positions": {
            "BTC": {
                "symbol": "BTC",
                "side": "long",
                "entry_price": 100_000.0,
                "size": 0.04,
                "entry_time_ms": 1,
                "unrealized_pnl": 0.0,
                "current_price": 100_000.0,
                "stop_loss_price": None,
                "take_profit_price": None,
                "metadata": {},
            },
        },
    })
    metrics = p2.build_live_dashboard_metrics({"BTC": 100_000.0})
    assert abs(metrics["capital"] - 10_000.0) < 0.01
    assert abs(metrics["daily_pnl"]) < 0.01, metrics["daily_pnl"]


async def test_overnight_unrealized_baseline() -> None:
    """Correct day_start at midnight includes prior unrealized — daily tracks only today's change."""
    p = PortfolioState(10_000.0)
    await _open_btc(p)
    await p.update_price("BTC", 105_000.0)
    p._cash += 50.0
    p._day_start_equity = 10_200.0
    p._day_start_unrealized = 200.0
    p._daily_pnl = 50.0
    p._refresh_dashboard_cache_unlocked()

    metrics = p.build_live_dashboard_metrics({"BTC": 105_000.0})
    assert abs(metrics["daily_realized_pnl"] - 50.0) < 0.01
    assert abs(metrics["daily_pnl"] - 50.0) < 0.01, metrics["daily_pnl"]


async def test_live_marks_match_positions_table() -> None:
    p = PortfolioState(10_000.0)
    await _open_btc(p)
    mark = 101_000.0
    metrics = p.build_live_dashboard_metrics({"BTC": mark})
    expected_unreal = (mark - 100_000.0) * 0.04
    assert abs(metrics["unrealized_pnl"] - expected_unreal) < 0.01
    assert abs(metrics["daily_pnl"]) < 0.01, "realised daily PnL unchanged before close"
    assert abs(metrics["daily_unrealized_pnl"] - expected_unreal) < 0.01


async def test_corrupt_day_start_meta_after_equity_fix() -> None:
    """Stale day_start from cash-only snapshots must not inflate Daily PnL."""
    p = PortfolioState(10_000.0)
    await _open_btc(p, entry=100_000.0, size=0.04)
    await p.update_price("BTC", 101_000.0)
    total = await p.current_capital
    # Simulate pre-fix DB meta: day_start omitted position MTM (~4000 notional).
    stale_start = total - 4_000.0
    await p.from_dict({
        "capital": total,
        "cash": p._cash,
        "peak_capital": 10_000.0,
        "daily_pnl": 6.0,
        "daily_realized_pnl": 6.0,
        "last_reset_date": p._last_reset_date,
        "day_start_equity": stale_start,
        "day_start_unrealized": 0.0,
        "positions": {
            "BTC": {
                "symbol": "BTC",
                "side": "long",
                "entry_price": 100_000.0,
                "size": 0.04,
                "entry_time_ms": 1,
                "unrealized_pnl": 40.0,
                "current_price": 101_000.0,
                "stop_loss_price": None,
                "take_profit_price": None,
                "metadata": {},
            },
        },
    })
    metrics = p.build_live_dashboard_metrics({"BTC": 101_000.0})
    assert abs(metrics["daily_pnl"] - 6.0) < 0.01, metrics["daily_pnl"]
    assert abs(metrics["daily_equity_pnl"] - 46.0) < 0.05, metrics["daily_equity_pnl"]
    assert abs(metrics["daily_realized_pnl"] - 6.0) < 0.01


async def test_funding_throttled_to_hourly() -> None:
    """Ctx ticks must not inflate daily realised PnL every few seconds."""
    p = PortfolioState(10_000.0)
    entry_ms = 1_700_000_000_000
    pos = Position(
        symbol="BTC",
        side="long",
        entry_price=100_000.0,
        size=0.04,
        entry_time_ms=entry_ms,
        metadata={"strategy": "test"},
    )
    await p.add_position(pos, cost=4_000.0)
    due_ms = entry_ms + 3_600_000
    rate = 0.0001
    mark = 100_000.0

    first = await p.apply_funding("BTC", rate, 0.04, "long", mark, now_ms=due_ms)
    second = await p.apply_funding("BTC", rate, 0.04, "long", mark, now_ms=due_ms + 5_000)
    snap = p.get_dashboard_snapshot_sync()

    assert abs(first) > 0.0
    assert second == 0.0
    assert abs(snap.daily_realized_pnl - first) < 0.01


if __name__ == "__main__":
    asyncio.run(test_legacy_restore_no_phantom_daily_pnl())
    asyncio.run(test_overnight_unrealized_baseline())
    asyncio.run(test_live_marks_match_positions_table())
    asyncio.run(test_corrupt_day_start_meta_after_equity_fix())
    asyncio.run(test_funding_throttled_to_hourly())
    print("ALL PORTFOLIO DASHBOARD TESTS PASSED [OK]")
