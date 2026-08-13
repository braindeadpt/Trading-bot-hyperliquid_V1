"""Golden tests for backtest/live parity (Phase 05)."""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone

from src.backtest.engine import BacktestEngine, BacktestConfig, _OpenPosition
from src.backtest.symbol_rounding import round_position_size
from src.backtest.replay_data_quality import ReplayDataQualityGate, SymbolReplayAudit
from src.core.correlation_monitor import CorrelationMonitor
from src.core.funding_blackout import FundingBlackoutFilter
from src.core.risk_manager import RiskManager
from src.core.signal_pipeline import GATE_ORDER, PipelineContext, SignalPipeline
from src.core.volatility_circuit import VolatilityCircuitBreaker
from src.data.database import Database
from src.strategies.base import MarketEvent, Signal, Strategy
from src.strategies.indicators import Candle
from src.utils.config import Config, load_config, resolve_kelly_enabled
import pytest

pytestmark = pytest.mark.integration_offline


def _cfg(
    *,
    sol_mult: float = 1.0,
    chase_enabled: bool = True,
    kelly_override: Optional[bool] = None,
    max_stop_losses: int = 4,
) -> Config:
    data: Dict[str, Any] = {
        "risk": {
            "max_positions": 5,
            "max_daily_trades": 0,
            "max_daily_stop_losses": max_stop_losses,
            "max_daily_loss_pct": 3.0,
            "per_trade_risk_pct": 1.0,
            "max_position_size_pct": 5.0,
            "leverage_max": 10.0,
            "taker_fee_pct": 0.04,
            "paper_slippage_pct": 0.02,
            "symbol_risk_multiplier": {"SOL": sol_mult},
            "chase_filter": {
                "enabled": chase_enabled,
                "lookback_hours": 3.0,
                "max_runup_pct": 0.008,
                "exempt_strategies": [],
            },
            "volatility_circuit_breaker": {"enabled": False},
            "funding_blackout": {"enabled": False},
        },
        "strategy": {
            "kelly": {"enabled": False},
            "cooldown": {"base_minutes": 30, "max_minutes": 120, "multiplier": 2.0},
            "portfolio_governance": {
                "max_directional_exposure_pct": 60.0,
                "max_sector_exposure_pct": 100.0,
            },
        },
        "execution": {
            "tca_enabled": False,
            "maker_orders": {
                "enabled": True,
                "maker_fee_pct": 0.01,
                "strategies": ["VWAPDeviation"],
            },
        },
        "backtest": {
            "kelly_override": kelly_override,
            "intrabar_conflict_policy": "pessimistic",
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(data, fh)
        path = fh.name
    return load_config(path)


def _signal(
    symbol: str = "BTC",
    size_pct: float = 0.01,
    stop: float = 0.012,
    strategy: str = "VWAPDeviation",
) -> Signal:
    return Signal(
        strategy=strategy,
        symbol=symbol,
        side="long",
        confidence=0.8,
        size_pct=size_pct,
        entry_price=50_000.0,
        stop_loss_pct=stop,
        take_profit_pct=0.024,
        reason="test",
        metadata={"sub_strategy": strategy, "atr_pct": 0.006},
    )


def _event(
    symbol: str = "BTC",
    price: float = 50_000.0,
    ts: int = 1_700_000_000_000,
) -> MarketEvent:
    c = Candle(open=price, high=price * 1.001, low=price * 0.999, close=price, volume=100.0, timestamp_ms=ts)
    return MarketEvent(
        symbol=symbol,
        price=price,
        timestamp_ms=ts,
        candle_1m=c,
        candle_15m=c,
        adx_14=22.0,
    )


class _PortfolioStub:
    def __init__(self, capital: float = 10_000.0) -> None:
        self.positions: Dict[str, Any] = {}
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.current_capital = capital

    def get_max_drawdown(self) -> float:
        return 0.0


def test_same_signal_same_notional_live_and_backtest() -> None:
    """Same capital/signal/stop → same notional via RiskManager."""
    cfg = _cfg()
    rm = RiskManager(cfg, None)
    capital = 10_000.0
    sig = _signal(size_pct=0.01, stop=0.012)

    live_size = rm.calculate_position_size(sig, capital, 0.006)
    live_notional = live_size * 50_000.0

    bt_size = rm.calculate_position_size(sig, capital, 0.006)
    bt_notional = bt_size * 50_000.0

    assert abs(live_notional - bt_notional) < 1e-6
    assert abs(live_notional - 5_000.0) < 1.0


def test_sol_multiplier_halves_notional() -> None:
    """SOL 0.5× risk multiplier halves final conviction notional when conviction binds."""
    cfg_data: Dict[str, Any] = {
        "risk": {
            "max_positions": 5,
            "max_daily_trades": 0,
            "max_daily_loss_pct": 100.0,
            "per_trade_risk_pct": 10.0,
            "max_position_size_pct": 100.0,
            "leverage_max": 20.0,
            "symbol_risk_multiplier": {"SOL": 0.5},
            "chase_filter": {"enabled": False},
            "volatility_circuit_breaker": {"enabled": False},
            "funding_blackout": {"enabled": False},
        },
        "strategy": {
            "kelly": {"enabled": False},
            "portfolio_governance": {
                "max_directional_exposure_pct": 200.0,
                "max_sector_exposure_pct": 200.0,
            },
        },
        "execution": {"tca_enabled": False},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(cfg_data, fh)
        cfg = load_config(fh.name)

    rm = RiskManager(cfg, None)
    pipeline = SignalPipeline(
        cfg, rm, use_regime_weights=False, kelly_enabled=False, for_backtest=True,
    )
    capital = 10_000.0
    base = _signal(symbol="SOL", size_pct=0.01, stop=0.02)

    base_size = rm.calculate_position_size(base, capital, 0.01)
    decision = pipeline.evaluate_gates(
        base, _event(symbol="SOL"), _PortfolioStub(), PipelineContext(), skip_tca=True,
    )
    assert decision.approved
    assert decision.signal is not None
    adj_size = rm.calculate_position_size(decision.signal, capital, 0.01)
    assert base_size > 0.0
    assert abs(adj_size - base_size * 0.5) < 1e-6


def test_same_sequence_same_gate_decisions() -> None:
    """Identical signal/event/state → identical gate outcome."""
    cfg = _cfg(chase_enabled=True)
    rm = RiskManager(cfg, None)
    pipeline = SignalPipeline(cfg, rm, for_backtest=True)
    ctx = PipelineContext()
    ctx.candles_15m_history = {
        "BTC": [
            Candle(49_000, 49_100, 48_900, 49_000, 1.0, 0),
            Candle(49_500, 49_600, 49_400, 49_500, 1.0, 3_600_000),
        ]
    }
    sig = _signal(strategy="TrendFollow")
    ev = _event()
    port = _PortfolioStub()
    d1 = pipeline.evaluate_gates(sig, ev, port, ctx, skip_tca=True)
    d2 = pipeline.evaluate_gates(sig, ev, port, ctx, skip_tca=True)
    assert d1.approved == d2.approved
    assert d1.gate == d2.gate
    assert d1.reason == d2.reason


def test_gate_sequence_order_is_canonical() -> None:
    assert GATE_ORDER.index("cooldown") < GATE_ORDER.index("chase_filter")
    assert GATE_ORDER.index("chase_filter") < GATE_ORDER.index("risk")
    assert GATE_ORDER.index("risk") < GATE_ORDER.index("tca")


def test_chase_filter_blocks_identical_runup() -> None:
    cfg = _cfg(chase_enabled=True)
    rm = RiskManager(cfg, None)
    pipeline = SignalPipeline(cfg, rm, for_backtest=True)
    ctx = PipelineContext()
    history: Dict[str, List[Candle]] = {
        "BTC": [
            Candle(49_000, 49_100, 48_900, 49_000, 1.0, 0),
            Candle(49_500, 49_600, 49_400, 49_500, 1.0, 3_600_000),
        ]
    }
    ctx.candles_15m_history = history
    sig = _signal(strategy="TrendFollow")
    reason = pipeline._chase.check(sig, history)
    assert reason is not None
    assert "chase runup" in reason

    decision = pipeline.evaluate_gates(sig, _event(), _PortfolioStub(), ctx, skip_tca=True)
    assert not decision.approved
    assert decision.gate == "chase_filter"


def test_daily_stop_streak_blocks_entries() -> None:
    cfg = _cfg(max_stop_losses=2)
    rm = RiskManager(cfg, None)
    for _ in range(2):
        class T:
            pnl_usd = -10.0
            pnl_pct = -0.01
            symbol = "BTC"
            reason = "stop_loss"
        rm.on_trade_closed(T())
    sig = _signal()
    ok, reason = rm.can_enter(sig, _PortfolioStub())
    assert not ok
    assert "daily_stop_streak" in reason


def test_intrabar_pessimistic_stop_before_tp() -> None:
    bt_cfg = BacktestConfig(intrabar_conflict_policy="pessimistic")
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.cfg = bt_cfg
    pos = _OpenPosition(
        id=1,
        strategy="Test",
        symbol="BTC",
        side="long",
        entry_price=50_000.0,
        entry_time_ms=0,
        size=0.1,
        stop_loss_price=49_500.0,
        take_profit_price=51_000.0,
    )
    from src.data.database import Candle as DBCandle

    c1m = DBCandle(
        symbol="BTC",
        timestamp_ms=60_000,
        open=50_000.0,
        high=51_200.0,
        low=49_400.0,
        close=50_100.0,
        volume=10.0,
    )
    result = engine._intrabar_stop_tp(pos, c1m)
    assert result is not None
    reason, fill = result
    assert reason == "stop_loss"
    assert fill == 49_500.0


@dataclass
class _MockBook:
    bids: list
    asks: list


def test_fees_maker_taker_parity_via_order_router() -> None:
    cfg = _cfg()
    rm = RiskManager(cfg, None)
    pipeline = SignalPipeline(cfg, rm, for_backtest=True)

    sig = _signal(strategy="VWAPDeviation")
    _, spec_no_book = pipeline.resolve_order_fees(sig)
    assert spec_no_book.order_type == "market"
    assert spec_no_book.entry_fee_pct == 0.0004

    book = _MockBook(bids=[type("L", (), {"price": 49_900.0})()], asks=[])
    _, spec_maker = pipeline.resolve_order_fees(sig, orderbook=book)
    assert spec_maker.order_type == "limit_maker"
    assert spec_maker.entry_fee_pct == 0.0001

    sig2 = _signal(strategy="TrendFollow")
    _, spec2 = pipeline.resolve_order_fees(sig2)
    assert spec2.entry_fee_pct == 0.0004
    assert spec2.order_type == "market"


def test_kelly_single_flag_with_backtest_override() -> None:
    cfg_live = _cfg(kelly_override=None)
    assert resolve_kelly_enabled(cfg_live, for_backtest=False) is False
    assert resolve_kelly_enabled(cfg_live, for_backtest=True) is False

    cfg_off = _cfg(kelly_override=False)
    assert resolve_kelly_enabled(cfg_off, for_backtest=True) is False


def test_correlation_gate_blocks_high_corr() -> None:
    cfg = _cfg()
    rm = RiskManager(cfg, None)
    monitor = CorrelationMonitor(lookback=60)
    for i in range(30):
        r = 0.01 + (i % 5) * 0.001
        monitor.add_return("BTC", r)
        monitor.add_return("ETH", r * 0.99)
    pipeline = SignalPipeline(
        cfg, rm, correlation_monitor=monitor, for_backtest=True,
    )
    port = _PortfolioStub()
    port.positions = {
        "BTC": type("P", (), {"entry_price": 50_000.0, "size": 0.1, "side": "long"})(),
    }
    decision = pipeline.evaluate_gates(
        _signal(symbol="ETH"), _event(symbol="ETH"), port, PipelineContext(), skip_tca=True,
    )
    assert not decision.approved
    assert decision.gate == "correlation"


def test_replay_data_quality_blocks_bar_gap() -> None:
    gate = ReplayDataQualityGate(max_bar_gap_ms=60_000, min_coverage_pct=0.0)
    audit = SymbolReplayAudit(
        symbol="BTC",
        coverage_pct=1.0,
        max_gap_ms=0,
        bar_count=100,
        expected_bars=100,
        funding_available=True,
        oi_available=False,
    )
    reason = gate.check_entry(
        "BTC",
        _event(ts=200_000),
        audit=audit,
        last_bar_ts=100_000,
        funding_ts_at=200_000,
        oi_ts_at=None,
    )
    assert reason is not None
    assert "replay_bar_gap" in reason

    pipeline = SignalPipeline(
        _cfg(), RiskManager(_cfg(), None),
        replay_quality=gate,
        for_backtest=True,
    )
    ctx = PipelineContext()
    ctx.replay_audit["BTC"] = audit
    ctx.last_bar_ts["BTC"] = 100_000
    decision = pipeline.evaluate_gates(
        _signal(), _event(ts=200_000), _PortfolioStub(), ctx, skip_tca=True,
    )
    assert not decision.approved
    assert decision.gate == "replay_data_quality"


def test_tca_strict_rejects_without_l2() -> None:
    cfg_data: Dict[str, Any] = {
        "risk": {
            "max_positions": 5,
            "max_daily_trades": 0,
            "taker_fee_pct": 0.04,
            "paper_slippage_pct": 0.02,
            "chase_filter": {"enabled": False},
            "volatility_circuit_breaker": {"enabled": False},
            "funding_blackout": {"enabled": False},
        },
        "strategy": {"kelly": {"enabled": False}},
        "execution": {"tca_enabled": True, "tca_mode": "strict"},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(cfg_data, fh)
        cfg = load_config(fh.name)
    pipeline = SignalPipeline(cfg, RiskManager(cfg, None), for_backtest=False)
    decision = pipeline.evaluate_tca_gate(_signal(), has_orderbook=False)
    assert not decision.approved
    assert decision.gate == "tca"
    assert "tca_strict_no_l2_book" in decision.reason


def test_tca_proxy_allows_without_l2() -> None:
    cfg_data: Dict[str, Any] = {
        "risk": {
            "max_positions": 5,
            "max_daily_trades": 0,
            "taker_fee_pct": 0.04,
            "paper_slippage_pct": 0.02,
            "chase_filter": {"enabled": False},
            "volatility_circuit_breaker": {"enabled": False},
            "funding_blackout": {"enabled": False},
        },
        "strategy": {"kelly": {"enabled": False}},
        "execution": {"tca_enabled": True},
        "backtest": {"tca_mode": "proxy"},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(cfg_data, fh)
        cfg = load_config(fh.name)
    pipeline = SignalPipeline(cfg, RiskManager(cfg, None), for_backtest=True)
    decision = pipeline.evaluate_tca_gate(_signal(), has_orderbook=False)
    assert decision.approved


def test_entry_debounce_blocks_rapid_reentry() -> None:
    cfg = _cfg(chase_enabled=False)
    rm = RiskManager(cfg, None)
    pipeline = SignalPipeline(cfg, rm, for_backtest=True)
    ctx = PipelineContext()
    ctx.last_entry_ms["BTC"] = 1_700_000_000_000
    decision = pipeline.evaluate_gates(
        _signal(),
        _event(ts=1_700_000_002_000),
        _PortfolioStub(),
        ctx,
        skip_tca=True,
    )
    assert not decision.approved
    assert decision.gate == "entry_debounce"


def test_gate_order_feed_before_cooldown() -> None:
    assert GATE_ORDER.index("feed_health") < GATE_ORDER.index("entry_debounce")
    assert GATE_ORDER.index("entry_debounce") < GATE_ORDER.index("cooldown")
    assert GATE_ORDER.index("correlation") < GATE_ORDER.index("risk")


def test_live_and_backtest_pipelines_agree_on_shared_gates() -> None:
    """Identical inputs → identical gate decisions in live AND backtest replay.

    Phase 05: SignalPipeline is the single source of truth for the entry
    gate chain. Building both pipelines from the same config (for_backtest
    False/True) and driving them with identical state must produce the same
    gate outcome for every shared gate.
    """
    cfg = _cfg(chase_enabled=True)
    rm = RiskManager(cfg, None)
    vol = VolatilityCircuitBreaker.from_config_dict({
        "enabled": True, "multiplier": 3.0, "baseline_window_bars": 20,
        "block_duration_min": 30, "min_samples": 1,
    })
    fb = FundingBlackoutFilter.from_config_dict({
        "enabled": True, "minutes_before": 5, "minutes_after": 5,
        "resets_utc": ["08:00"],
    })
    monitor = CorrelationMonitor(lookback=60)
    live = SignalPipeline(
        cfg, rm, correlation_monitor=monitor, vol_circuit=vol,
        funding_blackout=fb, for_backtest=False,
    )
    bt = SignalPipeline(
        cfg, rm, correlation_monitor=monitor, vol_circuit=vol,
        funding_blackout=fb, for_backtest=True,
    )
    ctx_live = PipelineContext()
    ctx_bt = PipelineContext()

    def _assert_same(sig, ev, port):
        dl = live.evaluate_gates(sig, ev, port, ctx_live, skip_tca=True)
        db = bt.evaluate_gates(sig, ev, port, ctx_bt, skip_tca=True)
        assert dl.approved == db.approved
        assert dl.gate == db.gate
        assert dl.reason == db.reason
        return dl

    ts0 = 1_700_000_000_000

    # 1. Fresh signal → both approve
    d = _assert_same(_signal(), _event(ts=ts0), _PortfolioStub())
    assert d.approved

    # 2. Entry debounce
    ctx_live.last_entry_ms["BTC"] = ts0
    ctx_bt.last_entry_ms["BTC"] = ts0
    d = _assert_same(_signal(), _event(ts=ts0 + 2_000), _PortfolioStub())
    assert not d.approved and d.gate == "entry_debounce"
    for c in (ctx_live, ctx_bt):
        c.last_entry_ms.clear()

    # 3. Cooldown
    for c in (ctx_live, ctx_bt):
        c.cooldown_state["VWAPDeviation:BTC"] = {
            "last_trade_ms": ts0, "duration_ms": 30 * 60_000,
            "consecutive_losses": 0,
        }
    d = _assert_same(_signal(), _event(ts=ts0 + 60_000), _PortfolioStub())
    assert not d.approved and d.gate == "cooldown"
    for c in (ctx_live, ctx_bt):
        c.cooldown_state.clear()

    # 4. Chase filter (TrendFollow is not exempt)
    runup = [
        Candle(49_000, 49_100, 48_900, 49_000, 1.0, ts0),
        Candle(49_500, 49_600, 49_400, 49_500, 1.0, ts0 + 3_600_000),
    ]
    ctx_live.candles_15m_history = {"BTC": list(runup)}
    ctx_bt.candles_15m_history = {"BTC": list(runup)}
    d = _assert_same(_signal(strategy="TrendFollow"), _event(ts=ts0), _PortfolioStub())
    assert not d.approved and d.gate == "chase_filter"
    for c in (ctx_live, ctx_bt):
        c.candles_15m_history.clear()

    # 5. Correlation gate (shared monitor → identical state)
    for i in range(30):
        r = 0.01 + (i % 5) * 0.001
        monitor.add_return("BTC", r)
        monitor.add_return("ETH", r * 0.99)
    port_corr = _PortfolioStub()
    port_corr.positions = {
        "BTC": type("P", (), {"entry_price": 50_000.0, "size": 0.1, "side": "long"})(),
    }
    d = _assert_same(_signal(symbol="ETH"), _event(symbol="ETH", ts=ts0), port_corr)
    assert not d.approved and d.gate == "correlation"

    # 6. Risk — max total positions (5/5)
    port_full = _PortfolioStub()
    port_full.positions = {f"X{i}": object() for i in range(5)}
    d = _assert_same(_signal(symbol="BTC"), _event(ts=ts0), port_full)
    assert not d.approved and d.gate == "risk"

    # 7. Volatility circuit (shared instance → identical block state).
    # Baseline = mean of the trailing window INCLUDING the current sample,
    # so establish ~20 calm samples first, then a 3.5x spike.
    for i in range(19):
        vol.update("BTC", 0.01, ts0 + i)
    vol.update("BTC", 0.04, ts0 + 1_000)  # mean=0.0115 → ratio 3.48 > 3
    d = _assert_same(_signal(), _event(ts=ts0 + 2_000), _PortfolioStub())
    assert not d.approved and d.gate == "vol_circuit"

    # 8. Funding blackout — 07:58 UTC, 2 min before the 08:00 reset
    blackout_ts = int(
        datetime(2023, 11, 14, 7, 58, tzinfo=timezone.utc).timestamp() * 1000
    )
    d = _assert_same(
        _signal(symbol="ETH"), _event(symbol="ETH", ts=blackout_ts), _PortfolioStub(),
    )
    assert not d.approved and d.gate == "funding_blackout"


def test_gate_manifest_documents_live_backtest_parity_contract() -> None:
    """gate_manifest() must pin the shared vs live-only gate split."""
    cfg = _cfg()
    rm = RiskManager(cfg, None)
    live = SignalPipeline(cfg, rm, for_backtest=False)
    bt = SignalPipeline(cfg, rm, for_backtest=True)

    m = live.gate_manifest()
    assert m["gate_parity_version"] == "phase05-gates-v1"
    assert m["shared_gate_order"] == list(GATE_ORDER)
    assert set(m["live_only_gates"]) == {
        "execution_block", "fill_ratio", "slippage_l2",
        "reconciliation_stale", "executor_debounce",
    }
    assert m["replay_substitutes"] == {"feed_health": "replay_data_quality"}
    # Live TCA is strict (needs L2 book); backtest is proxy (candle-only).
    assert live._tca_mode == "strict"
    assert bt._tca_mode == "proxy"


def test_backtest_engine_wires_real_risk_manager_and_pipeline() -> None:
    """Backtest replay must use the SAME RiskManager + SignalPipeline as live."""

    class _StubStrategy(Strategy):
        name = "Stub"

        def on_data(self, event):
            return None

        def on_position(self, position, event):
            return None

    cfg = _cfg()
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = Database(tmp.name)
    try:
        engine = BacktestEngine(
            database=db,
            strategy=_StubStrategy(),
            config=BacktestConfig(
                use_risk_manager=True,
                use_volatility_circuit=False,
                use_funding_blackout=False,
            ),
            symbols=["BTC"],
            risk_config=cfg,
        )
    finally:
        db.close()

    assert isinstance(engine._risk_manager, RiskManager)
    assert isinstance(engine._pipeline, SignalPipeline)
    assert engine._pipeline._for_backtest is True
    # The same RiskManager instance drives both gate decisions and sizing.
    assert engine._pipeline._risk is engine._risk_manager


def test_round_position_size_matches_floor() -> None:
    assert round_position_size("SOL", 1.239, _cfg()) == 1.23


def main() -> None:
    test_same_signal_same_notional_live_and_backtest()
    test_sol_multiplier_halves_notional()
    test_gate_sequence_order_is_canonical()
    test_same_sequence_same_gate_decisions()
    test_chase_filter_blocks_identical_runup()
    test_daily_stop_streak_blocks_entries()
    test_intrabar_pessimistic_stop_before_tp()
    test_fees_maker_taker_parity_via_order_router()
    test_kelly_single_flag_with_backtest_override()
    test_correlation_gate_blocks_high_corr()
    test_replay_data_quality_blocks_bar_gap()
    test_tca_strict_rejects_without_l2()
    test_tca_proxy_allows_without_l2()
    test_entry_debounce_blocks_rapid_reentry()
    test_gate_order_feed_before_cooldown()
    test_live_and_backtest_pipelines_agree_on_shared_gates()
    test_gate_manifest_documents_live_backtest_parity_contract()
    test_backtest_engine_wires_real_risk_manager_and_pipeline()
    test_round_position_size_matches_floor()
    print("test_backtest_live_parity: all passed")


if __name__ == "__main__":
    main()
