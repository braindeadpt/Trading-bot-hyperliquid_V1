"""Tests for the Fase 10 frozen-window gate metrics calculator.

Pure-function math (PF / expectancy_R / drawdown) is marked `unit` with
synthetic hand-computed trades. The end-to-end path (tmp sqlite DB with the
real trades-table schema + a tmp preregister manifest) is marked
`integration_offline`. Neither ever touches data/live/bot.db.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from src.research.phase10_gate_metrics import (
    Phase10GateMetricsError,
    STATUS_FAIL,
    STATUS_INSUFFICIENT_DATA,
    STATUS_PASS,
    build_gate_report,
    compute_cost_summary,
    compute_expectancy_r,
    compute_max_drawdown_pct,
    compute_profit_factor,
    load_qualifying_trades,
)
from src.research.phase10_preregister import persist_preregister_manifest
from src.utils.config import Config


def _trade(
    pnl_usd,
    *,
    entry_price=100.0,
    size=1.0,
    strategy="VolatilityBreakout",
    stop_loss_pct=None,
    atr_pct=None,
    entry_fee=0.0,
    cumulative_order_fee=0.0,
    funding_paid=0.0,
):
    meta = {}
    if stop_loss_pct is not None:
        meta["stop_loss_pct"] = stop_loss_pct
    if atr_pct is not None:
        meta["atr_pct"] = atr_pct
    return {
        "pnl_usd": pnl_usd,
        "entry_price": entry_price,
        "size": size,
        "strategy": strategy,
        "signal_metadata": json.dumps(meta) if meta else None,
        "entry_fee": entry_fee,
        "cumulative_order_fee": cumulative_order_fee,
        "funding_paid": funding_paid,
    }


# ---------------------------------------------------------------------------
# Profit factor
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_profit_factor_hand_computed():
    trades = [_trade(10.0), _trade(-5.0), _trade(20.0), _trade(-15.0)]
    # gross_profit=30, gross_loss=20 -> PF = 1.5
    assert compute_profit_factor(trades) == pytest.approx(1.5)


@pytest.mark.unit
def test_profit_factor_zero_losses_uses_sentinel():
    trades = [_trade(10.0), _trade(5.0)]
    assert compute_profit_factor(trades) == 99.0


@pytest.mark.unit
def test_profit_factor_all_losses_is_zero():
    trades = [_trade(-10.0), _trade(-5.0)]
    assert compute_profit_factor(trades) == 0.0


@pytest.mark.unit
def test_profit_factor_no_trades_is_zero():
    assert compute_profit_factor([]) == 0.0


# ---------------------------------------------------------------------------
# Expectancy R
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_expectancy_r_hand_computed_with_stop_loss_pct():
    # risk_usd = entry_price * stop_loss_pct * size
    # trade 1: risk = 100 * 0.02 * 1 = 2.0, pnl=4.0 -> R=2.0
    # trade 2: risk = 100 * 0.02 * 1 = 2.0, pnl=-1.0 -> R=-0.5
    trades = [
        _trade(4.0, stop_loss_pct=0.02),
        _trade(-1.0, stop_loss_pct=0.02),
    ]
    result = compute_expectancy_r(trades, config=None)
    assert result["expectancy_r"] == pytest.approx((2.0 + -0.5) / 2)
    assert result["trades_used"] == 2
    assert result["trades_total"] == 2


@pytest.mark.unit
def test_expectancy_r_falls_back_to_atr_pct_with_config():
    config = Config({
        "strategy": {"volatility_breakout": {"stop_loss_atr_multiplier": 2.0}},
    })
    # risk_usd = entry_price * (atr_pct * multiplier) * size = 100 * (0.01*2.0) * 1 = 2.0
    trades = [_trade(6.0, strategy="VolatilityBreakout", atr_pct=0.01)]
    result = compute_expectancy_r(trades, config=config)
    assert result["expectancy_r"] == pytest.approx(3.0)
    assert result["trades_used"] == 1


@pytest.mark.unit
def test_expectancy_r_excludes_trades_with_no_derivable_risk():
    trades = [
        _trade(4.0, stop_loss_pct=0.02),
        _trade(999.0),  # no stop_loss_pct, no atr_pct, no config -> excluded
    ]
    result = compute_expectancy_r(trades, config=None)
    assert result["trades_used"] == 1
    assert result["trades_total"] == 2
    assert result["expectancy_r"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Max drawdown %
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_max_drawdown_monotonic_increase_is_near_zero():
    trades = [_trade(10.0), _trade(10.0), _trade(10.0)]
    dd = compute_max_drawdown_pct(trades, initial_capital=1000.0)
    assert dd == pytest.approx(0.0, abs=1e-9)


@pytest.mark.unit
def test_max_drawdown_hand_computed():
    # equity: 1000 -> 1100 (peak) -> 1000 -> 900 -> 950
    # max drawdown = (1100-900)/1100 = 18.1818...%
    trades = [_trade(100.0), _trade(-100.0), _trade(-100.0), _trade(50.0)]
    dd = compute_max_drawdown_pct(trades, initial_capital=1000.0)
    assert dd == pytest.approx((1100 - 900) / 1100 * 100.0, rel=1e-6)


@pytest.mark.unit
def test_max_drawdown_no_trades_is_zero():
    assert compute_max_drawdown_pct([], initial_capital=1000.0) == 0.0


# ---------------------------------------------------------------------------
# Cost summary
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cost_summary_computes_pct_of_gross_profit():
    trades = [
        _trade(100.0, entry_fee=1.0, cumulative_order_fee=0.5, funding_paid=0.5),
        _trade(-20.0, entry_fee=1.0, cumulative_order_fee=0.0, funding_paid=0.0),
    ]
    summary = compute_cost_summary(trades)
    assert summary["total_costs_usd"] == pytest.approx(3.0)
    assert summary["gross_profit_usd"] == pytest.approx(100.0)
    assert summary["cost_pct_of_gross_profit"] == pytest.approx(3.0)


@pytest.mark.unit
def test_cost_summary_none_pct_when_no_gross_profit():
    trades = [_trade(-20.0, entry_fee=1.0)]
    summary = compute_cost_summary(trades)
    assert summary["cost_pct_of_gross_profit"] is None


# ---------------------------------------------------------------------------
# End-to-end: tmp sqlite DB (real trades-table schema) + tmp manifest
# ---------------------------------------------------------------------------

_TRADES_TABLE_SQL = """
    CREATE TABLE trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT    NOT NULL,
        side        TEXT    NOT NULL,
        entry_price REAL    NOT NULL,
        exit_price  REAL,
        entry_time  INTEGER NOT NULL,
        exit_time   INTEGER,
        size        REAL    NOT NULL,
        pnl_usd     REAL,
        pnl_pct     REAL,
        strategy    TEXT    NOT NULL,
        exit_reason TEXT,
        status      TEXT    NOT NULL DEFAULT 'open',
        sub_strategy TEXT,
        entry_fee   REAL    DEFAULT 0.0,
        funding_paid REAL   DEFAULT 0.0,
        cumulative_order_fee REAL DEFAULT 0,
        signal_metadata TEXT
    );
