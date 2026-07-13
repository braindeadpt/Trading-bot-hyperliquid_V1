"""Fase 10 live-vs-replay drift detector.

Compares what the live paper engine actually recorded to ``data/live/bot.db``
for a historical window against a fresh :class:`~src.backtest.engine.BacktestEngine`
replay of the *exact same candles* (a read-only snapshot of the same DB) under
the *exact same effective config*. Divergence here means live and backtest have
silently drifted apart — a bug only present in one code path — which would
invalidate the "backtest and live share the same logic" determinism principle
documented in ``AGENTS.md``.

Design
------
* **Live side**: closed ``trades`` rows (reusing
  :func:`src.research.phase10_gate_metrics.load_qualifying_trades` for the
  read-only query, then post-filtering by ``end_ms``) plus ``decision_audit``
  rows (verified schema: ``timestamp, decision_type, symbol, side, strategy,
  signal_confidence, result, reason, metadata`` — see
  ``Database._create_decision_audit_table``). ``decision_type`` values are the
  *exact* gate names from ``src.core.signal_pipeline.GATE_ORDER`` (confirmed by
  reading ``src/core/engine.py``'s ``decision_type_map``), so gate-rejection
  counts line up directly with backtest replay gate names modulo the
  normalization table below.
* **Replay side**: :class:`BacktestEngine` run against a **read-only backup
  copy** of ``data/live/bot.db`` (``sqlite3.Connection.backup`` from a
  ``mode=ro`` source connection into a tmp file) — this guarantees the replay
  sees byte-identical candles/funding/OI to what live actually traded on,
  while the live DB file itself is never opened in read-write mode and never
  written to.
* Strategy construction mirrors ``strategy.phase08.execution_strategies``
  exactly as ``phase10_preregister.py`` reads it (only the live-executing
  strategies produce trades in Fase 08 mode; shadow strategies are excluded
  because they never reach the live ``trades`` table either).

Known gap (documented, not papered over)
-----------------------------------------
The live ``trades`` table (see ``Database._create_trades_table`` /
``_migrate_trades_table``) has **no MFE/MAE columns at all** — favorable/
adverse excursion is only ever computed in the backtest engine
(``src/backtest/mfe_mae.py``). The live side of the MFE/MAE dimension is
therefore always reported as ``NOT_COMPARABLE`` rather than silently
defaulting to zero or being omitted. This is a real gap in what the live
engine persists, not a bug in this comparator.

Similarly, the ``trades`` table stores only the *filled* ``entry_price`` /
``exit_price`` — there is no persisted pre-fill "intended" price to compute
slippage against directly. Both sides are instead compared against the same
1m candle close (from the shared snapshot) as a common reference, documented
under "Fill price / slippage" below.

Drift thresholds
-----------------
Modelled on the existing tolerance convention in
``src/data/candle_providers/parity.py`` (relative tolerance + a small
absolute floor, rather than exact equality, because independent code paths
reading the same underlying data can differ by float rounding or by a few
milliseconds of wall-clock jitter without that being a real bug):

* ``entry_time_jitter_ms`` (60_000 = 1 candle): live signals fire off
  real-time WS ticks; replay fires off the bar-close event. Two candles
  apart is already suspicious; more than that is treated as an unmatched
  trade (a signal-count divergence), not a "jittered" match.
* ``hold_duration_diff_ms`` (300_000 = 5 candles): intrabar stop/TP fills in
  replay resolve to the bar boundary, while live fills resolve to whatever
  tick crossed the level; funding settlement rounding and debounce timers add
  a few more candles of slack before this is treated as drift.
* ``notional_rel_tol`` (0.25): position sizing is capital-proportional
  (``RiskManager.calculate_position_size``), and live capital diverges from
  the backtest's freshly-seeded capital over a multi-week window purely
  because of prior trades' PnL — a wide tolerance avoids flagging expected
  compounding drift as a bug. Only a >25% relative gap (same sign) or a sign
  flip is flagged.
* ``fee_rel_tol`` (0.05) / ``fee_abs_tol_usd`` (0.01): fees are a
  deterministic function of notional and a config fee rate, so they should
  match closely; the abs floor absorbs sub-cent rounding on tiny trades.
* ``slippage_bps_abs_tol`` (5.0 bps): both sides are compared against the
  same 1m candle close (see gap note above), so this tolerance covers the
  fact that live fills happen at a real, continuously-moving price while the
  replay fill is anchored to a static bar close — a few bps of "slippage
  proxy" difference is expected microstructure noise, not drift.
* ``gate_count_rel_tol`` (0.20) with an absolute floor of 5: gate-rejection
  counts over any given window are small-sample and noisy (live evaluates
  against the true, continuously updating tick stream; replay evaluates once
  per closed 1m bar), so raw counts are compared with a generous relative
  tolerance, and any comparison below the absolute floor is judged on the
  absolute floor instead of the (noisy) percentage.

Anything outside these tolerances, any exit_reason mismatch on a matched
trade, and any unmatched trade count are DRIFT_DETECTED — no numeric
tolerance is applied to those because they represent categorically different
decisions, not measurement noise.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.backtest.engine import BacktestEngine, build_backtest_config_from_yaml
from src.core.signal_pipeline import LIVE_ONLY_GATES
from src.data.database import Database
from src.research.phase10_gate_metrics import DEFAULT_DB_PATH, load_qualifying_trades
from src.strategies.base import Strategy
from src.strategies.factory import (
    DirectStrategyRouter,
    build_backtest_strategy,
    build_phase08_strategies,
)
from src.utils.config import Config, get_strategy_section, phase08_enabled
from src.utils.helpers import safe_float, safe_json_loads

logger = logging.getLogger(__name__)

PASS = "PASS"
DRIFT = "DRIFT_DETECTED"
NOT_COMPARABLE = "NOT_COMPARABLE"

# Backtest-only gate names -> the equivalent live decision_audit decision_type
# string (see src/core/engine.py's decision_type_map and
# src.core.signal_pipeline.GATE_ORDER). Anything not listed here is assumed
# identical on both sides.
_BACKTEST_TO_LIVE_GATE_NAME: Dict[str, str] = {
    "replay_data_quality": "feed_health",
    "chase_filter": "chase",
}

# Gates that only exist in the live path (execution-layer concerns with no
# backtest equivalent — see LIVE_ONLY_GATES) are excluded from the
# gate-rejection comparison entirely; they are reported separately as
# "live_only" so they are visible but never scored as drift.
_LIVE_ONLY_GATES = frozenset(LIVE_ONLY_GATES)

DRIFT_THRESHOLDS: Dict[str, float] = {
    "entry_time_jitter_ms": 60_000.0,
    "hold_duration_diff_ms": 300_000.0,
    "notional_rel_tol": 0.25,
    "fee_rel_tol": 0.05,
    "fee_abs_tol_usd": 0.01,
    "slippage_bps_abs_tol": 5.0,
    "gate_count_rel_tol": 0.20,
    "gate_count_abs_floor": 5,
}


# ---------------------------------------------------------------------------
# Data loading (read-only against data/live/bot.db)
# ---------------------------------------------------------------------------


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Mirror of ``phase10_gate_metrics._open_readonly`` — never writes."""
    if not db_path.exists():
        raise FileNotFoundError(f"Trades DB not found: {db_path}")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_live_trades(
    db_path: Path,
    *,
    start_ms: int,
    end_ms: int,
    execution_strategies: Sequence[str],
) -> List[Dict[str, Any]]:
    """Closed trades in [start_ms, end_ms] for the given strategies.

    Reuses ``load_qualifying_trades`` (identical read-only-connection logic)
    for the ``entry_time >= start_ms`` + strategy filter, then post-filters
    by ``end_ms`` in Python since that helper only takes a lower bound.
    """
    rows = load_qualifying_trades(
        db_path,
        window_start_ms=start_ms,
        execution_strategies=list(execution_strategies),
    )
    return [r for r in rows if int(r.get("exit_time") or 0) <= end_ms]


