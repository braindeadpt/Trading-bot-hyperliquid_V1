"""Shadow-only IV gate — record high/low-IV per routed trade, never enforce.

The IV gate (DVOL trailing percentile > 66.7) is INCONCLUSIVE in backtest
(n=13, docs/IV_HIGH_ONLY_AB_SPLIT.md), so it must NOT change execution.
``TradingEngine._record_iv_gate_shadow`` classifies each routed trade and
persists an ``iv_gate_shadow`` ShadowDecision — observability only.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.engine import TradingEngine  # noqa: E402
from src.data.dvol_feed import IV_HIGH_PCT  # noqa: E402
from src.strategies.base import MarketEvent, Signal  # noqa: E402
from src.strategies.indicators import Candle  # noqa: E402
from src.utils.config import Config  # noqa: E402


class _FakeRecorder:
    """Captures recorded ShadowDecisions without a research DB."""

    def __init__(self) -> None:
        self._db = None
        self.decisions: List[Any] = []

    def record(self, decision: Any) -> None:
        self.decisions.append(decision)


def _signal(strategy: str = "VWAPDeviation") -> Signal:
    return Signal(
        strategy=strategy,
        symbol="BTC",
        side="long",
        confidence=0.8,
        size_pct=0.01,
        entry_price=50_000.0,
        stop_loss_pct=0.012,
        take_profit_pct=0.024,
        reason="test",
        metadata={"sub_strategy": strategy},
    )


def _event(ts: int = 1_700_000_000_000) -> MarketEvent:
    c = Candle(50_000.0, 50_050.0, 49_950.0, 50_000.0, 100.0, ts)
    return MarketEvent(
        symbol="BTC", price=50_000.0, timestamp_ms=ts,
        candle_1m=c, candle_15m=c, adx_14=22.0,
    )


def _bare_engine(*, router: bool = True, dvol_enabled: bool = True) -> TradingEngine:
    eng = TradingEngine.__new__(TradingEngine)
    eng._phase08_regime_router = router
    eng._config = Config({"research": {"dvol_feed": {"enabled": dvol_enabled}}})
    eng._shadow_recorder = _FakeRecorder()
    return eng


def _record_and_get(
    engine: TradingEngine, pct, symbol: str = "BTC", strategy: str = "VWAPDeviation"
):
    import src.data.dvol_feed as dvol_mod

    orig = dvol_mod.current_dvol_percentile

    def fake_current(currency, ts_ms=None, db=None):
        return pct

    dvol_mod.current_dvol_percentile = fake_current
    try:
        engine._record_iv_gate_shadow(
            _signal(strategy=strategy), symbol=symbol, event=_event(),
        )
    finally:
        dvol_mod.current_dvol_percentile = orig
    return engine._shadow_recorder.decisions


def test_high_iv_recorded_with_metadata() -> None:
    eng = _bare_engine()
    decisions = _record_and_get(eng, 80.0)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.variant == "iv_gate_shadow"
    assert d.symbol == "BTC"
    assert d.strategy == "VWAPDeviation"
    assert d.would_enter is True
    assert d.reason == "iv_gate:high_iv"
    snap = d.market_snapshot
    meta = snap["metadata"]
    assert meta["iv_class"] == "high_iv"
    assert meta["iv_percentile"] == 80.0
    assert meta["iv_threshold"] == IV_HIGH_PCT
    assert meta["iv_currency"] == "BTC"


def test_low_iv_recorded() -> None:
    eng = _bare_engine()
    decisions = _record_and_get(eng, 30.0)
    assert len(decisions) == 1
    assert decisions[0].reason == "iv_gate:low_iv"
    assert decisions[0].market_snapshot["metadata"]["iv_class"] == "low_iv"


def test_unknown_when_no_dvol_data() -> None:
    eng = _bare_engine()
    decisions = _record_and_get(eng, None)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.reason == "iv_gate:unknown"
    meta = d.market_snapshot["metadata"]
    assert meta["iv_class"] == "unknown"
    assert meta["iv_percentile"] is None


def test_sol_uses_btc_proxy_currency() -> None:
    eng = _bare_engine()
    decisions = _record_and_get(eng, 90.0, symbol="SOL")
    assert len(decisions) == 1
    assert decisions[0].market_snapshot["metadata"]["iv_currency"] == "BTC"


def test_no_record_when_router_disabled() -> None:
    eng = _bare_engine(router=False)
    decisions = _record_and_get(eng, 80.0)
    assert decisions == []


def test_no_record_when_dvol_feed_disabled() -> None:
    eng = _bare_engine(dvol_enabled=False)
    decisions = _record_and_get(eng, 80.0)
    assert decisions == []


def test_record_failure_never_raises() -> None:
    """A broken recorder / DB must not raise into the signal loop."""
    eng = _bare_engine()

    class _Boom:
        _db = None

        def record(self, decision):
            raise RuntimeError("db locked")

    eng._shadow_recorder = _Boom()
    import src.data.dvol_feed as dvol_mod

    orig = dvol_mod.current_dvol_percentile
    dvol_mod.current_dvol_percentile = lambda *a, **k: 80.0
    try:
        # must not raise
        eng._record_iv_gate_shadow(_signal(), symbol="BTC", event=_event())
    finally:
        dvol_mod.current_dvol_percentile = orig


def test_iv_shadow_hook_is_after_route_before_execution() -> None:
    """Static regression: the shadow record sits between routing and execution.

    The routed signal must be classified (shadow) BEFORE ``_process_entry_signal``
    runs, and it must not alter the routed signal (execution path unchanged).
    """
    src = Path("src/core/engine.py").read_text(encoding="utf-8")
    start = src.index("best_signal = max(routed, key=lambda s: s.confidence)")
    end = src.index("await self._process_entry_signal(best_signal, event)", start)
    block = src[start:end]
    assert "_record_iv_gate_shadow(" in block
    assert block.index("_record_iv_gate_shadow(") < len(block)
