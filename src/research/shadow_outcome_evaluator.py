"""Shadow outcome evaluator — hypothetical bracket results from shadow decisions.

Research / observability only. Numbers here may later influence shadow→live
promotion decisions, so fill rules are intentionally **pessimistic** and
never invent missing bracket parameters.

Gross vs net
------------
``pnl_pct`` / ``r_multiple`` remain **gross** (price move only). Net metrics
subtract tier-0 fees + paper slippage and add funding PnL when candle
``funding_rate`` stamps are available. Queue position / adverse selection is
still not modelled — net is a lower bound on friction, not a full executable edge.

Candle source
-------------
Primary: research DB ``candles_1m`` rows with Hyperliquid-native ``source``
tags (``hl_ws_1m_tape_agg``, ``hl_candleSnapshot``, ``hl_node_trades_rebuild``).
GoldRush rows are excluded — AGENTS.md: GoldRush readiness is not validated.

Fallback: ``data/live/bot.db`` ``candles_1m`` opened **read-only** (``mode=ro``)
only to fill gaps when research HL candles are insufficient for a decision's
forward window. Never write to live.

Intra-candle ambiguity (CONSERVATIVE)
-------------------------------------
When a single candle's range touches **both** the stop-loss and take-profit
levels, resolve as **stop-loss hit first** (pessimistic bias). Documented in
``resolve_candle_exit`` and tested explicitly.

Gap-through
-----------
If a candle **opens** beyond SL or TP, fill at the **open** (worse than the
level for stops; better than the level for TPs) — not at the level price.

Max-hold (per strategy, from config via ``get_strategy_section``)
-----------------------------------------------------------------
======= ===================== ============================
Strategy Default used         Config key
======= ===================== ============================
OrderBookScalper 5 min        ``max_hold_seconds`` (300)
CVDOrderFlow     6 h          ``max_hold_hours``
ChecklistMeta    6 h          ``max_hold_hours``
FundingArbitrage 8 h          ``max_hold_hours``
FundingMomentum  12 h         ``max_hold_hours``
SpotPerpCarry    24 h         ``max_hold_hours``
======= ===================== ============================
"""

from __future__ import annotations

import bisect
import json
import logging
import sqlite3
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data.database import Candle
from src.data.research_database import ResearchDatabase
from src.research.phase10_gate_metrics import compute_profit_factor
from src.research.shadow_recorder import (
    ShadowDecision,
    ShadowRecorder,
    extract_bracket_params,
)
from src.utils.config import Config, get_strategy_section, load_config
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

IDEALIZED_FILL_DISCLAIMER = (
    "GROSS FILLS shown alongside NET (tier-0 fees + paper slippage + funding). "
    "Queue position not modelled — net still excludes adverse selection."
)

# Prefer HL-native research candles; GoldRush excluded (data readiness).
HL_CANDLE_SOURCES = frozenset(
    {
        "hl_ws_1m_tape_agg",
        "hl_candleSnapshot",
        "hl_node_trades_rebuild",
    }
)

STRATEGY_CONFIG_SECTION: Dict[str, str] = {
    "OrderBookScalper": "orderbook_scalper",
    "CVDOrderFlow": "cvd_orderflow",
    "ChecklistMeta": "checklist_meta",
    "FundingArbitrage": "funding_arbitrage",
    "FundingMomentum": "funding_momentum",
    "SpotPerpCarry": "spot_perp_carry",
    "VolatilityBreakout": "volatility_breakout",
    "VWAPDeviation": "vwap_deviation",
    "TopTraderFlow": "top_trader_flow",
}

# Documented defaults when config section is missing/empty.
DEFAULT_MAX_HOLD_MS: Dict[str, int] = {
    "OrderBookScalper": 300 * 1000,
    "CVDOrderFlow": 6 * 3600 * 1000,
    "ChecklistMeta": 6 * 3600 * 1000,
    "FundingArbitrage": 8 * 3600 * 1000,
    "FundingMomentum": 12 * 3600 * 1000,
    "SpotPerpCarry": 24 * 3600 * 1000,
    "TopTraderFlow": 120 * 3600 * 1000,  # 5d swing
}

SKIP_MISSING_BRACKET = "missing_bracket_params"
SKIP_INSUFFICIENT_CANDLES = "insufficient_candles"
SKIP_INVALID_SIDE = "invalid_side"
SKIP_WOULD_NOT_ENTER = "would_not_enter"
SKIP_OVERLAP_DEDUP = "overlap_dedup"

EXIT_TP = "take_profit"
EXIT_SL = "stop_loss"
EXIT_TIMEOUT = "timeout"
EXIT_GAP_SL = "gap_stop_loss"
EXIT_GAP_TP = "gap_take_profit"
EXIT_BIAS_FLIP = "bias_flip"

LIVE_DB_DEFAULT = Path("data") / "live" / "bot.db"


@dataclass(frozen=True)
class SimulatedOutcome:
    """One evaluated hypothetical trade."""

    decision_id: Optional[int]
    symbol: str
    strategy: str
    side: str
    entry_price: float
    entry_ts_ms: int
    exit_price: float
    exit_ts_ms: int
    exit_reason: str
    stop_loss_pct: float
    take_profit_pct: float
    size_pct: float
    pnl_pct: float  # GROSS (price move only)
    r_multiple: float  # GROSS R
    hold_minutes: float
    evaluated: bool = True
    skip_reason: Optional[str] = None
    fee_cost_pct: float = 0.0
    slip_cost_pct: float = 0.0
    funding_pnl_pct: float = 0.0
    net_pnl_pct: float = 0.0
    net_r_multiple: float = 0.0
    funding_coverage: float = 1.0
    cost_model_label: str = ""


@dataclass(frozen=True)
class ShadowCostModel:
    """Per-side fee/slip fractions (not percent points)."""

    entry_fee_frac: float
    exit_fee_frac: float
    entry_slip_frac: float
    exit_slip_frac: float
    label: str
    min_funding_coverage: float = 0.90

    @property
    def round_trip_fee_frac(self) -> float:
        return self.entry_fee_frac + self.exit_fee_frac

    @property
    def round_trip_slip_frac(self) -> float:
        return self.entry_slip_frac + self.exit_slip_frac


def resolve_shadow_cost_model(
    strategy_name: str,
    config: Optional[Config] = None,
) -> ShadowCostModel:
    """Resolve tier-0-aware entry/exit fees for a strategy."""
    cfg = config
    if cfg is None:
        try:
            cfg = load_config(Path("config/settings.yaml"))
        except Exception:  # noqa: BLE001
            cfg = Config({})

    taker = safe_float(cfg.get("risk.taker_fee_pct", 0.045), 0.045) / 100.0
    slip = safe_float(cfg.get("risk.paper_slippage_pct", 0.02), 0.02) / 100.0
    maker_cfg = cfg.get("execution.maker_orders", {}) or {}
    maker_enabled = bool(maker_cfg.get("enabled", False))
    maker = safe_float(maker_cfg.get("maker_fee_pct", 0.015), 0.015) / 100.0
    maker_strats = {str(s) for s in (maker_cfg.get("strategies") or [])}
    exit_as_maker = bool(maker_cfg.get("exit_as_maker", False))
    use_maker_entry = maker_enabled and strategy_name in maker_strats
    use_maker_exit = use_maker_entry and exit_as_maker
    entry_fee = maker if use_maker_entry else taker
    exit_fee = maker if use_maker_exit else taker
    label = (
        f"entry={'maker' if use_maker_entry else 'taker'} "
        f"exit={'maker' if use_maker_exit else 'taker'} "
        f"fee_rt_bps={(entry_fee + exit_fee) * 1e4:.2f} "
        f"slip_rt_bps={(2 * slip) * 1e4:.2f}"
    )
    return ShadowCostModel(
        entry_fee_frac=entry_fee,
        exit_fee_frac=exit_fee,
        entry_slip_frac=slip,
        exit_slip_frac=slip,
        label=label,
    )


