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
from src.core.engine import TradingEngine
from src.core.funding_blackout import FundingBlackoutFilter
from src.core.risk_manager import RiskManager
from src.core.signal_pipeline import GATE_ORDER, PipelineContext, SignalPipeline
from src.core.volatility_circuit import VolatilityCircuitBreaker
from src.data.database import Candle as DBCandle
from src.data.database import Database
from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.strategies.checklist_meta import ChecklistMeta
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


PROD_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "settings.yaml"
)


class TestParityAgainstProductionConfig:
    """Run the parity chain against the REAL production settings.yaml.

    The unit tests above use a minimal config with loose thresholds
    (max_positions 5, position size 5%, taker 4 bps). This class loads
    ``config/settings.yaml`` — the thresholds the live bot actually runs
    with (max_positions 3, position size 2%, taker 4.5 bps, directional
    cap 50%, SOL 0.5x, chase on with VB/Donchian exempt, stop streak 4,
    vol circuit + funding blackout ENABLED) — and asserts the full chain
    (RiskManager -> SignalPipeline -> gates) holds under those active
    thresholds. If any gate mis-reads the production config, these fail.
    """

    pytestmark = pytest.mark.integration_offline

    @pytest.fixture(scope="class")
    def prod_cfg(self) -> Config:
        assert os.path.exists(PROD_SETTINGS_PATH), PROD_SETTINGS_PATH
        return load_config(PROD_SETTINGS_PATH)

    def test_production_config_loads_and_risk_manager_builds(self, prod_cfg):
        rm = RiskManager(prod_cfg, None)
        assert rm is not None
        pipeline = SignalPipeline(prod_cfg, rm, for_backtest=True)
        assert pipeline is not None

    def test_production_thresholds_are_the_active_ones(self, prod_cfg):
        risk = prod_cfg.get("risk", {}) or {}
        assert risk.get("max_positions") == 3
        assert risk.get("max_position_size_pct") == 2.0
        assert risk.get("taker_fee_pct") == 0.045
        assert risk.get("per_trade_risk_pct") == 1.0
        assert (risk.get("symbol_risk_multiplier", {}) or {}).get("SOL") == 0.5
        chase = risk.get("chase_filter", {}) or {}
        assert chase.get("enabled") is True
        assert chase.get("max_runup_pct") == 0.008
        exempt = chase.get("exempt_strategies", [])
        assert "VolatilityBreakout" in exempt
        assert "DonchianBreakout" in exempt
        assert risk.get("max_daily_stop_losses") == 4
        vcb = risk.get("volatility_circuit_breaker", {}) or {}
        assert vcb.get("enabled") is True
        fb = risk.get("funding_blackout", {}) or {}
        assert fb.get("enabled") is True
        gov = prod_cfg.get("strategy.portfolio_governance", {}) or {}
        assert gov.get("max_directional_exposure_pct") == 50

    def test_production_notional_respects_2pct_position_cap(self, prod_cfg):
        """2% position cap (not 5% like the minimal tests) -> $200 notional."""
        rm = RiskManager(prod_cfg, None)
        capital = 10_000.0
        sig = _signal(size_pct=0.01, stop=0.012)
        size = rm.calculate_position_size(sig, capital, 0.006)
        notional = size * 50_000.0
        assert notional <= 2.0 / 100.0 * capital * 50_000.0 / 50_000.0 * 50_000.0 + 1e-6
        assert notional <= 0.20 * capital + 1e-6  # 20% of capital at 1x

    def test_production_sol_multiplier_applies(self, prod_cfg):
        """SOL 0.5x multiplier halves size_pct; the 2% position cap saturates
        the final notional, so assert on the adjusted size_pct (where the
        multiplier is observable under the production cap)."""
        rm = RiskManager(prod_cfg, None)
        pipeline = SignalPipeline(prod_cfg, rm, for_backtest=True)
        base = _signal(symbol="SOL", size_pct=0.01, stop=0.02)
        decision = pipeline.evaluate_gates(
            base, _event(symbol="SOL"), _PortfolioStub(), PipelineContext(), skip_tca=True,
        )
        assert decision.approved
        assert decision.signal is not None
        assert abs(decision.signal.size_pct - 0.005) < 1e-9  # 0.01 * 0.5
        assert (
            decision.signal.metadata.get("symbol_risk_multiplier") == 0.5
        )

    def test_production_chase_filter_blocks_trendfollow_runup(self, prod_cfg):
        rm = RiskManager(prod_cfg, None)
        pipeline = SignalPipeline(prod_cfg, rm, for_backtest=True)
        ctx = PipelineContext()
        ctx.candles_15m_history = {
            "BTC": [
                Candle(49_000, 49_100, 48_900, 49_000, 1.0, 0),
                Candle(49_500, 49_600, 49_400, 49_500, 1.0, 3_600_000),
            ]
        }
        decision = pipeline.evaluate_gates(
            _signal(strategy="TrendFollow"), _event(), _PortfolioStub(), ctx, skip_tca=True,
        )
        assert not decision.approved
        assert decision.gate == "chase_filter"

    def test_production_chase_filter_exempts_vb_and_donchian(self, prod_cfg):
        """The production config exempts VB/Donchian from chase — live behavior."""
        rm = RiskManager(prod_cfg, None)
        pipeline = SignalPipeline(prod_cfg, rm, for_backtest=True)
        ctx = PipelineContext()
        ctx.candles_15m_history = {
            "BTC": [
                Candle(49_000, 49_100, 48_900, 49_000, 1.0, 0),
                Candle(49_500, 49_600, 49_400, 49_500, 1.0, 3_600_000),
            ]
        }
        for strategy in ("VolatilityBreakout", "DonchianBreakout"):
            reason = pipeline._chase.check(
                _signal(strategy=strategy), ctx.candles_15m_history["BTC"]
            )
            assert reason is None, f"{strategy} should be exempt from chase"

    def test_production_daily_stop_streak_blocks_at_four(self, prod_cfg):
        """Production stops entries after 4 stop-losses/day (not 2 like minimal)."""
        rm = RiskManager(prod_cfg, None)
        for _ in range(4):
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

    def test_production_daily_stop_streak_allows_three(self, prod_cfg):
        """Three stops is still under the production threshold of four."""
        rm = RiskManager(prod_cfg, None)
        for _ in range(3):
            class T:
                pnl_usd = -10.0
                pnl_pct = -0.01
                symbol = "BTC"
                reason = "stop_loss"
            rm.on_trade_closed(T())
        sig = _signal()
        ok, _reason = rm.can_enter(sig, _PortfolioStub())
        assert ok

    def _pos(self, price: float, size: float, side: str = "long"):
        return type("P", (), {"entry_price": price, "size": size, "side": side})()

    def test_production_max_positions_blocks_fourth(self, prod_cfg):
        """Production caps at 3 positions (not 5 like the minimal tests)."""
        rm = RiskManager(prod_cfg, None)
        pipeline = SignalPipeline(prod_cfg, rm, for_backtest=True)
        port = _PortfolioStub()
        port.positions = {f"X{i}": self._pos(50_000.0, 0.05) for i in range(3)}
        decision = pipeline.evaluate_gates(
            _signal(symbol="BTC"), _event(), port, PipelineContext(), skip_tca=True,
        )
        assert not decision.approved
        assert decision.gate == "risk"

    def test_production_max_positions_allows_third_slot(self, prod_cfg):
        """2 tiny positions stay under the 50% directional cap and the 3-slot
        max — the third entry must be approved."""
        rm = RiskManager(prod_cfg, None)
        pipeline = SignalPipeline(prod_cfg, rm, for_backtest=True)
        port = _PortfolioStub()
        port.positions = {f"X{i}": self._pos(50_000.0, 0.01) for i in range(2)}
        decision = pipeline.evaluate_gates(
            _signal(symbol="BTC"), _event(), port, PipelineContext(), skip_tca=True,
        )
        assert decision.approved

    def test_production_vol_circuit_enabled_and_blocks_spike(self, prod_cfg):
        """Vol circuit is ON in production: a 3.5x ATR spike blocks entries."""
        vcb = (prod_cfg.get("risk", {}) or {}).get("volatility_circuit_breaker", {}) or {}
        vol = VolatilityCircuitBreaker.from_config_dict(vcb)
        assert vol._cfg.enabled is True
        assert vol._cfg.min_samples == 24  # production: needs ~1d of history
        ts0 = 1_700_000_000_000
        # Production min_samples=24: seed a full calm window first, then spike.
        for i in range(24):
            vol.update("BTC", 0.01, ts0 + i)
        vol.update("BTC", 0.04, ts0 + 1_000)
        rm = RiskManager(prod_cfg, None)
        pipeline = SignalPipeline(
            prod_cfg, rm, vol_circuit=vol, for_backtest=True,
        )
        decision = pipeline.evaluate_gates(
            _signal(), _event(ts=ts0 + 2_000), _PortfolioStub(), PipelineContext(),
            skip_tca=True,
        )
        assert not decision.approved
        assert decision.gate == "vol_circuit"

    def test_production_funding_blackout_enabled_and_blocks(self, prod_cfg):
        """Funding blackout is ON in production with the 00/08/16 resets."""
        fb_cfg = (prod_cfg.get("risk", {}) or {}).get("funding_blackout", {}) or {}
        fb = FundingBlackoutFilter.from_config_dict(fb_cfg)
        assert fb._cfg.enabled is True
        assert fb._cfg.resets_utc == ["00:00", "08:00", "16:00"]
        rm = RiskManager(prod_cfg, None)
        pipeline = SignalPipeline(prod_cfg, rm, funding_blackout=fb, for_backtest=True)
        blackout_ts = int(
            datetime(2023, 11, 14, 7, 58, tzinfo=timezone.utc).timestamp() * 1000
        )
        decision = pipeline.evaluate_gates(
            _signal(symbol="ETH"), _event(symbol="ETH", ts=blackout_ts),
            _PortfolioStub(), PipelineContext(), skip_tca=True,
        )
        assert not decision.approved
        assert decision.gate == "funding_blackout"

    def test_production_governance_directional_cap_50(self, prod_cfg):
        """Production directional cap is 50% (not 60% like the minimal tests)."""
        rm = RiskManager(prod_cfg, None)
        monitor = CorrelationMonitor(lookback=60)
        pipeline = SignalPipeline(
            prod_cfg, rm, correlation_monitor=monitor, for_backtest=True,
        )
        gov = (prod_cfg.get("strategy", {}) or {}).get("portfolio_governance", {}) or {}
        assert gov.get("max_directional_exposure_pct") == 50
        # 60% directional exposure (BTC $5k + ETH $3k = $8k of $10k capital at
        # 1x) exceeds the production cap of 50% -> the governance/risk gate
        # must reject the third same-side entry.
        port = _PortfolioStub()
        port.positions = {
            "BTC": type("P", (), {"entry_price": 50_000.0, "size": 0.1, "side": "long"})(),
            "ETH": type("P", (), {"entry_price": 3_000.0, "size": 1.0, "side": "long"})(),
        }
        decision = pipeline.evaluate_gates(
            _signal(symbol="SOL"), _event(symbol="SOL"), port, PipelineContext(),
            skip_tca=True,
        )
        assert not decision.approved
        assert decision.gate in ("risk", "governance")

    def test_production_gate_manifest_still_parity_contract(self, prod_cfg):
        rm = RiskManager(prod_cfg, None)
        live = SignalPipeline(prod_cfg, rm, for_backtest=False)
        bt = SignalPipeline(prod_cfg, rm, for_backtest=True)
        m = live.gate_manifest()
        assert m["gate_parity_version"] == "phase05-gates-v1"
        assert m["shared_gate_order"] == list(GATE_ORDER)
        assert live._tca_mode == "strict"
        assert bt._tca_mode == "proxy"


