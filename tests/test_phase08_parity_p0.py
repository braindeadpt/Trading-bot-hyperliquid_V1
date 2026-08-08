"""P0 parity fixes: Phase08 router in backtest + seq_guard after fill."""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from src.backtest.engine import BacktestConfig, BacktestEngine, build_backtest_config_from_yaml
from src.core.phase08_regime_router import SequentialContradictionGuard
from src.strategies.base import MarketEvent, Signal, Strategy
from src.strategies.factory import build_backtest_strategy, build_phase08_strategies
from src.utils.config import load_config

pytestmark = pytest.mark.unit


class _StubStrat(Strategy):
    def __init__(self, name: str, side: str = "long", conf: float = 0.8) -> None:
        self._name = name
        self._side = side
        self._conf = conf
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        self.calls += 1
        return Signal(
            strategy=self._name,
            symbol=event.symbol,
            side=self._side,
            confidence=self._conf,
            size_pct=0.01,
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
        )

    def on_position(self, position, event):  # noqa: ANN001
        return None


def test_build_backtest_strategy_uses_phase08_execution_set() -> None:
    cfg = load_config(Path("config/settings.yaml"))
    strat = build_backtest_strategy(cfg)
    assert strat.name == "DirectRouter"
    names = {s.name for s in strat._strategies}
    assert names == {"ChecklistMeta", "VWAPDeviation"}
    execution, _ = build_phase08_strategies(cfg)
    assert {s.name for s in execution} == names


def test_build_backtest_config_enables_phase08_router() -> None:
    cfg = load_config(Path("config/settings.yaml"))
    bt = build_backtest_config_from_yaml(cfg)
    assert bt.use_phase08_regime_router is True
    assert bt.use_regime_weights is False
    assert bt.adx_range_threshold == pytest.approx(20.0)
    assert bt.adx_trend_threshold == pytest.approx(25.0)


def test_backtest_collect_entry_blocks_vwap_in_trend() -> None:
    vb = _StubStrat("VolatilityBreakout", "long", 0.7)
    vwap = _StubStrat("VWAPDeviation", "short", 0.9)
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.strategy = MagicMock(_strategies=[vb, vwap])
    engine.cfg = BacktestConfig(use_phase08_regime_router=True)
    engine._phase08_seq_guard = SequentialContradictionGuard(block_ms=3_600_000)
    engine.gate_rejections = []

    event = MarketEvent(
        symbol="BTC", price=100.0, timestamp_ms=1_800_000_000_000, adx_14=30.0,
    )
    sig = BacktestEngine._collect_entry_signal(engine, event)
    assert sig is not None
    assert sig.strategy == "VolatilityBreakout"
    assert vb.calls == 1 and vwap.calls == 1


def test_backtest_collect_entry_blocks_vb_in_low_vol() -> None:
    vb = _StubStrat("VolatilityBreakout", "long", 0.9)
    vwap = _StubStrat("VWAPDeviation", "short", 0.7)
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.strategy = MagicMock(_strategies=[vb, vwap])
    engine.cfg = BacktestConfig(use_phase08_regime_router=True)
    engine._phase08_seq_guard = SequentialContradictionGuard(block_ms=3_600_000)
    engine.gate_rejections = []

    event = MarketEvent(
        symbol="BTC", price=100.0, timestamp_ms=1_800_000_000_000, adx_14=15.0,
    )
    sig = BacktestEngine._collect_entry_signal(engine, event)
    assert sig is not None
    assert sig.strategy == "VWAPDeviation"


def test_seq_guard_only_locks_after_explicit_record() -> None:
    """Contract: risk rejects must NOT call record (engine fix).

    Before the P0 fix, ``_process_entry_signal`` recorded immediately, so a
    risk-rejected long still blocked shorts for 1h. Recording is now deferred
    until paper executed / OMS first fill — this unit test locks the guard API
    contract that those call sites rely on.
    """
    guard = SequentialContradictionGuard(block_ms=3_600_000)
    t0 = 1_800_000_000_000
    # No prior fill → flip allowed
    assert guard.check("BTC", "short", t0) is None
    # Accepted long fill
    guard.record("BTC", "long", t0)
    assert guard.check("BTC", "short", t0 + 60_000) == "sequential_contradictory_signal"
    assert guard.check("BTC", "long", t0 + 60_000) is None


def test_engine_source_defers_seq_guard_record() -> None:
    """Static regression: early record in _process_entry_signal must stay gone."""
    src = Path("src/core/engine.py").read_text(encoding="utf-8")
    # Find the method body start and ensure the first seq_guard.record is
    # after the executed-status block, not before evaluate_gates.
    start = src.index("async def _process_entry_signal")
    end = src.index("\n    async def ", start + 10)
    body = src[start:end]
    assert "evaluate_gates" in body
    first_record = body.index("self._phase08_seq_guard.record")
    gates = body.index("evaluate_gates")
    assert first_record > gates
    assert 'sig_record["status"] = "executed"' in body
    assert body.index('sig_record["status"] = "executed"') < first_record