def load_live_decision_audit(
    db_path: Path,
    *,
    start_ms: int,
    end_ms: int,
    strategies: Sequence[str],
) -> List[Dict[str, Any]]:
    """Gate-rejection (and other) decision_audit rows in [start_ms, end_ms].

    Schema verified against ``Database._create_decision_audit_table``:
    ``timestamp, decision_type, symbol, side, strategy, signal_confidence,
    result, reason, metadata``. Opened strictly read-only.
    """
    strategies = list(strategies)
    if not strategies:
        return []
    placeholders = ",".join("?" for _ in strategies)
    sql = f"""
        SELECT *
        FROM decision_audit
        WHERE timestamp >= ? AND timestamp <= ?
          AND strategy IN ({placeholders})
        ORDER BY timestamp ASC
    """
    conn = _open_readonly(db_path)
    try:
        cur = conn.execute(sql, (int(start_ms), int(end_ms), *strategies))
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


def snapshot_live_db_readonly(src_path: Path, dest_path: Path) -> None:
    """Create a full, consistent copy of *src_path* at *dest_path*.

    Uses ``sqlite3.Connection.backup`` with the source opened via a
    ``mode=ro`` URI, so the original live DB file is never opened for
    writing and no query against it is anything but a read. The backup
    correctly folds in any pending WAL frames (unlike a raw file copy).
    """
    src_conn = _open_readonly(src_path)
    dest_conn = sqlite3.connect(str(dest_path))
    try:
        src_conn.backup(dest_conn)
        dest_conn.commit()
    finally:
        dest_conn.close()
        src_conn.close()


