"""v3.1.48 — structural deadlock fix (ChecklistMeta exec + governor floor + 2% sizing)."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.phase08_regime_router import (
    classify_market_regime,
    route_phase08_signals,
)
from src.core.risk_manager import RiskManager
from src.core.strategy_governor import StrategyGovernor
from src.data.database import Database, TradeEntry, TradeExit
from src.strategies.base import Signal
from src.utils.config import Config

pytestmark = pytest.mark.unit


def _sig(strategy: str, side: str = "long", symbol: str = "BTC") -> Signal:
    return Signal(
        strategy=strategy,
        symbol=symbol,
        side=side,
        confidence=0.8,
        size_pct=0.01,
        stop_loss_pct=0.01,
        take_profit_pct=0.02,
    )


def test_checklist_allowed_in_expansion_regime() -> None:
    cm = _sig("ChecklistMeta", "long")
    allowed, reason, blocked = route_phase08_signals([cm], adx=22.0, symbol="BTC")
    assert classify_market_regime(22.0) == "expansion"
    assert reason is None
    assert len(allowed) == 1
    assert allowed[0].strategy == "ChecklistMeta"
    assert blocked == []


def test_regime_fallback_promotes_checklist_when_unknown() -> None:
    """Unknown ADX blocks primary gates; fallback still promotes ChecklistMeta."""
    cm = _sig("ChecklistMeta", "long")
    vb = _sig("VolatilityBreakout", "long")
    allowed, reason, _ = route_phase08_signals(
        [vb, cm],
        adx=None,
        symbol="BTC",
        fallback_strategy="ChecklistMeta",
    )
    assert reason is None
    assert len(allowed) == 1
    assert allowed[0].strategy == "ChecklistMeta"


def test_expansion_no_allowed_without_checklist_still_reports_dead_zone() -> None:
    """VWAP-only in expansion still yields no_allowed (fallback needs ChecklistMeta)."""
    vwap = _sig("VWAPDeviation", "long")
    allowed, reason, blocked = route_phase08_signals(
        [vwap],
        adx=22.0,
        symbol="BTC",
        fallback_strategy="ChecklistMeta",
    )
    assert allowed == []
    assert reason == "regime_expansion_no_allowed_strategies"
    assert len(blocked) == 1


def test_governor_protects_last_execution_strategy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "gov.db")
        now = int(time.time() * 1000)
        for i in range(12):
            tid = db.save_trade_entry(
                TradeEntry(
                    "BTC",
                    "long",
                    100.0,
                    now - i * 1000,
                    1.0,
                    "StrategyEnsemble",
                    sub_strategy="ChecklistMeta",
                )
            )
            loss = -0.01 - (i * 0.001)
            db.update_trade_exit(
                TradeExit(tid, 99.0, now - i * 500, loss * 100, loss, "sl", "closed")
            )

        cfg = Config(
            {
                "strategy": {
                    "phase08": {
                        "execution_strategies": ["ChecklistMeta", "VWAPDeviation"],
                    },
                    "strategy_governance": {
                        "enabled": True,
                        "lookback_days": 30,
                        "min_trades": 10,
                        "min_sharpe": 0.0,
                        "eval_interval_ms": 0,
                        "min_active_strategies": 1,
                        "last_strategy_size_multiplier": 0.5,
                    },
                }
            }
        )
        gov = StrategyGovernor(cfg, db)
        # Pretend VWAP already disabled → ChecklistMeta is last protected.
        gov._disabled.add("VWAPDeviation")
        gov.evaluate(now)

        assert gov.is_enabled("ChecklistMeta")
        assert "ChecklistMeta" not in gov.disabled_strategies
        assert gov.size_multiplier("ChecklistMeta") == pytest.approx(0.5)
        db.close()


def test_sizing_2pct_allows_two_same_side_blocks_third() -> None:
    """2.0% × 10x = 20% notional; 2 same-side under 50% cap; 3rd same-side blocked."""
    risk = RiskManager(
        Config(
            {
                "risk": {
                    "max_positions": 3,
                    "max_position_size_pct": 2.0,
                    "leverage_max": 10.0,
                    "max_daily_trades": 0,
                },
                "strategy": {
                    "portfolio_governance": {
                        "max_directional_exposure_pct": 50,
                        "max_sector_exposure_pct": 100,
                    },
                },
            }
        ),
        None,
    )

    class Pos:
        def __init__(self, side: str, entry_price: float, size: float) -> None:
            self.side = side
            self.entry_price = entry_price
            self.size = size

    capital = 100_000.0
    # Two longs at ~20% notional each (cap).
    positions = {
        "BTC": Pos("long", 50_000.0, 0.4),  # $20k = 20%
        "ETH": Pos("long", 2_000.0, 10.0),  # $20k = 20%
    }

    class Portfolio:
        current_capital = capital
        daily_pnl = 0.0
        daily_trades = 0
        def get_max_drawdown(self) -> float:
            return 0.0

    portfolio = Portfolio()
    portfolio.positions = positions

    third_same = Signal(
        strategy="ChecklistMeta",
        symbol="SOL",
        side="long",
        confidence=0.8,
        size_pct=0.01,
        entry_price=100.0,
        stop_loss_pct=0.015,
        take_profit_pct=0.03,
        metadata={"atr_pct": 0.01},
    )
    ok, reason = risk.can_enter(third_same, portfolio)
    assert not ok
    assert "directional" in reason.lower()

    opposite = Signal(
        strategy="ChecklistMeta",
        symbol="SOL",
        side="short",
        confidence=0.8,
        size_pct=0.01,
        entry_price=100.0,
        stop_loss_pct=0.015,
        take_profit_pct=0.03,
        metadata={"atr_pct": 0.01},
    )
    ok2, reason2 = risk.can_enter(opposite, portfolio)
    assert ok2, reason2
