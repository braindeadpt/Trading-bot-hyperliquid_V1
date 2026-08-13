"""Production-config parity for the three remaining gate families.

Phase 05 pinned the shared RiskManager -> SignalPipeline -> gates chain
against the REAL ``config/settings.yaml`` (see
``TestParityAgainstProductionConfig`` in ``test_backtest_live_parity.py``).
That class covered risk/chase/cooldown/correlation/vol-circuit/funding-
blackout. This module extends the same side-by-side approach to the three
gates that only make sense when run against the production thresholds:

  1. **Feed health** (live ``feed_health``) vs **replay data quality**
     (backtest ``replay_data_quality``) — the live gate reads per-symbol
     ``SymbolFeedHealth`` / fleet health; the replay gate substitutes
     continuity + funding/OI freshness. The substitution contract is
     pinned by ``gate_manifest()``.

  2. **TCA strict vs proxy** — live ``execution.tca_mode: strict`` rejects
     entries without an L2 book; backtest ``backtest.tca_mode: proxy``
     allows candle-only paper slippage (Tier B). The same ``passes_tca_check``
     runs in both; only the L2 requirement differs.

  3. **Reconciliation** — live-only (exchange-as-source-of-truth). Blocks
     entries on stale/halt/drift/failing with the production thresholds
     (interval 60s, stale 120s, ADOPT_AND_PROTECT, HALT on mismatch).

No network, no live DB: everything is built from ``config/settings.yaml``
plus in-memory fakes.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROD_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"

from src.backtest.replay_data_quality import (  # noqa: E402
    ReplayDataQualityGate,
    SymbolReplayAudit,
)
from src.core.portfolio import PortfolioState  # noqa: E402
from src.core.reconciliation import ExchangeReconciler  # noqa: E402
from src.core.risk_manager import RiskManager  # noqa: E402
from src.core.signal_pipeline import (  # noqa: E402
    LIVE_ONLY_GATES,
    PipelineContext,
    SignalPipeline,
)
from src.core.tca import passes_tca_check  # noqa: E402
from src.data.market_data_health import compute_feed_status  # noqa: E402
from src.strategies.base import MarketEvent, Signal  # noqa: E402
from src.strategies.indicators import Candle  # noqa: E402
from src.utils.config import Config, load_config  # noqa: E402

pytestmark = pytest.mark.integration_offline


def _signal(
    symbol: str = "BTC",
    *,
    side: str = "long",
    strategy: str = "VWAPDeviation",
    take_profit_pct: float = 0.024,
    stop_loss_pct: float = 0.012,
    size_pct: float = 0.01,
) -> Signal:
    return Signal(
        strategy=strategy,
        symbol=symbol,
        side=side,
        confidence=0.8,
        size_pct=size_pct,
        entry_price=50_000.0,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        reason="test",
        metadata={"sub_strategy": strategy, "atr_pct": 0.006},
    )


def _event(symbol: str = "BTC", ts: int = 1_700_000_000_000) -> MarketEvent:
    c = Candle(
        open=50_000.0, high=50_050.0, low=49_950.0, close=50_000.0,
        volume=100.0, timestamp_ms=ts,
    )
    return MarketEvent(
        symbol=symbol, price=50_000.0, timestamp_ms=ts,
        candle_1m=c, candle_15m=c, adx_14=22.0,
    )


class _PortfolioStub:
    def __init__(self) -> None:
        self.positions: Dict[str, Any] = {}
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.current_capital = 10_000.0

    def get_max_drawdown(self) -> float:
        return 0.0


def _audit(
    symbol: str = "BTC",
    *,
    funding_available: bool = True,
    oi_available: bool = False,
) -> SymbolReplayAudit:
    return SymbolReplayAudit(
        symbol=symbol,
        coverage_pct=1.0,
        max_gap_ms=60_000,
        bar_count=100,
        expected_bars=100,
        funding_available=funding_available,
        oi_available=oi_available,
    )


def _reconciler_from_prod(cfg: Config) -> ExchangeReconciler:
    recon_cfg = cfg.get("reconciliation", {}) or {}
    return ExchangeReconciler(
        live_client=MagicMock(),
        portfolio=PortfolioState(10_000),
        orphan_exchange_policy=str(
            recon_cfg.get("orphan_exchange_policy", "ADOPT_AND_PROTECT")
        ),
        mismatch_policy=str(recon_cfg.get("mismatch_policy", "HALT")),
        stale_threshold_sec=float(recon_cfg.get("stale_threshold_sec", 120)),
    )


class TestFeedHealthGateParityProduction:
    """Live ``feed_health`` vs backtest ``replay_data_quality`` under prod config."""

    @pytest.fixture
    def prod_cfg(self) -> Config:
        assert os.path.exists(PROD_SETTINGS_PATH), PROD_SETTINGS_PATH
        return load_config(PROD_SETTINGS_PATH)

    def test_production_feed_health_gates_are_on(self, prod_cfg) -> None:
        md = prod_cfg.get("market_data", {}) or {}
        # block_entries_on_stale (with legacy alias fallback) must be truthy.
        assert bool(md.get("block_entries_on_stale", md.get("block_entries_on_red", True))) is True
        assert md.get("block_entries_on_ws_unhealthy") is True
        assert md.get("min_exchanges_for_green") == 2
        assert md.get("funding_stale_max_sec") == 300

    def test_production_replay_quality_mirrors_feed_health(self, prod_cfg) -> None:
        gate = ReplayDataQualityGate.from_config(prod_cfg)
        assert gate._require_funding is True
        assert gate._require_oi is False
        assert gate._max_funding_stale == 300_000
        # Live has no window-coverage / multi-day-gap entry kill; the replay
        # gate runs in parity mode so missing bars are simply absent.
        assert gate._parity_mode is True

    def test_manifest_substitutes_feed_health_with_replay_quality(self, prod_cfg) -> None:
        rm = RiskManager(prod_cfg, None)
        m = SignalPipeline(prod_cfg, rm, for_backtest=False).gate_manifest()
        assert m["replay_substitutes"] == {"feed_health": "replay_data_quality"}

    def test_live_feed_health_blocks_via_feed_block_fn(self, prod_cfg) -> None:
        """A live feed_block_fn rejection surfaces as gate ``feed_health``."""
        rm = RiskManager(prod_cfg, None)
        live = SignalPipeline(
            prod_cfg, rm, for_backtest=False,
            feed_block_fn=lambda sym: f"feed_red:{sym}",
        )
        decision = live.evaluate_gates(
            _signal(), _event(), _PortfolioStub(), PipelineContext(), skip_tca=True,
        )
        assert not decision.approved
        assert decision.gate == "feed_health"
        assert decision.reason == "feed_red:BTC"

    def test_backtest_replay_quality_blocks_on_stale_funding(self, prod_cfg) -> None:
        """Same position in the chain, but the replay substitute fires on
        stale funding instead of a red feed — gate ``replay_data_quality``."""
        gate = ReplayDataQualityGate.from_config(prod_cfg)
        rm = RiskManager(prod_cfg, None)
        bt = SignalPipeline(prod_cfg, rm, for_backtest=True, replay_quality=gate)
        ctx = PipelineContext()
        ctx.replay_audit["BTC"] = _audit(funding_available=True)
        ts = 1_700_000_000_000
        ctx.funding_ts_at["BTC"] = ts - 600_000  # 10 min old > 300s threshold
        decision = bt.evaluate_gates(
            _signal(), _event(ts=ts), _PortfolioStub(), ctx, skip_tca=True,
        )
        assert not decision.approved
        assert decision.gate == "replay_data_quality"
        assert "replay_funding_stale" in decision.reason

    def test_backtest_replay_quality_allows_fresh_funding(self, prod_cfg) -> None:
        gate = ReplayDataQualityGate.from_config(prod_cfg)
        rm = RiskManager(prod_cfg, None)
        bt = SignalPipeline(prod_cfg, rm, for_backtest=True, replay_quality=gate)
        ctx = PipelineContext()
        ctx.replay_audit["BTC"] = _audit(funding_available=True)
        ts = 1_700_000_000_000
        ctx.funding_ts_at["BTC"] = ts - 30_000  # 30s old -> fresh
        decision = bt.evaluate_gates(
            _signal(), _event(ts=ts), _PortfolioStub(), ctx, skip_tca=True,
        )
        assert decision.approved

    def test_feed_health_both_venues_down_is_red(self) -> None:
        status = compute_feed_status(
            cex_ok=False, cex_stale=True, cex_exchange_count=0,
            min_exchanges=2, hl_ok=False, hl_stale=True,
        )
        assert status == "red"

    def test_feed_health_cex_ok_hl_ok_is_green(self) -> None:
        status = compute_feed_status(
            cex_ok=True, cex_stale=False, cex_exchange_count=2,
            min_exchanges=2, hl_ok=True, hl_stale=False,
        )
        assert status == "green"

    def test_feed_health_cex_ok_but_hl_stale_is_yellow(self) -> None:
        # One healthy venue but a stale HL feed downgrades to yellow — not
        # green — which is the production contract that keeps entries
        # cautious during partial outages.
        status = compute_feed_status(
            cex_ok=True, cex_stale=False, cex_exchange_count=2,
            min_exchanges=2, hl_ok=True, hl_stale=True,
        )
        assert status == "yellow"


class TestTCAStrictProxyParityProduction:
    """Live TCA strict (needs L2) vs backtest proxy (candle-only) under prod config."""

    @pytest.fixture
    def prod_cfg(self) -> Config:
        assert os.path.exists(PROD_SETTINGS_PATH), PROD_SETTINGS_PATH
        return load_config(PROD_SETTINGS_PATH)

    def test_live_is_strict_backtest_is_proxy(self, prod_cfg) -> None:
        assert prod_cfg.get("execution.tca_mode") == "strict"
        assert prod_cfg.get("backtest.tca_mode") == "proxy"
        rm = RiskManager(prod_cfg, None)
        live = SignalPipeline(prod_cfg, rm, for_backtest=False)
        bt = SignalPipeline(prod_cfg, rm, for_backtest=True)
        assert live._tca_mode == "strict"
        assert bt._tca_mode == "proxy"

    def test_live_strict_rejects_without_l2_book(self, prod_cfg) -> None:
        live = SignalPipeline(prod_cfg, RiskManager(prod_cfg, None), for_backtest=False)
        decision = live.evaluate_tca_gate(_signal(), has_orderbook=False)
        assert not decision.approved
        assert decision.gate == "tca"
        assert decision.reason == "tca_strict_no_l2_book"

    def test_live_strict_allows_with_l2_book_when_edge_covers_cost(self, prod_cfg) -> None:
        live = SignalPipeline(prod_cfg, RiskManager(prod_cfg, None), for_backtest=False)
        decision = live.evaluate_tca_gate(_signal(), has_orderbook=True)
        assert decision.approved

    def test_backtest_proxy_allows_without_l2_book(self, prod_cfg) -> None:
        bt = SignalPipeline(prod_cfg, RiskManager(prod_cfg, None), for_backtest=True)
        decision = bt.evaluate_tca_gate(_signal(), has_orderbook=False)
        assert decision.approved

    def test_production_tca_fees_and_buffer_are_active(self, prod_cfg) -> None:
        assert prod_cfg.get("risk.taker_fee_pct") == 0.045
        assert prod_cfg.get("risk.paper_slippage_pct") == 0.02
        assert prod_cfg.get("execution.min_edge_buffer_pct") == 0.05

    def test_production_proxy_tca_rejects_thin_edge(self) -> None:
        # round-trip cost = 2*4.5bp fee + 2*2bp slip = 13bp; buffer 5bp -> 18bp.
        # A 10bp take-profit edge fails.
        ok, reason = passes_tca_check(
            _signal(take_profit_pct=0.001), 0.00045, 0.0002, 0.0005,
        )
        assert not ok
        assert "TCA reject" in reason

    def test_production_proxy_tca_allows_healthy_edge(self) -> None:
        ok, _reason = passes_tca_check(
            _signal(take_profit_pct=0.005), 0.00045, 0.0002, 0.0005,
        )
        assert ok


class TestReconciliationGateParityProduction:
    """Reconciliation is live-only; blocks entries on stale/halt/drift/failing."""

    @pytest.fixture
    def prod_cfg(self) -> Config:
        assert os.path.exists(PROD_SETTINGS_PATH), PROD_SETTINGS_PATH
        return load_config(PROD_SETTINGS_PATH)

    def test_production_reconciliation_config_active(self, prod_cfg) -> None:
        recon = prod_cfg.get("reconciliation", {}) or {}
        assert recon.get("enabled") is True
        assert recon.get("interval_sec") == 60
        assert recon.get("stale_threshold_sec") == 120
        assert recon.get("orphan_exchange_policy") == "ADOPT_AND_PROTECT"
        assert recon.get("mismatch_policy") == "HALT"
        assert recon.get("block_entries_when_stale") is True

    def test_reconciliation_is_live_only_and_unreplayed(self, prod_cfg) -> None:
        assert "reconciliation_stale" in LIVE_ONLY_GATES
        rm = RiskManager(prod_cfg, None)
        m = SignalPipeline(prod_cfg, rm, for_backtest=False).gate_manifest()
        assert "reconciliation_stale" in m["live_only_gates"]
        # No replay substitute: reconciliation has no backtest equivalent.
        assert "reconciliation_stale" not in m["replay_substitutes"]

    def test_fresh_reconciler_is_stale_and_blocks(self, prod_cfg) -> None:
        recon = _reconciler_from_prod(prod_cfg)
        assert recon.is_stale()
        assert recon.entries_blocked()
        assert recon.block_reason() == "reconciliation_stale"

    def test_recent_successful_pass_unblocks(self, prod_cfg) -> None:
        recon = _reconciler_from_prod(prod_cfg)
        recon._health.last_success_ts = time.time()
        recon._health.consecutive_failures = 0
        recon._health.stale = False
        assert not recon.is_stale()
        assert not recon.entries_blocked()
        assert recon.block_reason() is None

    def test_reconciler_blocks_on_drift(self, prod_cfg) -> None:
        recon = _reconciler_from_prod(prod_cfg)
        recon._health.last_success_ts = time.time()
        recon._health.drift_symbols = ["BTC"]
        assert recon.entries_blocked()
        assert recon.block_reason() == "reconciliation_drift:BTC"

    def test_reconciler_blocks_on_halt(self, prod_cfg) -> None:
        recon = _reconciler_from_prod(prod_cfg)
        recon._health.last_success_ts = time.time()
        recon._halt("mismatch:BTC")
        assert recon.entries_blocked()
        assert recon.block_reason() == "mismatch:BTC"

    def test_reconciler_blocks_on_consecutive_failures(self, prod_cfg) -> None:
        # The defensive branch: 3+ consecutive failures without a recorded halt.
        recon = _reconciler_from_prod(prod_cfg)
        recon._health.last_success_ts = time.time()
        recon._health.stale = False
        recon._health.consecutive_failures = 3
        assert recon.entries_blocked()
        assert recon.block_reason() == "reconciliation_failing"

    def test_live_feed_block_fn_carries_reconciliation_reason(self, prod_cfg) -> None:
        """The engine's feed_block_fn (``_entry_feed_block_reason``) folds the
        reconciliation gate into the live ``feed_health`` gate — a
        reconciliation_stale reason surfaces under gate ``feed_health``."""
        rm = RiskManager(prod_cfg, None)
        live = SignalPipeline(
            prod_cfg, rm, for_backtest=False,
            feed_block_fn=lambda sym: "reconciliation_stale",
        )
        decision = live.evaluate_gates(
            _signal(), _event(), _PortfolioStub(), PipelineContext(), skip_tca=True,
        )
        assert not decision.approved
        assert decision.gate == "feed_health"
        assert decision.reason == "reconciliation_stale"
