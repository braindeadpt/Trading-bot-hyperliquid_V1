"""Unit tests for scripts/iv_gate_shadow_recheck.py.

Pins the shadow→enforcement decision contract: the recheck stays in shadow
mode until >= TARGET_CLOSED closed trades carry an IV decision, then decides
PROMOTE (high_iv profitable, low_iv not — enforce at threshold 66.7),
REJECT (live sample contradicts the backtest direction — keep shadow) or
INCONCLUSIVE (below the n gate). The trigger metric reuses the exact join
from scripts/iv_gate_shadow_vs_pnl.py so the watchdog and the report can
never disagree about what counts as a matched IV decision.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import iv_gate_shadow_recheck as rc  # noqa: E402


def _slice(n_closed, net, *, wr=0.5):
    return {
        "n": n_closed, "n_closed": n_closed, "n_open": 0,
        "net_pnl_usd": net, "win_rate": wr,
        "avg_pnl_usd": net / n_closed if n_closed else None,
        "median_pnl_usd": net / n_closed if n_closed else None,
        "best_usd": net, "worst_usd": -net,
    }


def _report(n_high, hi_net, n_low, lo_net):
    return {
        "slices": {
            "high_iv": _slice(n_high, hi_net),
            "low_iv": _slice(n_low, lo_net),
            "unknown": _slice(0, 0.0),
        },
    }


def test_verdict_inconclusive_below_gate():
    v = rc.verdict(_report(10, 50.0, 10, -30.0))
    assert v["status"] == "INCONCLUSIVE"
    assert v["n_closed"] == 20


def test_verdict_promote_when_high_iv_wins():
    v = rc.verdict(_report(20, 50.0, 10, -30.0))
    assert v["status"] == "PROMOTE"
    assert "66.7" in v["detail"]  # threshold pinned in the decision


def test_verdict_reject_when_high_iv_not_positive():
    v = rc.verdict(_report(20, -10.0, 20, 10.0))
    assert v["status"] == "REJECT"
    assert "manter shadow" in v["detail"]


def test_verdict_reject_when_low_iv_also_profitable():
    """high_iv > low_iv is not enough — low_iv positive means blocking loses."""
    v = rc.verdict(_report(20, 50.0, 20, 40.0))
    assert v["status"] == "REJECT"


def test_verdict_exactly_at_gate_promotes():
    v = rc.verdict(_report(20, 50.0, 10, -30.0))
    assert v["n_closed"] == 30
    assert v["status"] == "PROMOTE"


def test_target_closed_matches_min_n_gate():
    from scripts.iv_gate_shadow_vs_pnl import MIN_N_GATE

    assert rc.TARGET_CLOSED == MIN_N_GATE == 30


def test_iv_decision_count_reuses_live_join(monkeypatch):
    """iv_decision_count reads the same build_report() the report script uses —
    the trigger and the report can never disagree."""
    captured = {}

    def fake_build_report(**kwargs):
        captured.update(kwargs)
        return _report(20, 50.0, 10, -30.0)

    monkeypatch.setattr(rc, "build_report", fake_build_report)
    n_closed, n_high, n_low = rc.iv_decision_count()
    assert (n_closed, n_high, n_low) == (30, 20, 10)


def test_iv_decision_count_zero_on_error(monkeypatch):
    monkeypatch.setattr(rc, "build_report", lambda **k: {"error": "no_trades"})
    assert rc.iv_decision_count() == (0, 0, 0)


def test_state_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    rc.STATE_PATH = p
    try:
        state = rc.load_state()
        assert state == {"triggered": False, "runs": []}
        state["triggered"] = True
        state["runs"].append({"ts": "t", "verdict": "PROMOTE"})
        rc.save_state(state)
        assert rc.load_state() == state
    finally:
        rc.STATE_PATH = rc.STATE_DIR / "iv_gate_shadow_recheck_state.json"
