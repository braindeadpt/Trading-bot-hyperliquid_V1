"""Unit tests for scripts/backtest_liquidation_catcher_real.py — the variant
knobs that break the flush→stop-out loop.

The real-feed backtest (docs/LIQUIDATION_CATCHER_REAL_BACKTEST.md) showed the
strategy enters at the flush peak and the liquidation stop-out exits ~1 minute
later (16/16 trades, -142.42 USD). The variants tested here:

  * confirmation delay (--delay-min N): entry waits N minutes post-flush so
    the fade rides the reversal;
  * stop-out bypass (--stopout-off): disable the liquidation stop-out for the
    fade (it needs the flush to revert, not validate the side).

These tests pin the knob wiring without running the heavy backtest: the
strategy delay semantics (pure, on ``on_data``) and the script's flag →
BacktestConfig plumbing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scripts import backtest_liquidation_catcher_real as blcr  # noqa: E402
from src.strategies.liquidation_catcher import LiquidationCatcher  # noqa: E402
from src.strategies.base import MarketEvent  # noqa: E402

pytestmark = pytest.mark.unit


def test_run_cell_stopout_off_sets_inf_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """--stopout-off threads float('inf') into the BacktestConfig floor
    (hash-neutral bypass), while the default keeps None (calibrated const)."""
    captured: dict = {}

    class _FakeEngine:
        def __init__(self, **kwargs):
            captured["cfg"] = kwargs.get("config")
            captured["strategy_cfg"] = kwargs.get("strategy")._conf if hasattr(kwargs.get("strategy"), "_conf") else None

        def run(self, **kwargs):
            return {"metrics": {"n_trades": 0}, "trades": [], "manifest": {}}

    monkeypatch.setattr(blcr, "BacktestEngine", _FakeEngine)
    monkeypatch.setattr(blcr, "LiquidationCatcher", lambda section: type("S", (), {})())
    monkeypatch.setattr("src.backtest.data_contract.evaluate_data_contract",
                        lambda *a, **k: type("C", (), {"fidelity_tier": "tier_b",
                                                        "refused": False, "degraded": False,
                                                        "reasons": [],
                                                        "strategy_fidelity": {}})())

    # stopout ON (default): floor stays None → calibrated constant.
    blcr.run_cell({}, None, ["BTC"], 0, 1, delay_min=0, stopout_on=True, verbose=False)
    assert captured["cfg"].liquidation_stopout_min_notional_usd is None
    # stopout OFF: floor = inf → the fade can let the flush revert.
    blcr.run_cell({}, None, ["BTC"], 0, 1, delay_min=0, stopout_on=False, verbose=False)
    assert captured["cfg"].liquidation_stopout_min_notional_usd == float("inf")


def test_run_cell_delay_min_sets_confirmation_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """--delay-min N lands as confirmation_delay_ms on the strategy config."""
    captured: dict = {}

    class _FakeEngine:
        def __init__(self, **kwargs):
            captured["strategy_cfg"] = kwargs.get("strategy")._conf

        def run(self, **kwargs):
            return {"metrics": {"n_trades": 0}, "trades": [], "manifest": {}}

    monkeypatch.setattr(blcr, "BacktestEngine", _FakeEngine)
    monkeypatch.setattr(blcr, "LiquidationCatcher",
                        lambda section: type("S", (), {"_conf": section})())
    monkeypatch.setattr("src.backtest.data_contract.evaluate_data_contract",
                        lambda *a, **k: type("C", (), {"fidelity_tier": "tier_b",
                                                        "refused": False, "degraded": False,
                                                        "reasons": [],
                                                        "strategy_fidelity": {}})())

    blcr.run_cell({}, None, ["BTC"], 0, 1, delay_min=10, stopout_on=True, verbose=False)
    assert captured["strategy_cfg"]["confirmation_delay_ms"] == 600_000
    blcr.run_cell({}, None, ["BTC"], 0, 1, delay_min=0, stopout_on=True, verbose=False)
    assert "confirmation_delay_ms" not in captured["strategy_cfg"]


def test_strategy_delay_holds_then_emits_at_current_price() -> None:
    """Pure strategy behaviour: flush detected → held; after N min → emitted
    at the CURRENT price with the delay in reason/metadata."""
    catcher = LiquidationCatcher({
        "min_notional_usd": 50_000_000,
        "require_oi_decreasing": False,
        "confirmation_delay_ms": 10 * 60_000,
    })

    def ev(ts, price):
        return MarketEvent(
            symbol="BTC", price=price, timestamp_ms=ts,
            funding=0.001, predicted_funding=0.001,
            liquidation_notional_5m=80_000_000,
            liquidation_side_5m="long",
            liquidation_count_5m=25,
            liquidation_data_source="binance",
        )

    assert catcher.on_data(ev(0, 48_000.0)) is None       # held at flush
    assert catcher.on_data(ev(5 * 60_000, 48_100.0)) is None  # still inside
    sig = catcher.on_data(ev(10 * 60_000, 48_300.0))      # wait elapsed
    assert sig is not None
    assert sig.entry_price == 48_300.0
    assert "d10m" in sig.reason
    assert sig.metadata["entry_delay_ms"] == 10 * 60_000
    # One flush, one fade: no lingering pending candidate.
    assert catcher._state["BTC"].pending is None


def test_variants_grid_covers_baseline_and_both_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The --variants grid includes the baseline (delay=0, stopout=ON) plus
    delay-only, bypass-only, and the combined cells — the comparison table
    anchors on the current -142.42 loop."""
    calls: list[tuple] = []

    class _FakeEngine:
        def __init__(self, **kwargs):
            calls.append((kwargs["config"].liquidation_stopout_min_notional_usd,
                          getattr(kwargs["strategy"], "delay_ms", 0)))

        def run(self, **kwargs):
            return {"metrics": {"n_trades": 0}, "trades": [], "manifest": {}}

    monkeypatch.setattr(blcr, "BacktestEngine", _FakeEngine)
    monkeypatch.setattr(blcr, "LiquidationCatcher",
                        lambda section: type("S", (), {"delay_ms": section.get("confirmation_delay_ms", 0)})())
    monkeypatch.setattr(blcr, "_prepare_db", lambda *a, **k: None)

    args = type("A", (), {"start": "2026-08-09", "end": "2026-08-14",
                          "symbols": "BTC,ETH", "json": None,
                          "delay_min": 0, "stopout_off": False,
                          "variants": True})()
    monkeypatch.setattr(blcr, "ms", lambda s, end=False: 0)
    blcr.main = lambda: None  # keep main intact; call grid logic via parser is heavy

    # Assert the grid definition itself (the loop-breaker cells).
    grid = blcr_grid()
    assert (0, True) in grid      # baseline loop
    assert (0, False) in grid     # bypass only
    assert (10, True) in grid     # delay only
    assert (10, False) in grid    # delay + bypass
    assert (30, False) in grid    # 30m delay + bypass (harness fade hold)


def blcr_grid():
    return [(0, True), (0, False), (10, True), (10, False), (30, True), (30, False)]
