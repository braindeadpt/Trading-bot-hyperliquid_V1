"""Tests for FASE 4: Portfolio Heat & Governance.

Validates:
  4.1 — Directional exposure limit (max 60% same direction)
  4.2 — Sector exposure cap (max 30% in crypto)
  4.3 — Daily drawdown circuit (>5% = stop entries)
  4.4 — Kelly Criterion sizing (Half-Kelly multiplier)
"""

import sys
sys.path.insert(0, r"C:\Users\Braindead\Documents\trading-bot-hyperliquid")

from src.core.risk_manager import RiskManager
from src.core.kelly_sizer import KellySizer
from src.strategies.base import Signal, Position
from src.utils.config import Config
import pytest

pytestmark = pytest.mark.unit


# Mock portfolio for testing
class MockPortfolio:
    def __init__(self, capital=100_000.0, positions=None, daily_pnl=0.0, daily_trades=0):
        self.current_capital = capital
        self.positions = positions or {}
        self.daily_pnl = daily_pnl
        self.daily_trades = daily_trades
        self._max_drawdown = 0.0

    def get_max_drawdown(self):
        return self._max_drawdown


def make_signal(symbol="BTC", side="long", size_pct=0.01, confidence=0.8):
    return Signal(
        strategy="Test", symbol=symbol, side=side, confidence=confidence,
        size_pct=size_pct, entry_price=50000.0, stop_loss_pct=0.01,
        take_profit_pct=0.02, reason="test",
    )


def test_daily_drawdown_circuit():
    print("=" * 60)
    print("TEST 4.3: Daily drawdown circuit")
    print("=" * 60)

    risk = RiskManager({"max_positions": 5}, None)
    risk.DAILY_DRAWDOWN_CIRCUIT_PCT = 0.05  # 5%

    # Portfolio lost 6% from today's peak
    portfolio = MockPortfolio(capital=94_000.0)
    # Simulate daily peak was 100K (use today's date so circuit doesn't reset)
    from src.utils.helpers import utc_now
    today = utc_now().strftime("%Y-%m-%d")
    risk._daily_peak_capital = 100_000.0
    risk._daily_drawdown_circuit_date = today

    sig = make_signal(size_pct=0.01)
    approved, reason = risk.can_enter(sig, portfolio)

    assert not approved, "Should reject when daily drawdown > 5%"
    assert "drawdown" in reason.lower(), f"Reason should mention drawdown: {reason}"
    print(f"Rejected correctly: {reason}")
    print("[PASS]\n")


def test_directional_exposure_limit():
    print("=" * 60)
    print("TEST 4.1: Directional exposure limit (max 60%)")
    print("=" * 60)

    risk = RiskManager({"max_positions": 5}, None)
    risk.MAX_DIRECTIONAL_EXPOSURE_PCT = 0.60  # 60%
    risk.MAX_SECTOR_EXPOSURE_PCT = 1.0  # disable sector cap for this test

    # Already 55% long — adding 10% more should fail
    class Pos:
        def __init__(self, side, entry_price, size):
            self.side = side
            self.entry_price = entry_price
            self.size = size

    positions = {
        "BTC": Pos("long", 50000.0, 1.1),   # $55K = 55%
    }
    portfolio = MockPortfolio(capital=100_000.0, positions=positions)

    sig = make_signal(symbol="ETH", side="long", size_pct=0.10)  # 10% more
    approved, reason = risk.can_enter(sig, portfolio)

    assert not approved, "Should reject when directional exposure > 60%"
    assert "directional" in reason.lower(), f"Reason should mention directional: {reason}"
    print(f"Rejected correctly: {reason}")

    # Short should be allowed (different direction, different symbol)
    sig_short = make_signal(symbol="ETH", side="short", size_pct=0.10)
    approved2, reason2 = risk.can_enter(sig_short, portfolio)
    assert approved2, f"Short should be allowed: {reason2}"
    print(f"Short allowed: {reason2}")
    print("[PASS]\n")


