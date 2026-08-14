"""Unit tests for scripts/vb_stopout_ab.py — the VB liquidation stop-out A/B.

The heavy part (two ~15 min backtests) is not exercised here; these tests pin
the pure pieces: the floor override reaches the BacktestConfig (None → the
calibrated constant, inf → disabled baseline), the short slice stats, and the
comparison render (exit-reason mix, intercepted shorts, matched-trade delta).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scripts import vb_stopout_ab as ab  # noqa: E402
from scripts.vb_regime_forensics import stats  # noqa: E402

pytestmark = pytest.mark.unit


def _trade(side: str, pnl: float, reason: str, entry: int = 1,
           symbol: str = "BTC") -> dict:
    return {
        "entry_time": entry, "exit_time": entry + 3_600_000,
        "symbol": symbol, "side": side,
        "entry_price": 50_000.0, "exit_price": 50_000.0,
        "pnl_usd": pnl, "pnl_pct": pnl / 500_000.0,
        "r_multiple": pnl / 100.0, "exit_reason": reason,
        "_regime": "trend", "_adx": 30.0, "_hold_min": 60.0,
    }


def test_floor_override_in_build_cfg() -> None:
    """None → the calibrated constant (live parity); inf → disabled."""
    cfg = {"risk": {"initial_capital": 10_000.0}, "strategy": {}}
    c_default = ab.build_cfg_with_stopout(cfg, None)
    assert c_default.liquidation_stopout_min_notional_usd is None
    c_off = ab.build_cfg_with_stopout(cfg, float("inf"))
    assert c_off.liquidation_stopout_min_notional_usd == float("inf")
    c_sweep = ab.build_cfg_with_stopout(cfg, 1_000_000.0)
    assert c_sweep.liquidation_stopout_min_notional_usd == 1_000_000.0


def test_short_slice_isolates_shorts() -> None:
    trades = [_trade("short", -50.0, "stop_loss", entry=1),
              _trade("short", -30.0, "liquidation_stop_out", entry=2),
              _trade("long", +80.0, "take_profit", entry=3)]
    s = ab.short_slice(trades)
    assert s["n"] == 2
    assert s["net"] == -80.0
    assert s["win_rate"] == 0.0
    # Longs stay out.
    assert stats([t for t in trades if t["side"] == "long"])["n"] == 1


def test_compare_reports_intercepted_shorts_and_delta() -> None:
    """Same short entry in both runs: baseline bleeds to stop_loss, the
    stop-out run intercepts it earlier at a smaller loss."""
    base = _trade("short", -100.0, "stop_loss", entry=1)
    so = _trade("short", -40.0, "liquidation_stop_out", entry=1)
    base_run = {"trades": [base, _trade("long", +10.0, "take_profit", entry=2)]}
    so_run = {"trades": [so, _trade("long", +10.0, "take_profit", entry=2)]}

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ab.compare(base_run, so_run)
    out = buf.getvalue()
    assert rc == 0
    # Exit-reason mix on shorts: stop_loss -> liquidation_stop_out.
    assert "stop_loss" in out
    assert "liquidation_stop_out" in out
    # Intercepted shorts counted and the P&L delta shown.
    assert "Shorts baseline que o stop-out interceptou: 1" in out
    assert "-100.00" in out
    assert "-40.00" in out
    assert "+60.00" in out  # delta


def test_compare_no_interception_when_disabled_floor_never_fires() -> None:
    """A run where the stop-out never fires produces no interception lines."""
    trades = [_trade("short", -100.0, "stop_loss", entry=1),
              _trade("long", +10.0, "take_profit", entry=2)]
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        ab.compare({"trades": trades}, {"trades": trades})
    out = buf.getvalue()
    assert "interceptou: 0" in out
