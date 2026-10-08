"""Replay exit path unit checks (live-vs-replay ChecklistMeta fixtures removed
2026-10-08 with the strategy module — the archived CM replay harness needed
the deleted class)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.strategies.base import ExitSignal, MarketEvent, Position

pytestmark = pytest.mark.integration_offline


@dataclass
class _FakeCandle:
    open: float
    high: float
    low: float
    close: float


def test_exit_path_prices_orderings() -> None:
    """Unit: P1 vs P2 OHLC sequences differ as documented."""
    eng = object.__new__(BacktestEngine)
    eng.cfg = BacktestConfig(exit_path_policy="favorable_first")
    c1m = _FakeCandle(open=100.0, high=110.0, low=90.0, close=105.0)
    p1_long = eng._exit_path_prices("long", c1m)  # type: ignore[arg-type]
    assert p1_long == [100.0, 110.0, 90.0, 105.0]
    eng.cfg = BacktestConfig(exit_path_policy="adverse_first")
    p2_long = eng._exit_path_prices("long", c1m)  # type: ignore[arg-type]
    assert p2_long == [100.0, 90.0, 110.0, 105.0]
    p2_short = eng._exit_path_prices("short", c1m)  # type: ignore[arg-type]
    assert p2_short == [100.0, 110.0, 90.0, 105.0]


def test_strategy_exit_fill_price_be_not_close() -> None:
    eng = object.__new__(BacktestEngine)
    eng.cfg = BacktestConfig(sl_to_be_buffer_pct=0.001)
    pos = MagicMock()
    pos.side = "long"
    pos.entry_price = 100.0
    pos.take_profit_price = 110.0
    be = eng._strategy_exit_fill_price(pos, "sl_to_be_hit_r0.60", path_price=95.0)
    assert abs(be - 100.1) < 1e-9
    pos.side = "short"
    be_s = eng._strategy_exit_fill_price(pos, "sl_to_be_hit_r0.60", path_price=105.0)
    assert abs(be_s - 99.9) < 1e-9


def test_process_exits_arms_be_on_favorable_then_cuts() -> None:
    """Synthetic: P1 walks H then L → BE ExitSignal filled at BE, not L/close."""

    class _ArmCutStrategy:
        name = "ChecklistMeta"

        def __init__(self) -> None:
            self.armed = False
            self.seen: List[float] = []

        def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
            self.seen.append(float(event.price))
            entry = float(position.entry_price)
            if position.side == "long":
                if event.price >= entry * 1.005:
                    self.armed = True
                if self.armed and event.price <= entry * 1.001:
                    return ExitSignal(
                        strategy=self.name,
                        symbol=position.symbol,
                        side=position.side,
                        confidence=0.7,
                        reason="sl_to_be_hit_r0.60",
                    )
            return None

    eng = object.__new__(BacktestEngine)
    eng.cfg = BacktestConfig(exit_path_policy="favorable_first", sl_to_be_buffer_pct=0.001)
    eng.strategy = _ArmCutStrategy()
    eng.positions_by_symbol = {"BTC": 1}
    closed: List[Any] = []

    @dataclass
    class _Pos:
        id: int = 1
        strategy: str = "ChecklistMeta"
        symbol: str = "BTC"
        side: str = "long"
        entry_price: float = 100.0
        size: float = 1.0
        entry_time_ms: int = 1
        stop_loss_price: float = 99.0
        take_profit_price: float = 103.0
        metadata: Dict[str, Any] = None  # type: ignore[assignment]
        excursion_id: str = "x"
        entry_commission: float = 0.0
        funding_paid: float = 0.0

        def __post_init__(self) -> None:
            if self.metadata is None:
                self.metadata = {"strategy": "ChecklistMeta"}

    pos = _Pos()
    eng.positions = {1: pos}

    def _close(pos_id, fill_price, ts, reason, capital):
        closed.append({"fill": fill_price, "reason": reason, "ts": ts})
        eng.positions_by_symbol.pop("BTC", None)
        eng.positions.pop(pos_id, None)
        return capital

    eng._close_position = _close  # type: ignore[method-assign]
    eng._intrabar_stop_tp = lambda *_a, **_k: None  # type: ignore[method-assign]

    event = MarketEvent(symbol="BTC", price=100.5, timestamp_ms=60_000)
    c1m = _FakeCandle(open=100.0, high=100.6, low=99.5, close=100.2)
    eng._process_exits(event, 100_000.0, c1m)  # type: ignore[arg-type]

    assert closed, f"expected BE exit; seen={eng.strategy.seen}"
    assert str(closed[0]["reason"]).startswith("sl_to_be")
    assert abs(float(closed[0]["fill"]) - 100.1) < 1e-9
    assert eng.strategy.seen == [100.0, 100.6, 99.5]  # stopped at L after arm
