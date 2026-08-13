"""Phase 07 behavioral tests — research DB, coverage, NULL volumes, MFE/MAE."""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.data_contract import (
    DataContractError,
    assert_data_contract_or_raise,
    evaluate_data_contract,
)
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.backtest.mfe_mae import (
    ExcursionTracker,
    POST_EXIT_KEYS,
    compute_intrade_excursion_fields,
    compute_post_exit_fields,
    enrich_trades_post_exit,
    strip_post_exit_from_trades,
)
from src.backtest.strategy_feed_requirements import (
    RequiredFeeds,
    TIER_A_OHLC,
    TIER_A_CVD,
    TIER_B_MISSING,
    resolve_strategy_tier,
    FeedAvailability,
)
from src.data.candle_backfill import kline_to_candle, split_taker_volume_from_kline
from src.data.coverage_audit import audit_candle_series
from src.data.database import Candle, FundingRecord
from src.data.hl_research_backfill import hl_snapshot_to_candle
from src.data.research_database import ResearchDatabase
from src.data.series_metadata import SeriesMetadata, SOURCE_HL_CANDLE_SNAPSHOT, VENUE_HYPERLIQUID
from src.strategies.base import MarketEvent, Strategy, Signal, Position, ExitSignal
from src.utils.config import Config, load_config
from src.utils.helpers import optional_float
import pytest

pytestmark = pytest.mark.integration_offline


def _research_db() -> ResearchDatabase:
    path = Path(tempfile.gettempdir()) / f"test_research_{uuid.uuid4().hex}.db"
    return ResearchDatabase(path)


def _seed_hl_candles(
    db: ResearchDatabase,
    symbol: str,
    start_ms: int,
    n_bars: int,
    *,
    gap_at: int | None = None,
) -> None:
    meta = SeriesMetadata.hl_candles(taker_split=False)
    candles: List[Candle] = []
    for i in range(n_bars):
        if gap_at is not None and i == gap_at:
            ts = start_ms + i * 60_000 + 180_000
        else:
            ts = start_ms + i * 60_000
        row = {
            "T": ts,
            "t": ts - 60_000,
            "o": "100",
            "h": "101",
            "l": "99",
            "c": "100.5",
            "v": "10",
            "n": 5,
        }
        candles.append(hl_snapshot_to_candle(row, symbol))
    db.save_research_candles(candles, "1m", meta)


