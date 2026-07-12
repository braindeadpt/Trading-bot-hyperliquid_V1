"""Backtest data-contract gate — per-strategy fidelity (Phase 07)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from src.backtest.strategy_feed_requirements import (
    REFUSED,
    StrategyFidelity,
    evaluate_strategies_fidelity,
    strategies_requiring_funding_oi,
)
from src.data.coverage_audit import (
    FeedCoverageReport,
    audit_auxiliary_feed,
    audit_candle_series,
    summarize_coverage_reports,
)
from src.data.database import Database
from src.data.research_database import ResearchDatabase
from src.data.series_metadata import (
    RESEARCH_PROTOCOL_VERSION,
    SOURCE_BINANCE_KLINES,
    SOURCE_HL_CANDLE_SNAPSHOT,
    VENUE_BINANCE,
    VENUE_HYPERLIQUID,
)
from src.utils.config import Config
from src.utils.helpers import safe_float


class DataContractError(Exception):
    """Raised when mandatory feeds do not cover the backtest window."""


@dataclass
class DataContractResult:
    """Outcome of pre-backtest data contract evaluation."""

    data_source: str
    venue: str
    fidelity_tier: str
    refused: bool
    degraded: bool
    reports: List[FeedCoverageReport] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    strategy_fidelity: Dict[str, StrategyFidelity] = field(default_factory=dict)

    def summary(self) -> Dict[str, Any]:
        cov = summarize_coverage_reports(self.reports)
        return {
            "data_source": self.data_source,
            "venue": self.venue,
            "fidelity_tier": self.fidelity_tier,
            "refused": self.refused,
            "degraded": self.degraded,
            "reasons": list(self.reasons),
            "coverage": cov,
            "strategy_fidelity": {
                k: v.to_dict() for k, v in self.strategy_fidelity.items()
            },
            "research_protocol_version": RESEARCH_PROTOCOL_VERSION,
        }


def _detect_venue(db: Database, symbol: str) -> tuple[str, str]:
    if isinstance(db, ResearchDatabase):
        sample = db.get_candle_metadata_sample(symbol, "1m", limit=1)
        if sample and sample.get("venue"):
            venue = str(sample["venue"])
            if venue == VENUE_HYPERLIQUID:
                return "sqlite_hl_research", venue
            if venue == VENUE_BINANCE:
                return "sqlite_binance_proxy", venue
            return f"sqlite_{venue}", venue
    return "sqlite_candles", "unknown"


def evaluate_data_contract(
    db: Database,
    symbols: Sequence[str],
    *,
    start_ms: Optional[int],
    end_ms: Optional[int],
    config: Optional[Config] = None,
    active_strategies: Optional[Sequence[str]] = None,
    require_hl_venue: bool = False,
    min_coverage_pct: float = 0.95,
    refuse_on_fail: Optional[bool] = None,
    strict_research: bool = False,
) -> DataContractResult:
    """Audit feeds and assign per-strategy fidelity tiers."""
    cfg = config or Config({})
    qc = cfg.get("backtest.replay_data_quality", {}) or {}
    min_cov = safe_float(qc.get("min_coverage_pct", min_coverage_pct * 100.0)) / 100.0
    research_cfg = cfg.get("research", {}) or {}
    require_hl = bool(research_cfg.get("require_hl_venue", require_hl_venue))
    if refuse_on_fail is None:
        refuse_on_fail = bool(research_cfg.get("refuse_insufficient_feeds", True))
    strict = strict_research or bool(research_cfg.get("strict_mode", False))

    reports: List[FeedCoverageReport] = []
    reasons: List[str] = []
    venues: List[str] = []
    sources: List[str] = []

    for sym in symbols:
        candles = db.get_candles(sym, "1m", limit=500_000, start_ms=start_ms, end_ms=end_ms)
        data_src, venue = _detect_venue(db, sym)
        venues.append(venue)
        sample = (
            db.get_candle_metadata_sample(sym, "1m", limit=1)
            if isinstance(db, ResearchDatabase)
            else None
        )
        source = str(sample.get("source", "")) if sample else ""
        if source:
            sources.append(source)

        vol_unit = str(sample.get("volume_unit", "base")) if sample else "base"
        candle_report = audit_candle_series(
            sym,
            candles,
            feed="candles_1m",
            start_ms=start_ms,
            end_ms=end_ms,
            venue=venue,
            source=source or data_src,
            volume_unit=vol_unit,
            min_coverage_pct=min_cov,
        )
        reports.append(candle_report)
        if not candle_report.passed:
            reasons.extend(f"{sym}:candles:{f}" for f in candle_report.failures)

        lo = start_ms or (candles[0].timestamp_ms if candles else 0)
        hi = end_ms or (candles[-1].timestamp_ms if candles else 0)

        funding_rows = db.get_funding_history(sym, limit=500_000)
        funding_pts: List[tuple] = []
        for r in funding_rows:
            ts = int(r["timestamp"])
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts > end_ms:
                continue
            if r.get("current") is None:
                continue
            funding_pts.append((ts, float(r["current"])))
        fund_report = audit_auxiliary_feed(
            sym,
            "funding",
            funding_pts,
            window_start_ms=lo,
            window_end_ms=hi,
            venue=venue,
            source=source or "funding_history",
            min_points=0,
        )
        reports.append(fund_report)

        oi_rows = db.get_oi_history(sym, limit=500_000)
        oi_pts: List[tuple] = []
        for r in oi_rows:
            ts = int(r["timestamp"])
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts > end_ms:
                continue
            if r.get("oi_total") is None:
                continue
            oi_pts.append((ts, float(r["oi_total"])))
        oi_report = audit_auxiliary_feed(
            sym,
            "oi",
            oi_pts,
            window_start_ms=lo,
            window_end_ms=hi,
            venue=venue,
            source=source or "oi_history",
            min_points=0,
        )
        reports.append(oi_report)

        if isinstance(db, ResearchDatabase):
            l2_n = db.count_l2_in_window(sym, start_ms, end_ms)
            tape_n = db.count_trade_tape_in_window(sym, start_ms, end_ms)
            taker_n = db.count_taker_split_bars(sym, start_ms, end_ms)
            reports.append(FeedCoverageReport(
                symbol=sym,
                feed="l2_snapshots",
                venue=venue,
                start_ms=lo,
                end_ms=hi,
                bar_count=l2_n,
                expected_bars=1,
                coverage_pct=1.0 if l2_n > 0 else 0.0,
                max_gap_ms=0,
                duplicate_count=0,
                close_time_violations=0,
                stale_pct=0.0 if l2_n > 0 else 100.0,
                feed_span_ms=hi - lo if l2_n > 0 else 0,
                volume_unit="n/a",
                source=source,
                passed=l2_n > 0,
                failures=[] if l2_n > 0 else ["l2_missing"],
            ))
            reports.append(FeedCoverageReport(
                symbol=sym,
                feed="trade_tape",
                venue=venue,
                start_ms=lo,
                end_ms=hi,
                bar_count=tape_n,
                expected_bars=10,
                coverage_pct=min(1.0, tape_n / 10.0),
                max_gap_ms=0,
                duplicate_count=0,
                close_time_violations=0,
                stale_pct=0.0 if tape_n >= 10 else 100.0,
                feed_span_ms=hi - lo if tape_n > 0 else 0,
                volume_unit="n/a",
                source=source,
                passed=tape_n >= 10,
                failures=[] if tape_n >= 10 else [f"tape_insufficient:{tape_n}"],
            ))
            reports.append(FeedCoverageReport(
                symbol=sym,
                feed="taker_split",
                venue=venue,
                start_ms=lo,
                end_ms=hi,
                bar_count=taker_n,
                expected_bars=10,
                coverage_pct=min(1.0, taker_n / 10.0),
                max_gap_ms=0,
                duplicate_count=0,
                close_time_violations=0,
                stale_pct=0.0 if taker_n >= 10 else 100.0,
                feed_span_ms=hi - lo if taker_n > 0 else 0,
                volume_unit="base",
                source=source,
                passed=taker_n >= 10,
                failures=[] if taker_n >= 10 else [f"taker_split_missing:{taker_n}"],
            ))

    strategy_names = list(active_strategies or [])
    strategy_fidelity = evaluate_strategies_fidelity(
        db,
        symbols,
        strategy_names,
        start_ms=start_ms,
        end_ms=end_ms,
        reports=reports,
        min_coverage_pct=min_cov,
    )

    for strat, fid in strategy_fidelity.items():
        if not fid.tier_a_eligible:
            reasons.append(f"{strat}:tier_a_blocked:{','.join(fid.missing_feeds) or fid.tier}")

    if strict and strategy_names:
        funding_oi_strats = strategies_requiring_funding_oi(strategy_names)
        for strat in funding_oi_strats:
            fid = strategy_fidelity.get(strat)
            if fid is None:
                continue
            if "funding" in fid.missing_feeds or "oi" in fid.missing_feeds:
                reasons.append(f"strict_research:{strat}:missing_historical_funding_oi")
                refuse_on_fail = True

    primary_venue = (
        VENUE_HYPERLIQUID
        if VENUE_HYPERLIQUID in venues
        else (venues[0] if venues else "unknown")
    )
    hl_source = any(s == SOURCE_HL_CANDLE_SNAPSHOT for s in sources)
    binance_proxy = any(s == SOURCE_BINANCE_KLINES for s in sources) or (
        primary_venue == VENUE_BINANCE
    )

    if hl_source and primary_venue == VENUE_HYPERLIQUID:
        data_source = "sqlite_hl_research"
        fidelity = "tier_a_hl_ohlc"
    elif binance_proxy:
        data_source = "sqlite_binance_proxy"
        fidelity = "tier_b_binance_proxy_not_production"
    else:
        data_source = "sqlite_candles"
        fidelity = "tier_b_unknown_source"

    if strategy_fidelity:
        tiers = {f.tier for f in strategy_fidelity.values()}
        if any(t.startswith("tier_a") for t in tiers):
            fidelity = min(
                (t for t in tiers if t.startswith("tier_a")),
                key=lambda t: t,
            )
        elif all(not f.tier_a_eligible for f in strategy_fidelity.values()):
            fidelity = "tier_b_missing_feeds"

    refused = False
    degraded = False
    if reasons:
        if refuse_on_fail:
            refused = True
            fidelity = REFUSED
        else:
            degraded = True
            if fidelity.startswith("tier_a"):
                fidelity = "tier_b_degraded_coverage"

    if require_hl and primary_venue != VENUE_HYPERLIQUID:
        reasons.append("require_hl_venue:venue_not_hyperliquid")
        if refuse_on_fail:
            refused = True
            fidelity = REFUSED
        else:
            degraded = True
            fidelity = "tier_b_degraded_coverage"

    return DataContractResult(
        data_source=data_source,
        venue=primary_venue,
        fidelity_tier=fidelity,
        refused=refused,
        degraded=degraded,
        reports=reports,
        reasons=reasons,
        strategy_fidelity=strategy_fidelity,
    )


def assert_data_contract_or_raise(result: DataContractResult) -> None:
    if result.refused:
        raise DataContractError(
            f"Backtest refused — insufficient feed coverage: {result.reasons}"
        )
