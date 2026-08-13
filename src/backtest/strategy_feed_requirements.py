"""Per-strategy feed requirements and fidelity tier resolution (Phase 07)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Flag, auto
from typing import Dict, List, Optional, Sequence, Set

from src.data.coverage_audit import FeedCoverageReport
from src.data.database import Database
from src.data.research_database import ResearchDatabase


class RequiredFeeds(Flag):
    """Feeds a strategy needs for production-grade (Tier A) replay."""

    HL_CANDLES = auto()
    TAKER_SPLIT = auto()
    L2_SNAPSHOTS = auto()
    TRADE_TAPE = auto()
    FUNDING = auto()
    OI = auto()
    LIQUIDATION = auto()
    BINANCE_PERP = auto()


# Strategy display names → required feeds for Tier A on that strategy.
STRATEGY_FEED_MAP: Dict[str, RequiredFeeds] = {
    # OHLC-only — Tier A with HL candles
    "VolatilityBreakout": RequiredFeeds.HL_CANDLES,
    "VWAPDeviation": RequiredFeeds.HL_CANDLES,
    "VWAPTrend": RequiredFeeds.HL_CANDLES,  # research-only; not in live ensemble
    "TrendFollow": RequiredFeeds.HL_CANDLES,
    "SmartMoneyFlow": RequiredFeeds.HL_CANDLES,
    "MeanReversion": RequiredFeeds.HL_CANDLES,
    "DonchianBreakout": RequiredFeeds.HL_CANDLES,
    "TrendPyramid": RequiredFeeds.HL_CANDLES,
    "RangeGrid": RequiredFeeds.HL_CANDLES,
    "SFPReversion": RequiredFeeds.HL_CANDLES,
    "VARejection": RequiredFeeds.HL_CANDLES,
    # Microstructure / flow
    "CVDOrderFlow": (
        RequiredFeeds.HL_CANDLES | RequiredFeeds.TAKER_SPLIT | RequiredFeeds.TRADE_TAPE
    ),
    "OrderBookScalper": RequiredFeeds.HL_CANDLES | RequiredFeeds.L2_SNAPSHOTS,
    # Funding / carry
    "FundingArbitrage": RequiredFeeds.HL_CANDLES | RequiredFeeds.FUNDING,
    "FundingMomentum": RequiredFeeds.HL_CANDLES | RequiredFeeds.FUNDING | RequiredFeeds.OI,
    "SpotPerpCarry": RequiredFeeds.HL_CANDLES | RequiredFeeds.FUNDING,
    # External feeds
    "LiquidationCatcher": RequiredFeeds.HL_CANDLES | RequiredFeeds.LIQUIDATION,
    "LeadLag": RequiredFeeds.HL_CANDLES | RequiredFeeds.BINANCE_PERP,
    # Meta — worst-of children; evaluated separately when decomposed
    "ChecklistMeta": RequiredFeeds.HL_CANDLES,
    "StrategyEnsemble": RequiredFeeds.HL_CANDLES,
}

TIER_A_OHLC = "tier_a_hl_ohlc"
TIER_A_CVD = "tier_a_hl_cvd"
TIER_A_OIR = "tier_a_hl_oir"
TIER_A_FUNDING = "tier_a_hl_funding"
TIER_B_PROXY = "tier_b_binance_proxy_not_production"
TIER_B_LIQUIDATION_PROXY = "tier_b_liquidation_proxy_not_production"
TIER_B_MISSING = "tier_b_missing_feeds"
TIER_B_DEGRADED = "tier_b_degraded_coverage"
TIER_B_PHASE08_SHADOW = "tier_b_phase08_shadow_only"
REFUSED = "refused_insufficient_feeds"

PHASE08_SHADOW_STRATEGIES = frozenset({
    "CVDOrderFlow",
    "OrderBookScalper",
    "FundingArbitrage",
    "FundingMomentum",
    "SpotPerpCarry",
    "ChecklistMeta",
})


@dataclass(frozen=True)
class FeedAvailability:
    """Which feeds are present for a symbol over the backtest window."""

    symbol: str
    hl_candles: bool = False
    hl_venue: bool = False
    taker_split: bool = False
    l2_snapshots: bool = False
    trade_tape: bool = False
    funding: bool = False
    oi: bool = False
    liquidation: bool = False
    liquidation_provenance: str = "none"  # none | proxy | real | mixed
    binance_perp: bool = False
    candle_coverage_pct: float = 0.0

    def has(self, feed: RequiredFeeds) -> bool:
        checks = {
            RequiredFeeds.HL_CANDLES: self.hl_candles and self.hl_venue,
            RequiredFeeds.TAKER_SPLIT: self.taker_split,
            RequiredFeeds.L2_SNAPSHOTS: self.l2_snapshots,
            RequiredFeeds.TRADE_TAPE: self.trade_tape,
            RequiredFeeds.FUNDING: self.funding,
            RequiredFeeds.OI: self.oi,
            # Liquidation counts as *present* only when at least one real-venue
            # row is replayed. Proxy-only liquidations are the candle+OI
            # heuristic — never production-grade for a liquidation strategy.
            RequiredFeeds.LIQUIDATION: self.liquidation and self.liquidation_real,
            RequiredFeeds.BINANCE_PERP: self.binance_perp,
        }
        for flag in RequiredFeeds:
            if feed & flag and not checks.get(flag, False):
                return False
        return True

    @property
    def liquidation_real(self) -> bool:
        """True when at least one replayed liquidation row has real provenance."""
        return self.liquidation_provenance in ("real", "mixed")

    def missing_labels(self, required: RequiredFeeds) -> List[str]:
        out: List[str] = []
        if required & RequiredFeeds.HL_CANDLES and not (self.hl_candles and self.hl_venue):
            out.append("hl_candles")
        if required & RequiredFeeds.TAKER_SPLIT and not self.taker_split:
            out.append("taker_split")
        if required & RequiredFeeds.L2_SNAPSHOTS and not self.l2_snapshots:
            out.append("l2_snapshots")
        if required & RequiredFeeds.TRADE_TAPE and not self.trade_tape:
            out.append("trade_tape")
        if required & RequiredFeeds.FUNDING and not self.funding:
            out.append("funding")
        if required & RequiredFeeds.OI and not self.oi:
            out.append("oi")
        if required & RequiredFeeds.LIQUIDATION and not self.liquidation:
            out.append("liquidation")
        elif required & RequiredFeeds.LIQUIDATION and not self.liquidation_real:
            out.append("liquidation_proxy_only")
        if required & RequiredFeeds.BINANCE_PERP and not self.binance_perp:
            out.append("binance_perp")
        return out


@dataclass
class StrategyFidelity:
    """Fidelity classification for one strategy."""

    strategy: str
    tier: str
    required_feeds: List[str] = field(default_factory=list)
    missing_feeds: List[str] = field(default_factory=list)
    tier_a_eligible: bool = False
    liquidation_provenance: Optional[str] = None  # none|proxy|real|mixed (if req.)

    def to_dict(self) -> Dict[str, object]:
        d: Dict[str, object] = {
            "strategy": self.strategy,
            "fidelity_tier": self.tier,
            "required_feeds": self.required_feeds,
            "missing_feeds": self.missing_feeds,
            "tier_a_eligible": self.tier_a_eligible,
        }
        if self.liquidation_provenance is not None:
            d["liquidation_provenance"] = self.liquidation_provenance
        return d


def _required_feed_labels(flags: RequiredFeeds) -> List[str]:
    return [f.name.lower() for f in RequiredFeeds if flags & f]


def resolve_strategy_tier(
    strategy_name: str,
    availability: FeedAvailability,
    *,
    min_coverage_pct: float = 0.95,
    phase08_shadow_only: bool = False,
) -> StrategyFidelity:
    """Assign fidelity tier for one strategy given feed availability."""
    if phase08_shadow_only and strategy_name in PHASE08_SHADOW_STRATEGIES:
        required = STRATEGY_FEED_MAP.get(strategy_name, RequiredFeeds.HL_CANDLES)
        labels = _required_feed_labels(required)
        return StrategyFidelity(
            strategy=strategy_name,
            tier=TIER_B_PHASE08_SHADOW,
            required_feeds=labels,
            missing_feeds=["phase08_shadow_only"],
            tier_a_eligible=False,
        )

    required = STRATEGY_FEED_MAP.get(strategy_name, RequiredFeeds.HL_CANDLES)
    missing = availability.missing_labels(required)
    if availability.candle_coverage_pct < min_coverage_pct:
        if "hl_candles" not in missing:
            missing.append(f"candle_coverage<{min_coverage_pct * 100:.0f}%")

    labels = _required_feed_labels(required)
    if not missing and availability.hl_venue:
        if required & RequiredFeeds.TAKER_SPLIT:
            tier = TIER_A_CVD
        elif required & RequiredFeeds.L2_SNAPSHOTS:
            tier = TIER_A_OIR
        elif required & (RequiredFeeds.FUNDING | RequiredFeeds.OI):
            tier = TIER_A_FUNDING
        else:
            tier = TIER_A_OHLC
        return StrategyFidelity(
            strategy=strategy_name,
            tier=tier,
            required_feeds=labels,
            missing_feeds=[],
            tier_a_eligible=True,
            liquidation_provenance=(
                availability.liquidation_provenance
                if required & RequiredFeeds.LIQUIDATION else None
            ),
        )

    if not availability.hl_venue:
        tier = TIER_B_PROXY
    elif (
        required & RequiredFeeds.LIQUIDATION
        and availability.liquidation
        and not availability.liquidation_real
    ):
        # Liquidations exist but only as proxy synthesis — never production-
        # grade for a liquidation strategy (LiquidationCatcher requires real).
        tier = TIER_B_LIQUIDATION_PROXY
    else:
        tier = TIER_B_MISSING

    return StrategyFidelity(
        strategy=strategy_name,
        tier=tier,
        required_feeds=labels,
        missing_feeds=missing,
        tier_a_eligible=False,
        liquidation_provenance=(
            availability.liquidation_provenance
            if required & RequiredFeeds.LIQUIDATION else None
        ),
    )


def probe_feed_availability(
    db: Database,
    symbol: str,
    *,
    start_ms: Optional[int],
    end_ms: Optional[int],
    candle_report: Optional[FeedCoverageReport] = None,
    min_coverage_pct: float = 0.95,
    min_taker_bars: int = 10,
    min_l2_samples: int = 1,
    min_tape_rows: int = 10,
    min_funding_points: int = 1,
    min_oi_points: int = 1,
) -> FeedAvailability:
    """Inspect DB for feed presence over the replay window."""
    from src.data.series_metadata import SOURCE_HL_CANDLE_SNAPSHOT, VENUE_HYPERLIQUID

    candles = db.get_candles(symbol, "1m", limit=500_000, start_ms=start_ms, end_ms=end_ms)
    cov = candle_report.coverage_pct if candle_report else (
        len(candles) / max(1, int(((end_ms or 0) - (start_ms or 0)) / 60_000) + 1)
        if candles and start_ms is not None and end_ms is not None else 0.0
    )

    hl_venue = False
    if isinstance(db, ResearchDatabase):
        sample = db.get_candle_metadata_sample(symbol, "1m", limit=1)
        if sample:
            hl_venue = (
                sample.get("venue") == VENUE_HYPERLIQUID
                and sample.get("source") == SOURCE_HL_CANDLE_SNAPSHOT
            )
        taker_split = db.count_taker_split_bars(symbol, start_ms, end_ms) >= min_taker_bars
        l2_snapshots = db.count_l2_in_window(symbol, start_ms, end_ms) >= min_l2_samples
        trade_tape = db.count_trade_tape_in_window(symbol, start_ms, end_ms) >= min_tape_rows
    else:
        taker_split = any(
            c.buy_volume is not None and c.sell_volume is not None
            for c in candles
        )
        l2_snapshots = False
        trade_tape = False

    funding_pts = 0
    for r in db.get_funding_history(
        symbol, limit=500_000, start_ms=start_ms, end_ms=end_ms,
    ):
        if r.get("current") is not None:
            funding_pts += 1
    oi_pts = 0
    for r in db.get_oi_history(
        symbol, limit=500_000, start_ms=start_ms, end_ms=end_ms,
    ):
        if r.get("oi_total") is not None:
            oi_pts += 1

    liq_rows = []
    if hasattr(db, "get_liquidation_events"):
        liq_rows = db.get_liquidation_events(
            symbol, limit=100, start_ms=start_ms, end_ms=end_ms,
        )
    # Distinguish real-venue rows (hl/okx/bybit/binance — production-grade
    # provenance) from proxy synthesis rows (candle+OI heuristic). A
    # liquidation strategy can only be Tier A when at least one real row is
    # replayed in the window.
    from src.exchanges.liquidation_event import is_real_liquidation_source

    n_real_liq = sum(
        1 for r in liq_rows if is_real_liquidation_source(r.get("source"))
    )
    n_proxy_liq = len(liq_rows) - n_real_liq
    if n_real_liq > 0 and n_proxy_liq > 0:
        liq_provenance = "mixed"
    elif n_real_liq > 0:
        liq_provenance = "real"
    elif n_proxy_liq > 0:
        liq_provenance = "proxy"
    else:
        liq_provenance = "none"
    bn_rows = []
    if hasattr(db, "get_binance_perp_prices"):
        try:
            bn_rows = db.get_binance_perp_prices(symbol, limit=100)
        except Exception:
            bn_rows = []

    return FeedAvailability(
        symbol=symbol,
        hl_candles=len(candles) > 0 and cov >= min_coverage_pct,
        hl_venue=hl_venue or (isinstance(db, ResearchDatabase) and len(candles) > 0),
        taker_split=taker_split,
        l2_snapshots=l2_snapshots,
        trade_tape=trade_tape,
        funding=funding_pts >= min_funding_points,
        oi=oi_pts >= min_oi_points,
        liquidation=len(liq_rows) > 0,
        liquidation_provenance=liq_provenance,
        binance_perp=len(bn_rows) > 0,
        candle_coverage_pct=cov,
    )


def evaluate_strategies_fidelity(
    db: Database,
    symbols: Sequence[str],
    strategy_names: Sequence[str],
    *,
    start_ms: Optional[int],
    end_ms: Optional[int],
    reports: Sequence[FeedCoverageReport],
    min_coverage_pct: float = 0.95,
    phase08_shadow_only: bool = False,
) -> Dict[str, StrategyFidelity]:
    """Worst-symbol fidelity per strategy (all symbols must satisfy feeds)."""
    reports_by_sym: Dict[str, FeedCoverageReport] = {}
    for r in reports:
        if r.feed == "candles_1m":
            reports_by_sym[r.symbol] = r

    out: Dict[str, StrategyFidelity] = {}
    for strat in strategy_names:
        worst: Optional[StrategyFidelity] = None
        for sym in symbols:
            avail = probe_feed_availability(
                db,
                sym,
                start_ms=start_ms,
                end_ms=end_ms,
                candle_report=reports_by_sym.get(sym),
                min_coverage_pct=min_coverage_pct,
            )
            fid = resolve_strategy_tier(
                strat,
                avail,
                min_coverage_pct=min_coverage_pct,
                phase08_shadow_only=phase08_shadow_only,
            )
            if worst is None or (not fid.tier_a_eligible and worst.tier_a_eligible):
                worst = fid
            elif not fid.tier_a_eligible and not worst.tier_a_eligible:
                if len(fid.missing_feeds) > len(worst.missing_feeds):
                    worst = fid
        if worst is not None:
            out[strat] = worst
    return out


def strategies_requiring_funding_oi(strategy_names: Sequence[str]) -> Set[str]:
    """Return strategy names that need funding and/or OI for strict research."""
    need: Set[str] = set()
    for name in strategy_names:
        req = STRATEGY_FEED_MAP.get(name, RequiredFeeds.HL_CANDLES)
        if req & (RequiredFeeds.FUNDING | RequiredFeeds.OI):
            need.add(name)
    return need