def test_sector_exposure_cap():
    print("=" * 60)
    print("TEST 4.2: Sector exposure cap (max 30%)")
    print("=" * 60)

    risk = RiskManager(
        Config({
            "risk": {"max_positions": 5},
            "strategy": {
                "portfolio_governance": {
                    "max_sector_exposure_pct": 30,
                },
            },
        }),
        None,
    )

    class Pos:
        def __init__(self, side, entry_price, size):
            self.side = side
            self.entry_price = entry_price
            self.size = size

    # v3.1.43 (commit d45cea6) switched position sizing from a direct
    # size_pct-to-notional mapping to risk-based sizing bounded by
    # risk.max_position_size_pct * leverage_max (default 5% * 1x = $5K on
    # $100K capital), so a "10% more" signal here only contributes ~5% of
    # notional regardless of the requested size_pct. Use a larger existing
    # position (27.5%) so 27.5% + ~5% new still clears the 30% sector cap.
    positions = {
        "BTC": Pos("long", 50000.0, 0.55),   # $27.5K = 27.5%
    }
    portfolio = MockPortfolio(capital=100_000.0, positions=positions)

    sig = make_signal(symbol="ETH", size_pct=0.10)  # requested size; risk-sized down to ~5%
    approved, reason = risk.can_enter(sig, portfolio)

    assert not approved, "Should reject when sector exposure > 30%"
    assert "sector" in reason.lower(), f"Reason should mention sector: {reason}"
    print(f"Rejected correctly: {reason}")
    print("[PASS]\n")


def test_kelly_sizer():
    print("=" * 60)
    print("TEST 4.4: Kelly Criterion sizing (Half-Kelly)")
    print("=" * 60)

    sizer = KellySizer(min_trades=10, half_kelly=True)

    # Not enough history — should return 1.0
    mult = sizer.get_size_multiplier()
    assert mult == 1.0, f"With no history should return 1.0, got {mult}"
    print("No history: multiplier=1.0 (no adjustment)")

    # Record winning trades (+4% each) and losing trades (-1% each)
    # Win rate=75%, avg_win=4%, avg_loss=1% → Kelly = 0.75 - (0.25*0.01/0.04) = 0.687
    for _ in range(15):
        sizer.record_trade(0.04)
    for _ in range(5):
        sizer.record_trade(-0.01)

    mult = sizer.get_size_multiplier()
    print(f"Win rate=75%, avg_win=4%, avg_loss=1%: multiplier={mult:.3f}")
    assert mult > 1.0, f"With positive edge, Kelly should increase size: {mult}"
    assert mult <= 2.0, f"Should not exceed max_multiplier: {mult}"

    # Record many losses
    sizer2 = KellySizer(min_trades=10, half_kelly=True)
    for _ in range(15):
        sizer2.record_trade(-0.02)
    for _ in range(5):
        sizer2.record_trade(0.01)

    mult2 = sizer2.get_size_multiplier()
    print(f"Win rate=25%, avg_win=1%, avg_loss=2%: multiplier={mult2:.3f}")
    assert mult2 < 1.0, f"With negative edge, Kelly should decrease size: {mult2}"
    assert mult2 >= 0.25, f"Should not go below min_multiplier: {mult2}"

    print("[PASS]\n")


def test_kelly_stats():
    print("=" * 60)
    print("TEST 4.4b: Kelly stats reporting")
    print("=" * 60)

    sizer = KellySizer(min_trades=5)
    for _ in range(8):
        sizer.record_trade(0.015)
    for _ in range(2):
        sizer.record_trade(-0.01)

    stats = sizer.get_stats()
    print(f"Stats: {stats}")
    assert stats["total_trades"] == 10
    assert stats["winning_trades"] == 8
    assert stats["win_rate"] == 0.8
    assert stats["sufficient_history"] is True
    print("[PASS]\n")


if __name__ == "__main__":
    test_daily_drawdown_circuit()
    test_directional_exposure_limit()
    test_sector_exposure_cap()
    test_kelly_sizer()
    test_kelly_stats()
    print("=" * 60)
    print("ALL FASE 4 TESTS PASSED [OK]")
    print("=" * 60)
