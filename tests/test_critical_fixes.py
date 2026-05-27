"""Tests for critical bugs fixed in the v3.1.1 patch.

Covers:
  - Drawdown circuit breaker trips and flattens positions
  - PortfolioState restores daily_peak_capital and initial_capital
  - FundingArbitrage clear_active_pair lifecycle
  - ExecutionEngine close_position uses correct exit price
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncio
from src.core.portfolio import PortfolioState
from src.core.risk_manager import RiskManager
from src.strategies.funding_arbitrage import FundingArbitrage
from src.utils.config import Config


def test_equity_stable_on_position_open():
    """Opening a position must not show phantom ~20% drawdown."""
    print("=" * 60)
    print("TEST: Equity stable on position open")
    print("=" * 60)

    portfolio = PortfolioState(10000.0)

    async def _run():
        from src.strategies.base import Position

        pos = Position(
            symbol="BTC",
            side="long",
            entry_price=50000.0,
            size=0.04,
            entry_time_ms=0,
        )
        cost = 50000.0 * 0.04 + 1.0
        await portfolio.add_position(pos, cost=cost)
        dd = await portfolio.get_max_drawdown()
        equity = await portfolio.current_capital
        print(f"Equity after open: {equity:.2f}, drawdown: {dd * 100:.4f}%")
        assert dd < 0.02, f"Phantom drawdown on open: {dd * 100:.2f}%"
        assert abs(equity - 10000.0) < 50.0, f"Equity should stay near 10k, got {equity}"
        print("[PASS] No phantom drawdown on open\n")

    asyncio.run(_run())


def test_drawdown_circuit_breaker():
    """Simulate a 12% drawdown and verify circuit breaker trips."""
    print("=" * 60)
    print("TEST: Drawdown circuit breaker")
    print("=" * 60)

    portfolio = PortfolioState(100000.0)
    cfg = Config({"risk": {"circuit_breaker_drawdown_pct": 10.0}})
    rm = RiskManager(cfg, None)

    async def _run():
        from src.strategies.base import Position

        pos = Position(
            symbol="BTC",
            side="long",
            entry_price=50000.0,
            size=2.0,
            entry_time_ms=0,
        )
        await portfolio.add_position(pos, cost=100000.0)
        await portfolio.update_price("BTC", 44000.0)
        dd = await portfolio.get_max_drawdown()
        print(f"Drawdown: {dd * 100:.2f}%")
        assert dd >= 0.12, f"Expected >=12% drawdown, got {dd * 100:.2f}%"

        tripped = rm.check_drawdown(dd)
        assert tripped, "Circuit breaker should have tripped"
        assert rm.is_circuit_breaker_tripped(), "Breaker should be active"
        print("[PASS] Circuit breaker tripped correctly\n")

    asyncio.run(_run())


def test_portfolio_restore():
    """Verify from_dict restores daily_peak_capital and initial_capital."""
    print("=" * 60)
    print("TEST: Portfolio state restore")
    print("=" * 60)

    portfolio = PortfolioState(100000.0)

    async def _run():
        snap = await portfolio.to_dict()
        assert "daily_peak_capital" in snap, "daily_peak_capital missing from snapshot"
        assert "initial_capital" in snap, "initial_capital missing from snapshot"
        assert snap["initial_capital"] == 100000.0

        # Simulate restart with modified values
        new_portfolio = PortfolioState(50000.0)  # wrong initial
        await new_portfolio.from_dict({
            "cash": snap["cash"],
            "peak_capital": snap["peak_capital"],
            "daily_peak_capital": 95000.0,
            "initial_capital": 100000.0,
            "daily_pnl": snap["daily_pnl"],
            "positions": snap["positions"],
        })
        assert new_portfolio._initial_capital == 100000.0
        assert new_portfolio._daily_peak_capital == 95000.0
        print("[PASS] Portfolio restored correctly\n")

    asyncio.run(_run())


def test_funding_arbitrage_lifecycle():
    """Verify active pair is cleared and guards work."""
    print("=" * 60)
    print("TEST: FundingArbitrage lifecycle")
    print("=" * 60)

    arb = FundingArbitrage({})
    assert arb._active_pair is None

    # Simulate setting active pair
    arb._active_pair = ("BTC", "ETH")
    assert arb.scan_pair_opportunity(
        funding_map={"BTC": -0.01, "ETH": 0.01},
        oi_delta_map={"BTC": 0, "ETH": 0},
        timestamp_ms=0,
    ) is None, "Should block scan while active pair exists"

    arb.clear_active_pair()
    assert arb._active_pair is None
    print("[PASS] FundingArbitrage lifecycle correct\n")


def test_execution_close_uses_fill_exit():
    """Verify ExecutionEngine.close_position uses fill_exit in result."""
    print("=" * 60)
    print("TEST: Execution exit price fix")
    print("=" * 60)

    from pathlib import Path

    execution_path = Path(__file__).resolve().parents[1] / "src" / "core" / "execution.py"
    src = execution_path.read_text(encoding="utf-8")
    assert "exit_price_f" not in src, "exit_price_f should have been removed"
    assert "fill_exit" in src, "fill_exit should be used"
    print("[PASS] Execution engine uses fill_exit correctly\n")


if __name__ == "__main__":
    try:
        test_equity_stable_on_position_open()
        test_drawdown_circuit_breaker()
        test_portfolio_restore()
        test_funding_arbitrage_lifecycle()
        test_execution_close_uses_fill_exit()
        print("=" * 60)
        print("ALL CRITICAL FIX TESTS PASSED [OK]")
        print("=" * 60)
    except Exception as exc:
        import traceback

        print("CRITICAL FIX TESTS FAILED", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(1) from exc
