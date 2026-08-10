"""Unit tests for shadow net-of-cost metrics (tier-0 fees)."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.data.database import Candle
from src.research.shadow_outcome_evaluator import (
    ShadowCostModel,
    _funding_during_hold,
    aggregate_scoreboard,
    resolve_shadow_cost_model,
    simulate_decision,
)
from src.research.shadow_recorder import ShadowDecision
from src.utils.config import Config

pytestmark = [pytest.mark.unit]


def _candle(ts: int, c: float = 100.0, funding: float | None = None) -> Candle:
    return Candle(
        symbol="BTC",
        timestamp_ms=ts,
        open=c,
        high=c + 1,
        low=c - 1,
        close=c,
        volume=1.0,
        funding_rate=funding,
    )


def _decision(*, price: float = 100.0, stop: float = 0.01, take: float = 0.05) -> ShadowDecision:
    snap = {
        "price": price,
        "stop_loss_pct": stop,
        "take_profit_pct": take,
        "size_pct": 0.01,
    }
    return ShadowDecision(
        row_id=1,
        symbol="BTC",
        strategy="VWAPDeviation",
        variant="phase08_shadow",
        side="long",
        would_enter=True,
        reason="test",
        timestamp_ms=1_000_000,
        market_snapshot=snap,
    )


def test_resolve_shadow_cost_model_tier0_maker_entry() -> None:
    cfg = Config(
        {
            "risk": {"taker_fee_pct": 0.045, "paper_slippage_pct": 0.02},
            "execution": {
                "maker_orders": {
                    "enabled": True,
                    "maker_fee_pct": 0.015,
                    "exit_as_maker": False,
                    "strategies": ["VWAPDeviation"],
                }
            },
        }
    )
    m = resolve_shadow_cost_model("VWAPDeviation", cfg)
    assert abs(m.entry_fee_frac - 0.00015) < 1e-12
    assert abs(m.exit_fee_frac - 0.00045) < 1e-12
    assert abs(m.round_trip_slip_frac - 0.0004) < 1e-12


def test_net_pnl_subtracts_fees_and_slip() -> None:
    # TP at +5% on long; costs should reduce net below gross
    model = ShadowCostModel(
        entry_fee_frac=0.00015,
        exit_fee_frac=0.00045,
        entry_slip_frac=0.0002,
        exit_slip_frac=0.0002,
        label="test",
    )
    d = _decision()
    # candle that hits TP (high reaches 105)
    candles = [
        Candle(
            symbol="BTC",
            timestamp_ms=1_000_000 + 60_000,
            open=100.0,
            high=105.5,
            low=99.5,
            close=105.0,
            volume=1.0,
        )
    ]
    out = simulate_decision(d, candles, max_hold_ms=300_000, cost_model=model)
    assert out.evaluated
    assert out.pnl_pct > 0
    assert out.net_pnl_pct < out.pnl_pct
    expected_cost = model.round_trip_fee_frac + model.round_trip_slip_frac
    assert abs((out.pnl_pct - out.net_pnl_pct) - expected_cost) < 1e-9


def test_funding_coverage_short_hold_is_one() -> None:
    pnl, cov = _funding_during_hold("long", [], 0, 10 * 60_000)
    assert pnl == 0.0
    assert cov == 1.0


def test_aggregate_exposes_net_fields() -> None:
    model = ShadowCostModel(0.00015, 0.00045, 0.0002, 0.0002, "t")
    d = _decision()
    candles = [
        Candle(
            symbol="BTC",
            timestamp_ms=1_060_000,
            open=100.0,
            high=105.5,
            low=99.5,
            close=105.0,
            volume=1.0,
        )
    ]
    out = simulate_decision(d, candles, max_hold_ms=300_000, cost_model=model)
    board = aggregate_scoreboard(
        "VWAPDeviation",
        [out],
        max_hold_ms=300_000,
        candle_source="test",
        n_decisions=1,
    )
    assert board.net_profit_factor >= 1.0
    assert board.mean_fee_cost_pct > 0
    assert "net_profit_factor" in board.to_dict()
