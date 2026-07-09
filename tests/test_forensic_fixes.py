"""v3.1.47 forensic fixes — unit tests for BE, gates, chase, sizing, stop streak."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.engine import TradingEngine
from src.core.risk_manager import RiskManager
from src.data.database import Database
from src.exchanges.hyperliquid_ws import DataBus
from src.core.execution import ExecutionEngine
from src.core.portfolio import PortfolioState
from src.strategies.base import MarketEvent, Position, Signal
from src.strategies.checklist_meta import ChecklistMeta
from src.strategies.indicators import Candle
from src.utils.config import Config


def _make_candles(
    start_price: float,
    end_price: float,
    n: int = 20,
    base_ts: int = 1_700_000_000_000,
) -> list[Candle]:
    """Linear price path for ATR / run-up helpers."""
    step = (end_price - start_price) / max(n - 1, 1)
    out: list[Candle] = []
    for i in range(n):
        px = start_price + step * i
        out.append(
            Candle(
                open=px,
                high=px * 1.001,
                low=px * 0.999,
                close=px,
                volume=1000.0,
                timestamp_ms=base_ts + i * 900_000,
            )
        )
    return out


def _make_engine(config_overrides: dict | None = None) -> TradingEngine:
    cfg_dict: dict = {
        "symbols": ["BTC", "ETH", "SOL"],
        "cooldown": {
            "base_minutes": 60,
            "max_minutes": 240,
            "multiplier": 2.0,
        },
        "risk": {
            "max_position_size_pct": 20.0,
            "leverage_max": 10.0,
            "max_slippage_pct": 0.2,
            "min_fill_ratio": 0.8,
            "symbol_risk_multiplier": {"SOL": 0.5},
            "chase_filter": {
                "enabled": True,
                "lookback_hours": 3.0,
                "max_runup_pct": 0.008,
                "exempt_strategies": ["VolatilityBreakout", "DonchianBreakout"],
            },
        },
        "strategy": {
            "adx_trend_threshold": 25.0,
            "adx_range_threshold": 20.0,
        },
    }
    if config_overrides:
        cfg_dict.update(config_overrides)
    config = Config(cfg_dict)
    db = Database(":memory:")
    portfolio = PortfolioState(config)
    risk = RiskManager(config, db)
    bus = DataBus()
    executor = ExecutionEngine(config, db, "paper")
    return TradingEngine(config, db, bus, [], risk, executor)


# (i) SL-to-BE arms at 0.6R
def test_sl_to_be_triggers_at_06r() -> None:
    strat = ChecklistMeta({
        "use_sl_to_be_after_1r": True,
        "sl_to_be_trigger_r": 0.6,
        "signal_throttle_ms": 0,
    })
    entry = 100.0
    sl = 98.0  # 2% stop → 1R = $2
    pos = Position(
        symbol="SOL",
        side="long",
        entry_price=entry,
        size=1.0,
        entry_time_ms=1_700_000_000_000,
        stop_loss_price=sl,
    )
    candles = _make_candles(100.0, 100.0, n=10)  # <15 bars → fixed 0.6R trigger
    state = strat._get_state("SOL")
    state.candles_15m = candles

    # 0.5R — not yet
    ev_half = MarketEvent(symbol="SOL", price=101.0, timestamp_ms=1_700_000_100_000)
    assert strat.on_position(pos, ev_half) is None
    assert state.sl_moved_to_be is False

    # 0.6R — arms BE (profit = 1.2 = 0.6 × 2)
    ev_be = MarketEvent(symbol="SOL", price=101.2, timestamp_ms=1_700_000_200_000)
    assert strat.on_position(pos, ev_be) is None
    assert state.sl_moved_to_be is True


# (ii) Counter-trend gate
def test_counter_trend_blocks_long() -> None:
    strat = ChecklistMeta({"counter_trend_adx_block": 30.0})
    reason = strat._counter_trend_block_reason(
        "long", adx=35.0, ema_fast=99.0, ema_slow=101.0,
    )
    assert reason is not None and reason.startswith("counter_trend_gate")


def test_counter_trend_allows_with_trend() -> None:
    strat = ChecklistMeta({"counter_trend_adx_block": 30.0})
    assert strat._counter_trend_block_reason(
        "long", adx=35.0, ema_fast=102.0, ema_slow=100.0,
    ) is None


# (iii) OIR gate
def test_oir_blocks_short_with_positive_oir() -> None:
    strat = ChecklistMeta({"require_oir_alignment": True, "oir_min_alignment": 0.10})
    reason = strat._oir_block_reason("short", oir=0.30)
    assert reason is not None and reason.startswith("oir_gate")


def test_oir_none_passes() -> None:
    strat = ChecklistMeta({"require_oir_alignment": True, "oir_min_alignment": 0.10})
    assert strat._oir_block_reason("long", oir=None) is None


# (iv) Chase filter
def test_chase_rejects_extended_runup() -> None:
    engine = _make_engine()
    # +1.2% over 3h (12 × 15m bars)
    candles = _make_candles(100.0, 101.2, n=13)
    engine._candles_15m_history["ETH"] = candles
    sig = Signal(
        strategy="ChecklistMeta",
        symbol="ETH",
        side="long",
        confidence=0.7,
        size_pct=0.01,
    )
    reason = engine._check_chase_filter(sig)
    assert reason is not None and "chase" in reason.lower()


def test_chase_exempt_volatility_breakout() -> None:
    engine = _make_engine()
    candles = _make_candles(100.0, 101.5, n=13)
    engine._candles_15m_history["ETH"] = candles
    sig = Signal(
        strategy="VolatilityBreakout",
        symbol="ETH",
        side="long",
        confidence=0.7,
        size_pct=0.01,
    )
    assert engine._check_chase_filter(sig) is None


# (v) Symbol risk multiplier halves size_pct
def test_symbol_risk_multiplier_halves_sol_size() -> None:
    engine = _make_engine()
    sig = Signal(
        strategy="ChecklistMeta",
        symbol="SOL",
        side="long",
        confidence=0.7,
        size_pct=0.02,
    )
    sym_mult = engine._symbol_risk_multipliers.get("SOL", 1.0)
    adjusted = sig.size_pct * sym_mult
    assert sym_mult == 0.5
    assert adjusted == 0.01


# (vi) Daily stop-loss streak circuit
def test_daily_stop_streak_blocks_fifth_entry() -> None:
    config = Config({
        "risk": {
            "max_daily_stop_losses": 4,
            "max_position_size_pct": 20.0,
            "leverage_max": 10.0,
        },
    })
    risk = RiskManager(config, Database(":memory:"))
    today = "2026-07-09"

    with patch("src.core.risk_manager.utc_now") as mock_utc:
        mock_utc.return_value = SimpleNamespace(strftime=lambda fmt: today)

        for i in range(4):
            risk.on_trade_closed(SimpleNamespace(
                pnl_usd=-10.0,
                reason=f"stop_loss_hit_{i}",
            ))

        portfolio = SimpleNamespace(
            daily_trades=0,
            positions={},
            daily_pnl=0.0,
            current_capital=20_000.0,
            get_max_drawdown=lambda: 0.0,
        )
        sig = Signal(
            strategy="ChecklistMeta",
            symbol="ETH",
            side="long",
            confidence=0.7,
            size_pct=0.001,
            stop_loss_pct=0.02,
        )
        approved, reason = risk.can_enter(sig, portfolio)
        assert not approved
        assert "daily_stop_streak_circuit" in reason


def test_daily_stop_streak_resets_next_day() -> None:
    config = Config({
        "risk": {
            "max_daily_stop_losses": 4,
            "max_position_size_pct": 20.0,
            "leverage_max": 10.0,
        },
    })
    risk = RiskManager(config, Database(":memory:"))

    with patch("src.core.risk_manager.utc_now") as mock_utc:
        mock_utc.return_value = SimpleNamespace(strftime=lambda fmt: "2026-07-09")
        for _ in range(4):
            risk.on_trade_closed(SimpleNamespace(pnl_usd=-5.0, reason="stop_loss"))

        mock_utc.return_value = SimpleNamespace(strftime=lambda fmt: "2026-07-10")
        portfolio = SimpleNamespace(
            daily_trades=0,
            positions={},
            daily_pnl=0.0,
            current_capital=20_000.0,
            get_max_drawdown=lambda: 0.0,
        )
        sig = Signal(
            strategy="ChecklistMeta",
            symbol="ETH",
            side="long",
            confidence=0.7,
            size_pct=0.001,
            stop_loss_pct=0.02,
        )
        approved, reason = risk.can_enter(sig, portfolio)
        assert approved, f"expected approved, got: {reason}"


# (vii) Cooldown regression (v3.1.46)
def test_cooldown_still_blocks_during_active_window() -> None:
    engine = _make_engine()
    now = int(time.time() * 1000)
    engine._cooldown_state["ChecklistMeta:SOL"] = {
        "last_trade_ms": now - 30 * 60_000,
        "duration_ms": 60 * 60_000,
        "consecutive_losses": 1,
        "adx": 25.0,
        "funding": 0.0001,
    }
    event = MarketEvent(
        symbol="SOL",
        price=150.0,
        timestamp_ms=now,
        funding=0.0001,
        predicted_funding=0.0001,
    )
    in_cd, reason = engine._is_in_cooldown("ChecklistMeta", "SOL", event)
    assert in_cd
    assert "remaining" in reason


if __name__ == "__main__":
    test_sl_to_be_triggers_at_06r()
    test_counter_trend_blocks_long()
    test_counter_trend_allows_with_trend()
    test_oir_blocks_short_with_positive_oir()
    test_oir_none_passes()
    test_chase_rejects_extended_runup()
    test_chase_exempt_volatility_breakout()
    test_symbol_risk_multiplier_halves_sol_size()
    test_daily_stop_streak_blocks_fifth_entry()
    test_daily_stop_streak_resets_next_day()
    test_cooldown_still_blocks_during_active_window()
    print("ALL FORENSIC FIX TESTS PASSED [OK]")
