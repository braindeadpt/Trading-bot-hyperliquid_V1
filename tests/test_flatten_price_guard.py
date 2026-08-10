"""Regression: flatten uses per-symbol prices; absurd exits are rejected."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine

pytestmark = pytest.mark.unit


@dataclass
class _Pos:
    id: int = 1
    strategy: str = "ChecklistMeta"
    symbol: str = "ETH"
    side: str = "long"
    entry_price: float = 1700.0
    size: float = 1.0
    entry_time_ms: int = 1
    stop_loss_price: float = 1600.0
    take_profit_price: float = 1800.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    excursion_id: int = 1
    funding_paid: float = 0.0
    entry_commission: float = 0.0
    next_funding_ts: int = 0


def _bare_engine() -> BacktestEngine:
    eng = object.__new__(BacktestEngine)
    eng.cfg = BacktestConfig(paper_slippage_pct=0.02, commission_pct=0.035)
    eng._price_index = {
        "ETH": [(1_000, 1690.0), (2_000, 1710.0)],
        "HYPE": [(1_000, 60.0), (2_000, 65.45)],
        "SOL": [(1_000, 100.0), (2_000, 100.0)],
    }
    eng.positions = {}
    eng.positions_by_symbol = {}
    eng.closed_trades = []
    eng._excursion_trackers = {}
    eng._daily_pnl = 0.0
    eng._capital = 100_000.0
    eng._risk_manager = None
    eng._pipeline = None
    return eng


def test_sanitize_rejects_hype_price_on_eth() -> None:
    eng = _bare_engine()
    pos = _Pos()
    safe = eng._sanitize_exit_price(pos, 65.45, "force_close_eod")
    assert abs(safe - 1710.0) < 1e-9  # ETH last close, not HYPE


def test_sanitize_accepts_normal_move() -> None:
    eng = _bare_engine()
    pos = _Pos()
    assert eng._sanitize_exit_price(pos, 1650.0, "stop_loss") == 1650.0


def test_force_close_eod_uses_per_symbol_last_close(caplog: pytest.LogCaptureFixture) -> None:
    """Old bug: one timeline-last close (HYPE) applied to every open pos."""
    eng = _bare_engine()
    eth = _Pos(id=1, symbol="ETH", entry_price=1700.0, size=1.0, excursion_id=1)
    hype = _Pos(id=2, symbol="HYPE", entry_price=70.0, size=10.0, excursion_id=2)
    eng.positions = {1: eth, 2: hype}
    eng.positions_by_symbol = {"ETH": 1, "HYPE": 2}
    from src.backtest.mfe_mae import ExcursionTracker

    eng._excursion_trackers = {
        1: ExcursionTracker(entry_price=1700.0, entry_time_ms=1, side="long", risk_usd=100.0),
        2: ExcursionTracker(entry_price=70.0, entry_time_ms=1, side="long", risk_usd=50.0),
    }

    # Simulate corrected EOD loop
    capital = 100_000.0
    with caplog.at_level(logging.ERROR):
        for pos_id in list(eng.positions.keys()):
            pos = eng.positions[pos_id]
            px_ts = eng._last_close_for_symbol(pos.symbol)
            assert px_ts is not None
            capital = eng._close_position(pos_id, px_ts[1], px_ts[0], "force_close_eod", capital)

    by_sym = {t["symbol"]: t for t in eng.closed_trades}
    assert abs(by_sym["ETH"]["exit_price"] - 1710.0) < 5.0  # allow slip
    assert by_sym["ETH"]["exit_price"] > 1000.0
    assert by_sym["HYPE"]["exit_price"] < 100.0
    # Must not record the cross-symbol catastrophe
    assert abs(by_sym["ETH"]["pnl_usd"]) < 500.0


def test_close_rejects_wrong_symbol_price_even_if_passed(caplog: pytest.LogCaptureFixture) -> None:
    eng = _bare_engine()
    eth = _Pos(id=1, symbol="ETH", entry_price=1700.0, size=11.8, excursion_id=1)
    eng.positions = {1: eth}
    eng.positions_by_symbol = {"ETH": 1}
    from src.backtest.mfe_mae import ExcursionTracker

    eng._excursion_trackers = {
        1: ExcursionTracker(entry_price=1700.0, entry_time_ms=1, side="long", risk_usd=100.0),
    }
    with caplog.at_level(logging.ERROR):
        # Deliberately pass HYPE price — safeguard must catch it
        eng._close_position(1, 65.45, 2_000, "force_close_eod", 100_000.0)
    assert eng.closed_trades
    t = eng.closed_trades[0]
    assert t["exit_price"] > 1000.0
    assert abs(t["pnl_usd"]) < 5_000.0  # not −$19k
    assert any("EXIT PRICE REJECTED" in r.message for r in caplog.records)


def test_be_exit_uses_paper_slip_not_size_aware() -> None:
    """BE fill at entry±buffer + paper slip → near-flat PnL (live parity)."""
    eng = _bare_engine()
    eng.cfg = BacktestConfig(
        paper_slippage_pct=0.02,
        commission_pct=0.035,
        sl_to_be_buffer_pct=0.001,
    )
    # ~$4200 notional like live CM
    entry = 100.0
    size = 42.0
    pos = _Pos(
        id=1,
        symbol="SOL",
        entry_price=entry,
        size=size,
        metadata={"exit_slippage_pct": 0.0002, "entry_fee_pct": 0.00035, "exit_fee_pct": 0.00035},
        excursion_id=1,
    )
    eng.positions = {1: pos}
    eng.positions_by_symbol = {"SOL": 1}
    from src.backtest.mfe_mae import ExcursionTracker

    eng._excursion_trackers = {
        1: ExcursionTracker(entry_price=entry, entry_time_ms=1, side="long", risk_usd=50.0),
    }
    be = entry * 1.001
    eng._close_position(1, be, 2_000, "sl_to_be_hit_r0.60", 100_000.0)
    t = eng.closed_trades[0]
    # Live BE avg ≈ −$0.14 on ~$4k; allow a few dollars
    assert abs(t["pnl_usd"]) < 5.0, t