def resolve_maker_entry_cost_model(
    config: Optional[Config] = None,
) -> ShadowCostModel:
    """Cost model for the ``phase08_shadow_maker`` research variant.

    Maker entry (limit at signal price, fills at the level or better) +
    taker exit (SL/TP/timeout market exits). Entry slip is zero — a
    resting limit never pays worse than its own price.
    """
    cfg = config
    if cfg is None:
        try:
            cfg = load_config(Path("config/settings.yaml"))
        except Exception:  # noqa: BLE001
            cfg = Config({})
    taker = safe_float(cfg.get("risk.taker_fee_pct", 0.045), 0.045) / 100.0
    slip = safe_float(cfg.get("risk.paper_slippage_pct", 0.02), 0.02) / 100.0
    maker_cfg = cfg.get("execution.maker_orders", {}) or {}
    maker = safe_float(maker_cfg.get("maker_fee_pct", 0.015), 0.015) / 100.0
    label = (
        f"entry=maker exit=taker "
        f"fee_rt_bps={(maker + taker) * 1e4:.2f} "
        f"slip_rt_bps={slip * 1e4:.2f} (entry slip=0: fills at limit or better)"
    )
    return ShadowCostModel(
        entry_fee_frac=maker,
        exit_fee_frac=taker,
        entry_slip_frac=0.0,
        exit_slip_frac=slip,
        label=label,
    )


def resolve_maker_entry_window_ms(config: Optional[Config] = None) -> int:
    """Maker fill window — mirrors live ``execution.maker_orders.timeout_ms``
    (unfilled limits cancel after it). 1m candles cannot resolve a 30s
    window sub-candle, so the probe accepts a touch anywhere inside the
    first candle covering the window — optimistic on fill timing, and the
    label says so."""
    cfg = config
    if cfg is None:
        try:
            cfg = load_config(Path("config/settings.yaml"))
        except Exception:  # noqa: BLE001
            cfg = Config({})
    maker_cfg = cfg.get("execution.maker_orders", {}) or {}
    return int(safe_float(maker_cfg.get("timeout_ms", 30000), 30000))


def _funding_during_hold(
    side: str,
    candles: Sequence[Candle],
    entry_ts_ms: int,
    exit_ts_ms: int,
    *,
    funding_samples: Optional[Sequence[Tuple[int, float]]] = None,
) -> Tuple[float, float]:
    """Return (funding_pnl_pct, coverage).

    Positive HL funding => longs pay shorts. HL settles funding at each
    **hour boundary**, so exactly one rate applies per boundary inside
    ``(entry, exit]`` — the last observed stamp at or before it. Sources,
    in preference order: candle ``funding_rate`` stamps, then
    ``funding_samples`` (``(ts_ms, hourly_rate_frac)`` tuples, e.g. from
    ``funding_history``).

    Coverage = boundaries with a found rate / total boundaries. Holds
    <30m or holds containing no boundary report coverage 1.0.
    """
    hold_ms = max(0, exit_ts_ms - entry_ts_ms)
    if hold_ms < 30 * 60_000:
        return 0.0, 1.0
    hour_ms = 3_600_000
    first_boundary = (entry_ts_ms // hour_ms + 1) * hour_ms
    boundaries = list(range(first_boundary, exit_ts_ms + 1, hour_ms))
    if not boundaries:
        return 0.0, 1.0

    stamps: List[Tuple[int, float]] = []
    for c in candles:
        if c.funding_rate is None:
            continue
        fr = safe_float(c.funding_rate, default=float("nan"))
        if fr == fr:  # finite
            stamps.append((int(c.timestamp_ms), float(fr)))
    for s in funding_samples or ():
        stamps.append((int(s[0]), float(s[1])))
    stamps.sort(key=lambda t: t[0])
    stamp_ts = [t for t, _ in stamps]

    covered = 0
    total = 0.0
    for b in boundaries:
        idx = bisect.bisect_right(stamp_ts, b) - 1
        if idx >= 0:
            total += stamps[idx][1]
            covered += 1
    coverage = covered / float(len(boundaries))
    # long pays positive funding
    sign = -1.0 if side.lower() == "long" else 1.0
    return sign * total, coverage


VARIANT_PHASE08_SHADOW = "phase08_shadow"
VARIANT_PHASE08_SHADOW_MAKER = "phase08_shadow_maker"
VARIANT_ROUTER_BLOCKED = "router_blocked"

SKIP_MAKER_NO_FILL = "maker_no_fill"

ROUTER_BLOCKED_SECTION_LABEL = (
    "counterfactual — signals the router blocked; idealized fills"
)

# Preregistered confirmation samples — the board for post-cutoff decisions
# is SEALED: only counts are exposed until indep_n reaches the target, so an
# interim read can never drive a stop/continue decision (no peeking rule in
# docs/PREREGISTER_VWAP_IV_GATE_2026-10-08.md — commit a70b3cb,
# 2026-10-08 20:17:36 UTC). Discovery rows (ts <= cutoff) keep the plain
# variant board; confirmation rows get ``variant#confirm``.
VARIANT_CONFIRM_SUFFIX = "#confirm"
PREREGISTERED_CONFIRMATIONS: Dict[Tuple[str, str], Tuple[int, int, int]] = {
    # (strategy, variant) -> (cutoff_ms, min_independent_n, expiry_ms).
    # expiry = cutoff + 2 × (target / historical independent-signal rate).
    # VWAP iv_gate: discovery 2026-08-13→09-17 (34.94d) yielded 30 indep →
    # 0.859/d → E[T60] = 69.9d → expiry = cutoff + 139.9d = 2027-02-25.
    # Reaching expiry still sealed → final read + verdict C (no retry).
    ("VWAPDeviation", "iv_gate_shadow"): (1791490656000, 60, 1803564432000),
}


# Regimes under which a preregistered variant could legitimately enter the
# confirmation sample (mirrors the discovery-era router eligibility). A
# post-cutoff row stamped with another ``router_regime`` — or explicitly
# flagged ``fallback_promoted`` — could only have arrived via fallback
# promotion and is excluded by flag; rows are never deleted.
_CONFIRM_REGIME_ALLOW: Dict[Tuple[str, str], frozenset] = {
    ("VWAPDeviation", "iv_gate_shadow"): frozenset({"range", "low_vol"}),
}


def _confirm_row_excluded(decision: "ShadowDecision") -> bool:
    meta = (decision.market_snapshot or {}).get("metadata") or {}
    if meta.get("fallback_promoted"):
        return True
    allowed = _CONFIRM_REGIME_ALLOW.get((decision.strategy, decision.variant or ""))
    regime = meta.get("router_regime")
    return allowed is not None and regime is not None and regime not in allowed


@dataclass
class StrategyScoreboard:
    """Aggregated outcomes for one (strategy, variant) pair (gross + net).

    Headline metrics (win_rate, profit_factor, expectancy_r, net_*,
    hold/cost means, wins/losses/timeouts) are computed over the
    INDEPENDENT outcome set — one open simulated position per symbol at a
    time (see ``independent_outcomes``). ``n_evaluated`` stays the raw
    evaluated count; ``n_independent`` is what n>=30-style gates must read.
    Re-emitting strategies (TopTraderFlow showed 14,066 evaluated ->
    53 independent) otherwise inflate n and distort PF by letting one
    market episode count as hundreds of "trades".
    """

    strategy: str
    variant: str = VARIANT_PHASE08_SHADOW
    n_decisions: int = 0
    n_evaluated: int = 0
    n_independent: int = 0
    # Cross-symbol effective n: independent outcomes whose [entry, exit]
    # windows overlap across symbols merge into one market episode.
    # Informational — gates read n_independent.
    n_clustered: int = 0
    n_overlapped: int = 0
    n_skipped: int = 0
    skip_reasons: Dict[str, int] = field(default_factory=dict)
    wins: int = 0
    losses: int = 0
    timeouts: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0  # GROSS R
    expectancy_r: float = 0.0  # GROSS R
    avg_hold_minutes: float = 0.0
    median_hold_minutes: float = 0.0
    gross_hypothetical_pnl_pct: float = 0.0
    net_profit_factor: float = 0.0
    net_expectancy_r: float = 0.0
    net_hypothetical_pnl_pct: float = 0.0
    mean_fee_cost_pct: float = 0.0
    mean_slip_cost_pct: float = 0.0
    mean_funding_pnl_pct: float = 0.0
    mean_funding_coverage: float = 1.0
    funding_coverage_ok: bool = True
    cost_model_label: str = ""
    max_hold_ms_used: int = 0
    candle_source: str = ""
    disclaimer: str = IDEALIZED_FILL_DISCLAIMER
    outcomes: List[SimulatedOutcome] = field(default_factory=list)
    independent_outcome_rows: List[SimulatedOutcome] = field(
        default_factory=list
    )
    # Preregistered-confirmation seal: when True the metric fields below are
    # intentionally NOT computed — the sample is read once, at the end.
    sealed: bool = False
    confirmation_min_indep: int = 0
    confirmation_cutoff_ms: Optional[int] = None
    # Preregistered expiry: if the sample is still under target past this
    # date the seal lifts for the final read (verdict C by protocol).
    confirmation_expiry_ms: Optional[int] = None
    confirmation_expired: bool = False
    # Post-cutoff rows excluded by flag (fallback promotion / ineligible
    # recorded regime) — audit trail, rows stay in the DB untouched.
    n_regime_excluded: int = 0

    @property
    def key(self) -> str:
        return scoreboard_key(self.strategy, self.variant)

    def to_dict(self, *, include_outcomes: bool = False) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "strategy": self.strategy,
            "variant": self.variant,
            "n_decisions": self.n_decisions,
            "n_evaluated": self.n_evaluated,
            "n_independent": self.n_independent,
            "n_clustered": self.n_clustered,
            "n_overlapped": self.n_overlapped,
            "n_skipped": self.n_skipped,
            "skip_reasons": dict(self.skip_reasons),
            "wins": self.wins,
            "losses": self.losses,
            "timeouts": self.timeouts,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "expectancy_r": self.expectancy_r,
            "avg_hold_minutes": self.avg_hold_minutes,
            "median_hold_minutes": self.median_hold_minutes,
            "gross_hypothetical_pnl_pct": self.gross_hypothetical_pnl_pct,
            "net_profit_factor": self.net_profit_factor,
            "net_expectancy_r": self.net_expectancy_r,
            "net_hypothetical_pnl_pct": self.net_hypothetical_pnl_pct,
            "mean_fee_cost_pct": self.mean_fee_cost_pct,
            "mean_slip_cost_pct": self.mean_slip_cost_pct,
            "mean_funding_pnl_pct": self.mean_funding_pnl_pct,
            "mean_funding_coverage": self.mean_funding_coverage,
            "funding_coverage_ok": self.funding_coverage_ok,
            "cost_model_label": self.cost_model_label,
            "max_hold_ms_used": self.max_hold_ms_used,
            "candle_source": self.candle_source,
            "disclaimer": self.disclaimer,
        }
        if self.sealed or self.confirmation_min_indep:
            d["sealed"] = self.sealed
            d["confirmation_min_indep"] = self.confirmation_min_indep
            d["confirmation_cutoff_ms"] = self.confirmation_cutoff_ms
            d["confirmation_expiry_ms"] = self.confirmation_expiry_ms
            d["confirmation_expired"] = self.confirmation_expired
        if self.n_regime_excluded:
            d["n_regime_excluded"] = self.n_regime_excluded
        if self.variant == VARIANT_ROUTER_BLOCKED:
            d["section_label"] = ROUTER_BLOCKED_SECTION_LABEL
        if include_outcomes:
            d["outcomes"] = [asdict(o) for o in self.outcomes]
        return d


