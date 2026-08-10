"""Institutional baseline-signal gate + portfolio battery + harness validation.

Measurement only. Does not modify production strategies or settings.yaml.

Usage:
  # Gate one strategy (exit 0=PASS, 1=FAIL, 2=INCONCLUSIVE)
  python scripts/baseline_signal_gate.py --strategy VWAPDeviation --folds W2 --seeds 200 --gate

  # Full portfolio battery (uses scan if present; else runs refs)
  python scripts/baseline_signal_gate.py --portfolio --seeds 200

  # Harness validation (engine vs fast-sim + positive control)
  python scripts/baseline_signal_gate.py --validate-harness --seeds 40
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.baseline_signal_test import (  # noqa: E402
    FOLDS,
    OUT_DIR,
    CandleStore,
    EntrySpec,
    always_side,
    build_b1,
    build_b2,
    build_b3,
    buy_and_hold,
    enrich_entries_atr,
    interpret,
    ms_from_date,
    percentile_rank,
    risk_from_cfg,
    run_baseline_seeds,
    run_strategy_reference,
    simulate_schedule,
    situate,
    trade_metrics,
    validate_fast_vs_engine,
)
from scripts.baseline_portfolio_scan import PRIORITY, tier_note  # noqa: E402
from src.backtest.engine import BacktestEngine, build_backtest_config_from_yaml  # noqa: E402
from src.data.database import Database  # noqa: E402
from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy  # noqa: E402
from src.strategies.factory import DirectStrategyRouter  # noqa: E402
from src.utils.config import load_config  # noqa: E402

logging.basicConfig(level=logging.ERROR)
for _n in (
    "src.core.volatility_circuit",
    "src.backtest.engine",
    "src.strategies",
    "src.core.risk_manager",
    "src.core.funding_blackout",
):
    logging.getLogger(_n).setLevel(logging.ERROR)

MIN_TRADES = 30


def gate_verdict(fold_result: Dict[str, Any], *, min_trades: int = MIN_TRADES) -> Dict[str, Any]:
    """Three-condition gate: B1≥p95 AND n≥min_trades AND expectancy>0 (PF>1).

    Returns which conditions failed so FAIL is never opaque.
    INCONCLUSIVE only when underpowered (n_trades < min_trades) or baselines missing.
    """
    eng = fold_result.get("strategy_engine") or fold_result.get("checklist_meta_engine") or {}
    n = int(eng.get("n_trades") or 0)
    pf = float(eng.get("profit_factor") or 0.0)
    exp = float(eng.get("expectancy") or 0.0)

    conditions = {
        "n_trades_ok": n >= min_trades,
        "b1_ge_p95": False,
        "profitable": (exp > 0.0) and (pf > 1.0),
    }
    failed: List[str] = []

    if n < min_trades:
        failed.append(f"n_trades={n}<{min_trades}")
        return {
            "verdict": "INCONCLUSIVE",
            "reason": (
                f"INCONCLUSIVE (underpowered): {'; '.join(failed)} — "
                "not evidence of no-edge; do not kill the strategy on this alone"
            ),
            "n_trades": n,
            "pf": pf,
            "expectancy": exp,
            "pf_percentile": None,
            "conditions": conditions,
            "failed_conditions": failed,
        }

    baselines = fold_result.get("baselines") or {}
    if "B1_random_direction" not in baselines:
        failed.append("no_baseline_distributions")
        return {
            "verdict": "INCONCLUSIVE",
            "reason": f"INCONCLUSIVE: {'; '.join(failed)}",
            "n_trades": n,
            "pf": pf,
            "expectancy": exp,
            "pf_percentile": None,
            "conditions": conditions,
            "failed_conditions": failed,
        }

    vs = baselines["B1_random_direction"]["vs_real_fast"]["profit_factor"]
    pct = float(vs["percentile"])
    conditions["b1_ge_p95"] = pct >= 95.0

    if not conditions["b1_ge_p95"]:
        failed.append(f"B1_pf_percentile={pct}<95")
    if not conditions["profitable"]:
        failed.append(f"not_profitable(expectancy={exp}, PF={pf}; need expectancy>0 and PF>1)")

    if not failed:
        return {
            "verdict": "PASS",
            "reason": (
                f"PASS: B1 PF percentile={pct}≥95, n_trades={n}≥{min_trades}, "
                f"expectancy={exp}>0, PF={pf}>1"
            ),
            "n_trades": n,
            "pf": pf,
            "expectancy": exp,
            "pf_percentile": pct,
            "above_p95": bool(vs["above_p95"]),
            "above_p50": bool(vs["above_p50"]),
            "conditions": conditions,
            "failed_conditions": [],
        }

    return {
        "verdict": "FAIL",
        "reason": f"FAIL: {'; '.join(failed)}",
        "n_trades": n,
        "pf": pf,
        "expectancy": exp,
        "pf_percentile": pct,
        "above_p95": bool(vs["above_p95"]),
        "above_p50": bool(vs["above_p50"]),
        "conditions": conditions,
        "failed_conditions": failed,
    }


def run_fold_strategy(
    fold_key: str,
    cfg: Any,
    db: Database,
    symbols: List[str],
    strategy_name: str,
    n_seeds: int,
) -> Dict[str, Any]:
    risk = risk_from_cfg(cfg, strategy_name)
    label, start, end = FOLDS[fold_key]
    start_ms, end_ms = ms_from_date(start), ms_from_date(end, end=True)
    print(f"\n======== {strategy_name} / {label} ========", flush=True)

    print(f"  [1] {strategy_name} reference...", flush=True)
    t0 = time.time()
    ref = run_strategy_reference(cfg, db, symbols, start_ms, end_ms, strategy_name)
    print(
        f"      n={ref['metrics']['n_trades']} PF={ref['metrics']['profit_factor']} "
        f"PnL={ref['metrics']['total_pnl']} ({time.time()-t0:.1f}s)",
        flush=True,
    )

    store = CandleStore()
    store.load(db, symbols, start_ms, end_ms)
    entries = enrich_entries_atr(ref["entries"], store, risk)
    sanity = validate_fast_vs_engine(store, entries, risk, ref["pnls"])
    print(
        f"      sanity engine={sanity['engine_pnl']} fast={sanity['fast_pnl']} "
        f"n={sanity['engine_n']}/{sanity['fast_n']}",
        flush=True,
    )

    if int(ref["metrics"]["n_trades"]) < 15:
        return {
            "strategy": strategy_name,
            "fold": label,
            "n_seeds": n_seeds,
            "strategy_engine": ref["metrics"],
            "strategy_fast_sim": ref["metrics"],
            "checklist_meta_engine": ref["metrics"],
            "checklist_meta_fast_sim": ref["metrics"],
            "fast_sim_sanity": sanity,
            "baselines": {},
            "passive": {},
            "skipped": True,
            "skip_reason": f"n_trades<{15}",
            "tier": tier_note(strategy_name),
        }

    b1 = run_baseline_seeds(
        "B1", lambda rng: build_b1(entries, rng), store, risk, n_seeds, 1000,
    )
    b2 = run_baseline_seeds(
        "B2",
        lambda rng: build_b2(entries, store, risk, start_ms, end_ms, rng),
        store,
        risk,
        n_seeds,
        2000,
    )
    b3 = run_baseline_seeds(
        "B3",
        lambda rng: build_b3(entries, store, risk, start_ms, end_ms, rng),
        store,
        risk,
        n_seeds,
        3000,
    )
    bh = buy_and_hold(store, symbols, start_ms, end_ms, risk)
    al = trade_metrics([t.pnl_usd for t in simulate_schedule(store, always_side(entries, "long"), risk)])
    ash = trade_metrics([t.pnl_usd for t in simulate_schedule(store, always_side(entries, "short"), risk)])
    fast_m = trade_metrics([t.pnl_usd for t in simulate_schedule(store, entries, risk)])
    real_m = ref["metrics"]

    return {
        "strategy": strategy_name,
        "fold": label,
        "window": {"start": start, "end": end},
        "n_seeds": n_seeds,
        "tier": tier_note(strategy_name),
        "strategy_engine": real_m,
        "strategy_fast_sim": fast_m,
        "checklist_meta_engine": real_m,
        "checklist_meta_fast_sim": fast_m,
        "fast_sim_sanity": sanity,
        "baselines": {
            "B1_random_direction": {
                **{k: v for k, v in b1.items() if k != "seeds"},
                "vs_real_fast": situate(fast_m, b1),
            },
            "B2_random_timing": {
                **{k: v for k, v in b2.items() if k != "seeds"},
                "vs_real_fast": situate(fast_m, b2),
            },
            "B3_random_both": {
                **{k: v for k, v in b3.items() if k != "seeds"},
                "vs_real_fast": situate(fast_m, b3),
            },
        },
        "passive": {
            "buy_and_hold_equal_weight": bh,
            "always_long_at_cm_times": al,
            "always_short_at_cm_times": ash,
        },
        "skipped": False,
    }


# ---------------------------------------------------------------------------
# Positive-control strategy (intentional look-ahead)
# ---------------------------------------------------------------------------


class PeekAheadEdge(Strategy):
    """Cheat: enter in the direction of the NEXT 1m bar close vs open."""

    def __init__(self, store: CandleStore, sl_pct: float = 0.008, tp_pct: float = 0.016) -> None:
        self._store = store
        self._sl = sl_pct
        self._tp = tp_pct
        self._last_ms: Dict[str, int] = {}

    @property
    def name(self) -> str:
        return "PeekAheadEdge"

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        bars = self._store.bars_1m.get(event.symbol) or []
        idx = self._store.idx_1m.get(event.symbol, {}).get(event.timestamp_ms)
        if idx is None or idx + 1 >= len(bars):
            return None
        if event.timestamp_ms - self._last_ms.get(event.symbol, 0) < 30 * 60_000:
            return None
        nxt = bars[idx + 1]
        if nxt.c > nxt.o:
            side = "long"
        elif nxt.c < nxt.o:
            side = "short"
        else:
            return None
        self._last_ms[event.symbol] = event.timestamp_ms
        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=side,
            confidence=0.9,
            size_pct=0.0075,
            entry_price=event.price,
            stop_loss_pct=self._sl,
            take_profit_pct=self._tp,
            reason="peek_ahead_1bar",
        )

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        return None


def validate_harness(cfg: Any, db: Database, symbols: List[str], n_seeds: int) -> Dict[str, Any]:
    """Positive control must ≥p95; fast-sim vs engine B1 must agree within 20pp."""
    # Use a window where OHLC strategies fire enough trades
    start, end = "2026-06-13", "2026-06-27"  # 2 weeks of W2
    start_ms, end_ms = ms_from_date(start), ms_from_date(end, end=True)
    risk = risk_from_cfg(cfg, "DonchianBreakout")

    print("=== Harness: positive control (1-bar peek, exit same bar) ===", flush=True)
    store = CandleStore()
    store.load(db, symbols, start_ms, end_ms)

    # True look-ahead edge: trade bar i open→close knowing the direction in advance.
    # Gross = |c-o| every time; only fees/slip can make it lose → must beat random B1.
    peek_pnls: List[float] = []
    peek_entries: List[EntrySpec] = []
    for sym in symbols:
        bars = [b for b in (store.bars_1m.get(sym) or []) if start_ms <= b.ts <= end_ms]
        for i in range(0, len(bars), 30):  # every 30m to keep count manageable
            b = bars[i]
            if b.c == b.o:
                continue
            side = "long" if b.c > b.o else "short"
            size = risk.base_size_pct * risk.initial_capital / max(b.o, 1e-9)
            entry = b.o * (1.0 + risk.slippage_bps / 10_000.0) if side == "long" else b.o * (
                1.0 - risk.slippage_bps / 10_000.0
            )
            exit_px = b.c * (1.0 - risk.slippage_bps / 10_000.0) if side == "long" else b.c * (
                1.0 + risk.slippage_bps / 10_000.0
            )
            gross = (exit_px - entry) * size if side == "long" else (entry - exit_px) * size
            fees = (entry + exit_px) * size * risk.fee_pct
            peek_pnls.append(gross - fees)
            # For B1 scramble: same times, random side — use tiny SL/TP so 1-bar path matters less;
            # we compare via same 1-bar PnL function with random side.
            peek_entries.append(
                EntrySpec(b.ts, sym, side, 0.5, 0.5, size, b.o)  # wide stops unused
            )

    def _one_bar_pnl(spec: EntrySpec) -> float:
        bars = store.bars_1m.get(spec.symbol) or []
        idx = store.idx_1m.get(spec.symbol, {}).get(spec.entry_time_ms)
        if idx is None:
            return 0.0
        b = bars[idx]
        side = spec.side
        entry = b.o * (1.0 + risk.slippage_bps / 10_000.0) if side == "long" else b.o * (
            1.0 - risk.slippage_bps / 10_000.0
        )
        exit_px = b.c * (1.0 - risk.slippage_bps / 10_000.0) if side == "long" else b.c * (
            1.0 + risk.slippage_bps / 10_000.0
        )
        gross = (exit_px - entry) * size if side == "long" else (entry - exit_px) * size
        # bug: size should be spec.size
        gross = (exit_px - entry) * spec.size if side == "long" else (entry - exit_px) * spec.size
        fees = (entry + exit_px) * spec.size * risk.fee_pct
        return gross - fees

    peek_m = trade_metrics(peek_pnls)
    b1_pfs = []
    for i in range(n_seeds):
        rng = random.Random(9000 + i * 9973)
        pnls = []
        for e in peek_entries:
            side = rng.choice(["long", "short"])
            pnls.append(_one_bar_pnl(EntrySpec(e.entry_time_ms, e.symbol, side, 0.5, 0.5, e.size)))
        b1_pfs.append(trade_metrics(pnls)["profit_factor"])
    peek_pct = percentile_rank(peek_m["profit_factor"], b1_pfs, higher_better=True)
    ok_control = peek_pct >= 95.0
    print(
        f"  Peek1bar n={peek_m['n_trades']} PF={peek_m['profit_factor']} "
        f"PnL={peek_m['total_pnl']} B1%ile={peek_pct} p95={ok_control}",
        flush=True,
    )

    print("=== Harness: fast-sim vs engine B1 (DonchianBreakout, 2w) ===", flush=True)
    strategy_name = "DonchianBreakout"
    risk = risk_from_cfg(cfg, strategy_name)
    ref = run_strategy_reference(cfg, db, symbols, start_ms, end_ms, strategy_name)
    entries_v = enrich_entries_atr(ref["entries"], store, risk)
    print(f"  Donchian ref n={len(entries_v)} engine_pf={ref['metrics']['profit_factor']}", flush=True)

    fast_real = trade_metrics([t.pnl_usd for t in simulate_schedule(store, entries_v, risk)])
    fast_b1 = run_baseline_seeds(
        "B1_fast", lambda rng: build_b1(entries_v, rng), store, risk, n_seeds, 1100,
    )
    fast_pct = situate(fast_real, fast_b1)["profit_factor"]["percentile"]

    engine_seeds = min(20, n_seeds)
    engine_pfs: List[float] = []
    engine_real_pf = float(ref["metrics"]["profit_factor"])

    class _Sched(Strategy):
        def __init__(self, specs: List[EntrySpec], name: str) -> None:
            self._map = {(e.entry_time_ms, e.symbol): e for e in specs}
            self._name = name

        @property
        def name(self) -> str:
            return self._name

        def on_data(self, event: MarketEvent) -> Optional[Signal]:
            e = self._map.get((event.timestamp_ms, event.symbol))
            if e is None:
                return None
            return Signal(
                strategy=self._name,
                symbol=e.symbol,
                side=e.side,
                confidence=0.7,
                size_pct=0.0075,
                entry_price=event.price,
                stop_loss_pct=e.stop_loss_pct,
                take_profit_pct=e.take_profit_pct,
                reason="scheduled_baseline",
            )

        def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
            return None

    print(f"  Engine B1 seeds={engine_seeds}...", flush=True)
    for i in range(engine_seeds):
        rng = random.Random(1100 + i * 9973)
        specs = build_b1(entries_v, rng)
        bt = build_backtest_config_from_yaml(cfg)
        bt.use_volatility_circuit = False
        bt.use_funding_blackout = False
        bt.max_daily_trades = 0
        bt.use_microstructure_proxy = True
        bt.exit_path_policy = "adverse_first"
        eng = BacktestEngine(
            database=db,
            strategy=DirectStrategyRouter([_Sched(specs, strategy_name)]),
            config=bt,
            symbols=symbols,
            risk_config=cfg,
        )
        t0 = time.time()
        result = eng.run(start_ms=start_ms, end_ms=end_ms)
        trades = [t for t in (result.get("trades") or []) if t.get("strategy") == strategy_name]
        m = trade_metrics([float(t.get("pnl_usd") or 0) for t in trades])
        engine_pfs.append(m["profit_factor"])
        if (i + 1) % 5 == 0:
            print(f"    engine seed {i+1}/{engine_seeds} ({time.time()-t0:.1f}s)", flush=True)

    eng_pct = percentile_rank(engine_real_pf, engine_pfs, higher_better=True)
    delta = abs(fast_pct - eng_pct)
    ok_agree = delta <= 20.0

    out = {
        "window": {"start": start, "end": end},
        "positive_control": {
            "strategy": "PeekOneBarOpenClose",
            "metrics": peek_m,
            "b1_pf_percentile": peek_pct,
            "passes_p95": ok_control,
            "note": "Enter at open knowing close direction; exit at close. Must beat random side.",
        },
        "fast_vs_engine": {
            "strategy": strategy_name,
            "n_trades_engine": ref["metrics"]["n_trades"],
            "fast_sim_b1_percentile": fast_pct,
            "engine_b1_percentile": eng_pct,
            "engine_seeds": engine_seeds,
            "abs_delta_pct_points": round(delta, 2),
            "agree_within_20pp": ok_agree,
            "engine_real_pf": engine_real_pf,
            "fast_real_pf": fast_real["profit_factor"],
        },
        "harness_ok": bool(ok_control and ok_agree),
    }
    print(json.dumps(out, indent=2), flush=True)
    return out


def run_portfolio(cfg, db, symbols, n_seeds: int) -> Dict[str, Any]:
    scan_path = OUT_DIR / "baseline_portfolio_scan.json"
    scan = json.loads(scan_path.read_text(encoding="utf-8")) if scan_path.exists() else None

    results: Dict[str, Any] = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "seeds": n_seeds,
        "min_trades": MIN_TRADES,
        "strategies": {},
        "table": [],
    }

    names = [n for n in PRIORITY if n in {
        "VWAPDeviation", "VolatilityBreakout", "CVDOrderFlow", "SpotPerpCarry",
        "FundingMomentum", "OrderBookScalper", "FundingArbitrage", "DonchianBreakout",
        "SFPReversion", "VARejection", "RangeGrid", "TrendPyramid", "LeadLag",
        "LiquidationCatcher", "ChecklistMeta", "SmartMoneyFlow", "FundingExtreme",
    }]

    for name in names:
        strat_out: Dict[str, Any] = {"tier": tier_note(name), "folds": {}}
        for fk in ("W2", "W3"):
            # Skip full seeds if scan says too few trades
            if scan and name in scan.get("strategies", {}):
                n_scan = int(scan["strategies"][name]["folds"][fk].get("n_trades") or 0)
                if n_scan < 15:
                    print(f"SKIP {name}/{fk} n={n_scan} < 15", flush=True)
                    row = {
                        "strategy": name,
                        "fold": fk,
                        "n_trades": n_scan,
                        "B1_pf_pctile": None,
                        "B2_pf_pctile": None,
                        "B3_pf_pctile": None,
                        "pf": None,
                        "expectancy": None,
                        "verdict": "INCONCLUSIVE",
                        "failed_conditions": [f"n_trades={n_scan}<15"],
                        "reason": f"n_trades={n_scan} < 15",
                        "tier": tier_note(name)["tier"],
                    }
                    results["table"].append(row)
                    strat_out["folds"][fk] = {
                        "skipped": True,
                        "n_trades": n_scan,
                        "gate": {
                            "verdict": "INCONCLUSIVE",
                            "reason": f"n_trades={n_scan} < 15",
                            "n_trades": n_scan,
                        },
                    }
                    continue

            fr = run_fold_strategy(fk, cfg, db, symbols, name, n_seeds)
            gate = gate_verdict(fr)
            fr["gate"] = gate
            strat_out["folds"][fk] = {k: v for k, v in fr.items() if k != "_seed_arrays"}

            b1p = b2p = b3p = None
            if not fr.get("skipped") and fr.get("baselines"):
                b1p = fr["baselines"]["B1_random_direction"]["vs_real_fast"]["profit_factor"]["percentile"]
                b2p = fr["baselines"]["B2_random_timing"]["vs_real_fast"]["profit_factor"]["percentile"]
                b3p = fr["baselines"]["B3_random_both"]["vs_real_fast"]["profit_factor"]["percentile"]
            n_tr = int((fr.get("strategy_engine") or {}).get("n_trades") or 0)
            results["table"].append(
                {
                    "strategy": name,
                    "fold": fk,
                    "n_trades": n_tr,
                    "B1_pf_pctile": b1p,
                    "B2_pf_pctile": b2p,
                    "B3_pf_pctile": b3p,
                    "pf": gate.get("pf"),
                    "expectancy": gate.get("expectancy"),
                    "verdict": gate["verdict"],
                    "failed_conditions": gate.get("failed_conditions") or [],
                    "reason": gate.get("reason"),
                    "tier": tier_note(name)["tier"],
                    "conservative_test": tier_note(name)["conservative_vs_strategy"],
                }
            )
        results["strategies"][name] = strat_out

    return results


def _render_portfolio_md(payload: Dict[str, Any], harness: Optional[Dict[str, Any]]) -> str:
    lines = [
        "# Portfolio baseline-signal gate",
        "",
        f"Created: {payload.get('created_utc')}",
        f"Seeds: {payload.get('seeds')} | min_trades: {payload.get('min_trades')}",
        f"Criterion: {payload.get('criterion', 'B1≥p95 AND n≥30 AND expectancy>0 (PF>1)')}",
        "",
        "## Gate rule (three cumulative conditions)",
        "",
        "PASS ⇔ **B1 PF percentile ≥ 95** AND **n_trades ≥ "
        f"{payload.get('min_trades')}** AND **expectancy > 0 (PF > 1)**.",
        "Missing power (n < min) → INCONCLUSIVE (not tested — never kill on this alone).",
        "Any other miss → FAIL; report which condition(s) failed.",
        "",
        "## Verdict table",
        "",
        "| Strategy | Fold | n | PF | B1 %ile | B2 %ile | B3 %ile | Verdict | Failed conditions | Tier |",
        "|----------|------|--:|---:|--------:|--------:|--------:|---------|-------------------|------|",
    ]
    for row in payload.get("table") or []:
        failed = row.get("failed_conditions") or []
        failed_s = "; ".join(failed) if failed else "—"
        lines.append(
            f"| {row['strategy']} | {row['fold']} | {row['n_trades']} | "
            f"{row.get('pf')} | {row['B1_pf_pctile']} | {row['B2_pf_pctile']} | {row['B3_pf_pctile']} | "
            f"**{row['verdict']}** | {failed_s} | {row['tier']} |"
        )
    lines.append("")

    passes = [r for r in payload.get("table") or [] if r["verdict"] == "PASS"]
    fails = [r for r in payload.get("table") or [] if r["verdict"] == "FAIL"]
    incon = [r for r in payload.get("table") or [] if r["verdict"] == "INCONCLUSIVE"]
    lines.append("## Recommendation")
    lines.append("")
    if passes:
        lines.append(
            f"**Concentrate on:** {', '.join(sorted({r['strategy'] for r in passes}))} "
            "(passed three-condition gate on at least one fold)."
        )
        lines.append(
            "Other strategies should leave execution until they pass the gate."
        )
    else:
        lines.append(
            "**No strategy passed the three-condition gate** (B1≥p95 + powered sample + "
            "PF>1). Beating noise without profitability is not edge — see SmartMoneyFlow W3 "
            "(B1 p96, PF~0.27) as the cautionary case. Recommend: stop polishing ChecklistMeta/"
            "VWAP gates; shrink execution to observation/paper-minimum; redirect to new edge research."
        )
    lines.append("")
    lines.append(f"- PASS rows: {len(passes)} | FAIL: {len(fails)} | INCONCLUSIVE: {len(incon)}")
    lines.append("")

    if harness:
        lines.append("## Harness validation")
        lines.append("")
        pc = harness.get("positive_control") or {}
        fe = harness.get("fast_vs_engine") or {}
        lines.append(
            f"- Positive control PeekAheadEdge B1 %ile={pc.get('b1_pf_percentile')} "
            f"passes_p95={pc.get('passes_p95')}"
        )
        lines.append(
            f"- Fast vs engine ({fe.get('strategy')}): fast%ile={fe.get('fast_sim_b1_percentile')} "
            f"engine%ile={fe.get('engine_b1_percentile')} "
            f"Δ={fe.get('abs_delta_pct_points')}pp agree={fe.get('agree_within_20pp')}"
        )
        lines.append(f"- **Harness OK:** {harness.get('harness_ok')}")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", type=str, default="")
    ap.add_argument("--folds", type=str, default="W2,W3")
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--gate", action="store_true", help="Exit 0/1/2 for PASS/FAIL/INCONCLUSIVE")
    ap.add_argument("--portfolio", action="store_true")
    ap.add_argument("--validate-harness", action="store_true")
    ap.add_argument("--min-trades", type=int, default=30)
    args = ap.parse_args()

    min_trades = args.min_trades
    # bind for nested helpers
    import scripts.baseline_signal_gate as _self

    _self.MIN_TRADES = min_trades

    cfg = load_config(ROOT / "config" / "settings.yaml")
    snap = ROOT / "data" / "live" / "bot_ruleset_validate.db"
    db = Database(str(snap if snap.exists() else ROOT / "data" / "live" / "bot.db"))
    symbols = list(cfg.get("assets") or ["BTC", "ETH", "SOL", "HYPE"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    harness = None
    if args.validate_harness:
        harness = validate_harness(cfg, db, symbols, max(args.seeds, 40))
        path = OUT_DIR / "baseline_harness_validation.json"
        path.write_text(json.dumps(harness, indent=2), encoding="utf-8")
        print(f"Wrote {path}", flush=True)
        if not args.portfolio and not args.strategy:
            return 0 if harness.get("harness_ok") else 1

    if args.portfolio:
        payload = run_portfolio(cfg, db, symbols, args.seeds)
        if harness:
            payload["harness"] = harness
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = OUT_DIR / f"baseline_portfolio_battery_{ts}.json"
        # Slim JSON: drop nested seed-heavy bits already excluded
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (OUT_DIR / "baseline_portfolio_battery_latest.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        md = OUT_DIR / "BASELINE_PORTFOLIO_GATE_REPORT.md"
        md.write_text(_render_portfolio_md(payload, harness), encoding="utf-8")
        print(f"Wrote {out}\nWrote {md}", flush=True)
        return 0

    if not args.strategy:
        ap.error("Need --strategy, --portfolio, or --validate-harness")

    fold_keys = [f.strip() for f in args.folds.split(",") if f.strip() in FOLDS]
    gates = []
    payload = {
        "strategy": args.strategy,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "criterion": "B1≥p95 AND n≥30 AND expectancy>0 (PF>1)",
        "folds": {},
    }
    for fk in fold_keys:
        fr = run_fold_strategy(fk, cfg, db, symbols, args.strategy, args.seeds)
        g = gate_verdict(fr)
        fr["gate"] = g
        gates.append(g)
        payload["folds"][fk] = fr
        print(f"  GATE {args.strategy}/{fk}: {g}", flush=True)

    out = OUT_DIR / f"baseline_gate_{args.strategy}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {out}", flush=True)

    if args.gate:
        # Best fold wins for PASS; all inconclusive → 2; else FAIL
        if any(g["verdict"] == "PASS" for g in gates):
            return 0
        if all(g["verdict"] == "INCONCLUSIVE" for g in gates):
            return 2
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