"""


def _seed_tmp_db(db_path: Path, rows):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_TRADES_TABLE_SQL)
        for i, r in enumerate(rows):
            conn.execute(
                """
                INSERT INTO trades (
                    symbol, side, entry_price, exit_price, entry_time, exit_time,
                    size, pnl_usd, pnl_pct, strategy, exit_reason, status,
                    sub_strategy, entry_fee, funding_paid, cumulative_order_fee,
                    signal_metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "BTC", "long", r["entry_price"], r["entry_price"] + 1,
                    r["entry_time"], r["exit_time"], r["size"], r["pnl_usd"],
                    0.0, r["strategy"], "signal", "closed", r["strategy"],
                    r.get("entry_fee", 0.0), r.get("funding_paid", 0.0),
                    r.get("cumulative_order_fee", 0.0), r.get("signal_metadata"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _make_config():
    return Config({
        "mode": "paper",
        "risk": {"initial_capital": 10_000.0},
        "strategy": {
            "phase08": {"execution_strategies": ["VolatilityBreakout", "VWAPDeviation"]},
        },
    })


@pytest.fixture()
def tmp_path(tmp_path):  # noqa: ARG001 - shadow pytest's tmp_path
    """Redirect to a scratch dir INSIDE the project.

    The preregister manifest writer refuses to write outside the project
    root (AUDIT-004 safe-path guard), so the stock OS-temp-dir ``tmp_path``
    can't be used for manifest paths here. The sqlite tmp DB itself has no
    such restriction, but both are created under the same scratch dir for
    simplicity. Removed after the test.
    """
    project_root = Path(__file__).resolve().parents[1]
    scratch = project_root / "data" / "research" / f"_test_phase10_gate_{uuid.uuid4().hex}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.mark.integration_offline
def test_load_qualifying_trades_filters_by_window_and_strategy(tmp_path):
    db_path = tmp_path / "tmp_bot.db"
    rows = [
        {"entry_price": 100.0, "entry_time": 1000, "exit_time": 2000, "size": 1.0,
         "pnl_usd": 5.0, "strategy": "VolatilityBreakout"},
        {"entry_price": 100.0, "entry_time": 500, "exit_time": 1500, "size": 1.0,
         "pnl_usd": 5.0, "strategy": "VolatilityBreakout"},  # before window
        {"entry_price": 100.0, "entry_time": 1200, "exit_time": 2200, "size": 1.0,
         "pnl_usd": 5.0, "strategy": "ChecklistMeta"},  # not in execution set
    ]
    _seed_tmp_db(db_path, rows)

    qualifying = load_qualifying_trades(
        db_path, window_start_ms=900, execution_strategies=["VolatilityBreakout", "VWAPDeviation"]
    )
    assert len(qualifying) == 1
    assert qualifying[0]["strategy"] == "VolatilityBreakout"


@pytest.mark.integration_offline
def test_build_gate_report_insufficient_data_below_min_trades(tmp_path):
    db_path = tmp_path / "tmp_bot.db"
    manifest_path = tmp_path / "phase10_preregister.json"
    config = _make_config()
    persist_preregister_manifest(config, path=manifest_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    window_start_ms = manifest["window_start_ms"]

    rows = [
        {"entry_price": 100.0, "entry_time": window_start_ms + 10, "exit_time": window_start_ms + 20,
         "size": 1.0, "pnl_usd": 5.0, "strategy": "VolatilityBreakout",
         "signal_metadata": json.dumps({"stop_loss_pct": 0.02})}
        for _ in range(18)
    ]
    _seed_tmp_db(db_path, rows)

    report = build_gate_report(db_path=db_path, manifest_path=manifest_path, config=config)

    assert report["trade_count"] == 18
    assert report["criteria"]["min_trades"]["status"] == STATUS_FAIL
    assert report["criteria"]["profit_factor"]["status"] == STATUS_INSUFFICIENT_DATA
    assert report["criteria"]["expectancy_r"]["status"] == STATUS_INSUFFICIENT_DATA
    assert report["criteria"]["max_drawdown_pct"]["status"] == STATUS_INSUFFICIENT_DATA
    assert report["gate_met"] is False


@pytest.mark.integration_offline
def test_build_gate_report_all_pass_with_sufficient_good_trades(tmp_path):
    db_path = tmp_path / "tmp_bot.db"
    manifest_path = tmp_path / "phase10_preregister.json"
    config = _make_config()
    persist_preregister_manifest(config, path=manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    window_start_ms = manifest["window_start_ms"]

    # 100 trades, mostly small wins with a couple of small losses -> PF high,
    # expectancy_r > 0, drawdown well under 5%.
    rows = []
    for i in range(100):
        pnl = 5.0 if i % 5 != 0 else -1.0  # 80 wins of 5, 20 losses of 1
        rows.append({
            "entry_price": 100.0,
            "entry_time": window_start_ms + i * 1000,
            "exit_time": window_start_ms + i * 1000 + 500,
            "size": 1.0,
            "pnl_usd": pnl,
            "strategy": "VolatilityBreakout",
            "signal_metadata": json.dumps({"stop_loss_pct": 0.02}),
        })
    _seed_tmp_db(db_path, rows)

    report = build_gate_report(db_path=db_path, manifest_path=manifest_path, config=config)

    assert report["trade_count"] == 100
    assert report["criteria"]["min_trades"]["status"] == STATUS_PASS
    assert report["criteria"]["profit_factor"]["status"] == STATUS_PASS
    assert report["criteria"]["expectancy_r"]["status"] == STATUS_PASS
    assert report["criteria"]["max_drawdown_pct"]["status"] == STATUS_PASS
    assert report["gate_met"] is True


@pytest.mark.integration_offline
def test_build_gate_report_raises_when_manifest_missing(tmp_path):
    db_path = tmp_path / "tmp_bot.db"
    _seed_tmp_db(db_path, [])
    missing_manifest_path = tmp_path / "nope.json"
    with pytest.raises(Phase10GateMetricsError):
        build_gate_report(db_path=db_path, manifest_path=missing_manifest_path)
