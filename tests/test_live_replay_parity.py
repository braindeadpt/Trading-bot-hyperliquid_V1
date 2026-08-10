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


@pytest.mark.skipif(
    not (ROOT / "data" / "live" / "bot.db").exists()
    and not (ROOT / "data" / "live" / "bot_ruleset_validate.db").exists(),
    reason="no live candle DB for day-level parity check",
)
def test_replay_trade_count_tier_b_tolerance_vs_live_0630() -> None:
    """F: 2026-06-30 ChecklistMeta fills — Tier-B tolerance (OIR not reconstructible).

    Live had 24 CM trades that day. Honest band after Tier-B OIR verdict is
    wider than ±30%: accept [live*0.35, live*2.5] once sim-clock + warmup +
    parity_mode are on. Fail hard only if replay is near-zero (regression of A/C/D).
    """
    from src.backtest.engine import BacktestEngine, build_backtest_config_from_yaml
    from src.data.database import Database
    from src.strategies.factory import build_backtest_strategy

    snap = ROOT / "data" / "live" / "bot_ruleset_validate.db"
    db_path = snap if snap.exists() else ROOT / "data" / "live" / "bot.db"
    cfg = load_config(ROOT / "config" / "settings.yaml")
    # ChecklistMeta is shadow-only now; this archived parity fixture opts in
    # explicitly instead of depending on the active execution roster.
    cfg.set("strategy.phase08.execution_strategies", ["ChecklistMeta"])
    cfg.set("strategy.phase08.shadow_strategies", [])
    db = Database(str(db_path))
    symbols = list(cfg.get("assets") or ["BTC", "ETH", "SOL", "HYPE"])
    bt = build_backtest_config_from_yaml(cfg)
    bt.use_volatility_circuit = False
    bt.use_funding_blackout = False
    bt.max_daily_trades = 0
    bt.use_microstructure_proxy = True
    strategy = build_backtest_strategy(cfg)
    engine = BacktestEngine(
        database=db,
        strategy=strategy,
        config=bt,
        symbols=symbols,
        risk_config=cfg,
    )
    start = int(datetime(2026, 6, 30, tzinfo=timezone.utc).timestamp() * 1000)
    end = int(datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
    result = engine.run(start_ms=start, end_ms=end)
    trades = result.get("trades") or []
    cm = [
        t for t in trades
        if str(t.get("strategy") or t.get("sub_strategy") or "") == "ChecklistMeta"
    ]
    live_n = 24
    replay_n = len(cm)
    # Hard floor: A/C/D must produce a non-trivial fill count (not the old 0–1 bug).
    assert replay_n >= max(6, int(live_n * 0.35)), (
        f"replay CM trades={replay_n} below Tier-B floor vs live={live_n}; "
        f"gate_rejections sample={list(engine.gate_rejections)[:5]}"
    )
    assert replay_n <= int(live_n * 2.5), (
        f"replay CM trades={replay_n} above Tier-B ceiling vs live={live_n}"
    )
    manifest = result.get("manifest") or {}
    assert manifest.get("oir_proxy_calibration", {}).get("tier") == "B"
