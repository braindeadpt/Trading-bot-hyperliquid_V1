"""Pre-OOS consolidation checks — parity, statistics, Phase08 (no holdout/OOS)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.holdout_ledger import HoldoutGuard, HoldoutLedger, HoldoutViolationError
from src.backtest.monte_carlo import group_trades_into_blocks
from src.backtest.statistical_validation import (
    DSR_PROXY_LABEL,
    PBO_PROXY_LABEL,
    build_multiple_testing_report,
)
from src.core.phase08_regime_router import (
    SequentialContradictionGuard,
    classify_market_regime,
    route_phase08_signals,
)
from src.core.risk_manager import RiskManager
from src.core.signal_pipeline import GATE_ORDER, SignalPipeline
from src.data.research_microstructure import ResearchMicrostructureRecorder
from src.data.research_database import ResearchDatabase
from src.exchanges.hyperliquid_ws import DataBus, HlTrade, HyperliquidWSClient
from src.research.phase08_preregister import (
    PREREGISTER_PROTOCOL,
    build_preregister_manifest,
    persist_preregister_manifest,
    verify_preregister_integrity,
)
from src.strategies.base import Signal
from src.utils.config import Config, load_config
import pytest

pytestmark = pytest.mark.integration_offline


def _phase08_cfg() -> Config:
    return load_config(Path("config/settings.yaml"))


def _test_ledger_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    research = root / "data" / "research"
    research.mkdir(parents=True, exist_ok=True)
    return research / f"_test_holdout_{uuid.uuid4().hex}.json"


def test_gate_manifest_documents_parity() -> None:
    cfg = _phase08_cfg()
    pipeline = SignalPipeline(cfg, RiskManager(cfg, None), for_backtest=True)
    manifest = pipeline.gate_manifest()
    assert manifest["gate_parity_version"] == "phase05-gates-v1"
    assert "correlation" in manifest["shared_gate_order"]
    assert "strict rejects entries without L2" in manifest["tca_fidelity"]
    assert manifest["entry_debounce_ms"] >= 0
    assert GATE_ORDER.index("correlation") < GATE_ORDER.index("risk")


def test_preregister_v2_immutable_hash() -> None:
    root = Path(__file__).resolve().parent.parent
    research = root / "data" / "research"
    research.mkdir(parents=True, exist_ok=True)
    path = research / f"_test_preregister_{uuid.uuid4().hex}.json"
    try:
        cfg = _phase08_cfg()
        persist_preregister_manifest(cfg, path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["protocol"] == PREREGISTER_PROTOCOL
        assert data.get("experiment_id")
        assert data.get("manifest_hash")
        assert data["adx_contract"]["timeframe"] == "15m"
        assert data["adx_contract"]["closed_candles_only"] is True
        verify_preregister_integrity(data)
    finally:
        path.unlink(missing_ok=True)


def test_low_vol_regime_not_range() -> None:
    assert classify_market_regime(15.0) == "low_vol"
    assert classify_market_regime(15.0) != "range"


def test_sequential_contradiction_guard_blocks_flip() -> None:
    guard = SequentialContradictionGuard(block_ms=3_600_000)
    sig_long = Signal(
        strategy="VolatilityBreakout", symbol="BTC", side="long",
        confidence=0.7, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    sig_short = Signal(
        strategy="VolatilityBreakout", symbol="BTC", side="short",
        confidence=0.6, size_pct=0.01, stop_loss_pct=0.02, take_profit_pct=0.04,
    )
    ts = 1_700_000_000_000
    routed, _ = route_phase08_signals(
        [sig_long], adx=30.0, symbol="BTC",
        seq_guard=guard, timestamp_ms=ts,
    )[:2]
    assert len(routed) == 1
    guard.record("BTC", "long", ts)
    blocked, reason = route_phase08_signals(
        [sig_short], adx=30.0, symbol="BTC",
        seq_guard=guard, timestamp_ms=ts + 60_000,
    )[:2]
    assert blocked == []
    assert reason == "sequential_contradictory_signal"


def test_block_bootstrap_utc_days_only() -> None:
    day_a = 1_700_000_000_000
    day_b = day_a + 86_400_000
    trades = [
        {"pnl_usd": 1.0, "exit_time": day_a},
        {"pnl_usd": 2.0, "exit_time": day_a + 3_600_000},
        {"pnl_usd": 3.0, "exit_time": day_b},
    ]
    blocks = group_trades_into_blocks(trades, mode="day")
    assert len(blocks) == 2
    for block in blocks:
        assert len(block) in (1, 2)


def test_dsr_pbo_are_internal_proxies() -> None:
    report = build_multiple_testing_report(
        n_trials=10,
        n_observations=40,
        observed_sharpe=1.0,
        is_scores=[0.8, 0.3],
        oos_scores=[0.2, 0.5],
    )
    d = report.as_dict()
    assert DSR_PROXY_LABEL in d
    assert PBO_PROXY_LABEL in d
    assert d["is_academic_implementation"] is False


def test_holdout_guard_persists_across_restarts() -> None:
    ledger_path = _test_ledger_path()
    try:
        g1 = HoldoutGuard(HoldoutLedger(ledger_path))
        g1.begin_window()
        g1.freeze_params({"x": 1})
        g1.set_holdout_context(
            strategy_name="VolatilityBreakout",
            test_start_ms=100,
            test_end_ms=200,
            config_hash="deadbeef",
        )
        g1.evaluate_holdout(lambda: True)

        g2 = HoldoutGuard(HoldoutLedger(ledger_path))
        g2.begin_window()
        g2.freeze_params({"x": 1})
        try:
            g2.set_holdout_context(
                strategy_name="VolatilityBreakout",
                test_start_ms=100,
                test_end_ms=200,
                config_hash="deadbeef",
            )
            blocked = False
        except HoldoutViolationError:
            blocked = True
        assert blocked
    finally:
        ledger_path.unlink(missing_ok=True)


async def _tape_backpressure() -> None:
    root = Path(__file__).resolve().parent.parent
    path = root / "data" / "research" / f"_test_tape_{uuid.uuid4().hex}.db"
    db = ResearchDatabase(path)
    bus = DataBus(rate_limit_hz=100)
    ws = HyperliquidWSClient(bus=bus, symbols=["BTC"])
    rec = ResearchMicrostructureRecorder(
        bus, db, ["BTC"],
        tape_queue_max=1000,
        flush_interval_sec=60.0,
        health_interval_sec=60.0,
    )
    rec.attach_ws_client(ws)
    await rec.start()
    if rec._tape_consumer_task is not None:
        rec._tape_consumer_task.cancel()
        try:
            await rec._tape_consumer_task
        except asyncio.CancelledError:
            pass
        rec._tape_consumer_task = None
    now = int(time.time() * 1000)
    for i in range(1001):
        rec._on_trade_raw(
            HlTrade(symbol="BTC", timestamp_ms=now + i, price=100.0, size=1.0, side="B", tid=i),
        )
    await asyncio.sleep(0)
    assert rec._tape_dropped > 0, f"expected backpressure drops, got {rec._tape_dropped}"
    await rec.stop()
    del db
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def test_tape_recorder_backpressure_counter() -> None:
    asyncio.run(_tape_backpressure())


def test_tca_proxy_never_tier_a_even_with_hl_data_contract() -> None:
    from src.backtest.run_manifest import resolve_fidelity_tier

    tier = resolve_fidelity_tier(
        use_microstructure_proxy=False,
        tca_mode="proxy",
        data_contract_tier="tier_a_hl_ohlc",
    )
    assert tier == "tier_b_tca_proxy"
    assert not tier.startswith("tier_a")


def test_pre_parity_inventory_doc_exists() -> None:
    doc = Path("docs/PRE_PARITY_BACKTEST_RESULTS.md")
    text = doc.read_text(encoding="utf-8")
    assert "Pre-Parity" in text
    assert "consolidation" in text.lower() or "Pre-OOS" in text


if __name__ == "__main__":
    test_gate_manifest_documents_parity()
    print("  gate manifest parity OK")
    test_preregister_v2_immutable_hash()
    print("  preregister v2 hash OK")
    test_low_vol_regime_not_range()
    print("  low_vol regime OK")
    test_sequential_contradiction_guard_blocks_flip()
    print("  sequential contradiction OK")
    test_block_bootstrap_utc_days_only()
    print("  UTC block bootstrap OK")
    test_dsr_pbo_are_internal_proxies()
    print("  DSR/PBO proxy labels OK")
    test_holdout_guard_persists_across_restarts()
    print("  holdout persistence OK")
    test_tape_recorder_backpressure_counter()
    print("  tape backpressure OK")
    test_tca_proxy_never_tier_a_even_with_hl_data_contract()
    print("  TCA proxy tier cap OK")
    test_pre_parity_inventory_doc_exists()
    print("  pre-parity inventory doc OK")
    print("test_pre_oos_consolidation: all passed")
