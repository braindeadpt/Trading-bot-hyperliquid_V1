"""Shadow outcome evaluator — hypothetical bracket results from shadow decisions.

Research / observability only. Numbers here may later influence shadow→live
promotion decisions, so fill rules are intentionally **pessimistic** and
never invent missing bracket parameters.

Idealized fills
---------------
All simulated PnL is **idealized**: no fees, no slippage, no queue position.
Label every user-facing scoreboard with that disclaimer.

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

import json
import logging
import sqlite3
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data.database import Candle
from src.data.research_database import DEFAULT_RESEARCH_DB_PATH, ResearchDatabase
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
    "IDEALIZED FILLS: no fees, no slippage, no queue position — "
    "hypothetical gross only; not executable edge."
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
}

# Documented defaults when config section is missing/empty.
DEFAULT_MAX_HOLD_MS: Dict[str, int] = {
    "OrderBookScalper": 300 * 1000,
    "CVDOrderFlow": 6 * 3600 * 1000,
    "ChecklistMeta": 6 * 3600 * 1000,
    "FundingArbitrage": 8 * 3600 * 1000,
    "FundingMomentum": 12 * 3600 * 1000,
    "SpotPerpCarry": 24 * 3600 * 1000,
}

SKIP_MISSING_BRACKET = "missing_bracket_params"
SKIP_INSUFFICIENT_CANDLES = "insufficient_candles"
SKIP_INVALID_SIDE = "invalid_side"
SKIP_WOULD_NOT_ENTER = "would_not_enter"

EXIT_TP = "take_profit"
EXIT_SL = "stop_loss"
EXIT_TIMEOUT = "timeout"
EXIT_GAP_SL = "gap_stop_loss"
EXIT_GAP_TP = "gap_take_profit"

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
    pnl_pct: float
    r_multiple: float
    hold_minutes: float
    evaluated: bool = True
    skip_reason: Optional[str] = None


VARIANT_PHASE08_SHADOW = "phase08_shadow"
VARIANT_ROUTER_BLOCKED = "router_blocked"

ROUTER_BLOCKED_SECTION_LABEL = (
    "counterfactual — signals the router blocked; idealized fills"
)


@dataclass
class StrategyScoreboard:
    """Aggregated idealized outcomes for one (strategy, variant) pair."""

    strategy: str
    variant: str = VARIANT_PHASE08_SHADOW
    n_decisions: int = 0
    n_evaluated: int = 0
    n_skipped: int = 0
    skip_reasons: Dict[str, int] = field(default_factory=dict)
    wins: int = 0
    losses: int = 0
    timeouts: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    avg_hold_minutes: float = 0.0
    median_hold_minutes: float = 0.0
    gross_hypothetical_pnl_pct: float = 0.0
    max_hold_ms_used: int = 0
    candle_source: str = ""
    disclaimer: str = IDEALIZED_FILL_DISCLAIMER
    outcomes: List[SimulatedOutcome] = field(default_factory=list)

    @property
    def key(self) -> str:
        return scoreboard_key(self.strategy, self.variant)

    def to_dict(self, *, include_outcomes: bool = False) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "strategy": self.strategy,
            "variant": self.variant,
            "n_decisions": self.n_decisions,
            "n_evaluated": self.n_evaluated,
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
            "max_hold_ms_used": self.max_hold_ms_used,
            "candle_source": self.candle_source,
            "disclaimer": self.disclaimer,
        }
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
) -> Tuple[List[Candle], str]:
    """Load subsequent 1m candles for outcome simulation (read-only paths).

    Returns ``(candles ASC, source_label)``.
    """
    end_ms = entry_ts_ms + max_hold_ms + 60_000  # small buffer past timeout
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
) -> SimulatedOutcome:
    """Simulate one shadow entry against forward 1m candles."""
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
                decision, entry, stop, take, size, side, exit_px, candle.timestamp_ms, reason
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
    )


def dataclasses_replace(outcome: SimulatedOutcome, **kwargs: Any) -> SimulatedOutcome:
    data = asdict(outcome)
    data.update(kwargs)
    return SimulatedOutcome(**data)


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
) -> SimulatedOutcome:
    pnl = _pnl_pct(side, entry, exit_px)
    r = _r_multiple(pnl, stop)
    hold_min = max(0.0, (exit_ts - decision.timestamp_ms) / 60_000.0)
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
        pnl_pct=pnl,
        r_multiple=r,
        hold_minutes=hold_min,
        evaluated=True,
        skip_reason=None,
    )


def aggregate_scoreboard(
    strategy: str,
    outcomes: Sequence[SimulatedOutcome],
    *,
    max_hold_ms: int,
    candle_source: str,
    n_decisions: int,
    variant: str = VARIANT_PHASE08_SHADOW,
) -> StrategyScoreboard:
    """Build scoreboard metrics. PF uses R-multiples as the pnl series."""
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

    if not evaluated:
        return board

    for o in evaluated:
        if o.exit_reason == EXIT_TIMEOUT:
            board.timeouts += 1
        if o.r_multiple > 0:
            board.wins += 1
        elif o.r_multiple < 0:
            board.losses += 1
        # r_multiple == 0: flat — neither win nor loss

    board.win_rate = board.wins / len(evaluated) if evaluated else 0.0
    # Profit factor on R-multiples (same sentinel as phase10 / monte_carlo)
    pf_trades = [{"pnl_usd": o.r_multiple} for o in evaluated]
    board.profit_factor = compute_profit_factor(pf_trades)
    board.expectancy_r = sum(o.r_multiple for o in evaluated) / len(evaluated)
    holds = [o.hold_minutes for o in evaluated]
    board.avg_hold_minutes = sum(holds) / len(holds)
    board.median_hold_minutes = float(statistics.median(holds))
    board.gross_hypothetical_pnl_pct = sum(o.pnl_pct for o in evaluated)
    return board


def evaluate_shadow_decisions(
    decisions: Sequence[ShadowDecision],
    *,
    config: Optional[Config] = None,
    research_db_path: Path = DEFAULT_RESEARCH_DB_PATH,
    live_db_path: Optional[Path] = LIVE_DB_DEFAULT,
    candle_loader: Optional[Any] = None,
) -> Dict[str, StrategyScoreboard]:
    """Evaluate decisions into scoreboards keyed by ``strategy::variant``.

    ``phase08_shadow`` and ``router_blocked`` for the same strategy never share
    a scoreboard entry.
    """
    cfg = config
    if cfg is None:
        try:
            cfg = load_config(Path("config/settings.yaml"))
        except Exception:  # noqa: BLE001
            cfg = Config({})

    by_key: Dict[Tuple[str, str], List[ShadowDecision]] = {}
    for d in decisions:
        variant = d.variant or VARIANT_PHASE08_SHADOW
        by_key.setdefault((d.strategy, variant), []).append(d)

    boards: Dict[str, StrategyScoreboard] = {}
    for (strategy, variant), group in by_key.items():
        max_hold = resolve_max_hold_ms(strategy, cfg)
        outcomes: List[SimulatedOutcome] = []
        sources_seen: List[str] = []
        for d in group:
            if candle_loader is not None:
                candles, src = candle_loader(d.symbol, d.timestamp_ms, max_hold)
            else:
                candles, src = load_forward_candles(
                    symbol=d.symbol,
                    entry_ts_ms=d.timestamp_ms,
                    max_hold_ms=max_hold,
                    research_db_path=Path(research_db_path),
                    live_db_path=Path(live_db_path) if live_db_path else None,
                )
            sources_seen.append(src)
            outcomes.append(simulate_decision(d, candles, max_hold_ms=max_hold))
        source_label = max(set(sources_seen), key=sources_seen.count) if sources_seen else ""
        board = aggregate_scoreboard(
            strategy,
            outcomes,
            max_hold_ms=max_hold,
            candle_source=source_label,
            n_decisions=len(group),
            variant=variant,
        )
        boards[board.key] = board
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
    research = db or ResearchDatabase(DEFAULT_RESEARCH_DB_PATH)
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
            f"{'strategy':20} {'variant':16} {'n_eval':>6} {'skip':>5} {'wins':>5} "
            f"{'loss':>5} {'tout':>5} {'WR%':>6} {'PF':>6} {'E[R]':>7} "
            f"{'PnL%':>8} {'avgHoldm':>8}"
        )
        lines.append("-" * 120)
        for b in section_boards:
            lines.append(
                f"{b.strategy:20} {b.variant:16} {b.n_evaluated:6d} {b.n_skipped:5d} "
                f"{b.wins:5d} {b.losses:5d} {b.timeouts:5d} "
                f"{100.0 * b.win_rate:6.1f} {b.profit_factor:6.2f} "
                f"{b.expectancy_r:7.3f} {100.0 * b.gross_hypothetical_pnl_pct:8.3f} "
                f"{b.avg_hold_minutes:8.2f}"
            )
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
    research_db_path: Path = DEFAULT_RESEARCH_DB_PATH,
    live_db_path: Optional[Path] = LIVE_DB_DEFAULT,
    config: Optional[Config] = None,
    persist: bool = False,
) -> Dict[str, Any]:
    """Load decisions, evaluate, optionally persist. Returns JSON-able summary."""
    db = ResearchDatabase(research_db_path)
    recorder = ShadowRecorder(db)
    since_ms: Optional[int] = None
    if since_days is not None:
        since_ms = int(time.time() * 1000 - float(since_days) * 86_400_000)
    decisions = recorder.load_decisions(
        strategy=strategy,
        variant=variant,
        since_ms=since_ms,
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
