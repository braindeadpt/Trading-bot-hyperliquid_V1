"""Phase 08 behavioral tests — edge isolation, regime router, shadow isolation."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.strategy_feed_requirements import (
    TIER_A_OHLC,
    TIER_B_PHASE08_SHADOW,
    FeedAvailability,
    resolve_strategy_tier,
)
from src.core.phase08_regime_router import (
    classify_market_regime,
    route_phase08_signals,
)
from src.data.research_database import ResearchDatabase
from src.data.research_microstructure import ResearchMicrostructureRecorder
from src.exchanges.hyperliquid_ws import DataBus, HlL2Book, HlLevel, HlTrade, HyperliquidWSClient
from src.research.phase08_preregister import (
    PREREGISTER_PROTOCOL,
    build_preregister_manifest,
    verify_preregister_integrity,
)
from src.strategies.base import Signal
from src.strategies.factory import (
    PHASE08_DEFAULT_EXECUTION,
    PHASE08_DEFAULT_SHADOW,
    build_live_strategies,
    build_phase08_strategies,
    phase08_enabled,
)
from src.utils.config import Config, load_config, resolve_kelly_enabled
import pytest

pytestmark = pytest.mark.integration_offline


def _cfg_with_phase08(enabled: bool = True) -> Config:
    base = load_config(Path("config/settings.yaml"))
    data = dict(base.raw)
    strat = dict(data.get("strategy", {}))
    strat["phase08"] = {
        "enabled": enabled,
        "paper_only": True,
        "execution_strategies": list(PHASE08_DEFAULT_EXECUTION),
        "shadow_strategies": list(PHASE08_DEFAULT_SHADOW),
        "regime_router": {"enabled": True, "adx_range_threshold": 20.0, "adx_trend_threshold": 25.0},
    }
    strat["checklist_meta"] = {**(strat.get("checklist_meta") or {}), "enabled": False}
    strat["kelly"] = {**(strat.get("kelly") or {}), "enabled": False}
    data["strategy"] = strat
    data["mode"] = "paper"
    return Config(data)


def test_phase08_factory_splits_execution_and_shadow() -> None:
    cfg = _cfg_with_phase08(True)
    assert phase08_enabled(cfg)
    execution, shadow = build_phase08_strategies(cfg)
    exec_names = {s.name for s in execution}
    shadow_names = {s.name for s in shadow}
    assert exec_names == set(PHASE08_DEFAULT_EXECUTION)
    assert "ChecklistMeta" in exec_names
    assert "VolatilityBreakout" in shadow_names
    assert "CVDOrderFlow" in shadow_names
    assert "ChecklistMeta" not in shadow_names
    assert execution[0] is not shadow[0]


def test_shadow_instances_are_separate_from_execution() -> None:
    cfg = _cfg_with_phase08(True)
    execution, shadow = build_phase08_strategies(cfg)
    exec_ids = {id(s) for s in execution}
    for s in shadow:
        assert id(s) not in exec_ids
        assert getattr(s, "_shadow_instance", False) is True
    for s in execution:
        assert getattr(s, "_execution_instance", False) is True


def test_regime_router_vb_trend_vwap_range() -> None:
    vb_sig = Signal(
        strategy="VolatilityBreakout", symbol="BTC", side="long",
        confidence=0.7, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    vwap_sig = Signal(
        strategy="VWAPDeviation", symbol="BTC", side="short",
        confidence=0.8, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    # Expansion-only rework: VB is BLOCKED in trend (ADX 30); VWAP is also
    # not eligible in trend -> the batch resolves to no allowed strategy.
    trend_only, trend_reason, _ = route_phase08_signals([vb_sig, vwap_sig], adx=30.0, symbol="BTC")
    assert trend_only == []
    assert trend_reason == "regime_trend_no_allowed_strategies"
    assert classify_market_regime(30.0) == "trend"

    range_only, _, _ = route_phase08_signals([vb_sig, vwap_sig], adx=15.0, symbol="BTC")
    assert len(range_only) == 1
    assert range_only[0].strategy == "VWAPDeviation"
    assert classify_market_regime(15.0) == "low_vol"

    expansion, _, blocked_exp = route_phase08_signals([vb_sig], adx=22.0, symbol="BTC")
    assert len(expansion) == 1
    assert expansion[0].strategy == "VolatilityBreakout"
    assert classify_market_regime(22.0) == "expansion"
    assert blocked_exp == []


def test_regime_router_rejects_contradictory_signals() -> None:
    vb_long = Signal(
        strategy="VolatilityBreakout", symbol="BTC", side="long",
        confidence=0.7, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    vwap_short = Signal(
        strategy="VWAPDeviation", symbol="BTC", side="short",
        confidence=0.8, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    # Contradiction test: two opposite VB signals in EXPANSION (the only
    # regime where VB is eligible post-rework) -> both pass the regime gate
    # and the router must reject the batch as contradictory.
    a = Signal(
        strategy="VolatilityBreakout", symbol="BTC", side="long",
        confidence=0.7, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    b = Signal(
        strategy="VolatilityBreakout", symbol="BTC", side="short",
        confidence=0.6, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    routed, reason, _blocked = route_phase08_signals([a, b], adx=22.0, symbol="BTC")
    assert routed == []
    assert reason == "contradictory_simultaneous_signals"


def test_kelly_disabled_via_single_flag() -> None:
    cfg = _cfg_with_phase08(True)
    assert resolve_kelly_enabled(cfg, for_backtest=False) is False
    assert "kelly_in_execution" not in (cfg.get("strategy.phase08", {}) or {})


def test_preregister_manifest_documents_no_edge() -> None:
    cfg = _cfg_with_phase08(True)
    manifest = build_preregister_manifest(cfg)
    assert manifest["protocol"] == PREREGISTER_PROTOCOL
    assert manifest.get("experiment_id")
    assert manifest.get("manifest_hash")
    verify_preregister_integrity(manifest)
    assert manifest["edge_demonstrated"] is False
    assert manifest["oos_status"] == "pending"
    assert "tier_a_hl_ohlc" in manifest["fidelity_note"]
    assert manifest["adx_contract"]["closed_candles_only"] is True
    assert "VolatilityBreakout" in manifest["strategies"]
    assert "kill_criteria" in manifest["strategies"]["VolatilityBreakout"]


def test_phase08_fidelity_caps_shadow_strategies() -> None:
    full = FeedAvailability(
        symbol="BTC",
        hl_candles=True,
        hl_venue=True,
        taker_split=True,
        l2_snapshots=True,
        trade_tape=True,
        funding=True,
        oi=True,
        candle_coverage_pct=0.99,
    )
    vb = resolve_strategy_tier("VolatilityBreakout", full, phase08_shadow_only=True)
    assert vb.tier == TIER_A_OHLC
    cvd = resolve_strategy_tier("CVDOrderFlow", full, phase08_shadow_only=True)
    assert cvd.tier == TIER_B_PHASE08_SHADOW


async def _run_microstructure_raw_test() -> None:
    path = Path(tempfile.gettempdir()) / f"test_micro_{uuid.uuid4().hex}.db"
    db = ResearchDatabase(path)
    bus = DataBus(rate_limit_hz=10)  # would drop trades on DataBus path
    ws = HyperliquidWSClient(bus=bus, symbols=["BTC"])
    rec = ResearchMicrostructureRecorder(
        bus, db, ["BTC"],
        l2_min_interval_ms=0,
        tape_gap_threshold_ms=2_000,
        health_interval_sec=60.0,
        flush_interval_sec=0.1,
    )
    rec.attach_ws_client(ws)
    await rec.start()
    now = int(time.time() * 1000)
    trade = HlTrade(symbol="BTC", timestamp_ms=now, price=100.0, size=1.0, side="B", tid=99)
    ws._raw_trade_listeners[0](trade)  # raw tap, not bus.publish
    bus.publish(
        "orderbook:BTC",
        HlL2Book(
            symbol="BTC",
            timestamp_ms=now,
            bids=[HlLevel(99.9, 10.0)],
            asks=[HlLevel(100.1, 8.0)],
        ),
    )
    await asyncio.sleep(0.25)
    await rec.stop()
    tape_count = db.count_trade_tape_in_window("BTC", now - 60_000, now + 60_000)
    assert tape_count >= 1, f"raw tap expected ≥1 row, got {tape_count}"


def test_raw_trade_tap_bypasses_databus() -> None:
    asyncio.run(_run_microstructure_raw_test())


def test_build_live_strategies_returns_shadow_when_phase08() -> None:
    cfg = _cfg_with_phase08(True)
    execution, shadow = build_live_strategies(cfg)
    assert len(execution) == 2
    assert len(shadow) >= 4


def test_evaluate_shadow_strategies_persists_bracket_fields() -> None:
    """Observability enrichment: stop/TP/size/metadata land in market_snapshot."""
    from src.core.engine import TradingEngine
    from src.core.execution import ExecutionEngine
    from src.core.risk_manager import RiskManager
    from src.data.database import Database
    from src.exchanges.hyperliquid_ws import DataBus
    from src.research.shadow_recorder import ShadowDecision
    from src.strategies.base import MarketEvent, Signal

    class _Stub:
        name = "StubShadow"

        def __init__(self) -> None:
            self._shadow_instance = True

        def on_data(self, event: MarketEvent) -> Signal:
            return Signal(
                strategy=self.name,
                symbol=event.symbol,
                side="short",
                confidence=0.61,
                size_pct=0.007,
                stop_loss_pct=0.002,
                take_profit_pct=0.0025,
                metadata={"src": "phase08_test"},
            )

    class _Cap:
        def __init__(self) -> None:
            self.last: ShadowDecision | None = None

        def record(self, decision: ShadowDecision) -> None:
            self.last = decision

    class _Boom(_Cap):
        def record(self, decision: ShadowDecision) -> None:
            self.last = decision
            raise RuntimeError("must not escape engine loop")

    cfg = Config(
        {
            "symbols": ["BTC"],
            "strategy": {"cooldown": {"base_minutes": 30, "max_minutes": 120}},
            "risk": {"max_position_size_pct": 5.0, "leverage_max": 5.0},
        }
    )
    engine = TradingEngine(
        cfg,
        Database(":memory:"),
        DataBus(),
        [],
        RiskManager(cfg, Database(":memory:")),
        ExecutionEngine(cfg, Database(":memory:"), "paper"),
        shadow_strategies=[_Stub()],
    )
    cap = _Cap()
    engine._shadow_recorder = cap  # type: ignore[assignment]
    engine._evaluate_shadow_strategies(
        MarketEvent(symbol="BTC", price=64000.0, timestamp_ms=1),
        "BTC",
    )
    assert cap.last is not None
    snap = cap.last.market_snapshot or {}
    assert snap["stop_loss_pct"] == 0.002
    assert snap["take_profit_pct"] == 0.0025
    assert snap["size_pct"] == 0.007
    assert snap["metadata"]["src"] == "phase08_test"

    boom = _Boom()
    engine._shadow_recorder = boom  # type: ignore[assignment]
    engine._evaluate_shadow_strategies(
        MarketEvent(symbol="BTC", price=64000.0, timestamp_ms=2),
        "BTC",
    )
    assert boom.last is not None  # invoked; exception swallowed


def _make_router_engine(*, throttle_ms: int = 300_000):
    """Minimal engine with Phase08 regime router on and a VWAP-like stub."""
    from src.core.engine import TradingEngine
    from src.core.execution import ExecutionEngine
    from src.core.risk_manager import RiskManager
    from src.data.database import Database
    from src.exchanges.hyperliquid_ws import DataBus
    from src.research.shadow_recorder import ShadowDecision
    from src.strategies.base import MarketEvent, Signal

    class _VWAPStub:
        name = "VWAPDeviation"

        def __init__(self) -> None:
            self.SIGNAL_THROTTLE_MS = throttle_ms
            self._next: Signal | None = None

        def on_data(self, event: MarketEvent) -> Signal | None:
            return self._next

    class _Cap:
        def __init__(self) -> None:
            self.rows: List[ShadowDecision] = []

        def record(self, decision: ShadowDecision) -> None:
            self.rows.append(decision)

    class _Boom(_Cap):
        def record(self, decision: ShadowDecision) -> None:
            self.rows.append(decision)
            raise RuntimeError("recorder must not break block path")

    cfg = Config(
        {
            "symbols": ["BTC", "ETH"],
            "mode": "paper",
            "strategy": {
                "cooldown": {"base_minutes": 30, "max_minutes": 120},
                "phase08": {
                    "enabled": True,
                    "paper_only": True,
                    "regime_router": {
                        "enabled": True,
                        "adx_range_threshold": 20.0,
                        "adx_trend_threshold": 25.0,
                    },
                },
            },
            "risk": {"max_position_size_pct": 5.0, "leverage_max": 5.0},
        }
    )
    stub = _VWAPStub()
    db = Database(":memory:")
    engine = TradingEngine(
        cfg,
        db,
        DataBus(),
        [stub],
        RiskManager(cfg, db),
        ExecutionEngine(cfg, db, "paper"),
    )
    assert engine._phase08_regime_router is True
    cap = _Cap()
    engine._shadow_recorder = cap  # type: ignore[assignment]
    return engine, stub, cap, _Boom, MarketEvent, Signal


def test_a_router_blocked_recorded_with_brackets_and_still_blocked() -> None:
    """Blocked VWAP in expansion is recorded; routed list stays empty (no exec)."""
    from src.core.phase08_regime_router import route_phase08_signals

    engine, stub, cap, _Boom, MarketEvent, Signal = _make_router_engine()
    sig = Signal(
        strategy="VWAPDeviation",
        symbol="BTC",
        side="long",
        confidence=0.9,
        size_pct=0.01,
        stop_loss_pct=0.015,
        take_profit_pct=0.03,
        metadata={"sigma": -4.64},
    )
    stub._next = sig
    event = MarketEvent(symbol="BTC", price=100_000.0, timestamp_ms=10_000_000)
    engine._latest_adx["BTC"] = 22.0  # expansion dead zone

    routed, reason, blocked = route_phase08_signals(
        [sig], 22.0, symbol="BTC",
        adx_range_threshold=20.0, adx_trend_threshold=25.0,
    )
    assert routed == []
    assert blocked == [sig]
    assert reason is not None and "expansion" in reason

    engine._record_router_blocked_signals(
        blocked, event=event, regime="expansion", adx=22.0
    )
    assert len(cap.rows) == 1
    row = cap.rows[0]
    assert row.variant == "router_blocked"
    assert row.would_enter is True
    assert row.reason == "router_blocked:expansion"
    assert row.strategy == "VWAPDeviation"
    snap = row.market_snapshot or {}
    assert snap["stop_loss_pct"] == pytest.approx(0.015)
    assert snap["take_profit_pct"] == pytest.approx(0.03)
    assert snap["size_pct"] == pytest.approx(0.01)
    assert snap["metadata"]["router_regime"] == "expansion"
    assert snap["metadata"]["router_adx"] == pytest.approx(22.0)
    assert snap["metadata"]["sigma"] == pytest.approx(-4.64)
    # Signal still blocked — we never called _process_entry_signal
    assert routed == []


def test_b_router_block_throttle_same_key() -> None:
    engine, stub, cap, _Boom, MarketEvent, Signal = _make_router_engine(throttle_ms=300_000)
    stub.SIGNAL_THROTTLE_MS = 300_000
    event = MarketEvent(symbol="BTC", price=100.0, timestamp_ms=1_000_000)
    sig = Signal(
        strategy="VWAPDeviation", symbol="BTC", side="long",
        confidence=0.8, size_pct=0.01, stop_loss_pct=0.01, take_profit_pct=0.02,
    )
    engine._record_router_blocked_signals([sig], event=event, regime="expansion", adx=22.0)
    assert len(cap.rows) == 1

    # Within window → not recorded
    event2 = MarketEvent(symbol="BTC", price=101.0, timestamp_ms=1_000_000 + 299_999)
    engine._record_router_blocked_signals([sig], event=event2, regime="expansion", adx=22.0)
    assert len(cap.rows) == 1

    # After window → recorded
    event3 = MarketEvent(symbol="BTC", price=102.0, timestamp_ms=1_000_000 + 300_000)
    engine._record_router_blocked_signals([sig], event=event3, regime="expansion", adx=22.0)
    assert len(cap.rows) == 2


def test_c_router_block_throttle_different_symbol_or_side() -> None:
    engine, stub, cap, _Boom, MarketEvent, Signal = _make_router_engine(throttle_ms=1_800_000)
    ts = 5_000_000
    base = dict(confidence=0.8, size_pct=0.01, stop_loss_pct=0.01, take_profit_pct=0.02)
    sig_btc_long = Signal(strategy="VWAPDeviation", symbol="BTC", side="long", **base)
    sig_eth_long = Signal(strategy="VWAPDeviation", symbol="ETH", side="long", **base)
    sig_btc_short = Signal(strategy="VWAPDeviation", symbol="BTC", side="short", **base)

    engine._record_router_blocked_signals(
        [sig_btc_long],
        event=MarketEvent(symbol="BTC", price=100.0, timestamp_ms=ts),
        regime="expansion",
        adx=22.0,
    )
    engine._record_router_blocked_signals(
        [sig_eth_long],
        event=MarketEvent(symbol="ETH", price=3000.0, timestamp_ms=ts + 1),
        regime="expansion",
        adx=22.0,
    )
    engine._record_router_blocked_signals(
        [sig_btc_short],
        event=MarketEvent(symbol="BTC", price=100.0, timestamp_ms=ts + 2),
        regime="expansion",
        adx=22.0,
    )
    assert len(cap.rows) == 3
    keys = {(r.strategy, r.symbol, r.side) for r in cap.rows}
    assert keys == {
        ("VWAPDeviation", "BTC", "long"),
        ("VWAPDeviation", "ETH", "long"),
        ("VWAPDeviation", "BTC", "short"),
    }


def test_d_router_block_recorder_exception_does_not_break_loop() -> None:
    engine, stub, cap, Boom, MarketEvent, Signal = _make_router_engine()
    boom = Boom()
    engine._shadow_recorder = boom  # type: ignore[assignment]
    sig = Signal(
        strategy="VWAPDeviation", symbol="BTC", side="long",
        confidence=0.8, size_pct=0.01, stop_loss_pct=0.01, take_profit_pct=0.02,
    )
    # Must not raise
    engine._record_router_blocked_signals(
        [sig],
        event=MarketEvent(symbol="BTC", price=100.0, timestamp_ms=1),
        regime="expansion",
        adx=22.0,
    )
    assert len(boom.rows) == 1


def test_f_router_block_no_recorder_no_crash() -> None:
    engine, stub, cap, _Boom, MarketEvent, Signal = _make_router_engine()
    engine._shadow_recorder = None
    # Force lazy init to fail by monkeypatching
    def _fail() -> None:
        raise RuntimeError("no db")

    engine._ensure_shadow_recorder = lambda: None  # type: ignore[method-assign]
    sig = Signal(
        strategy="VWAPDeviation", symbol="BTC", side="long",
        confidence=0.8, size_pct=0.01, stop_loss_pct=0.01, take_profit_pct=0.02,
    )
    engine._record_router_blocked_signals(
        [sig],
        event=MarketEvent(symbol="BTC", price=100.0, timestamp_ms=1),
        regime="expansion",
        adx=22.0,
    )


if __name__ == "__main__":
    test_phase08_factory_splits_execution_and_shadow()
    print("  factory split OK")
    test_shadow_instances_are_separate_from_execution()
    print("  shadow isolation OK")
    test_regime_router_vb_trend_vwap_range()
    print("  regime router OK")
    test_regime_router_rejects_contradictory_signals()
    print("  contradictory block OK")
    test_kelly_disabled_via_single_flag()
    print("  kelly single flag OK")
    test_preregister_manifest_documents_no_edge()
    print("  preregister manifest OK")
    test_phase08_fidelity_caps_shadow_strategies()
    print("  fidelity caps OK")
    test_raw_trade_tap_bypasses_databus()
    print("  raw trade tap OK")
    test_build_live_strategies_returns_shadow_when_phase08()
    print("  build_live_strategies OK")
    test_evaluate_shadow_strategies_persists_bracket_fields()
    print("  shadow enrichment OK")
    test_a_router_blocked_recorded_with_brackets_and_still_blocked()
    print("  router_blocked record OK")
    test_b_router_block_throttle_same_key()
    print("  throttle OK")
    test_c_router_block_throttle_different_symbol_or_side()
    print("  throttle key OK")
    test_d_router_block_recorder_exception_does_not_break_loop()
    print("  recorder exception OK")
    test_f_router_block_no_recorder_no_crash()
    print("  no recorder OK")
    print("All Phase08 tests passed.")