def scoreboard_key(strategy: str, variant: str) -> str:
    """Stable dict key separating shadow vs router_blocked scoreboards."""
    return f"{strategy}::{variant or VARIANT_PHASE08_SHADOW}"


def resolve_max_hold_ms(
    strategy_name: str,
    config: Optional[Config] = None,
) -> int:
    """Resolve max-hold timeout in ms from strategy config section."""
    section_name = STRATEGY_CONFIG_SECTION.get(strategy_name)
    section: Dict[str, Any] = {}
    if config is not None and section_name:
        section = get_strategy_section(config, section_name)
    if section:
        if "max_hold_seconds" in section:
            return int(float(section["max_hold_seconds"]) * 1000)
        if "max_hold_minutes" in section:
            return int(float(section["max_hold_minutes"]) * 60_000)
        if "max_hold_hours" in section:
            return int(float(section["max_hold_hours"]) * 3_600_000)
    return int(DEFAULT_MAX_HOLD_MS.get(strategy_name, 6 * 3600 * 1000))


def _sl_tp_prices(
    side: str,
    entry: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> Tuple[float, float]:
    side_l = side.lower()
    if side_l == "long":
        return entry * (1.0 - stop_loss_pct), entry * (1.0 + take_profit_pct)
    if side_l == "short":
        return entry * (1.0 + stop_loss_pct), entry * (1.0 - take_profit_pct)
    raise ValueError(f"invalid side: {side}")


def resolve_candle_exit(
    *,
    side: str,
    entry: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    candle: Candle,
) -> Optional[Tuple[float, str]]:
    """Apply gap-through + conservative dual-touch rule for one candle.

    CONSERVATIVE AMBIGUITY RULE: if the candle range touches both SL and TP,
    assume SL was hit first (pessimistic). Gap-through fills at open.
    """
    sl, tp = _sl_tp_prices(side, entry, stop_loss_pct, take_profit_pct)
    o, h, l, c = (  # noqa: E741
        float(candle.open),
        float(candle.high),
        float(candle.low),
        float(candle.close),
    )
    side_l = side.lower()

    if side_l == "long":
        # Gap through SL at open (open below stop)
        if o <= sl:
            return o, EXIT_GAP_SL
        # Gap through TP at open (open above take-profit)
        if o >= tp:
            return o, EXIT_GAP_TP
        hit_sl = l <= sl
        hit_tp = h >= tp
        if hit_sl and hit_tp:
            # CONSERVATIVE: SL first when both touched in one candle
            return sl, EXIT_SL
        if hit_sl:
            return sl, EXIT_SL
        if hit_tp:
            return tp, EXIT_TP
        return None

    if side_l == "short":
        if o >= sl:
            return o, EXIT_GAP_SL
        if o <= tp:
            return o, EXIT_GAP_TP
        hit_sl = h >= sl
        hit_tp = l <= tp
        if hit_sl and hit_tp:
            return sl, EXIT_SL
        if hit_sl:
            return sl, EXIT_SL
        if hit_tp:
            return tp, EXIT_TP
        return None

    return None


def _pnl_pct(side: str, entry: float, exit_price: float) -> float:
    if entry <= 0:
        return 0.0
    if side.lower() == "long":
        return (exit_price - entry) / entry
    return (entry - exit_price) / entry


def _r_multiple(pnl_pct: float, stop_loss_pct: float) -> float:
    if stop_loss_pct <= 0:
        return 0.0
    return pnl_pct / stop_loss_pct


def load_forward_candles(
    *,
    symbol: str,
    entry_ts_ms: int,
    max_hold_ms: int,
    research_db_path: Path,
    live_db_path: Optional[Path] = None,
    prefer_live_fallback: bool = True,
    extra_lead_ms: int = 0,
) -> Tuple[List[Candle], str]:
    """Load subsequent 1m candles for outcome simulation (read-only paths).

    ``extra_lead_ms`` extends the window when the position can start after
    the decision (maker-entry variant waits up to the fill window).

    Returns ``(candles ASC, source_label)``.
    """
    end_ms = entry_ts_ms + max_hold_ms + int(extra_lead_ms) + 60_000  # buffer
    research = _load_research_hl_candles(
        research_db_path, symbol, entry_ts_ms, end_ms
    )
    source = "research:hl_native"
    merged = {c.timestamp_ms: c for c in research}

    need_fallback = len(merged) == 0 or (
        max(merged) < entry_ts_ms + max_hold_ms if merged else True
    )
    if prefer_live_fallback and need_fallback and live_db_path is not None:
        live = _load_live_candles_ro(live_db_path, symbol, entry_ts_ms, end_ms)
        for c in live:
            # Prefer research HL when both have the same timestamp
            if c.timestamp_ms not in merged:
                merged[c.timestamp_ms] = c
        if live:
            source = (
                "research:hl_native+live:bot.db(ro)"
                if research
                else "live:bot.db(ro)"
            )

    candles = [merged[k] for k in sorted(merged) if k > entry_ts_ms]
    return candles, source


def _load_research_hl_candles(
    db_path: Path,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> List[Candle]:
    if not db_path.exists():
        return []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(candles_1m)").fetchall()}
        if "source" in cols:
            placeholders = ",".join("?" for _ in HL_CANDLE_SOURCES)
            sql = f"""
                SELECT symbol, timestamp_ms, open, high, low, close, volume,
                       funding_rate, oi_total, oi_delta, buy_volume, sell_volume,
                       trade_count
                FROM candles_1m
                WHERE symbol = ?
                  AND timestamp_ms > ?
                  AND timestamp_ms <= ?
                  AND source IN ({placeholders})
                ORDER BY timestamp_ms ASC
            """
            params: List[Any] = [symbol, start_ms, end_ms, *sorted(HL_CANDLE_SOURCES)]
        else:
            sql = """
                SELECT symbol, timestamp_ms, open, high, low, close, volume,
                       funding_rate, oi_total, oi_delta, buy_volume, sell_volume,
                       trade_count
                FROM candles_1m
                WHERE symbol = ?
                  AND timestamp_ms > ?
                  AND timestamp_ms <= ?
                ORDER BY timestamp_ms ASC
            """
            params = [symbol, start_ms, end_ms]
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_candle(r) for r in rows]


def _load_live_candles_ro(
    db_path: Path,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> List[Candle]:
    """Open live bot.db read-only — never write."""
    if not db_path.exists():
        return []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT symbol, timestamp_ms, open, high, low, close, volume,
                   funding_rate, oi_total, oi_delta, buy_volume, sell_volume,
                   trade_count
            FROM candles_1m
            WHERE symbol = ?
              AND timestamp_ms > ?
              AND timestamp_ms <= ?
            ORDER BY timestamp_ms ASC
            """,
            (symbol, start_ms, end_ms),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("live candle read failed (ro): %s", exc)
        return []
    finally:
        conn.close()
    return [_row_candle(r) for r in rows]


def _load_funding_samples(
    db_path: Path,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> List[Tuple[int, float]]:
    """Load ``(ts_ms, hourly_rate_frac)`` from live ``funding_history`` (ro).

    ``funding_history.predicted`` stores the per-8h-period funding rate
    (fraction). Empirically ``predicted / 8`` matches the hourly
    ``candles_1m.funding_rate`` stamps (~1e-5..5e-6), so we normalise to an
    hourly-equivalent rate here.
    """
    if not db_path.exists():
        return []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT timestamp, predicted
            FROM funding_history
            WHERE symbol = ?
              AND timestamp > ?
              AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (symbol, int(start_ms), int(end_ms)),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("funding_history read failed (ro): %s", exc)
        return []
    finally:
        conn.close()
    out: List[Tuple[int, float]] = []
    for r in rows:
        pred = safe_float(r["predicted"], default=float("nan"))
        if pred == pred:  # finite
            out.append((int(r["timestamp"]), float(pred) / 8.0))
    return out


def _row_candle(row: sqlite3.Row) -> Candle:
    return Candle(
        symbol=str(row["symbol"]),
        timestamp_ms=int(row["timestamp_ms"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=safe_float(row["volume"], default=0.0),
        funding_rate=row["funding_rate"],
        oi_total=row["oi_total"],
        oi_delta=row["oi_delta"],
        buy_volume=row["buy_volume"] if "buy_volume" in row.keys() else None,
        sell_volume=row["sell_volume"] if "sell_volume" in row.keys() else None,
        trade_count=row["trade_count"] if "trade_count" in row.keys() else None,
    )


def simulate_decision(
    decision: ShadowDecision,
    candles: Sequence[Candle],
    *,
    max_hold_ms: int,
    cost_model: Optional[ShadowCostModel] = None,
    bias_samples: Optional[Sequence[Dict[str, Any]]] = None,
    bias_threshold: float = 0.55,
    funding_samples: Optional[Sequence[Tuple[int, float]]] = None,
) -> SimulatedOutcome:
    """Simulate one shadow entry against forward 1m candles.

    When *bias_samples* are provided (TopTraderFlow), hybrid exit also fires on
    aggregate bias flip against the position before SL/TP/timeout.

    *funding_samples*: ``(ts_ms, hourly_rate_frac)`` tuples used for funding
    PnL when candle ``funding_rate`` stamps are absent (research candles
    never carry them — live ``funding_history`` supplies the fallback).
    """
    base = SimulatedOutcome(
        decision_id=decision.row_id,
        symbol=decision.symbol,
        strategy=decision.strategy,
        side=str(decision.side or ""),
        entry_price=0.0,
        entry_ts_ms=decision.timestamp_ms,
        exit_price=0.0,
        exit_ts_ms=decision.timestamp_ms,
        exit_reason="",
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        size_pct=0.0,
        pnl_pct=0.0,
        r_multiple=0.0,
        hold_minutes=0.0,
        evaluated=False,
        skip_reason=None,
    )
    if not decision.would_enter:
        return dataclasses_replace(base, skip_reason=SKIP_WOULD_NOT_ENTER)

    bracket = extract_bracket_params(decision.market_snapshot)
    if bracket is None:
        return dataclasses_replace(base, skip_reason=SKIP_MISSING_BRACKET)

    side = (decision.side or "").lower()
    if side not in ("long", "short"):
        return dataclasses_replace(base, skip_reason=SKIP_INVALID_SIDE)

    entry = float(bracket["price"])
    stop = float(bracket["stop_loss_pct"])
    take = float(bracket["take_profit_pct"])
    size = float(bracket["size_pct"])
    deadline = decision.timestamp_ms + max_hold_ms
    model = cost_model or resolve_shadow_cost_model(decision.strategy)

    # Prefer threshold from recorded metadata when present
    meta = (decision.market_snapshot or {}).get("metadata") or {}
    thr = float(meta.get("bias_threshold", bias_threshold))
    samples = list(bias_samples or [])
    sample_idx = 0

    if not candles:
        return dataclasses_replace(
            base,
            entry_price=entry,
            stop_loss_pct=stop,
            take_profit_pct=take,
            size_pct=size,
            skip_reason=SKIP_INSUFFICIENT_CANDLES,
        )

    last_candle: Optional[Candle] = None
    for candle in candles:
        if candle.timestamp_ms <= decision.timestamp_ms:
            continue
        last_candle = candle

        # Hybrid: bias flip samples up to this candle timestamp
        while sample_idx < len(samples):
            sample = samples[sample_idx]
            ts = int(sample.get("timestamp_ms", 0))
            if ts <= decision.timestamp_ms:
                sample_idx += 1
                continue
            if ts > candle.timestamp_ms:
                break
            b = float(sample.get("net_bias", 0.0))
            flipped = (side == "long" and b <= -thr) or (
                side == "short" and b >= thr
            )
            sample_idx += 1
            if flipped:
                return _finish_outcome(
                    decision,
                    entry,
                    stop,
                    take,
                    size,
                    side,
                    float(candle.open),
                    ts,
                    EXIT_BIAS_FLIP,
                    candles=candles,
                    cost_model=model,
                    funding_samples=funding_samples,
                )

        hit = resolve_candle_exit(
            side=side,
            entry=entry,
            stop_loss_pct=stop,
            take_profit_pct=take,
            candle=candle,
        )
        if hit is not None:
            exit_px, reason = hit
            return _finish_outcome(
                decision,
                entry,
                stop,
                take,
                size,
                side,
                exit_px,
                candle.timestamp_ms,
                reason,
                candles=candles,
                cost_model=model,
                funding_samples=funding_samples,
            )
        if candle.timestamp_ms >= deadline:
            return _finish_outcome(
                decision,
                entry,
                stop,
                take,
                size,
                side,
                float(candle.close),
                candle.timestamp_ms,
                EXIT_TIMEOUT,
                candles=candles,
                cost_model=model,
                funding_samples=funding_samples,
            )

    # Exhausted available candles before SL/TP/timeout → incomplete data
    if last_candle is None or last_candle.timestamp_ms < deadline:
        return dataclasses_replace(
            base,
            entry_price=entry,
            stop_loss_pct=stop,
            take_profit_pct=take,
            size_pct=size,
            skip_reason=SKIP_INSUFFICIENT_CANDLES,
        )
    return _finish_outcome(
        decision,
        entry,
        stop,
        take,
        size,
        side,
        float(last_candle.close),
        last_candle.timestamp_ms,
        EXIT_TIMEOUT,
        candles=candles,
        cost_model=model,
        funding_samples=funding_samples,
    )


def dataclasses_replace(outcome: SimulatedOutcome, **kwargs: Any) -> SimulatedOutcome:
    data = asdict(outcome)
    data.update(kwargs)
    return SimulatedOutcome(**data)


def _maker_fill_price(candle: Candle, side: str, limit: float) -> Optional[float]:
    """Fill price if a resting limit at ``limit`` executes inside the candle.

    Long: fills when ``low <= limit``; a gap through (``open <= limit``)
    fills at the open (better than the posted price). Short mirrored.
    Returns ``None`` when the level was never touched.
    """
    o, h, l = (  # noqa: E741
        float(candle.open),
        float(candle.high),
        float(candle.low),
    )
    if side == "long":
        if o <= limit:
            return o
        if l <= limit:
            return limit
        return None
    if side == "short":
        if o >= limit:
            return o
        if h >= limit:
            return limit
        return None
    return None


def simulate_decision_maker(
    decision: ShadowDecision,
    candles: Sequence[Candle],
    *,
    max_hold_ms: int,
    maker_entry_window_ms: int = 30_000,
    cost_model: Optional[ShadowCostModel] = None,
    funding_samples: Optional[Sequence[Tuple[int, float]]] = None,
) -> SimulatedOutcome:
    """Maker-entry variant of ``simulate_decision`` (research counterfactual).

    Posts a resting limit at the signal price and waits up to
    ``maker_entry_window_ms`` (mirrors live ``execution.maker_orders.
    timeout_ms``) for a candle touch. No touch => ``maker_no_fill`` — the
    skip count is the data: it measures how often the maker entry would
    never have happened. After a fill, the SL/TP/timeout exit path is
    identical to the taker variant (fill candle included, so the
    conservative dual-touch rule still applies); the hold deadline runs
    from the fill. Bias-flip exits are not modelled in this variant.
    """
    base = SimulatedOutcome(
        decision_id=decision.row_id,
        symbol=decision.symbol,
        strategy=decision.strategy,
        side=str(decision.side or ""),
        entry_price=0.0,
        entry_ts_ms=decision.timestamp_ms,
        exit_price=0.0,
        exit_ts_ms=decision.timestamp_ms,
        exit_reason="",
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        size_pct=0.0,
        pnl_pct=0.0,
        r_multiple=0.0,
        hold_minutes=0.0,
        evaluated=False,
        skip_reason=None,
    )
    if not decision.would_enter:
        return dataclasses_replace(base, skip_reason=SKIP_WOULD_NOT_ENTER)
    bracket = extract_bracket_params(decision.market_snapshot)
    if bracket is None:
        return dataclasses_replace(base, skip_reason=SKIP_MISSING_BRACKET)
    side = (decision.side or "").lower()
    if side not in ("long", "short"):
        return dataclasses_replace(base, skip_reason=SKIP_INVALID_SIDE)

    limit = float(bracket["price"])
    stop = float(bracket["stop_loss_pct"])
    take = float(bracket["take_profit_pct"])
    size = float(bracket["size_pct"])
    model = cost_model or resolve_maker_entry_cost_model()
    window_end = decision.timestamp_ms + int(maker_entry_window_ms)

    fill_px: Optional[float] = None
    fill_ts: Optional[int] = None
    fill_idx = -1
    for i, candle in enumerate(candles):
        if candle.timestamp_ms <= decision.timestamp_ms:
            continue
        if candle.timestamp_ms > window_end:
            break
        f = _maker_fill_price(candle, side, limit)
        if f is not None:
            fill_px, fill_ts, fill_idx = f, candle.timestamp_ms, i
            break
    if fill_px is None or fill_ts is None:
        return dataclasses_replace(
            base,
            entry_price=limit,
            stop_loss_pct=stop,
            take_profit_pct=take,
            size_pct=size,
            skip_reason=SKIP_MAKER_NO_FILL,
        )

    # Position exists from the fill — hold deadline runs from fill_ts.
    fill_decision = replace(decision, timestamp_ms=fill_ts)
    deadline = fill_ts + max_hold_ms
    last_candle: Optional[Candle] = None
    for candle in candles[fill_idx:]:
        last_candle = candle
        hit = resolve_candle_exit(
            side=side,
            entry=fill_px,
            stop_loss_pct=stop,
            take_profit_pct=take,
            candle=candle,
        )
        if hit is not None:
            exit_px, reason = hit
            return _finish_outcome(
                fill_decision,
                fill_px,
                stop,
                take,
                size,
                side,
                exit_px,
                candle.timestamp_ms,
                reason,
                candles=candles[fill_idx:],
                cost_model=model,
                funding_samples=funding_samples,
            )
        if candle.timestamp_ms >= deadline:
            return _finish_outcome(
                fill_decision,
                fill_px,
                stop,
                take,
                size,
                side,
                float(candle.close),
                candle.timestamp_ms,
                EXIT_TIMEOUT,
                candles=candles[fill_idx:],
                cost_model=model,
                funding_samples=funding_samples,
            )

    if last_candle is None or last_candle.timestamp_ms < deadline:
        return dataclasses_replace(
            base,
            entry_price=fill_px,
            stop_loss_pct=stop,
            take_profit_pct=take,
            size_pct=size,
            skip_reason=SKIP_INSUFFICIENT_CANDLES,
        )
    return _finish_outcome(
        fill_decision,
        fill_px,
        stop,
        take,
        size,
        side,
        float(last_candle.close),
        last_candle.timestamp_ms,
        EXIT_TIMEOUT,
        candles=candles[fill_idx:],
        cost_model=model,
        funding_samples=funding_samples,
    )


def _finish_outcome(
    decision: ShadowDecision,
    entry: float,
    stop: float,
    take: float,
    size: float,
    side: str,
    exit_px: float,
    exit_ts: int,
    reason: str,
    *,
    candles: Sequence[Candle] = (),
    cost_model: Optional[ShadowCostModel] = None,
    funding_samples: Optional[Sequence[Tuple[int, float]]] = None,
) -> SimulatedOutcome:
    gross = _pnl_pct(side, entry, exit_px)
    r_gross = _r_multiple(gross, stop)
    hold_min = max(0.0, (exit_ts - decision.timestamp_ms) / 60_000.0)
    model = cost_model or resolve_shadow_cost_model(decision.strategy)
    fee = model.round_trip_fee_frac
    slip = model.round_trip_slip_frac
    funding, fund_cov = _funding_during_hold(
        side, candles, decision.timestamp_ms, exit_ts,
        funding_samples=funding_samples,
    )
    net = gross - fee - slip + funding
    r_net = _r_multiple(net, stop)
    return SimulatedOutcome(
        decision_id=decision.row_id,
        symbol=decision.symbol,
        strategy=decision.strategy,
        side=side,
        entry_price=entry,
        entry_ts_ms=decision.timestamp_ms,
        exit_price=exit_px,
        exit_ts_ms=exit_ts,
        exit_reason=reason,
        stop_loss_pct=stop,
        take_profit_pct=take,
        size_pct=size,
        pnl_pct=gross,
        r_multiple=r_gross,
        hold_minutes=hold_min,
        evaluated=True,
        skip_reason=None,
        fee_cost_pct=fee,
        slip_cost_pct=slip,
        funding_pnl_pct=funding,
        net_pnl_pct=net,
        net_r_multiple=r_net,
        funding_coverage=fund_cov,
        cost_model_label=model.label,
    )


def _overlap_skip_outcome(decision: ShadowDecision) -> SimulatedOutcome:
    """Placeholder outcome for decisions dropped by the pre-simulation
    independence filter — never evaluated, never occupies a slot."""
    return SimulatedOutcome(
        decision_id=decision.row_id,
        symbol=decision.symbol,
        strategy=decision.strategy,
        side=str(decision.side or ""),
        entry_price=0.0,
        entry_ts_ms=decision.timestamp_ms,
        exit_price=0.0,
        exit_ts_ms=decision.timestamp_ms,
        exit_reason="",
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        size_pct=0.0,
        pnl_pct=0.0,
        r_multiple=0.0,
        hold_minutes=0.0,
        evaluated=False,
        skip_reason=SKIP_OVERLAP_DEDUP,
    )


def independent_outcomes(
    outcomes: Sequence[SimulatedOutcome],
) -> List[SimulatedOutcome]:
    """First-come, non-overlapping evaluated outcomes per symbol.

    One open simulated position per (board, symbol) at a time: an outcome
    counts only if its ``entry_ts_ms >= exit_ts_ms`` of the last kept
    outcome on that symbol. Non-evaluated outcomes never occupy the slot.
    Mirrors scripts/research/shadow_dedupe_recheck.py exactly — that
    script was the external review of this rule.
    """
    by_sym: Dict[str, List[SimulatedOutcome]] = defaultdict(list)
    for o in outcomes:
        if o.evaluated:
            by_sym[o.symbol].append(o)
    kept: List[SimulatedOutcome] = []
    for outs in by_sym.values():
        outs.sort(key=lambda o: o.entry_ts_ms)
        busy_until = -1
        for o in outs:
            if o.entry_ts_ms >= busy_until:
                kept.append(o)
                busy_until = o.exit_ts_ms
    return kept


def clustered_episode_count(outcomes: Sequence[SimulatedOutcome]) -> int:
    """Merge overlapping ``[entry, exit]`` intervals across all symbols.

    Cross-symbol effective sample size: BTC/ETH/SOL/HYPE positions open at
    the same time share one market episode and are not independent
    observations. Same boundary convention as ``independent_outcomes`` — a
    window starting exactly when another ended does not overlap.
    Informational only; gates keep reading ``n_independent``.
    """
    intervals = sorted(
        (o.entry_ts_ms, o.exit_ts_ms) for o in outcomes if o.evaluated
    )
    n_clusters = 0
    cur_end = -1
    for entry_ts, exit_ts in intervals:
        if entry_ts >= cur_end:
            n_clusters += 1
            cur_end = exit_ts
        else:
            cur_end = max(cur_end, exit_ts)
    return n_clusters


def aggregate_scoreboard(
    strategy: str,
    outcomes: Sequence[SimulatedOutcome],
    *,
    max_hold_ms: int,
    candle_source: str,
    n_decisions: int,
    variant: str = VARIANT_PHASE08_SHADOW,
    min_funding_coverage: float = 0.90,
    sealed_min_indep: Optional[int] = None,
    confirmation_cutoff_ms: Optional[int] = None,
    confirmation_expiry_ms: Optional[int] = None,
    now_ms: Optional[int] = None,
) -> StrategyScoreboard:
    """Build scoreboard metrics. Gross PF uses R; net PF uses net R.

    ``sealed_min_indep`` implements the no-peeking rule for preregistered
    confirmation samples: while ``n_independent < sealed_min_indep`` the
    board carries counts only — metric fields stay at their defaults so an
    interim read can never inform a stop/continue decision. The seal lifts
    at the final read — either ``n_independent`` reaching the target, or
    ``confirmation_expiry_ms`` passing (the preregistered deadline: an
    expired, under-target sample is verdict C by rule).
    """
    disclaimer = IDEALIZED_FILL_DISCLAIMER
    if variant == VARIANT_ROUTER_BLOCKED:
        disclaimer = f"{ROUTER_BLOCKED_SECTION_LABEL}. {IDEALIZED_FILL_DISCLAIMER}"
    board = StrategyScoreboard(
        strategy=strategy,
        variant=variant,
        n_decisions=n_decisions,
        max_hold_ms_used=max_hold_ms,
        candle_source=candle_source,
        disclaimer=disclaimer,
    )
    evaluated = [o for o in outcomes if o.evaluated]
    skipped = [o for o in outcomes if not o.evaluated]
    board.n_evaluated = len(evaluated)
    board.n_skipped = len(skipped)
    board.outcomes = list(outcomes)
    for o in skipped:
        reason = o.skip_reason or "unknown"
        board.skip_reasons[reason] = board.skip_reasons.get(reason, 0) + 1

    indep = independent_outcomes(outcomes)
    board.n_independent = len(indep)
    board.n_clustered = clustered_episode_count(indep)
    board.n_overlapped = len(evaluated) - len(indep)
    board.independent_outcome_rows = indep

    if sealed_min_indep is not None:
        board.confirmation_min_indep = sealed_min_indep
        board.confirmation_cutoff_ms = confirmation_cutoff_ms
        board.confirmation_expiry_ms = confirmation_expiry_ms
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        board.confirmation_expired = (
            confirmation_expiry_ms is not None
            and now > confirmation_expiry_ms
            and len(indep) < sealed_min_indep
        )
        board.sealed = (
            len(indep) < sealed_min_indep and not board.confirmation_expired
        )
        if board.sealed:
            return board

    if not evaluated:
        return board

    net_wins = 0
    net_losses = 0
    for o in indep:
        if o.exit_reason == EXIT_TIMEOUT:
            board.timeouts += 1
        if o.r_multiple > 0:
            board.wins += 1
        elif o.r_multiple < 0:
            board.losses += 1
        if o.net_r_multiple > 0:
            net_wins += 1
        elif o.net_r_multiple < 0:
            net_losses += 1

    board.win_rate = board.wins / len(indep) if indep else 0.0
    pf_trades = [{"pnl_usd": o.r_multiple} for o in indep]
    board.profit_factor = compute_profit_factor(pf_trades)
    board.expectancy_r = sum(o.r_multiple for o in indep) / len(indep)
    holds = [o.hold_minutes for o in indep]
    board.avg_hold_minutes = sum(holds) / len(holds)
    board.median_hold_minutes = float(statistics.median(holds))
    board.gross_hypothetical_pnl_pct = sum(o.pnl_pct for o in indep)

    net_pf_trades = [{"pnl_usd": o.net_r_multiple} for o in indep]
    board.net_profit_factor = compute_profit_factor(net_pf_trades)
    board.net_expectancy_r = sum(o.net_r_multiple for o in indep) / len(indep)
    board.net_hypothetical_pnl_pct = sum(o.net_pnl_pct for o in indep)
    board.mean_fee_cost_pct = sum(o.fee_cost_pct for o in indep) / len(indep)
    board.mean_slip_cost_pct = sum(o.slip_cost_pct for o in indep) / len(indep)
    board.mean_funding_pnl_pct = sum(o.funding_pnl_pct for o in indep) / len(indep)
    board.mean_funding_coverage = sum(o.funding_coverage for o in indep) / len(indep)
    board.funding_coverage_ok = board.mean_funding_coverage >= min_funding_coverage
    board.cost_model_label = evaluated[0].cost_model_label
    _ = (net_wins, net_losses)  # reserved for future net WR column
    return board


def evaluate_shadow_decisions(
    decisions: Sequence[ShadowDecision],
    *,
    config: Optional[Config] = None,
    research_db_path: Optional[Path] = None,
    live_db_path: Optional[Path] = LIVE_DB_DEFAULT,
    candle_loader: Optional[Any] = None,
    include_maker_variant: bool = True,
    excluded_counts: Optional[Dict[str, int]] = None,
) -> Dict[str, StrategyScoreboard]:
    """Evaluate decisions into scoreboards keyed by ``strategy::variant``.

    ``phase08_shadow`` and ``router_blocked`` for the same strategy never share
    a scoreboard entry. With ``include_maker_variant`` each group also gets a
    ``phase08_shadow_maker`` counterfactual board (limit-at-signal fill model)
    answering "do maker economics rescue the gross edge?"
    """
    cfg = config
    if cfg is None:
        try:
            cfg = load_config(Path("config/settings.yaml"))
        except Exception:  # noqa: BLE001
            cfg = Config({})

    if research_db_path is None:
        research_db_path = ResearchDatabase.resolve_path(cfg)
    else:
        research_db_path = Path(research_db_path)

    by_key: Dict[Tuple[str, str], List[ShadowDecision]] = {}
    regime_excluded: Dict[str, int] = {}
    for d in decisions:
        variant = d.variant or VARIANT_PHASE08_SHADOW
        prereg = PREREGISTERED_CONFIRMATIONS.get((d.strategy, variant))
        if prereg is not None and d.timestamp_ms > prereg[0]:
            # Post-cutoff rows belong to the sealed confirmation sample —
            # they never mix into the discovery board. Rows that could only
            # have entered via fallback promotion are excluded by flag.
            if _confirm_row_excluded(d):
                key = scoreboard_key(d.strategy, variant + VARIANT_CONFIRM_SUFFIX)
                regime_excluded[key] = regime_excluded.get(key, 0) + 1
                continue
            variant = variant + VARIANT_CONFIRM_SUFFIX
        by_key.setdefault((d.strategy, variant), []).append(d)
    if excluded_counts is not None:
        excluded_counts.update(regime_excluded)

    boards: Dict[str, StrategyScoreboard] = {}
    bias_store = None
    for (strategy, variant), group in by_key.items():
        is_confirm = variant.endswith(VARIANT_CONFIRM_SUFFIX)
        prereg = (
            PREREGISTERED_CONFIRMATIONS.get(
                (strategy, variant[: -len(VARIANT_CONFIRM_SUFFIX)])
            )
            if is_confirm
            else None
        )
        group_maker = include_maker_variant and not is_confirm
        max_hold = resolve_max_hold_ms(strategy, cfg)
        cost_model = resolve_shadow_cost_model(strategy, cfg)
        thr = 0.55
        if strategy == "TopTraderFlow" and cfg is not None:
            try:
                from src.utils.config import get_strategy_section

                sec = get_strategy_section(cfg, "top_trader_flow")
                thr = float(sec.get("bias_threshold", 0.55))
            except Exception:  # noqa: BLE001
                thr = 0.55
            if bias_store is None:
                try:
                    from src.research.top_trader_store import TopTraderStore

                    bias_store = TopTraderStore(
                        ResearchDatabase(Path(research_db_path), read_only=True)
                    )
                except Exception:  # noqa: BLE001
                    bias_store = False  # type: ignore[assignment]
        outcomes: List[SimulatedOutcome] = []
        maker_outcomes: List[SimulatedOutcome] = []
        maker_model = resolve_maker_entry_cost_model(cfg)
        maker_window = resolve_maker_entry_window_ms(cfg)
        # The maker fill can land up to the fill window after the signal —
        # candle coverage must reach decision + window + max_hold.
        lead = maker_window if group_maker else 0
        sources_seen: List[str] = []
        funding_cache: Dict[str, List[Tuple[int, float]]] = {}
        # Pre-simulation independence filter — same rule the scoreboard
        # applies post-hoc, so overlapped decisions skip the expensive
        # candle load + simulation entirely (14k signals -> ~53 sims for
        # TopTraderFlow). Exactness:
        #   taker — entry_ts == decision ts, so ts < busy_until is decisive;
        #   maker — entry_ts == fill ts (<= ts + maker_window), so skip only
        #           when even the latest possible fill is still inside the
        #           open slot;
        #   a skip only happens when the outcome could never be counted —
        #   the slot is armed exclusively by evaluated outcomes, matching
        #   ``independent_outcomes``.
        busy_until: Dict[str, int] = {}
        maker_busy_until: Dict[str, int] = {}
        for d in sorted(group, key=lambda x: (x.symbol, x.timestamp_ms)):
            taker_blocked = d.timestamp_ms < busy_until.get(d.symbol, -1)
            maker_blocked = group_maker and (
                d.timestamp_ms + maker_window < maker_busy_until.get(d.symbol, -1)
            )
            if taker_blocked and (not group_maker or maker_blocked):
                outcomes.append(_overlap_skip_outcome(d))
                if group_maker:
                    maker_outcomes.append(_overlap_skip_outcome(d))
                continue
            if candle_loader is not None:
                candles, src = candle_loader(d.symbol, d.timestamp_ms, max_hold)
            else:
                candles, src = load_forward_candles(
                    symbol=d.symbol,
                    entry_ts_ms=d.timestamp_ms,
                    max_hold_ms=max_hold,
                    research_db_path=Path(research_db_path),
                    live_db_path=Path(live_db_path) if live_db_path else None,
                    extra_lead_ms=lead,
                )
            sources_seen.append(src)
            samples: Optional[List[Dict[str, Any]]] = None
            if strategy == "TopTraderFlow" and bias_store not in (None, False):
                samples = bias_store.load_bias_samples(  # type: ignore[union-attr]
                    d.symbol,
                    start_ms=d.timestamp_ms,
                    end_ms=d.timestamp_ms + max_hold,
                )
            # Funding fallback: research candles never carry funding_rate
            # stamps; pull hourly-equivalent samples from live funding_history.
            if d.symbol not in funding_cache:
                funding_cache[d.symbol] = (
                    _load_funding_samples(
                        Path(live_db_path),
                        d.symbol,
                        min(x.timestamp_ms for x in group),
                        max(x.timestamp_ms for x in group) + max_hold,
                    )
                    if live_db_path
                    else []
                )
            if taker_blocked:
                outcomes.append(_overlap_skip_outcome(d))
            else:
                taker_out = simulate_decision(
                    d,
                    candles,
                    max_hold_ms=max_hold,
                    cost_model=cost_model,
                    bias_samples=samples,
                    bias_threshold=thr,
                    funding_samples=funding_cache[d.symbol],
                )
                outcomes.append(taker_out)
                if taker_out.evaluated:
                    busy_until[d.symbol] = taker_out.exit_ts_ms
            if group_maker:
                if maker_blocked:
                    maker_outcomes.append(_overlap_skip_outcome(d))
                else:
                    maker_out = simulate_decision_maker(
                        d,
                        candles,
                        max_hold_ms=max_hold,
                        maker_entry_window_ms=maker_window,
                        cost_model=maker_model,
                        funding_samples=funding_cache[d.symbol],
                    )
                    maker_outcomes.append(maker_out)
                    if maker_out.evaluated:
                        maker_busy_until[d.symbol] = maker_out.exit_ts_ms
        source_label = max(set(sources_seen), key=sources_seen.count) if sources_seen else ""
        board = aggregate_scoreboard(
            strategy,
            outcomes,
            max_hold_ms=max_hold,
            candle_source=source_label,
            n_decisions=len(group),
            variant=variant,
            min_funding_coverage=cost_model.min_funding_coverage,
            sealed_min_indep=prereg[1] if prereg is not None else None,
            confirmation_cutoff_ms=prereg[0] if prereg is not None else None,
            confirmation_expiry_ms=prereg[2] if prereg is not None else None,
        )
        boards[board.key] = board
        if group_maker:
            maker_board = aggregate_scoreboard(
                strategy,
                maker_outcomes,
                max_hold_ms=max_hold,
                candle_source=source_label,
                n_decisions=len(group),
                variant=VARIANT_PHASE08_SHADOW_MAKER,
                min_funding_coverage=maker_model.min_funding_coverage,
            )
            boards[maker_board.key] = maker_board
    for key, n in regime_excluded.items():
        board = boards.get(key)
        if board is None:
            # Every confirm row was excluded — surface a sealed empty board
            # so the count is visible instead of silently absent.
            strategy, variant = key.split("::", 1)
            board = StrategyScoreboard(
                strategy=strategy,
                variant=variant,
                sealed=True,
            )
            boards[key] = board
        board.n_regime_excluded = n
    return boards


def ensure_scoreboard_table(db: ResearchDatabase) -> None:
    """CREATE TABLE IF NOT EXISTS for scoreboard history (additive)."""
    with db._write_lock:
        conn = db._conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shadow_outcome_scoreboards (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluated_at_ms   INTEGER NOT NULL,
                strategy          TEXT    NOT NULL,
                since_ms          INTEGER,
                until_ms          INTEGER,
                candle_source     TEXT    NOT NULL,
                metrics_json      TEXT    NOT NULL,
                disclaimer        TEXT    NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shadow_scoreboard_ts "
            "ON shadow_outcome_scoreboards(evaluated_at_ms);"
        )
        conn.commit()


def persist_scoreboards(
    boards: Dict[str, StrategyScoreboard],
    *,
    db: Optional[ResearchDatabase] = None,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    evaluated_at_ms: Optional[int] = None,
) -> int:
    """Persist scoreboard snapshots. Returns rows written."""
    research = db or ResearchDatabase.open()
    ensure_scoreboard_table(research)
    ts = int(evaluated_at_ms if evaluated_at_ms is not None else time.time() * 1000)
    written = 0
    with research._write_lock:
        conn = research._conn()
        for board in boards.values():
            conn.execute(
                """
                INSERT INTO shadow_outcome_scoreboards
                (evaluated_at_ms, strategy, since_ms, until_ms,
                 candle_source, metrics_json, disclaimer)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    board.strategy,
                    since_ms,
                    until_ms,
                    board.candle_source,
                    json.dumps(board.to_dict(include_outcomes=False), sort_keys=True),
                    board.disclaimer,
                ),
            )
            written += 1
        conn.commit()
    return written


