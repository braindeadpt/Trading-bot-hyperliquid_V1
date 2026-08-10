"""Regression: validate_phase08 summarizer must match trade PnL sums."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "validate_phase08_ruleset_12w",
    ROOT / "scripts" / "validate_phase08_ruleset_12w.py",
)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)
_summarize = _mod._summarize

pytestmark = pytest.mark.unit


def test_summarize_total_pnl_and_expectancy_match_trades() -> None:
    trades = [
        {"strategy": "ChecklistMeta", "pnl_usd": 100.0, "fees": 1.0},
        {"strategy": "ChecklistMeta", "pnl_usd": -40.0, "fees": 1.0},
        {"strategy": "VWAPDeviation", "pnl_usd": 20.0, "fees": 0.5},
    ]
    # metrics deliberately omit total_pnl / expectancy (the old bug path)
    result = {
        "metrics": {
            "n_trades": 3,
            "win_rate": 0.6667,
            "profit_factor": 3.0,
            "sharpe_ratio": 1.2,
            "max_drawdown": 0.05,
            "avg_trade": 26.6667,
            "avg_win": 60.0,
            "avg_loss": -40.0,
        },
        "trades": trades,
    }
    out = _summarize(result)
    assert out["total_pnl"] == pytest.approx(80.0)
    assert out["net_pnl"] == pytest.approx(80.0)
    assert out["expectancy"] == pytest.approx(26.6667)
    assert out["n_trades"] == 3
    assert out["by_strategy"]["ChecklistMeta"] == 2
    assert abs(out["total_pnl"] - sum(t["pnl_usd"] for t in trades)) < 1e-9


def test_summarize_zero_trades_not_nan() -> None:
    out = _summarize({"metrics": {}, "trades": []})
    assert out["total_pnl"] == 0.0
    assert out["expectancy"] == 0.0
    assert out["n_trades"] == 0