# ---------------------------------------------------------------------------
# Strategy / execution-set resolution (mirrors phase10_preregister.py)
# ---------------------------------------------------------------------------


def resolve_execution_strategies(config: Config) -> List[str]:
    """Read ``strategy.phase08.execution_strategies`` exactly like
    ``phase10_preregister.build_preregister_manifest`` does."""
    p08 = get_strategy_section(config, "phase08")
    return sorted(str(s) for s in p08.get("execution_strategies", []) or [])


def build_replay_strategy(config: Config) -> Strategy:
    """Build the replay-side strategy object to match what live actually runs.

    When Fase 08 is enabled, only the execution strategies produce trades
    live (shadow strategies are signal-tracked but never executed / never
    reach ``trades``), so the replay must be restricted the same way —
    ``build_backtest_strategy`` alone does *not* do this (it only special-
    cases ``strategy.ensemble.enabled``), so Fase 08 routing is applied here
    explicitly, reusing ``build_phase08_strategies`` + ``DirectStrategyRouter``
    exactly as ``src/strategies/factory.build_live_strategies`` does for the
    live engine.
    """
    if phase08_enabled(config):
        execution, _shadow = build_phase08_strategies(config)
        return DirectStrategyRouter(execution)
    return build_backtest_strategy(config)


def run_replay(
    config: Config,
    *,
    symbols: Sequence[str],
    start_ms: int,
    end_ms: int,
    live_db_path: Path,
    strategy_override: Optional[Strategy] = None,
) -> Dict[str, Any]:
    """Run BacktestEngine over a read-only snapshot of the live DB.

    Returns the standard ``BacktestEngine.run()`` result dict plus a
    ``gate_rejections`` key (see the additive ``BacktestEngine.gate_rejections``
    list — diagnostic-only, does not change simulated trading behaviour).

    ``strategy_override`` lets callers (tests) supply a deterministic
    :class:`Strategy` double instead of the full live registry — the rest of
    the pipeline (RiskManager, SignalPipeline gates, fees/slippage, MFE/MAE)
    is still the real, unmodified ``BacktestEngine``.
    """
    with tempfile.TemporaryDirectory(prefix="live_vs_replay_") as tmp:
        snapshot_path = Path(tmp) / "replay_snapshot.db"
        snapshot_live_db_readonly(live_db_path, snapshot_path)
        db = Database(snapshot_path)
        try:
            strategy = strategy_override or build_replay_strategy(config)
            bt_config = build_backtest_config_from_yaml(config)
            engine = BacktestEngine(
                db, strategy, bt_config, symbols=list(symbols), risk_config=config,
            )
            result = engine.run(start_ms, end_ms)
            result["gate_rejections"] = list(engine.gate_rejections)
            return result
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Trade matching
# ---------------------------------------------------------------------------


