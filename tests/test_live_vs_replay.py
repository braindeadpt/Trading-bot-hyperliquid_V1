"""Tests for the Fase 10 live-vs-replay drift detector.

End-to-end path (tmp sqlite DB with the real trades/decision_audit/candle
schema, driven through the real BacktestEngine via a deterministic
one-shot Strategy double injected via ``strategy_override=``) is marked
``integration_offline``. Never touches ``data/live/bot.db``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from src.data.database import Candle, Database
from src.research.live_vs_replay import (
    DRIFT,
    PASS,
    build_live_vs_replay_report,
    match_trades,
)
from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.utils.config import Config

pytestmark = pytest.mark.integration_offline

BASE_TS = 1_700_000_000_000
STEP_MS = 60_000
N_CANDLES = 20
ENTRY_IDX = 5
EXIT_AFTER_MS = 10 * STEP_MS  # exit fires 10 candles after entry -> idx 15


class _OneShotStrategy(Strategy):
    """Deterministic single-trade strategy for controlled replay testing.

    Enters exactly once, at a fixed candle timestamp, and exits exactly
    ``exit_after_ms`` later — no stop-loss/take-profit distance is set close
    enough to be hit intrabar (flat synthetic candles), so the only exit
    path is this strategy's own on_position signal.
    """

    def __init__(self, symbol: str, entry_ts: int, exit_after_ms: int, name: str) -> None:
        self._symbol = symbol
        self._entry_ts = entry_ts
        self._exit_after_ms = exit_after_ms
        self._entered = False
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        if self._entered or event.symbol != self._symbol or event.timestamp_ms != self._entry_ts:
            return None
        self._entered = True
        return Signal(
            strategy=self._name,
            symbol=self._symbol,
            side="long",
            confidence=0.9,
            size_pct=0.01,
            entry_price=event.price,
            stop_loss_pct=0.05,
            take_profit_pct=None,
            reason="test_entry",
            metadata={"atr_pct": 0.01},
        )

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        if event.timestamp_ms - position.entry_time_ms >= self._exit_after_ms:
            return ExitSignal(
                strategy=self._name, symbol=self._symbol, side="close",
                confidence=1.0, reason="test_exit",
            )
        return None


def _permissive_config() -> Config:
    return Config({
        "assets": ["BTC"],
        "risk": {
            "initial_capital": 10_000.0,
            "max_positions": 5,
            "max_daily_trades": 0,
            "max_daily_loss_pct": 100.0,
            "per_trade_risk_pct": 1.0,
            "max_position_size_pct": 50.0,
            "leverage_max": 20.0,
            "taker_fee_pct": 0.0,
            "paper_slippage_pct": 0.0,
            "chase_filter": {"enabled": False},
            "volatility_circuit_breaker": {"enabled": False},
            "funding_blackout": {"enabled": False},
        },
        "strategy": {
            "kelly": {"enabled": False},
            "cooldown": {"base_minutes": 0, "max_minutes": 0, "multiplier": 1.0},
            "portfolio_governance": {
                "max_directional_exposure_pct": 200.0,
                "max_sector_exposure_pct": 200.0,
            },
            "phase08": {"execution_strategies": ["VolatilityBreakout"]},
        },
        "execution": {"tca_enabled": False},
        "backtest": {
            "initial_capital": 10_000.0,
            "commission_pct": 0.0,
            "slippage_bps": 0.0,
            "use_microstructure_proxy": False,
            "use_funding": False,
            "use_oi": False,
            "replay_data_quality": {"require_funding": False, "require_oi": False},
        },
    })


@pytest.fixture()
def tmp_bot_db(tmp_path: Path) -> Path:
    """A tmp sqlite DB carrying the real schema (candles + trades + decision_audit).

    Built via the real ``Database`` class (never data/live/bot.db) so the
    schema is guaranteed identical to production.
    """
    db_path = tmp_path / "tmp_bot.db"
    db = Database(db_path)
    candles = [
        Candle(
            symbol="BTC",
            timestamp_ms=BASE_TS + i * STEP_MS,
            open=100.0, high=100.0, low=100.0, close=100.0,
            volume=10.0,
        )
        for i in range(N_CANDLES)
    ]
    db.save_candles(candles, "1m")
    db.close()
    return db_path


def _insert_live_trade(
    db_path: Path,
    *,
    entry_time: int,
    exit_time: int,
    entry_price: float,
    exit_price: float,
    size: float,
    pnl_usd: float,
    strategy: str = "VolatilityBreakout",
    exit_reason: str = "test_exit",
    entry_fee: float = 0.0,
    cumulative_order_fee: float = 0.0,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO trades (
                symbol, side, entry_price, exit_price, entry_time, exit_time,
                size, pnl_usd, pnl_pct, strategy, exit_reason, status,
                entry_fee, cumulative_order_fee, funding_paid, signal_metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, 0.0, ?)
            """,
            (
                "BTC", "long", entry_price, exit_price, entry_time, exit_time,
                size, pnl_usd, 0.0, strategy, exit_reason,
                entry_fee, cumulative_order_fee, json.dumps({"stop_loss_pct": 0.05}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _run_replay_only(db_path: Path, config: Config):
    """Run just the replay side to discover the ground-truth trade the
    one-shot strategy produces, so the synthetic 'live' trade can be built
    to match (or deliberately diverge from) it."""
    from src.research.live_vs_replay import run_replay

    strategy = _OneShotStrategy("BTC", BASE_TS + ENTRY_IDX * STEP_MS, EXIT_AFTER_MS, "VolatilityBreakout")
    return run_replay(
        config,
        symbols=["BTC"],
        start_ms=BASE_TS,
        end_ms=BASE_TS + (N_CANDLES - 1) * STEP_MS,
        live_db_path=db_path,
        strategy_override=strategy,
    )


@pytest.mark.integration_offline
def test_zero_drift_reports_pass(tmp_bot_db: Path) -> None:
    config = _permissive_config()
    replay_result = _run_replay_only(tmp_bot_db, config)
    assert len(replay_result["trades"]) == 1
    rt = replay_result["trade_analytics"][0]

    # Synthetic "live" trade built to exactly match the replay's own trade —
    # this is the true zero-drift case (both sides recorded the same fill,
    # same fees, same exit).
    _insert_live_trade(
        tmp_bot_db,
        entry_time=rt["entry_time"],
        exit_time=rt["exit_time"],
        entry_price=rt["entry_price"],
        exit_price=rt["exit_price"],
        size=rt["size"],
        pnl_usd=rt["pnl_usd"],
        exit_reason=rt["exit_reason"],
        entry_fee=rt["fees_paid"],
        cumulative_order_fee=0.0,
    )

    strategy = _OneShotStrategy("BTC", BASE_TS + ENTRY_IDX * STEP_MS, EXIT_AFTER_MS, "VolatilityBreakout")
    report = build_live_vs_replay_report(
        config=config,
        start_ms=BASE_TS,
        end_ms=BASE_TS + (N_CANDLES - 1) * STEP_MS,
        symbols=["BTC"],
        live_db_path=tmp_bot_db,
        execution_strategies=["VolatilityBreakout"],
        strategy_override=strategy,
    )

    assert report["live_trade_count"] == 1
    assert report["replay_trade_count"] == 1
    assert report["matched_trade_count"] == 1
    assert report["verdict"] == PASS
    for name, dim in report["dimensions"].items():
        assert dim["verdict"] in (PASS, "NOT_COMPARABLE"), f"{name} unexpectedly {dim['verdict']}"
    # MFE/MAE is documented as never comparable (live persists no such columns).
    assert report["dimensions"]["mfe_mae"]["verdict"] == "NOT_COMPARABLE"


@pytest.mark.integration_offline
def test_exit_reason_mismatch_flags_drift(tmp_bot_db: Path) -> None:
    config = _permissive_config()
    replay_result = _run_replay_only(tmp_bot_db, config)
    rt = replay_result["trade_analytics"][0]
    assert rt["exit_reason"] == "test_exit"

    # Deliberately inject a mismatch the backtest would never produce under
    # these rules (no stop-loss can trigger on flat candles).
    _insert_live_trade(
        tmp_bot_db,
        entry_time=rt["entry_time"],
        exit_time=rt["exit_time"],
        entry_price=rt["entry_price"],
        exit_price=rt["exit_price"],
        size=rt["size"],
        pnl_usd=rt["pnl_usd"],
        exit_reason="stop_loss",  # <-- deliberate divergence
        entry_fee=rt["fees_paid"],
        cumulative_order_fee=0.0,
    )

    strategy = _OneShotStrategy("BTC", BASE_TS + ENTRY_IDX * STEP_MS, EXIT_AFTER_MS, "VolatilityBreakout")
    report = build_live_vs_replay_report(
        config=config,
        start_ms=BASE_TS,
        end_ms=BASE_TS + (N_CANDLES - 1) * STEP_MS,
        symbols=["BTC"],
        live_db_path=tmp_bot_db,
        execution_strategies=["VolatilityBreakout"],
        strategy_override=strategy,
    )

    assert report["verdict"] == DRIFT
    assert report["dimensions"]["exit_reason"]["verdict"] == DRIFT
    assert report["dimensions"]["exit_reason"]["mismatch_count"] == 1
    # Other numeric dimensions (identical numbers) should still be clean.
    assert report["dimensions"]["notional"]["verdict"] == PASS
    assert report["dimensions"]["fees"]["verdict"] == PASS


@pytest.mark.integration_offline
def test_missing_live_trade_flags_signal_count_drift(tmp_bot_db: Path) -> None:
    """Replay produces a trade but live recorded none — an unexplained
    signal-count divergence, the clearest form of drift."""
    config = _permissive_config()
    strategy = _OneShotStrategy("BTC", BASE_TS + ENTRY_IDX * STEP_MS, EXIT_AFTER_MS, "VolatilityBreakout")

    report = build_live_vs_replay_report(
        config=config,
        start_ms=BASE_TS,
        end_ms=BASE_TS + (N_CANDLES - 1) * STEP_MS,
        symbols=["BTC"],
        live_db_path=tmp_bot_db,
        execution_strategies=["VolatilityBreakout"],
        strategy_override=strategy,
    )

    assert report["live_trade_count"] == 0
    assert report["replay_trade_count"] == 1
    assert report["verdict"] == DRIFT
    assert report["dimensions"]["signal_count"]["verdict"] == DRIFT


@pytest.mark.unit
def test_match_trades_respects_jitter_tolerance() -> None:
    live = [{"strategy": "S", "symbol": "BTC", "side": "long", "entry_time": 1_000_000}]
    replay_close = [{"strategy": "S", "symbol": "BTC", "side": "long", "entry_time": 1_000_000 + 30_000}]
    replay_far = [{"strategy": "S", "symbol": "BTC", "side": "long", "entry_time": 1_000_000 + 200_000}]

    pairs, unmatched_live, unmatched_replay = match_trades(live, replay_close)
    assert len(pairs) == 1
    assert not unmatched_live and not unmatched_replay

    pairs2, unmatched_live2, unmatched_replay2 = match_trades(live, replay_far)
    assert len(pairs2) == 0
    assert len(unmatched_live2) == 1
    assert len(unmatched_replay2) == 1