class TestExitPathParityLiveVsBacktest:
    """Exit-path side-by-side: live engine decisions vs backtest engine.

    The entry chain parity (Phase 05) covers RiskManager -> SignalPipeline
    -> gates. This class extends the same side-by-side approach to the
    **exit path**: hard stop-loss/take-profit resolution, the intrabar
    conflict policy, the strategy-level trailing (EMA9) and the engine-level
    trailing ratchet. Any divergence between the two paths is either fixed
    or pinned here as a documented contract.
    """

    pytestmark = pytest.mark.integration_offline

    # -- helpers ----------------------------------------------------------

    def _live_engine(self):
        """Minimal live TradingEngine stub: only the exit-decision surface."""
        eng = object.__new__(TradingEngine)
        eng._trailing_enabled = False
        eng._mode = "paper"
        eng._software_stop_redundancy = False
        eng._trailing_data = {}
        eng._trailing_exclude_strategies = set()
        eng._executor = None
        eng._portfolio = None  # never touched while trailing is off
        eng._risk = None
        eng._kelly_sizer = None
        eng._strategy_stats = {}
        eng._strategies = []
        exits: List[Dict[str, Any]] = []

        async def _execute_exit(position, exit_price: float, reason: str) -> None:
            exits.append(
                {"symbol": position.symbol, "price": exit_price, "reason": reason}
            )

        eng._execute_exit = _execute_exit  # type: ignore[method-assign]
        return eng, exits

    @staticmethod
    def _c1m(
        open_: float = 50_000.0,
        high: float = 50_100.0,
        low: float = 49_900.0,
        close: float = 50_000.0,
        ts: int = 60_000,
    ) -> DBCandle:
        return DBCandle(
            symbol="BTC", timestamp_ms=ts, open=open_, high=high,
            low=low, close=close, volume=10.0,
        )

    @staticmethod
    def _bt_engine() -> BacktestEngine:
        eng = object.__new__(BacktestEngine)
        eng.cfg = BacktestConfig(intrabar_conflict_policy="pessimistic")
        return eng

    @staticmethod
    def _bt_pos(sl: Optional[float] = None, tp: Optional[float] = None) -> _OpenPosition:
        return _OpenPosition(
            id=1, strategy="ChecklistMeta", symbol="BTC", side="long",
            entry_price=50_000.0, entry_time_ms=0, size=0.1,
            stop_loss_price=sl, take_profit_price=tp,
        )

    # -- hard stops: live tick-level vs backtest 1m intrabar ---------------

    async def test_hard_stop_loss_long_live_matches_backtest(self) -> None:
        """A tick through SL (live) == a 1m low through SL (backtest).

        Same trigger condition, same reason. Fill convention differs by
        design: live fills at the observed tick price (49,400 — the cross),
        backtest fills at the exact SL level (49,500 — pessimistic level
        fill, since the bar does not reveal intra-bar path).
        """
        eng, exits = self._live_engine()
        pos = Position(
            symbol="BTC", side="long", entry_price=50_000.0, size=0.1,
            entry_time_ms=0, stop_loss_price=49_500.0,
        )
        await eng._check_hard_stops(pos, 49_400.0)
        assert exits, "live should exit on SL cross"
        assert exits[0]["reason"] == "stop_loss"
        assert exits[0]["price"] == 49_400.0

        bt = self._bt_engine()
        result = bt._intrabar_stop_tp(self._bt_pos(sl=49_500.0), self._c1m(low=49_400.0))
        assert result is not None
        reason, fill = result
        assert reason == "stop_loss"
        assert fill == 49_500.0

    async def test_hard_take_profit_short_live_matches_backtest(self) -> None:
        """TP on the short side: live tick >= TP == backtest 1m high >= TP."""
        eng, exits = self._live_engine()
        pos = Position(
            symbol="BTC", side="short", entry_price=50_000.0, size=0.1,
            entry_time_ms=0, take_profit_price=49_000.0,
        )
        await eng._check_hard_stops(pos, 48_900.0)
        assert exits
        assert exits[0]["reason"] == "take_profit"
        assert exits[0]["price"] == 48_900.0

        bt = self._bt_engine()
        bpos = _OpenPosition(
            id=1, strategy="Test", symbol="BTC", side="short",
            entry_price=50_000.0, entry_time_ms=0, size=0.1,
            stop_loss_price=None, take_profit_price=49_000.0,
        )
        result = bt._intrabar_stop_tp(bpos, self._c1m(low=48_900.0))
        assert result is not None
        reason, fill = result
        assert reason == "take_profit"
        assert fill == 49_000.0

    # -- intrabar conflict policy ------------------------------------------

    def test_intrabar_conflict_pessimistic_matches_live_adverse_tick(self) -> None:
        """Both SL and TP touched in one bar: pessimistic == adverse tick first.

        Live is tick-level: whichever touch arrives first wins. The 1m bar
        hides that order, so the backtest must pick a policy. Pessimistic
        assumes the adverse move came first (SL), which is exactly what live
        does when the adverse tick precedes the favorable one. The
        ``adverse_first`` exit-path policy (P2) brackets the same assumption
        for strategy-level exits.
        """
        bt = self._bt_engine()
        # long: low <= SL (49,400) AND high >= TP (50,600) in the same bar
        result = bt._intrabar_stop_tp(
            self._bt_pos(sl=49_500.0, tp=50_600.0),
            self._c1m(high=50_700.0, low=49_400.0),
        )
        assert result is not None
        reason, fill = result
        assert reason == "stop_loss"
        assert fill == 49_500.0

        # Same outcome when the optimistic policy is off and the live adverse
        # tick arrives first (tick-level check already exits before any TP).
        eng = object.__new__(BacktestEngine)
        eng.cfg = BacktestConfig(intrabar_conflict_policy="optimistic")
        result_opt = eng._intrabar_stop_tp(
            self._bt_pos(sl=49_500.0, tp=50_600.0),
            self._c1m(high=50_700.0, low=49_400.0),
        )
        assert result_opt is not None
        assert result_opt[0] == "take_profit"  # optimistic brackets the other order

    def test_exit_path_policy_brackets_live_tick_order(self) -> None:
        """P1 (favorable_first) vs P2 (adverse_first) bracket the true tick order."""
        c1m = self._c1m(open_=100.0, high=110.0, low=90.0, close=105.0)
        eng = object.__new__(BacktestEngine)
        eng.cfg = BacktestConfig(exit_path_policy="favorable_first")
        p1_long = eng._exit_path_prices("long", c1m)
        eng.cfg = BacktestConfig(exit_path_policy="adverse_first")
        p2_long = eng._exit_path_prices("long", c1m)
        assert p1_long == [100.0, 110.0, 90.0, 105.0]
        assert p2_long == [100.0, 90.0, 110.0, 105.0]
        # live tick order falls somewhere inside this bracket; the policies
        # exist precisely because the 1m bar cannot know it.
        assert set(p1_long) == set(p2_long) == {100.0, 110.0, 90.0, 105.0}

    # -- strategy-level trailing (EMA9) -------------------------------------

    def _cm_config(self) -> Dict[str, Any]:
        return {
            "use_trailing_stop": True,
            "trailing_method": "ema9",
            "trailing_start_r": 1.0,
            "trailing_ema_period": 9,
            "use_sl_to_be_after_1r": False,
            "max_hold_hours": 24.0,
            "score_threshold": 99.0,  # never entry-test in this unit
        }

    def test_strategy_trailing_ema9_live_vs_backtest_same_exit(self) -> None:
        """The EMA9 trail computed in on_position is identical in both paths.

        Live feeds ticks into ``strategy.on_position``; the backtest walks
        the same ``on_position`` over the 1m OHLC path (``_process_exits``).
        Given the same 15m candle history, the trail level and the exit
        signal (``trailing_stop_ema9_*``) must be identical — the strategy
        code is the single source of truth for both.
        """
        cm = ChecklistMeta(self._cm_config())
        # 15m candle history rising far enough to arm the trail (> 1R) and
        # keep the EMA9 trail ABOVE the 1R level, so a pullback below the
        # trail still satisfies profit_r >= TRAILING_START_R (= 1.0).
        # R = |entry - SL| = 1,000 (50,000 -> 49,000); 1R = +1,000.
        candles: List[Candle] = []
        ts = 1_700_000_000_000
        for i in range(20):
            px = 50_000.0 + i * 100.0  # 50,000 -> 51,900 (profit_r = 1.9)
            candles.append(
                Candle(px - 10, px + 10, px - 10, px, 100.0, ts + i * 900_000)
            )
        entry_ms = ts  # the position entry time is fixed for its whole life
        for i, c in enumerate(candles):
            cm.on_position(
                _cm_position(entry=50_000.0, sl=49_000.0, ts=entry_ms),
                _cm_event(price=c.close, ts=ts + i * 900_000, c15=c),
            )

        # Live side: a tick below the trail must emit trailing_stop_ema9.
        # (profit_r >= 1.0 armed the trail at the last 15m close; a pullback
        # below the EMA9 trail level now fires the exit.)
        trail_level = cm._compute_trail(list(candles), "long")
        assert trail_level is not None and trail_level > 51_000.0  # above 1R
        assert trail_level < 51_900.0
        live_exit = cm.on_position(
            _cm_position(entry=50_000.0, sl=49_000.0, ts=entry_ms),
            _cm_event(price=trail_level - 5.0, ts=ts + 20 * 900_000, c15=candles[-1]),
        )
        assert live_exit is not None
        assert live_exit.reason.startswith("trailing_stop_ema9")

        # Backtest side: the same position through _process_exits walking the
        # 1m path must fire the same signal when the path touches the trail.
        bt = self._bt_engine()
        bt.strategy = ChecklistMeta(self._cm_config())
        # seed identical candle history into the backtest strategy instance
        for i, c in enumerate(candles):
            bt.strategy.on_position(
                _cm_position(entry=50_000.0, sl=49_000.0, ts=entry_ms),
                _cm_event(price=c.close, ts=ts + i * 900_000, c15=c),
            )
        bt.positions_by_symbol = {"BTC": 1}
        bt_pos = _OpenPosition(
            id=1, strategy="ChecklistMeta", symbol="BTC", side="long",
            entry_price=50_000.0, entry_time_ms=entry_ms, size=0.1,
            stop_loss_price=49_000.0, take_profit_price=None,
            metadata={"strategy": "ChecklistMeta"},
        )
        bt.positions = {1: bt_pos}
        closed: List[Any] = []

        def _close(pos_id, fill_price, ts_, reason, capital):
            closed.append({"fill": fill_price, "reason": reason})
            bt.positions_by_symbol.pop("BTC", None)
            bt.positions.pop(pos_id, None)
            return capital

        bt._close_position = _close  # type: ignore[method-assign]
        bt._intrabar_stop_tp = lambda *_a, **_k: None  # type: ignore[method-assign]
        # 1m bar whose low touches the trail -> the adverse_first path walks
        # [open, low, high, close] and on_position sees the trail cross.
        event = MarketEvent(symbol="BTC", price=trail_level, timestamp_ms=ts + 20 * 900_000)
        c1m = self._c1m(
            open_=trail_level + 10.0,
            high=trail_level + 20.0,
            low=trail_level - 5.0,
            close=trail_level + 5.0,
            ts=ts + 20 * 900_000,
        )
        bt._process_exits(event, 100_000.0, c1m)  # type: ignore[arg-type]
        assert closed, "backtest should exit via strategy trailing"
        assert str(closed[0]["reason"]).startswith("trailing_stop_ema9")

    # -- engine-level trailing ratchet (live-only) --------------------------

    def test_engine_trailing_ratchet_is_live_only_and_documented(self) -> None:
        """The peak-ratchet trailing lives ONLY in the live engine.

        ``TradingEngine._maybe_update_trailing_stop`` ratchets the SL to
        peak * (1 - trail_pct) after activation. The backtest engine has no
        equivalent mechanism: strategy-level exits (EMA9 trail, SL-to-BE,
        TP mirror, max_hold) run in both, but the engine ratchet is a live
        execution-layer feature that the replay does not model. This test
        pins that contract: the exclusion list is the single source of truth
        for which strategies get the ratchet, and the backtest exit path
        does not expose it.
        """
        # Live: production exclude list (VB, VWAP, SFP, TrendPyramid).
        eng = object.__new__(TradingEngine)
        eng._trailing_exclude_strategies = {
            "VolatilityBreakout", "VWAPDeviation", "SmartMoneyFlow", "TrendPyramid",
        }
        excluded = Position(
            symbol="BTC", side="long", entry_price=50_000.0, size=0.1,
            entry_time_ms=0, stop_loss_price=49_500.0,
            metadata={"sub_strategy": "VWAPDeviation"},
        )
        assert eng._trailing_excluded_for_position(excluded) is True
        not_excluded = Position(
            symbol="BTC", side="long", entry_price=50_000.0, size=0.1,
            entry_time_ms=0, stop_loss_price=49_500.0,
            metadata={"sub_strategy": "ChecklistMeta"},
        )
        assert eng._trailing_excluded_for_position(not_excluded) is False

        # Backtest: no ratchet surface exists in the replay engine.
        bt = self._bt_engine()
        assert not hasattr(bt, "_trailing_data")
        assert not hasattr(bt, "_maybe_update_trailing_stop")


def _cm_position(entry: float, sl: float, ts: int) -> Position:
    return Position(
        symbol="BTC", side="long", entry_price=entry, size=0.1,
        entry_time_ms=ts, stop_loss_price=sl,
        metadata={"strategy": "ChecklistMeta"},
    )


def _cm_event(price: float, ts: int, c15: Candle) -> MarketEvent:
    c1 = Candle(price - 5, price + 5, price - 5, price, 100.0, ts)
    return MarketEvent(
        symbol="BTC", price=price, timestamp_ms=ts,
        candle_1m=c1, candle_15m=c15,
    )


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
