"""Live vs replay parity gates (sim clock, parity_mode, Tier-B OIR tolerance)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import OIR_PROXY_CALIBRATION
from src.backtest.replay_data_quality import ReplayDataQualityGate, SymbolReplayAudit
from src.core.risk_manager import RiskManager
from src.strategies.base import MarketEvent, Signal
from src.utils.config import Config, load_config

pytestmark = pytest.mark.integration_offline


def _risk_cfg(**overrides: Any) -> Config:
    data: Dict[str, Any] = {
        "risk": {
            "max_positions": 5,
            "max_daily_trades": 0,
            "max_daily_stop_losses": 4,
            "max_daily_loss_pct": 3.0,
            "per_trade_risk_pct": 1.0,
            "max_position_size_pct": 5.0,
            "leverage_max": 10.0,
            "volatility_circuit_breaker": {"enabled": False},
            "funding_blackout": {"enabled": False},
        },
        "strategy": {
            "kelly": {"enabled": False},
            "portfolio_governance": {
                "max_directional_exposure_pct": 60.0,
                "max_sector_exposure_pct": 100.0,
            },
        },
    }
    data["risk"].update(overrides)
    path = ROOT / "data" / "tmp_parity_risk.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


def _portfolio() -> SimpleNamespace:
    return SimpleNamespace(
        daily_trades=0,
        positions={},
        daily_pnl=0.0,
        current_capital=100_000.0,
        get_max_drawdown=lambda: 0.0,
    )


def _sig() -> Signal:
    return Signal(
        strategy="ChecklistMeta",
        symbol="BTC",
        side="long",
        confidence=0.8,
        size_pct=0.01,
        stop_loss_pct=0.02,
    )


def _event(ts: int = 1_700_000_000_000) -> MarketEvent:
    return MarketEvent(symbol="BTC", price=50_000.0, timestamp_ms=ts)


def test_risk_manager_all_day_boundaries_follow_sim_clock() -> None:
    """A: stop streak + daily DD day keys use set_sim_time, not wall clock."""
    rm = RiskManager(_risk_cfg(), None)
    day1 = int(datetime(2026, 6, 28, 18, 0, tzinfo=timezone.utc).timestamp() * 1000)
    day2 = int(datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)

    rm.set_sim_time(day1)
    for _ in range(4):
        rm.on_trade_closed(SimpleNamespace(pnl_usd=-10.0, reason="stop_loss"))
    ok, reason = rm.can_enter(_sig(), _portfolio())
    assert not ok
    assert "daily_stop_streak" in reason

    # Without sim clock advance, wall-clock "today" would leave the circuit
    # permanently tripped across a multi-day backtest. Advancing sim day clears it.
    rm.set_sim_time(day2)
    ok2, reason2 = rm.can_enter(_sig(), _portfolio())
    assert ok2, reason2
    assert rm._utc_day() == "2026-06-30"


def test_parity_mode_skips_coverage_and_gap_kills() -> None:
    """C: parity_mode does not reject on coverage/gap; strict mode still does."""
    audit = SymbolReplayAudit(
        symbol="BTC",
        coverage_pct=0.40,
        max_gap_ms=10_000_000,
        bar_count=10,
        expected_bars=100,
        funding_available=True,
        oi_available=False,
    )
    strict = ReplayDataQualityGate(
        min_coverage_pct=0.95,
        max_bar_gap_ms=60_000,
        parity_mode=False,
        require_funding=False,
    )
    assert "replay_coverage_low" in (strict.check_entry(
        "BTC", _event(200_000), audit=audit, last_bar_ts=100_000,
        funding_ts_at=None, oi_ts_at=None,
    ) or "")

    parity = ReplayDataQualityGate(
        min_coverage_pct=0.95,
        max_bar_gap_ms=60_000,
        parity_mode=True,
        require_funding=False,
    )
    assert parity.check_entry(
        "BTC", _event(200_000), audit=audit, last_bar_ts=100_000,
        funding_ts_at=None, oi_ts_at=None,
    ) is None


def test_oir_proxy_calibration_documents_tier_b() -> None:
    """B: candle OIR proxy is measured unusable → ChecklistMeta Tier B in replay."""
    cal = OIR_PROXY_CALIBRATION
    assert cal["verdict"] == "unusable"
    assert cal["tier"] == "B"
    assert float(cal["w_oir_score_share_pct"]) == pytest.approx(12.5)
    assert float(cal["corr"]) < 0.5
    assert float(cal["oir_gate_agree_pct"]) < 70.0


def test_config_parity_mode_default_true() -> None:
    gate = ReplayDataQualityGate.from_config(Config({
        "backtest": {"replay_data_quality": {}},
    }))
    assert gate._parity_mode is True


def test_sparse_funding_rejects_all_entries_fresh_funding_passes() -> None:
    """Root cause of the VB 05-24..06-25 gap: sparse funding (2.9 rows/day)
    is always stale vs the 5-min freshness contract, so every signal is
    rejected with replay_funding_stale; dense funding passes.

    Reproduces the exact mechanism found in the gap trace (63/64 VB signals
    blocked) so the contract is pinned without needing the historical DB.
    """
    audit = SymbolReplayAudit(
        symbol="BTC",
        coverage_pct=1.0,
        max_gap_ms=60_000,
        bar_count=1000,
        expected_bars=1000,
        funding_available=True,
        oi_available=False,
    )
    gate = ReplayDataQualityGate(
        min_coverage_pct=0.95,
        max_bar_gap_ms=120_000,
        max_funding_stale_ms=300_000,  # production value (settings.yaml)
        require_funding=True,
        require_oi=False,
    )
    # Sparse funding: last funding point 8h before the entry -> stale -> reject.
    stale_ts = 1_700_000_000_000
    funding_8h_ago = stale_ts - 8 * 3_600_000
    reason = gate.check_entry(
        "BTC", _event(stale_ts), audit=audit, last_bar_ts=stale_ts - 60_000,
        funding_ts_at=funding_8h_ago, oi_ts_at=None,
    )
    assert reason is not None
    assert reason.startswith("replay_funding_stale")

    # Dense funding: last point 60s ago -> fresh -> passes.
    assert gate.check_entry(
        "BTC", _event(stale_ts), audit=audit, last_bar_ts=stale_ts - 60_000,
        funding_ts_at=stale_ts - 60_000, oi_ts_at=None,
    ) is None

    # No funding series at all -> no_series variant.
    assert gate.check_entry(
        "BTC", _event(stale_ts), audit=audit, last_bar_ts=stale_ts - 60_000,
        funding_ts_at=None, oi_ts_at=None,
    ) == "replay_funding_stale:no_series"
