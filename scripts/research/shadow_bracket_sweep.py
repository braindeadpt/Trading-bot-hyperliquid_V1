"""Offline bracket sweep over stored ``shadow_decisions``.

Research-only, no network, no writes to live DBs. Re-scores recorded shadow
decisions with different ``take_profit`` multiples (in R of the recorded
stop) and ``max_hold`` horizons, using the same pessimistic fill rules as
``shadow_outcome_evaluator`` (gap-through, conservative dual-touch,
tier-0 fees + paper slippage + funding fallback from live
``funding_history``).

Each decision's candles are loaded ONCE for the largest ``max_hold`` and
reused across the grid, so the sweep is cheap.

Usage:
    python scripts/research/shadow_bracket_sweep.py \
        --strategies LiquidationCatcher,VWAPDeviation \
        --tp-r 1.5,2,3,4 --max-hold-h 4,8,24
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.research_database import ResearchDatabase
from src.research.shadow_outcome_evaluator import (
    IDEALIZED_FILL_DISCLAIMER,
    _load_funding_samples,
    aggregate_scoreboard,
    load_forward_candles,
    resolve_shadow_cost_model,
    simulate_decision,
)
from src.research.shadow_recorder import (
    ShadowDecision,
    ShadowRecorder,
    extract_bracket_params,
)
from src.utils.config import load_config


def _with_tp_override(
    decision: ShadowDecision, tp_r: float
) -> Optional[ShadowDecision]:
    """Return a copy of *decision* with take_profit_pct = stop × tp_r.

    Keeps the recorded stop-loss — the sweep explores TP asymmetry and
    horizon, not entry quality. None when the stored bracket is missing.
    """
    snap = decision.market_snapshot or {}
    bracket = extract_bracket_params(snap)
    if bracket is None:
        return None
    new_snap = dict(snap)
    new_snap["take_profit_pct"] = bracket["stop_loss_pct"] * tp_r
    return dataclasses.replace(decision, market_snapshot=new_snap)


def run_sweep(
    *,
    strategies: Sequence[str],
    tp_rs: Sequence[float],
    max_holds_h: Sequence[float],
    since_days: Optional[float],
    research_db_path: Path,
    live_db_path: Optional[Path],
) -> Dict[str, Any]:
    cfg = load_config(Path("config/settings.yaml"))
    db = ResearchDatabase(research_db_path)
    recorder = ShadowRecorder(db)

    max_hold_cap_ms = int(max(max_holds_h) * 3_600_000)
    since_ms = (
        int(time.time() * 1000 - since_days * 86_400_000)
        if since_days
        else None
    )

    results: Dict[str, Any] = {}
    for strategy in strategies:
        decisions = recorder.load_decisions(strategy=strategy, since_ms=since_ms)
        if not decisions:
            results[strategy] = {"error": "no decisions", "cells": {}}
            continue

        cost_model = resolve_shadow_cost_model(strategy, cfg)
        variants = sorted({d.variant or "phase08_shadow" for d in decisions})

        # Per-symbol funding samples covering the whole decision span.
        sym_span: Dict[str, Tuple[int, int]] = {}
        for d in decisions:
            lo, hi = sym_span.get(d.symbol, (d.timestamp_ms, d.timestamp_ms))
            sym_span[d.symbol] = (
                min(lo, d.timestamp_ms),
                max(hi, d.timestamp_ms),
            )
        funding_by_symbol: Dict[str, List[Tuple[int, float]]] = {}
        for sym, (lo, hi) in sym_span.items():
            funding_by_symbol[sym] = (
                _load_funding_samples(
                    live_db_path, sym, lo, hi + max_hold_cap_ms
                )
                if live_db_path
                else []
            )

        # Load each decision's forward candles once (largest horizon).
        candle_cache: Dict[int, Tuple[list, str]] = {}
        for d in decisions:
            candles, src = load_forward_candles(
                symbol=d.symbol,
                entry_ts_ms=d.timestamp_ms,
                max_hold_ms=max_hold_cap_ms,
                research_db_path=Path(research_db_path),
                live_db_path=live_db_path,
            )
            candle_cache[d.row_id or id(d)] = (candles, src)

        cells: Dict[str, Any] = {}
        for variant in variants:
            group = [d for d in decisions if (d.variant or "phase08_shadow") == variant]
            for tp_r in tp_rs:
                for hold_h in max_holds_h:
                    hold_ms = int(hold_h * 3_600_000)
                    outcomes = []
                    sources: List[str] = []
                    for d in group:
                        d2 = _with_tp_override(d, tp_r)
                        if d2 is None:
                            # still counts as missing-bracket skip via simulate
                            d2 = d
                        candles, src = candle_cache[d.row_id or id(d)]
                        sources.append(src)
                        outcomes.append(
                            simulate_decision(
                                d2,
                                candles,
                                max_hold_ms=hold_ms,
                                cost_model=cost_model,
                                funding_samples=funding_by_symbol.get(d.symbol),
                            )
                        )
                    board = aggregate_scoreboard(
                        strategy,
                        outcomes,
                        max_hold_ms=hold_ms,
                        candle_source=(
                            max(set(sources), key=sources.count)
                            if sources
                            else ""
                        ),
                        n_decisions=len(group),
                        variant=variant,
                    )
                    key = f"{variant}|tp={tp_r}R|hold={hold_h}h"
                    cells[key] = board.to_dict(include_outcomes=False)
                    # per-symbol net split for later inspection
                    by_sym: Dict[str, float] = {}
                    for o in board.outcomes:
                        if o.evaluated:
                            by_sym[o.symbol] = by_sym.get(o.symbol, 0.0) + o.net_pnl_pct
                    cells[key]["net_pnl_pct_by_symbol"] = by_sym
        results[strategy] = {
            "n_decisions": len(decisions),
            "variants": variants,
            "cost_model": cost_model.label,
            "cells": cells,
        }
    return {
        "disclaimer": IDEALIZED_FILL_DISCLAIMER,
        "generated_at_ms": int(time.time() * 1000),
        "tp_rs": list(tp_rs),
        "max_holds_h": list(max_holds_h),
        "strategies": results,
    }


def _fmt(results: Dict[str, Any]) -> str:
    lines = [IDEALIZED_FILL_DISCLAIMER, ""]
    for strat, data in results["strategies"].items():
        if "error" in data:
            lines.append(f"== {strat}: {data['error']}")
            continue
        lines.append(
            f"== {strat}  n_decisions={data['n_decisions']}  cost={data['cost_model']}"
        )
        lines.append(
            f"{'variant':16} {'tp':>5} {'hold':>5} {'n_eval':>6} {'WR%':>6} "
            f"{'PF_n':>6} {'E[R]_n':>7} {'PnL%_n':>8} {'t/o%':>5} {'fund_cov':>8}"
        )
        lines.append("-" * 95)
        for key, c in sorted(data["cells"].items()):
            variant, tp, hold = key.split("|")
            to_pct = (
                100.0 * c["timeouts"] / c["n_evaluated"] if c["n_evaluated"] else 0.0
            )
            lines.append(
                f"{variant:16} {tp[3:]:>5} {hold[5:]:>5} {c['n_evaluated']:6d} "
                f"{100.0 * c['win_rate']:6.1f} {c['net_profit_factor']:6.2f} "
                f"{c['net_expectancy_r']:7.3f} "
                f"{100.0 * c['net_hypothetical_pnl_pct']:8.3f} "
                f"{to_pct:5.0f} {c['mean_funding_coverage']:8.2f}"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strategies", default="LiquidationCatcher,VWAPDeviation")
    ap.add_argument("--tp-r", default="1.5,2,3,4")
    ap.add_argument("--max-hold-h", default="4,8,24")
    ap.add_argument("--since-days", type=float, default=None)
    ap.add_argument("--research-db", default=None)
    ap.add_argument("--live-db", default="data/live/bot.db")
    ap.add_argument("--out", default=None, help="write JSON results to path")
    args = ap.parse_args()

    cfg = load_config(Path("config/settings.yaml"))
    research_db = Path(args.research_db) if args.research_db else ResearchDatabase.resolve_path(cfg)
    live_db = Path(args.live_db) if args.live_db else None

    results = run_sweep(
        strategies=[s.strip() for s in args.strategies.split(",") if s.strip()],
        tp_rs=[float(x) for x in args.tp_r.split(",")],
        max_holds_h=[float(x) for x in args.max_hold_h.split(",")],
        since_days=args.since_days,
        research_db_path=research_db,
        live_db_path=live_db,
    )
    print(_fmt(results))
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, sort_keys=True))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