@dataclass
class TradePair:
    live: Dict[str, Any]
    replay: Dict[str, Any]
    entry_time_diff_ms: int


def match_trades(
    live_trades: Sequence[Dict[str, Any]],
    replay_trades: Sequence[Dict[str, Any]],
    *,
    jitter_tol_ms: float = DRIFT_THRESHOLDS["entry_time_jitter_ms"],
) -> Tuple[List[TradePair], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Greedy nearest-neighbour match by (strategy, symbol, side, entry_time).

    Returns ``(pairs, unmatched_live, unmatched_replay)``. A live trade only
    matches a replay trade of the same strategy/symbol/side whose entry_time
    is within ``jitter_tol_ms`` and which hasn't already been claimed by a
    closer live trade.
    """
    groups: Dict[Tuple[str, str, str], Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]] = {}
    for t in live_trades:
        key = (str(t.get("strategy")), str(t.get("symbol")), str(t.get("side")))
        groups.setdefault(key, ([], []))[0].append(t)
    for t in replay_trades:
        key = (str(t.get("strategy")), str(t.get("symbol")), str(t.get("side")))
        groups.setdefault(key, ([], []))[1].append(t)

    pairs: List[TradePair] = []
    unmatched_live: List[Dict[str, Any]] = []
    unmatched_replay: List[Dict[str, Any]] = []

    for (_strat, _sym, _side), (lives, replays) in groups.items():
        lives_sorted = sorted(lives, key=lambda t: int(t.get("entry_time") or 0))
        replays_left = sorted(replays, key=lambda t: int(t.get("entry_time") or 0))
        for lt in lives_sorted:
            lt_entry = int(lt.get("entry_time") or 0)
            best_idx: Optional[int] = None
            best_diff: Optional[float] = None
            for idx, rt in enumerate(replays_left):
                rt_entry = int(rt.get("entry_time") or 0)
                diff = abs(rt_entry - lt_entry)
                if diff > jitter_tol_ms:
                    continue
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_idx = idx
            if best_idx is None:
                unmatched_live.append(lt)
                continue
            rt = replays_left.pop(best_idx)
            pairs.append(TradePair(live=lt, replay=rt, entry_time_diff_ms=int(best_diff or 0)))
        unmatched_replay.extend(replays_left)

    return pairs, unmatched_live, unmatched_replay


# ---------------------------------------------------------------------------
# Per-dimension comparisons
# ---------------------------------------------------------------------------


@dataclass
class DimensionResult:
    dimension: str
    verdict: str
    detail: str
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension,
            "verdict": self.verdict,
            "detail": self.detail,
            **self.extra,
        }


def _dim_signal_count(
    live_trades: Sequence[Dict[str, Any]],
    replay_trades: Sequence[Dict[str, Any]],
    unmatched_live: Sequence[Dict[str, Any]],
    unmatched_replay: Sequence[Dict[str, Any]],
) -> DimensionResult:
    n_live, n_replay = len(live_trades), len(replay_trades)
    if n_live == n_replay and not unmatched_live and not unmatched_replay:
        return DimensionResult(
            "signal_count", PASS,
            f"{n_live} live trades matched {n_replay} replay trades 1:1.",
            extra={"live_count": n_live, "replay_count": n_replay},
        )
    return DimensionResult(
        "signal_count", DRIFT,
        f"live={n_live} replay={n_replay} unmatched_live={len(unmatched_live)} "
        f"unmatched_replay={len(unmatched_replay)}.",
        extra={
            "live_count": n_live,
            "replay_count": n_replay,
            "unmatched_live": len(unmatched_live),
            "unmatched_replay": len(unmatched_replay),
        },
    )


def _dim_exit_reason(pairs: Sequence[TradePair]) -> DimensionResult:
    mismatches = [
        p for p in pairs
        if str(p.live.get("exit_reason")) != str(p.replay.get("exit_reason"))
    ]
    if not pairs:
        return DimensionResult("exit_reason", NOT_COMPARABLE, "No matched trades.")
    if not mismatches:
        return DimensionResult(
            "exit_reason", PASS, f"All {len(pairs)} matched trades agree on exit_reason.",
        )
    sample = [
        {
            "symbol": p.live.get("symbol"),
            "entry_time": p.live.get("entry_time"),
            "live_exit_reason": p.live.get("exit_reason"),
            "replay_exit_reason": p.replay.get("exit_reason"),
        }
        for p in mismatches[:10]
    ]
    return DimensionResult(
        "exit_reason", DRIFT,
        f"{len(mismatches)}/{len(pairs)} matched trades have a different exit_reason.",
        extra={"mismatch_count": len(mismatches), "sample": sample},
    )


def _rel_diff(a: float, b: float) -> float:
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom


def _dim_notional(pairs: Sequence[TradePair], tol: float) -> DimensionResult:
    if not pairs:
        return DimensionResult("notional", NOT_COMPARABLE, "No matched trades.")
    worst = 0.0
    flagged = []
    for p in pairs:
        live_notional = safe_float(p.live.get("entry_price")) * safe_float(p.live.get("size"))
        replay_notional = safe_float(p.replay.get("entry_price")) * safe_float(p.replay.get("size"))
        rel = _rel_diff(live_notional, replay_notional)
        worst = max(worst, rel)
        if rel > tol:
            flagged.append({
                "symbol": p.live.get("symbol"), "entry_time": p.live.get("entry_time"),
                "live_notional": live_notional, "replay_notional": replay_notional,
                "rel_diff": rel,
            })
    verdict = DRIFT if flagged else PASS
    detail = (
        f"max relative notional diff={worst:.3f} across {len(pairs)} trades "
        f"(tolerance={tol})."
    )
    return DimensionResult(
        "notional", verdict, detail,
        extra={"max_rel_diff": worst, "flagged_count": len(flagged), "sample": flagged[:10]},
    )


def _dim_fees(pairs: Sequence[TradePair], rel_tol: float, abs_tol_usd: float) -> DimensionResult:
    if not pairs:
        return DimensionResult("fees", NOT_COMPARABLE, "No matched trades.")
    flagged = []
    worst = 0.0
    for p in pairs:
        live_fee = (
            safe_float(p.live.get("entry_fee"))
            + safe_float(p.live.get("cumulative_order_fee"))
        )
        replay_fee = safe_float(p.replay.get("fees_paid"))
        diff = abs(live_fee - replay_fee)
        rel = _rel_diff(live_fee, replay_fee)
        worst = max(worst, rel)
        if diff > abs_tol_usd and rel > rel_tol:
            flagged.append({
                "symbol": p.live.get("symbol"), "entry_time": p.live.get("entry_time"),
                "live_fee_usd": live_fee, "replay_fee_usd": replay_fee, "rel_diff": rel,
            })
    verdict = DRIFT if flagged else PASS
    detail = (
        f"max relative fee diff={worst:.3f} across {len(pairs)} trades "
        f"(rel_tol={rel_tol}, abs_floor_usd={abs_tol_usd})."
    )
    return DimensionResult(
        "fees", verdict, detail,
        extra={"max_rel_diff": worst, "flagged_count": len(flagged), "sample": flagged[:10]},
    )


def _dim_hold_duration(pairs: Sequence[TradePair], tol_ms: float) -> DimensionResult:
    if not pairs:
        return DimensionResult("hold_duration", NOT_COMPARABLE, "No matched trades.")
    flagged = []
    worst = 0
    for p in pairs:
        live_hold = int(p.live.get("exit_time") or 0) - int(p.live.get("entry_time") or 0)
        replay_hold = int(p.replay.get("exit_time") or 0) - int(p.replay.get("entry_time") or 0)
        diff = abs(live_hold - replay_hold)
        worst = max(worst, diff)
        if diff > tol_ms:
            flagged.append({
                "symbol": p.live.get("symbol"), "entry_time": p.live.get("entry_time"),
                "live_hold_ms": live_hold, "replay_hold_ms": replay_hold, "diff_ms": diff,
            })
    verdict = DRIFT if flagged else PASS
    detail = f"max hold-duration diff={worst}ms across {len(pairs)} trades (tolerance={tol_ms}ms)."
    return DimensionResult(
        "hold_duration", verdict, detail,
        extra={"max_diff_ms": worst, "flagged_count": len(flagged), "sample": flagged[:10]},
    )


def _candle_close_at_or_before(db: Database, symbol: str, ts_ms: int) -> Optional[float]:
    rows = db.get_candles(symbol, "1m", limit=1, start_ms=ts_ms - 5 * 60_000, end_ms=ts_ms)
    if not rows:
        return None
    return float(rows[-1].close)


def _dim_slippage(
    pairs: Sequence[TradePair],
    *,
    replay_db: Optional[Database],
    abs_tol_bps: float,
) -> DimensionResult:
    """Proxy slippage: entry_price vs the 1m candle close at-or-before entry.

    Both sides are compared against the *same* candle series (the snapshot
    used for the replay), because neither the live ``trades`` table nor the
    backtest trade record persists a pre-fill "intended" price — see module
    docstring.
    """
    if not pairs:
        return DimensionResult("fill_price_slippage", NOT_COMPARABLE, "No matched trades.")
    if replay_db is None:
        return DimensionResult(
            "fill_price_slippage", NOT_COMPARABLE, "No candle DB available for reference price.",
        )
    flagged = []
    worst = 0.0
    n_computed = 0
    for p in pairs:
        symbol = str(p.live.get("symbol"))
        entry_time = int(p.live.get("entry_time") or 0)
        ref_close = _candle_close_at_or_before(replay_db, symbol, entry_time)
        if ref_close is None or ref_close <= 0:
            continue
        n_computed += 1
        side = str(p.live.get("side"))
        sign = 1.0 if side == "long" else -1.0

        live_entry = safe_float(p.live.get("entry_price"))
        replay_entry = safe_float(p.replay.get("entry_price"))
        live_bps = sign * (live_entry - ref_close) / ref_close * 10_000.0
        replay_bps = sign * (replay_entry - ref_close) / ref_close * 10_000.0
        diff_bps = abs(live_bps - replay_bps)
        worst = max(worst, diff_bps)
        if diff_bps > abs_tol_bps:
            flagged.append({
                "symbol": symbol, "entry_time": entry_time,
                "live_slippage_bps": live_bps, "replay_slippage_bps": replay_bps,
                "diff_bps": diff_bps,
            })
    if n_computed == 0:
        return DimensionResult(
            "fill_price_slippage", NOT_COMPARABLE,
            "No reference candle close available for any matched trade.",
        )
    verdict = DRIFT if flagged else PASS
    detail = (
        f"max |slippage proxy| diff={worst:.2f}bps across {n_computed} trades "
        f"(tolerance={abs_tol_bps}bps)."
    )
    return DimensionResult(
        "fill_price_slippage", verdict, detail,
        extra={"max_diff_bps": worst, "flagged_count": len(flagged), "sample": flagged[:10]},
    )


def _dim_mfe_mae(pairs: Sequence[TradePair]) -> DimensionResult:
    """Always NOT_COMPARABLE — the live trades table has no MFE/MAE columns.

    See module docstring "Known gap" section. This is a real limitation of
    what the live engine persists, not a bug in this comparator; it is
    surfaced explicitly rather than silently treated as a PASS.
    """
    return DimensionResult(
        "mfe_mae", NOT_COMPARABLE,
        "Live trades table has no mfe_usd/mae_usd columns (only computed in "
        "backtest via src/backtest/mfe_mae.py) — cannot be compared until the "
        "live engine persists these fields.",
        extra={"replay_only_sample": [
            {
                "symbol": p.replay.get("symbol"),
                "entry_time": p.replay.get("entry_time"),
                "mfe_usd": p.replay.get("mfe_usd"),
                "mae_usd": p.replay.get("mae_usd"),
            }
            for p in pairs[:10]
        ]},
    )


def _normalize_gate_name(gate: str) -> str:
    return _BACKTEST_TO_LIVE_GATE_NAME.get(gate, gate)


def _dim_gate_rejections(
    live_decisions: Sequence[Dict[str, Any]],
    replay_rejections: Sequence[Dict[str, Any]],
    *,
    rel_tol: float,
    abs_floor: int,
) -> DimensionResult:
    live_rejected = [d for d in live_decisions if str(d.get("result")) == "rejected"]
    live_counts: Dict[str, int] = {}
    for d in live_rejected:
        gate = str(d.get("decision_type"))
        live_counts[gate] = live_counts.get(gate, 0) + 1

    replay_counts: Dict[str, int] = {}
    for r in replay_rejections:
        gate = _normalize_gate_name(str(r.get("gate")))
        replay_counts[gate] = replay_counts.get(gate, 0) + 1

    live_only_present = {g: n for g, n in live_counts.items() if g in _LIVE_ONLY_GATES}
    comparable_live = {g: n for g, n in live_counts.items() if g not in _LIVE_ONLY_GATES}

    all_gates = sorted(set(comparable_live) | set(replay_counts))
    flagged = []
    for gate in all_gates:
        lc = comparable_live.get(gate, 0)
        rc = replay_counts.get(gate, 0)
        diff = abs(lc - rc)
        threshold = max(abs_floor, rel_tol * max(lc, rc))
        if diff > threshold:
            flagged.append({"gate": gate, "live_count": lc, "replay_count": rc, "diff": diff})

    verdict = DRIFT if flagged else PASS
    detail = (
        f"{len(all_gates)} comparable gates checked "
        f"(rel_tol={rel_tol}, abs_floor={abs_floor}); {len(flagged)} outside tolerance. "
        f"{len(live_only_present)} live-only gate(s) present but not scored: "
        f"{sorted(live_only_present) if live_only_present else 'none'}."
    )
    return DimensionResult(
        "gate_rejections", verdict, detail,
        extra={
            "live_counts": comparable_live,
            "replay_counts": replay_counts,
            "live_only_gate_counts": live_only_present,
            "flagged": flagged,
        },
    )


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------


def build_live_vs_replay_report(
    *,
    config: Config,
    start_ms: int,
    end_ms: int,
    symbols: Sequence[str],
    live_db_path: Optional[Path] = None,
    execution_strategies: Optional[Sequence[str]] = None,
    strategy_override: Optional[Strategy] = None,
) -> Dict[str, Any]:
    """Build the full live-vs-replay drift report for [start_ms, end_ms].

    Never opens ``live_db_path`` for writing (read-only mode=ro connections
    and a backup-based snapshot only, see module docstring). ``strategy_override``
    is exposed for deterministic testing — see :func:`run_replay`.
    """
    db_path = live_db_path or DEFAULT_DB_PATH
    strategies = list(execution_strategies) if execution_strategies else resolve_execution_strategies(config)
    if not strategies:
        raise ValueError(
            "No execution strategies resolved — pass execution_strategies= "
            "explicitly or set strategy.phase08.execution_strategies in config."
        )

    live_trades = load_live_trades(
        db_path, start_ms=start_ms, end_ms=end_ms, execution_strategies=strategies,
    )
    live_decisions = load_live_decision_audit(
        db_path, start_ms=start_ms, end_ms=end_ms, strategies=strategies,
    )

    replay_result = run_replay(
        config, symbols=symbols, start_ms=start_ms, end_ms=end_ms, live_db_path=db_path,
        strategy_override=strategy_override,
    )
    replay_trades = replay_result.get("trade_analytics") or replay_result.get("trades") or []
    replay_rejections = replay_result.get("gate_rejections", [])

    pairs, unmatched_live, unmatched_replay = match_trades(live_trades, replay_trades)

    # Reopen the same snapshot briefly for the slippage-proxy candle lookups.
    # (run_replay's snapshot is torn down with its tmp dir on return, so we
    # rebuild a small one here scoped to just the symbols involved.)
    slippage_dim: DimensionResult
    with tempfile.TemporaryDirectory(prefix="live_vs_replay_slip_") as tmp:
        snap = Path(tmp) / "slip_snapshot.db"
        snapshot_live_db_readonly(db_path, snap)
        slip_db = Database(snap)
        try:
            slippage_dim = _dim_slippage(
                pairs, replay_db=slip_db,
                abs_tol_bps=DRIFT_THRESHOLDS["slippage_bps_abs_tol"],
            )
        finally:
            slip_db.close()

    dimensions: Dict[str, DimensionResult] = {
        "signal_count": _dim_signal_count(live_trades, replay_trades, unmatched_live, unmatched_replay),
        "gate_rejections": _dim_gate_rejections(
            live_decisions, replay_rejections,
            rel_tol=DRIFT_THRESHOLDS["gate_count_rel_tol"],
            abs_floor=int(DRIFT_THRESHOLDS["gate_count_abs_floor"]),
        ),
        "notional": _dim_notional(pairs, DRIFT_THRESHOLDS["notional_rel_tol"]),
        "fill_price_slippage": slippage_dim,
        "fees": _dim_fees(
            pairs, DRIFT_THRESHOLDS["fee_rel_tol"], DRIFT_THRESHOLDS["fee_abs_tol_usd"],
        ),
        "hold_duration": _dim_hold_duration(pairs, DRIFT_THRESHOLDS["hold_duration_diff_ms"]),
        "mfe_mae": _dim_mfe_mae(pairs),
        "exit_reason": _dim_exit_reason(pairs),
    }

    overall_verdict = DRIFT if any(d.verdict == DRIFT for d in dimensions.values()) else PASS

    return {
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "symbols": list(symbols),
        "execution_strategies": strategies,
        "live_trade_count": len(live_trades),
        "replay_trade_count": len(replay_trades),
        "matched_trade_count": len(pairs),
        "unmatched_live_count": len(unmatched_live),
        "unmatched_replay_count": len(unmatched_replay),
        "unmatched_live_sample": unmatched_live[:10],
        "unmatched_replay_sample": unmatched_replay[:10],
        "dimensions": {name: d.to_dict() for name, d in dimensions.items()},
        "thresholds": dict(DRIFT_THRESHOLDS),
        "verdict": overall_verdict,
    }


def format_report_text(report: Dict[str, Any]) -> str:
    lines = [
        "=== Fase 10 Live-vs-Replay Drift Report ===",
        f"window: {report['window_start_ms']} .. {report['window_end_ms']}",
        f"symbols: {', '.join(report['symbols'])}",
        f"execution_strategies: {', '.join(report['execution_strategies'])}",
        f"live_trades={report['live_trade_count']} replay_trades={report['replay_trade_count']} "
        f"matched={report['matched_trade_count']} "
        f"unmatched_live={report['unmatched_live_count']} "
        f"unmatched_replay={report['unmatched_replay_count']}",
        "",
        "Dimensions:",
    ]
    for name, d in report["dimensions"].items():
        lines.append(f"  [{d['verdict']:<15}] {name:<20} {d['detail']}")
    lines.append("")
    lines.append(f"VERDICT: {report['verdict']}")
    return "\n".join(lines)