def _cfg_research() -> Config:
    data: Dict[str, Any] = {
        "research": {
            "refuse_insufficient_feeds": True,
            "require_hl_venue": True,
            "strict_mode": True,
            "min_coverage_pct": 90.0,
        },
        "backtest": {
            "replay_data_quality": {
                "min_coverage_pct": 90.0,
                "require_funding": False,
            },
        },
        "risk": {
            "max_positions": 5,
            "max_daily_trades": 0,
            "per_trade_risk_pct": 1.0,
            "max_position_size_pct": 5.0,
            "leverage_max": 10.0,
            "volatility_circuit_breaker": {"enabled": False},
            "funding_blackout": {"enabled": False},
        },
        "strategy": {
            "kelly": {"enabled": False},
            "cooldown": {"base_minutes": 30, "max_minutes": 120, "multiplier": 2.0},
            "portfolio_governance": {
                "max_directional_exposure_pct": 100.0,
                "max_sector_exposure_pct": 100.0,
            },
        },
        "execution": {"tca_enabled": False},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(data, fh)
        return load_config(fh.name)


class _OneShotStrategy(Strategy):
    def __init__(self) -> None:
        self._fired = False
        self._entered = False

    @property
    def name(self) -> str:
        return "VolatilityBreakout"

    def on_data(self, event: MarketEvent) -> Signal | None:
        if self._fired:
            return None
        self._fired = True
        return Signal(
            symbol=event.symbol,
            side="long",
            confidence=0.9,
            strategy=self.name,
            size_pct=0.01,
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            reason="test",
        )

    def on_position(self, position: Position, event: MarketEvent) -> ExitSignal | None:
        if not self._entered:
            self._entered = True
            return None
        return ExitSignal(
            strategy=self.name,
            symbol=position.symbol,
            side="close",
            confidence=1.0,
            reason="test_exit",
        )


def test_optional_float_preserves_none() -> None:
    assert optional_float(None) is None
    assert optional_float("") is None
    assert optional_float(0.0) == 0.0


def test_binance_taker_split_null_when_field_missing() -> None:
    k_full = [0, "1", "2", "3", "4", "100", 0, 0, 50, "60"]
    buy, sell, ok = split_taker_volume_from_kline(k_full, 100.0)
    assert ok is True
    assert buy == 60.0 and sell == 40.0

    k_short = [0, "1", "2", "3", "4", "100", 0, 0, 50]
    buy2, sell2, ok2 = split_taker_volume_from_kline(k_short, 100.0)
    assert ok2 is False
    assert buy2 is None and sell2 is None

    c = kline_to_candle(k_full, "BTC")
    assert c.buy_volume == 60.0
    c2 = kline_to_candle(k_short, "BTC")
    assert c2.buy_volume is None and c2.sell_volume is None


def test_hl_candle_null_taker_volumes() -> None:
    row = {
        "T": 1_700_000_060_000,
        "t": 1_700_000_000_000,
        "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "3", "n": 2,
    }
    c = hl_snapshot_to_candle(row, "BTC")
    assert c.funding_rate is None
    assert c.oi_total is None
    assert c.buy_volume is None and c.sell_volume is None


def test_strategy_fidelity_ohlc_vs_cvd() -> None:
    ohlc_avail = FeedAvailability(
        symbol="BTC",
        hl_candles=True,
        hl_venue=True,
        candle_coverage_pct=0.99,
    )
    ohlc = resolve_strategy_tier("VolatilityBreakout", ohlc_avail)
    assert ohlc.tier_a_eligible and ohlc.tier == TIER_A_OHLC

    cvd = resolve_strategy_tier("CVDOrderFlow", ohlc_avail)
    assert not cvd.tier_a_eligible
    assert "taker_split" in cvd.missing_feeds
    assert cvd.tier == TIER_B_MISSING


def _liq_avail(provenance: str) -> FeedAvailability:
    return FeedAvailability(
        symbol="BTC",
        hl_candles=True,
        hl_venue=True,
        liquidation=True,
        liquidation_provenance=provenance,
        candle_coverage_pct=0.99,
    )


def test_liquidation_catcher_tier_a_with_real_provenance() -> None:
    avail = _liq_avail("real")
    fid = resolve_strategy_tier("LiquidationCatcher", avail)
    assert fid.tier_a_eligible
    assert fid.tier == TIER_A_OHLC
    assert fid.liquidation_provenance == "real"
    assert fid.missing_feeds == []


def test_liquidation_catcher_tier_b_when_proxy_only() -> None:
    """Liquidation rows exist but only as proxy synthesis — LiquidationCatcher
    requires real provenance, so this must NOT be production-grade."""
    from src.backtest.strategy_feed_requirements import TIER_B_LIQUIDATION_PROXY

    avail = _liq_avail("proxy")
    fid = resolve_strategy_tier("LiquidationCatcher", avail)
    assert not fid.tier_a_eligible
    assert fid.tier == TIER_B_LIQUIDATION_PROXY
    assert fid.liquidation_provenance == "proxy"
    assert "liquidation_proxy_only" in fid.missing_feeds
    # has() must also treat proxy-only as not-present
    assert not avail.has(RequiredFeeds.LIQUIDATION)


def test_liquidation_catcher_mixed_provenance_is_tier_a() -> None:
    """Mixed real+proxy replay still counts as Tier A (real rows exist)."""
    avail = _liq_avail("mixed")
    fid = resolve_strategy_tier("LiquidationCatcher", avail)
    assert fid.tier_a_eligible
    assert fid.tier == TIER_A_OHLC
    assert fid.liquidation_provenance == "mixed"


def test_liquidation_absent_stays_missing() -> None:
    avail = FeedAvailability(
        symbol="BTC", hl_candles=True, hl_venue=True, candle_coverage_pct=0.99,
    )
    fid = resolve_strategy_tier("LiquidationCatcher", avail)
    assert not fid.tier_a_eligible
    assert fid.tier == TIER_B_MISSING
    assert "liquidation" in fid.missing_feeds
    assert fid.liquidation_provenance == "none"


def test_liquidation_provenance_reported_in_to_dict() -> None:
    fid = resolve_strategy_tier("LiquidationCatcher", _liq_avail("proxy"))
    d = fid.to_dict()
    assert d["liquidation_provenance"] == "proxy"
    assert d["fidelity_tier"] == "tier_b_liquidation_proxy_not_production"
    # non-liquidation strategies never carry the field
    ohlc = resolve_strategy_tier("VolatilityBreakout", _liq_avail("real"))
    assert "liquidation_provenance" not in ohlc.to_dict()


def test_probe_classifies_real_vs_proxy_rows() -> None:
    """probe_feed_availability classifies replayed liquidation rows by source:
    real venues → real/mixed, proxy-only → proxy."""
    from src.backtest.strategy_feed_requirements import probe_feed_availability

    from src.data.database import LiquidationRecord

    db = _research_db()
    start = 1_700_000_000_000
    _seed_hl_candles(db, "BTC", start, 30)
    db.save_liquidation(LiquidationRecord(
        symbol="BTC", timestamp_ms=start + 60_000,
        notional_usd=2_000_000.0, side="long", source="proxy",
    ))

    avail = probe_feed_availability(
        db, "BTC", start_ms=start, end_ms=start + 29 * 60_000,
    )
    assert avail.liquidation is True
    assert avail.liquidation_provenance == "proxy"
    assert not avail.liquidation_real

    # add a real-venue row → mixed
    db.save_liquidation(LiquidationRecord(
        symbol="BTC", timestamp_ms=start + 120_000,
        notional_usd=3_000_000.0, side="short", source="okx",
    ))
    avail2 = probe_feed_availability(
        db, "BTC", start_ms=start, end_ms=start + 29 * 60_000,
    )
    assert avail2.liquidation_provenance == "mixed"
    assert avail2.liquidation_real is True
    db.close()


def test_run_manifest_reports_effective_tier_per_strategy() -> None:
    """build_run_manifest carries per-strategy effective fidelity + liquidation
    provenance when the data contract summary provides it."""
    from src.backtest.run_manifest import build_run_manifest

    summary = {
        "data_source": "sqlite_hl_research",
        "venue": "hyperliquid",
        "fidelity_tier": "tier_a_hl_ohlc",
        "refused": False,
        "degraded": False,
        "reasons": [],
        "coverage": {},
        "strategy_fidelity": {
            "LiquidationCatcher": {
                "strategy": "LiquidationCatcher",
                "fidelity_tier": "tier_b_liquidation_proxy_not_production",
                "required_feeds": ["hl_candles", "liquidation"],
                "missing_feeds": ["liquidation_proxy_only"],
                "tier_a_eligible": False,
                "liquidation_provenance": "proxy",
            },
            "VolatilityBreakout": {
                "strategy": "VolatilityBreakout",
                "fidelity_tier": "tier_a_hl_ohlc",
                "required_feeds": ["hl_candles"],
                "missing_feeds": [],
                "tier_a_eligible": True,
            },
        },
        "research_protocol_version": "v1",
    }
    m = build_run_manifest(
        Config({}),
        symbols=["BTC"],
        data_source="sqlite_hl_research",
        tca_mode="proxy",
        data_contract_summary=summary,
    )
    sf = m["strategy_fidelity"]
    assert sf["LiquidationCatcher"]["fidelity_tier"] == \
        "tier_b_liquidation_proxy_not_production"
    assert sf["LiquidationCatcher"]["liquidation_provenance"] == "proxy"
    assert not sf["LiquidationCatcher"]["tier_a_eligible"]
    assert sf["VolatilityBreakout"]["fidelity_tier"] == "tier_a_hl_ohlc"
    assert sf["VolatilityBreakout"]["tier_a_eligible"] is True
    # the data_contract subtree still carries the full per-strategy detail
    assert m["data_contract"]["strategy_fidelity"]["LiquidationCatcher"][
        "liquidation_provenance"] == "proxy"


def test_strict_research_refuses_funding_strategy_without_feeds() -> None:
    db = _research_db()
    start = 1_700_000_000_000
    _seed_hl_candles(db, "BTC", start, 60)
    cfg = _cfg_research()
    result = evaluate_data_contract(
        db,
        ["BTC"],
        start_ms=start,
        end_ms=start + 59 * 60_000,
        config=cfg,
        active_strategies=["VolatilityBreakout", "FundingArbitrage"],
        refuse_on_fail=True,
        strict_research=True,
    )
    assert result.refused
    assert any("FundingArbitrage" in r for r in result.reasons)
    vb = result.strategy_fidelity.get("VolatilityBreakout")
    assert vb is not None and vb.tier_a_eligible


def test_post_exit_not_in_execution_trades() -> None:
    db = _research_db()
    start = 1_700_000_000_000
    meta = SeriesMetadata.hl_candles()
    candles: List[Candle] = []
    for i in range(20):
        px = 100.0 + i * 0.5
        ts = start + i * 60_000
        candles.append(
            Candle("BTC", ts, px, px + 1, px - 1, px, 10.0)
        )
    db.save_research_candles(candles, "1m", meta)
    for tf in ("5m", "15m", "1h"):
        db.save_research_candles(candles, tf, meta)

    cfg = _cfg_research()
    cfg._data["research"]["refuse_insufficient_feeds"] = False
    cfg._data["research"]["strict_mode"] = False

    engine = BacktestEngine(
        database=db,
        strategy=_OneShotStrategy(),
        config=BacktestConfig(
            use_risk_manager=False,
            use_volatility_circuit=False,
            use_funding_blackout=False,
            use_microstructure_proxy=False,
            use_external_feeds_replay=False,
            max_daily_trades=0,
        ),
        symbols=["BTC"],
        risk_config=cfg,
    )
    result = engine.run(start_ms=start, end_ms=start + 19 * 60_000)
    for t in result["trades"]:
        for key in POST_EXIT_KEYS:
            assert key not in t, f"post_exit leaked into execution trades: {key}"
    analytics = result.get("trade_analytics", [])
    assert analytics
    assert any(a.get("post_exit_15m_pct") is not None for a in analytics)


def test_intrade_mfe_without_post_exit() -> None:
    tracker = ExcursionTracker(100.0, 0, "long", 200.0)
    tracker.update_bar(110.0, 99.0, 60_000)
    fields = compute_intrade_excursion_fields(
        tracker, size=10.0, net_pnl_usd=15.0, fees_paid=2.0,
    )
    assert "mfe_r" in fields
    assert "post_exit_15m_pct" not in fields


def test_research_db_separate_no_prune() -> None:
    db = _research_db()
    _seed_hl_candles(db, "BTC", 1_700_000_000_000, 5)
    before = db.count_candles("BTC", "1m")
    assert db.prune_old_data(days=1) == {}
    assert db.count_candles("BTC", "1m") == before
    sample = db.get_candle_metadata_sample("BTC")
    assert sample is not None
    assert sample["venue"] == VENUE_HYPERLIQUID


def test_data_contract_refuses_insufficient_coverage() -> None:
    db = _research_db()
    start = 1_700_000_000_000
    _seed_hl_candles(db, "BTC", start, 3)
    cfg = _cfg_research()
    cfg._data["research"]["strict_mode"] = False
    result = evaluate_data_contract(
        db,
        ["BTC"],
        start_ms=start,
        end_ms=start + 10 * 60_000,
        config=cfg,
        active_strategies=["VolatilityBreakout"],
        refuse_on_fail=True,
    )
    try:
        assert_data_contract_or_raise(result)
        raise AssertionError("expected DataContractError")
    except DataContractError:
        pass


def run_all() -> None:
    tests = [
        test_optional_float_preserves_none,
        test_binance_taker_split_null_when_field_missing,
        test_hl_candle_null_taker_volumes,
        test_strategy_fidelity_ohlc_vs_cvd,
        test_strict_research_refuses_funding_strategy_without_feeds,
        test_post_exit_not_in_execution_trades,
        test_intrade_mfe_without_post_exit,
        test_research_db_separate_no_prune,
        test_data_contract_refuses_insufficient_coverage,
    ]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nAll {len(tests)} Phase 07 tests passed.")


if __name__ == "__main__":
    run_all()