def format_scoreboard_table(boards: Dict[str, StrategyScoreboard]) -> str:
    """Human-readable multi-strategy scoreboard, variant sections separated."""
    lines = [IDEALIZED_FILL_DISCLAIMER, ""]

    def _emit_section(title: str, section_boards: List[StrategyScoreboard]) -> None:
        if not section_boards:
            return
        lines.append(title)
        lines.append(
            "  metrics over INDEPENDENT outcomes (one open position per "
            "symbol); n_eval = raw evaluated decisions; n_clu = cross-symbol "
            "merged episodes (informational)"
        )
        lines.append(
            f"{'strategy':20} {'variant':16} {'n_eval':>6} {'n_ind':>6} "
            f"{'n_clu':>6} "
            f"{'WR%':>6} {'PF_g':>6} {'PF_n':>6} {'E[R]_n':>7} {'PnL%_n':>8} "
            f"{'fee_bps':>7} {'fund_cov':>8}"
        )
        lines.append("-" * 136)
        for b in section_boards:
            if b.sealed:
                expiry = ""
                if b.confirmation_expiry_ms:
                    expiry = (
                        ", deadline "
                        + time.strftime(
                            "%Y-%m-%d",
                            time.gmtime(b.confirmation_expiry_ms / 1000),
                        )
                        + " (verdict C if reached under target)"
                    )
                lines.append(
                    f"{b.strategy:20} {b.variant:16} {b.n_evaluated:6d} "
                    f"{b.n_independent:6d} {b.n_clustered:6d}   SEALED — "
                    f"preregistered confirmation, metrics read once at "
                    f"indep_n>={b.confirmation_min_indep}{expiry}"
                )
                continue
            if b.confirmation_expired:
                lines.append(
                    f"  EXPIRED under target — preregistered final read, "
                    f"verdict C by rule (deadline "
                    + time.strftime(
                        "%Y-%m-%d", time.gmtime(b.confirmation_expiry_ms or 0)
                    )
                    + ")"
                )
            lines.append(
                f"{b.strategy:20} {b.variant:16} {b.n_evaluated:6d} "
                f"{b.n_independent:6d} {b.n_clustered:6d} "
                f"{100.0 * b.win_rate:6.1f} {b.profit_factor:6.2f} "
                f"{b.net_profit_factor:6.2f} {b.net_expectancy_r:7.3f} "
                f"{100.0 * b.net_hypothetical_pnl_pct:8.3f} "
                f"{b.mean_fee_cost_pct * 1e4:7.2f} "
                f"{b.mean_funding_coverage:8.2f}"
            )
            if b.variant == VARIANT_PHASE08_SHADOW_MAKER:
                note = "counterfactual maker-entry variant — not evidence about the taker/paper strategy"
                if b.strategy == "JevJudge":
                    note = (
                        "counterfactual 'Jev entraria por maker?' — NOT "
                        "evidence about the paper-executed JevJudge strategy"
                    )
                lines.append(f"  {note}")
            if not b.funding_coverage_ok:
                lines.append(
                    "  funding_coverage_ok=False — net metrics INCONCLUSIVE for PASS gates"
                )
            if b.cost_model_label:
                lines.append(f"  cost: {b.cost_model_label}")
            if b.skip_reasons:
                reasons = ", ".join(
                    f"{k}={v}" for k, v in sorted(b.skip_reasons.items())
                )
                lines.append(f"  skip_reasons: {reasons}")
                lines.append(
                    f"  max_hold_ms={b.max_hold_ms_used}  candles={b.candle_source}"
                )
        lines.append("")

    ordered = sorted(boards.values(), key=lambda b: (b.variant, b.strategy))
    shadow = [b for b in ordered if b.variant != VARIANT_ROUTER_BLOCKED]
    blocked = [b for b in ordered if b.variant == VARIANT_ROUTER_BLOCKED]
    _emit_section("=== phase08_shadow (true shadow strategies) ===", shadow)
    _emit_section(
        f"=== router_blocked — {ROUTER_BLOCKED_SECTION_LABEL} ===",
        blocked,
    )
    return "\n".join(lines).rstrip() + "\n"


