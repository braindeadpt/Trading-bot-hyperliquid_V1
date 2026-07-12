"""Tests for risk-based position sizing (size_pct = % capital to risk)."""

from __future__ import annotations

import os
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.risk_manager import RiskManager
from src.strategies.base import Position, Signal
from src.utils.config import load_config
import pytest

pytestmark = pytest.mark.unit


def _make_rm(
    *,
    leverage: float = 10.0,
    max_pos_pct: float = 5.0,
    per_trade_risk: float = 1.0,
    max_daily_loss: float = 3.0,
    per_trade_frac: float = 33.0,
    max_directional_pct: float = 60.0,
    max_sector_pct: float = 100.0,
) -> RiskManager:
    cfg_data = {
        "risk": {
            "max_daily_loss_pct": max_daily_loss,
            "max_position_size_pct": max_pos_pct,
            "leverage_max": leverage,
            "per_trade_risk_pct": per_trade_risk,
            "per_trade_risk_fraction_of_daily_loss": per_trade_frac,
        },
        "strategy": {
            "portfolio_governance": {
                "max_directional_exposure_pct": max_directional_pct,
                "max_sector_exposure_pct": max_sector_pct,
            },
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(cfg_data, fh)
        path = fh.name
    cfg = load_config(path)
    return RiskManager(cfg, None)


def _signal(
    size_pct: float,
    *,
    entry_price: float = 50_000.0,
    stop_loss_pct: float | None = None,
) -> Signal:
    return Signal(
        strategy="TestStrategy",
        symbol="BTC",
        side="long",
        confidence=0.85,
        size_pct=size_pct,
        entry_price=entry_price,
        stop_loss_pct=stop_loss_pct,
    )


class _PortfolioStub:
    def __init__(
        self,
        capital: float,
        positions: dict | None = None,
        daily_pnl: float = 0.0,
        daily_trades: int = 0,
    ) -> None:
        self.current_capital = capital
        self.positions = positions or {}
        self.daily_pnl = daily_pnl
        self.daily_trades = daily_trades

    def get_max_drawdown(self) -> float:
        return 0.0


def test_size_pct_as_risk_yields_realistic_notional() -> None:
    """(a) size_pct 1% + stop 1.2% → notional ≈ capital×0.01/0.012 when cap allows."""
    rm = _make_rm(
        leverage=20.0,
        max_pos_pct=100.0,
        per_trade_risk=10.0,
        max_daily_loss=100.0,
        per_trade_frac=100.0,
    )
    capital = 10_000.0
    stop = 0.012
    sig = _signal(0.01, stop_loss_pct=stop)
    size = rm.calculate_position_size(sig, capital, atr_pct=0.006)
    notional = size * 50_000.0
    expected = capital * 0.01 / stop
    assert abs(notional - expected) < 50.0, f"got {notional}, expected ~{expected}"


def test_user_example_capped_at_hard_limit() -> None:
    """Production caps: $10k, 1% risk, 1.2% stop → $5k notional, ~0.6% effective risk."""
    rm = _make_rm(leverage=10.0, max_pos_pct=5.0)
    capital = 10_000.0
    stop = 0.012
    sig = _signal(0.01, stop_loss_pct=stop)
    size = rm.calculate_position_size(sig, capital, atr_pct=0.006)
    notional = size * 50_000.0
    assert abs(notional - 5_000.0) < 1.0
    effective_risk = notional * stop / capital
    assert abs(effective_risk - 0.006) < 0.001


def test_hard_cap_max_position_size_times_leverage() -> None:
    """(c) Hard cap at max_position_size_pct × leverage binds before conviction."""
    rm = _make_rm(leverage=10.0, max_pos_pct=5.0)
    capital = 10_000.0
    sig = _signal(0.01, stop_loss_pct=0.012)
    size = rm.calculate_position_size(sig, capital, atr_pct=0.006)
    notional = size * 50_000.0
    max_allowed = capital * 0.05 * 10.0
    assert notional <= max_allowed + 1e-6
    assert abs(notional - max_allowed) < 1e-6


def test_per_trade_risk_ceiling_limits_high_size_pct() -> None:
    """(b) per_trade_risk_pct caps notional when size_pct exceeds risk budget."""
    rm = _make_rm(per_trade_risk=1.0, max_daily_loss=3.0, per_trade_frac=33.0)
    capital = 10_000.0
    stop = 0.02
    sig = _signal(0.05, stop_loss_pct=stop)
    size = rm.calculate_position_size(sig, capital, atr_pct=0.01)
    notional = size * 50_000.0
    risk_amount = capital * rm._per_trade_risk_pct_effective
    expected_risk_cap = risk_amount / stop
    assert notional <= expected_risk_cap + 1e-6
    assert notional < capital * 0.05 * 10.0


def test_portfolio_leverage_gate_rejects_excess() -> None:
    """(d) Gate rejects entry that would exceed leverage_max."""
    rm = _make_rm(
        leverage=1.0,
        max_pos_pct=50.0,
        max_directional_pct=200.0,
        max_sector_pct=200.0,
    )
    capital = 10_000.0
    existing = Position(
        symbol="ETH",
        side="long",
        entry_price=1_000.0,
        size=9.0,
        entry_time_ms=0,
    )
    portfolio = _PortfolioStub(
        capital=capital,
        positions={"ETH": existing},
    )
    sig = _signal(0.01, entry_price=50_000.0, stop_loss_pct=0.05)
    approved, reason = rm.can_enter(sig, portfolio)
    assert not approved
    assert "Portfolio leverage limit" in reason


def test_portfolio_leverage_allows_within_limit() -> None:
    rm = _make_rm(leverage=10.0, max_pos_pct=5.0)
    capital = 10_000.0
    existing = Position(
        symbol="ETH",
        side="long",
        entry_price=2_000.0,
        size=0.5,
        entry_time_ms=0,
    )
    portfolio = _PortfolioStub(capital=capital, positions={"ETH": existing})
    sig = _signal(0.005, entry_price=50_000.0, stop_loss_pct=0.02)
    approved, reason = rm.can_enter(sig, portfolio)
    assert approved, reason


def test_kelly_multiplier_scales_risk_notional() -> None:
    """Kelly applied via size_pct (as engine does) halves conviction notional."""
    rm = _make_rm(
        leverage=20.0,
        max_pos_pct=100.0,
        per_trade_risk=10.0,
        max_daily_loss=100.0,
        per_trade_frac=100.0,
    )
    capital = 10_000.0
    stop = 0.02
    base = 0.004
    size_full = rm.calculate_position_size(
        _signal(base, stop_loss_pct=stop), capital, atr_pct=0.01
    )
    size_half = rm.calculate_position_size(
        _signal(base * 0.5, stop_loss_pct=stop), capital, atr_pct=0.01
    )
    assert abs(size_half - size_full * 0.5) < 1e-9


def test_get_metrics_includes_portfolio_leverage() -> None:
    rm = _make_rm(leverage=10.0)
    pos = Position(symbol="BTC", side="long", entry_price=50_000.0, size=0.08, entry_time_ms=0)
    portfolio = _PortfolioStub(capital=10_000.0, positions={"BTC": pos})
    metrics = rm.get_metrics(portfolio)
    assert abs(metrics["portfolio_leverage"] - 0.4) < 1e-6
    assert metrics["total_notional_usd"] == 4_000.0


def main() -> None:
    test_size_pct_as_risk_yields_realistic_notional()
    test_user_example_capped_at_hard_limit()
    test_hard_cap_max_position_size_times_leverage()
    test_per_trade_risk_ceiling_limits_high_size_pct()
    test_portfolio_leverage_gate_rejects_excess()
    test_portfolio_leverage_allows_within_limit()
    test_kelly_multiplier_scales_risk_notional()
    test_get_metrics_includes_portfolio_leverage()
    print("test_position_sizing: all passed")


if __name__ == "__main__":
    main()
