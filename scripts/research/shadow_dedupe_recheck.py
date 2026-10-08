"""Shadow scoreboard pseudo-replication recheck (research-only, read-only).

The shadow evaluator scores EVERY recorded decision as an independent trade.
Strategies that re-emit while their condition holds (TopTraderFlow: every
5 min throttle, 120h hold) produce hundreds of overlapping "trades" from one
market episode. This script re-scores each board with ONE position per
(strategy, symbol) at a time: a decision only counts if it arrives after the
previous counted trade on that symbol has exited.

READ-ONLY GUARANTEE: every ResearchDatabase is forced to read_only=True
(SQLite mode=ro URI, no DDL), decisions are read with a raw mode=ro
connection, and the live DB is only touched by the evaluator's own mode=ro
loaders. Nothing is persisted; output goes to stdout only.

Usage (run from repo root):
    python scripts/research/shadow_dedupe_recheck.py
    python scripts/research/shadow_dedupe_recheck.py --strategy TopTraderFlow
    python scripts/research/shadow_dedupe_recheck.py \
        --research-db /abs/path/hyperliquid.db --live-db /abs/path/bot.db
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import sqlite3  # noqa: E402

from src.data.research_database import ResearchDatabase  # noqa: E402

# Force every ResearchDatabase opened in this process to read-only.
_orig_init = ResearchDatabase.__init__


def _ro_init(self, db_path, *, read_only=True):  # noqa: ANN001
    _orig_init(self, db_path, read_only=True)


ResearchDatabase.__init__ = _ro_init  # type: ignore[method-assign]

from src.research.shadow_outcome_evaluator import (  # noqa: E402
    evaluate_shadow_decisions,
    independent_outcomes,
)
from src.research.shadow_recorder import ShadowRecorder  # noqa: E402
from src.utils.config import load_config  # noqa: E402


def _stats(outs):
    n = len(outs)
    if n == 0:
        return dict(n=0, wr=0.0, pf_net=0.0, mean_net_pct=0.0)
    net = [o.net_pnl_pct for o in outs]
    gains = sum(x for x in net if x > 0)
    losses = -sum(x for x in net if x < 0)
    return dict(
        n=n,
        wr=100.0 * sum(1 for x in net if x > 0) / n,
        pf_net=(gains / losses) if losses > 0 else float("inf"),
        mean_net_pct=100.0 * sum(net) / n,
    )


def dedupe(outcomes):
    """Keep one open simulated position per symbol (first-come, non-overlapping).

    Delegates to ``independent_outcomes`` — the canonical implementation
    now lives in the evaluator so scoreboards and this recheck share one
    rule."""
    return independent_outcomes(outcomes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--research-db", default=None, help="absolute path; default = research.database.path from config")
    ap.add_argument("--live-db", default=None, help="absolute path; default = data/live/bot.db under repo root")
    args = ap.parse_args()
    try:
        cfg = load_config(ROOT / "config" / "settings.yaml")
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: could not load config/settings.yaml: {exc}")
        return 2
    db_path = Path(args.research_db) if args.research_db else ResearchDatabase.resolve_path(cfg)
    live_path = Path(args.live_db) if args.live_db else ROOT / "data" / "live" / "bot.db"
    for label, p in (("research DB", db_path), ("live DB", live_path)):
        if not p.exists():
            print(f"FATAL: {label} not found: {p}")
            return 2
        print(f"{label}: {p.resolve()}  ({p.stat().st_size / 1e6:,.1f} MB)")

    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = "SELECT id, symbol, strategy, variant, side, would_enter, reason, timestamp_ms, snapshot_json FROM shadow_decisions WHERE would_enter = 1"
    params = []
    if args.strategy:
        sql += " AND strategy = ?"
        params.append(args.strategy)
    rows = conn.execute(sql + " ORDER BY timestamp_ms ASC", params).fetchall()
    conn.close()
    decisions = [ShadowRecorder._row_to_decision(r) for r in rows]
    print(f"decisions loaded (would_enter=1): {len(decisions)}\n")
    if not decisions:
        print("FATAL: 0 decisions — wrong research DB path? Refusing to report empty boards.")
        return 3

    boards = evaluate_shadow_decisions(
        decisions, config=cfg, research_db_path=db_path.resolve(), live_db_path=live_path.resolve()
    )

    hdr = f"{'board':48} {'raw n':>6} {'raw WR':>7} {'raw PF':>7} | {'indep n':>7} {'WR':>6} {'PF net':>7} {'mean%':>7}  per-symbol indep n"
    print(hdr)
    print("-" * len(hdr))
    for key, b in sorted(boards.items()):
        outs = list(getattr(b, "outcomes", []) or [])
        raw = _stats([o for o in outs if o.evaluated])
        kept = dedupe(outs)
        d = _stats(kept)
        per_sym = defaultdict(int)
        for o in kept:
            per_sym[o.symbol] += 1
        print(
            f"{key:48} {raw['n']:>6} {raw['wr']:>6.1f}% {raw['pf_net']:>7.3f} | "
            f"{d['n']:>7} {d['wr']:>5.1f}% {d['pf_net']:>7.3f} {d['mean_net_pct']:>7.2f}  {dict(per_sym)}"
        )
    print("\nRule of thumb: a gate of n>=30 must be read on 'indep n', never on 'raw n'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