def run_evaluation(
    *,
    strategy: Optional[str] = None,
    variant: Optional[str] = None,
    since_days: Optional[float] = None,
    research_db_path: Optional[Path] = None,
    live_db_path: Optional[Path] = LIVE_DB_DEFAULT,
    config: Optional[Config] = None,
    persist: bool = False,
) -> Dict[str, Any]:
    """Load decisions, evaluate, optionally persist. Returns JSON-able summary."""
    cfg = config
    if cfg is None:
        try:
            cfg = load_config(Path("config/settings.yaml"))
        except Exception:  # noqa: BLE001
            cfg = Config({})
    if research_db_path is None:
        research_db_path = ResearchDatabase.resolve_path(cfg)
    else:
        research_db_path = Path(research_db_path)
    # Read paths must not run DDL or take write locks on the research DB —
    # only ``--persist`` writes (the scoreboard snapshot), so the handle is
    # RW exactly then.
    db = ResearchDatabase(research_db_path, read_only=not persist)
    recorder = ShadowRecorder(db)
    since_ms: Optional[int] = None
    if since_days is not None:
        since_ms = int(time.time() * 1000 - float(since_days) * 86_400_000)
    decisions = recorder.load_decisions(
        strategy=strategy,
        variant=variant,
        since_ms=since_ms,
        # Evidence path: ``since_days=None`` must keep the full-history
        # contract — opt out of the 90d default read bound explicitly.
        window_ms=None,
    )
    boards = evaluate_shadow_decisions(
        decisions,
        config=config,
        research_db_path=Path(research_db_path),
        live_db_path=Path(live_db_path) if live_db_path else None,
    )
    if persist and boards:
        until_ms = max((d.timestamp_ms for d in decisions), default=None)
        persist_scoreboards(boards, db=db, since_ms=since_ms, until_ms=until_ms)
    return {
        "disclaimer": IDEALIZED_FILL_DISCLAIMER,
        "n_decisions_loaded": len(decisions),
        "variant_filter": variant,
        "strategies": {k: v.to_dict() for k, v in boards.items()},
        "persisted": bool(persist),
        "table": format_scoreboard_table(boards),
    }
