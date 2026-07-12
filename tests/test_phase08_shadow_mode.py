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
    assert "ChecklistMeta" in shadow_names
    assert "CVDOrderFlow" in shadow_names
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
    trend_only, _ = route_phase08_signals([vb_sig, vwap_sig], adx=30.0, symbol="BTC")
    assert len(trend_only) == 1
    assert trend_only[0].strategy == "VolatilityBreakout"

    range_only, _ = route_phase08_signals([vb_sig, vwap_sig], adx=15.0, symbol="BTC")
    assert len(range_only) == 1
    assert range_only[0].strategy == "VWAPDeviation"
    assert classify_market_regime(15.0) == "low_vol"

    expansion, _ = route_phase08_signals([vb_sig], adx=22.0, symbol="BTC")
    assert len(expansion) == 1
    assert classify_market_regime(22.0) == "expansion"


def test_regime_router_rejects_contradictory_signals() -> None:
    vb_long = Signal(
        strategy="VolatilityBreakout", symbol="BTC", side="long",
        confidence=0.7, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    vwap_short = Signal(
        strategy="VWAPDeviation", symbol="BTC", side="short",
        confidence=0.8, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    # Force both through regime filter using expansion ADX (VB allowed) — inject VWAP bypass
    # by using expansion where only VB should pass; contradictory test uses both in expansion
    # with manual regime hack: at adx=22 only VB passes, so test contradiction in trend with
    # two same-strategy opposite — use two VB signals instead
    a = Signal(
        strategy="VolatilityBreakout", symbol="BTC", side="long",
        confidence=0.7, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    b = Signal(
        strategy="VolatilityBreakout", symbol="BTC", side="short",
        confidence=0.6, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    routed, reason = route_phase08_signals([a, b], adx=30.0, symbol="BTC")
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
    print("All Phase08 tests passed.")
