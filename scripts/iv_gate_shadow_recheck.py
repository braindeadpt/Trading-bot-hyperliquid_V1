#!/usr/bin/env python3
"""IV gate shadow recheck — decide shadow vs enforcement when the sample is big enough.

The IV gate is currently **shadow-only**: the regime router records an
``iv_gate_shadow`` decision per routed trade (research DB, IV class in the
snapshot metadata) but never blocks execution. The backtest evidence
(docs/IV_HIGH_ONLY_AB_SPLIT.md) suggested keeping both strategies only in
high_iv (threshold 66.7) with net +42.99 USD — but at n=13 it was INCONCLUSIVE
below the n>=30 evidence gate.

This watchdog makes the shadow→enforcement decision automatic and idempotent:

  * Every CHECK_HOURS (default 6h) it measures how many **closed** executed
    trades carry an IV decision (via the join in
    ``scripts/iv_gate_shadow_vs_pnl.py`` — the single source of truth).
  * When n >= 30 closed with a decision and no recheck has been recorded yet,
    it re-runs the full high_iv vs low_iv comparison, extracts the slices and
    writes a decision report (docs/IV_GATE_SHADOW_RECHECK_RESULT.md).
  * The verdict decides **shadow vs enforcement** with threshold 66.7:
      - PROMOTE  : high_iv slice profitable AND low_iv not — enforce the gate.
      - REJECT   : the live sample contradicts the backtest direction — keep
                   shadow, do not enforce (and never silently flip the router).
      - INCONCLUSIVE: n < 30 or slices too small — keep shadow, keep collecting.
  * The result is persisted in the supervisor shared state (gitignored), so a
    restart never re-triggers a completed run. ``--force`` re-runs the
    comparison now without consuming the trigger (smoke tests / manual
    checkpoints).

The decision is **a recommendation + report only**: flipping the router from
shadow to enforcement is a deliberate, reviewed change (the router code
enforces the gate; nothing here touches the OMS).

Usage:
  python scripts/iv_gate_shadow_recheck.py            # daemon (every 6h)
  python scripts/iv_gate_shadow_recheck.py --once     # single check, exit
  python scripts/iv_gate_shadow_recheck.py --force    # run the comparison now
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.iv_gate_shadow_vs_pnl import (  # noqa: E402
    BACKTEST_EVIDENCE,
    MIN_N_GATE,
    build_report,
    resolve_db_paths,
)

STATE_DIR = ROOT / "data" / "research"
STATE_PATH = STATE_DIR / "iv_gate_shadow_recheck_state.json"
REPORT_PATH = ROOT / "docs" / "IV_GATE_SHADOW_RECHECK_RESULT.md"

TARGET_CLOSED = MIN_N_GATE  # 30 — matches the backtest evidence gate.
CHECK_HOURS = 6
IV_THRESHOLD = 66.7  # high_iv cut (DVOL percentile(30d) > 66.7) — pinned.
CONCENTRATION_FRACTION = 0.8  # ~80% of the sample in one strategy/symbol


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def iv_decision_count() -> Tuple[int, int, int]:
    """(n_closed_with_decision, n_high_closed, n_low_closed) via the live join.

    Reads both DBs read-only and reuses the exact join from
    ``iv_gate_shadow_vs_pnl.py`` — the trigger and the report can never
    disagree about what counts as a matched IV decision.
    """
    report = build_report()
    if report.get("error"):
        return 0, 0, 0
    hi = report["slices"]["high_iv"]
    lo = report["slices"]["low_iv"]
    n_closed = (hi["n_closed"] or 0) + (lo["n_closed"] or 0)
    return n_closed, hi["n_closed"] or 0, lo["n_closed"] or 0


def concentration_caveat(
    report: Dict[str, Any], threshold: float = CONCENTRATION_FRACTION
) -> Dict[str, Any]:
    """Max single-strategy / single-symbol share of the closed trades that
    carry an IV decision — per IV class and combined.

    A sample dominated by one strategy or one symbol (~80%+) means the
    gate's verdict reflects that single driver, not the IV regime broadly
    (the caveat to flag in the verdict and the panel, yellow). Pure —
    reads only ``per_strategy`` / ``per_symbol`` from the report (the
    ``build_report`` breakdowns).
    """
    per_strategy = report.get("per_strategy") or {}
    per_symbol = report.get("per_symbol") or {}
    classes = ("high_iv", "low_iv")

    def _top(by_key: Dict[str, Any]) -> Tuple[Optional[str], float]:
        """Top key + share of closed counts. Accepts both the report's
        {key: slice_stats} maps and the combined {key: int} counters."""
        closed: List[Tuple[str, int]] = []
        for k, v in by_key.items():
            n = v.get("n_closed") if isinstance(v, dict) else int(v or 0)
            closed.append((k, n or 0))
        total = sum(c for _, c in closed)
        if total <= 0:
            return None, 0.0
        top_key, top_c = max(closed, key=lambda kv: kv[1])
        return top_key, round(top_c / total, 4)

    strat_combined: Dict[str, int] = {}
    sym_combined: Dict[str, int] = {}
    by_class: Dict[str, Any] = {}
    for cls in classes:
        top_strat, strat_share = _top(per_strategy.get(cls) or {})
        top_sym, sym_share = _top(per_symbol.get(cls) or {})
        by_class[cls] = {
            "top_strategy": top_strat,
            "top_strategy_share": strat_share,
            "top_symbol": top_sym,
            "top_symbol_share": sym_share,
        }
        for k, v in (per_strategy.get(cls) or {}).items():
            strat_combined[k] = strat_combined.get(k, 0) + (v.get("n_closed") or 0)
        for k, v in (per_symbol.get(cls) or {}).items():
            sym_combined[k] = sym_combined.get(k, 0) + (v.get("n_closed") or 0)
    top_strat, strat_share = _top(strat_combined)
    top_sym, sym_share = _top(sym_combined)
    combined = {
        "top_strategy": top_strat,
        "top_strategy_share": strat_share,
        "top_symbol": top_sym,
        "top_symbol_share": sym_share,
    }
    shares = [
        strat_share, sym_share,
        by_class["high_iv"]["top_strategy_share"],
        by_class["high_iv"]["top_symbol_share"],
        by_class["low_iv"]["top_strategy_share"],
        by_class["low_iv"]["top_symbol_share"],
    ]
    return {
        "flagged": any(s >= threshold for s in shares),
        "threshold": threshold,
        "combined": combined,
        "by_class": by_class,
    }


def run_comparison() -> Optional[Dict[str, Any]]:
    """Re-run the full comparison and return the report dict (or None on error)."""
    report = build_report()
    if report.get("error"):
        log(f"comparison aborted — {report['error']}")
        return None
    return report


def _caveat_txt(concentration: Dict[str, Any]) -> str:
    """One-line concentration caveat appended to the verdict detail (empty
    when the sample is not concentrated)."""
    if not concentration.get("flagged"):
        return ""
    c = concentration.get("combined") or {}
    top_s = c.get("top_strategy") or "—"
    top_y = c.get("top_symbol") or "—"
    return (
        f" ⚠ Concentração: top estratégia `{top_s}` "
        f"{c.get('top_strategy_share', 0) * 100:.0f}% e top símbolo "
        f"`{top_y}` {c.get('top_symbol_share', 0) * 100:.0f}% da amostra "
        f"(≥{CONCENTRATION_FRACTION * 100:.0f}%) — o veredito reflete um "
        f"único driver, não o regime IV."
    )


def verdict(report: Dict[str, Any]) -> Dict[str, Any]:
    """Decide shadow vs enforcement (threshold 66.7). Pure — testable.

    Rules (closed trades with an IV decision):
      * n < TARGET_CLOSED            -> INCONCLUSIVE (keep shadow).
      * high_iv net > 0 and low_iv net <= 0
                                     -> PROMOTE (enforce high_iv-only at 66.7).
      * otherwise                    -> REJECT (sample contradicts the
                                        backtest direction — do not enforce).

    The returned decision always carries the sample concentration
    (``concentration_caveat``): when one strategy or symbol drives ~80%+ of
    the sample, ``concentration_caveat`` is True and the detail names the
    dominant driver.
    """
    hi = report["slices"]["high_iv"]
    lo = report["slices"]["low_iv"]
    n_closed = (hi["n_closed"] or 0) + (lo["n_closed"] or 0)
    hi_net = hi["net_pnl_usd"]
    lo_net = lo["net_pnl_usd"]
    concentration = concentration_caveat(report)
    caveat = _caveat_txt(concentration)
    if n_closed < TARGET_CLOSED:
        return {
            "status": "INCONCLUSIVE",
            "detail": (
                f"n={n_closed} closed trades com decisão IV < {TARGET_CLOSED} — "
                f"amostra insuficiente; manter shadow, continuar a recolher."
            ) + caveat,
            "n_closed": n_closed,
            "concentration_caveat": concentration["flagged"],
            "concentration": concentration,
        }
    if hi_net > 0 and lo_net <= 0:
        return {
            "status": "PROMOTE",
            "detail": (
                f"high_iv {hi_net:+.2f} USD (n={hi['n_closed']}) e low_iv "
                f"{lo_net:+.2f} USD (n={lo['n_closed']}) — mesma direção do "
                f"backtest: promover o gate a enforcement com threshold {IV_THRESHOLD}."
            ) + caveat,
            "n_closed": n_closed,
            "concentration_caveat": concentration["flagged"],
            "concentration": concentration,
        }
    return {
        "status": "REJECT",
        "detail": (
            f"high_iv {hi_net:+.2f} USD (n={hi['n_closed']}) e low_iv "
            f"{lo_net:+.2f} USD (n={lo['n_closed']}) — não confirma a direção do "
            f"backtest (ou high_iv não é positivo); manter shadow, não enforce."
        ) + caveat,
        "n_closed": n_closed,
        "concentration_caveat": concentration["flagged"],
        "concentration": concentration,
    }


def project_decision(
    hi: Dict[str, Any],
    lo: Dict[str, Any],
    *,
    concentration: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Project the shadow→enforcement decision from the CURRENT slices,
    before the n>=30 trigger fires (dashboard panel).

    Pure — the same rule as ``verdict()`` but WITHOUT the evidence gate:
    whatever the direction the live sample points to today is shown as a
    projection, flagged ``provisional`` when n < TARGET_CLOSED. Lets the
    operator watch PROMOTE/REJECT form while the sample is still small,
    instead of waiting for the watchdog run. When ``concentration`` (from
    ``concentration_caveat``) is passed and flagged, the projection is
    marked ``concentration_caveat`` and the detail names the driver.
    """
    concentration = concentration or {
        "flagged": False, "threshold": CONCENTRATION_FRACTION,
        "combined": {}, "by_class": {},
    }
    caveat = _caveat_txt(concentration)
    n_closed = (hi.get("n_closed") or 0) + (lo.get("n_closed") or 0)
    hi_net = hi.get("net_pnl_usd")
    lo_net = lo.get("net_pnl_usd")
    if hi_net is None or lo_net is None:
        direction = "N/A"
        detail = "slices ainda sem PnL fechado."
    elif hi_net > 0 and lo_net <= 0:
        direction = "PROMOTE"
        detail = (
            f"high_iv {hi_net:+.2f} USD (n={hi.get('n_closed') or 0}) e low_iv "
            f"{lo_net:+.2f} USD (n={lo.get('n_closed') or 0}) — aponta a "
            f"enforcement (high_iv-only, threshold {IV_THRESHOLD})."
        )
    else:
        direction = "REJECT"
        detail = (
            f"high_iv {hi_net:+.2f} USD (n={hi.get('n_closed') or 0}) e low_iv "
            f"{lo_net:+.2f} USD (n={lo.get('n_closed') or 0}) — não confirma o "
            f"backtest; manter shadow."
        )
    return {
        "status": direction,
        "provisional": n_closed < TARGET_CLOSED,
        "n_closed": n_closed,
        "high_net_usd": hi_net,
        "low_net_usd": lo_net,
        "detail": detail + caveat,
        "concentration_caveat": bool(concentration.get("flagged")),
        "concentration": concentration,
    }


def _fmt_money(v: Optional[float]) -> str:
    return f"{v:+.2f}" if v is not None else "—"


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v * 100:.0f}%" if v is not None else "—"


def write_report(report: Dict[str, Any], v: Dict[str, Any],
                 n_closed: int, n_high: int, n_low: int) -> None:
    hi = report["slices"]["high_iv"]
    lo = report["slices"]["low_iv"]
    rows = [
        "# IV Gate Shadow Recheck — shadow vs enforcement",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by "
        "`scripts/iv_gate_shadow_recheck.py`._",
        "",
        f"**Trigger: n={n_closed} closed trades com decisão IV (gate "
        f"{TARGET_CLOSED})** · high_iv={n_high} · low_iv={n_low} · "
        f"threshold IV = {IV_THRESHOLD} (DVOL percentile 30d).",
        "",
        "## Slices (PnL realizado, via o join de `scripts/iv_gate_shadow_vs_pnl.py`)",
        "",
        "| classe | n | closed | open | WR | net | avg | mediana | best / worst |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cls, s in (("high_iv", hi), ("low_iv", lo), ("unknown", report["slices"]["unknown"])):
        rows.append(
            f"| {cls} | {s['n']} | {s['n_closed']} | {s['n_open']} | "
            f"{_fmt_pct(s['win_rate'])} | {_fmt_money(s['net_pnl_usd'])} | "
            f"{_fmt_money(s['avg_pnl_usd'])} | {_fmt_money(s['median_pnl_usd'])} | "
            f"{_fmt_money(s['best_usd'])} / {_fmt_money(s['worst_usd'])} |"
        )
    rows += [
        "",
        "## Decisão",
        "",
        f"**{v['status']}** — {v['detail']}",
    ]
    if v.get("concentration_caveat"):
        c = (v.get("concentration") or {}).get("combined") or {}
        rows += [
            "",
            f"* ⚠ **Caveat de concentração**: top estratégia "
            f"`{c.get('top_strategy') or '—'}` "
            f"{c.get('top_strategy_share', 0) * 100:.0f}% e top símbolo "
            f"`{c.get('top_symbol') or '—'}` "
            f"{c.get('top_symbol_share', 0) * 100:.0f}% da amostra — o "
            f"veredito reflete um único driver, não o regime IV.",
        ]
    rows += [
        "",
        "## Referência",
        "",
        f"* Backtest high_iv-only: +{BACKTEST_EVIDENCE['net_high_iv_only_usd']:.2f} USD "
        f"(n={BACKTEST_EVIDENCE['n']}, {BACKTEST_EVIDENCE['source']}).",
        "* O gate IV é **shadow-only** até esta decisão: o router registra a classe "
        "IV por trade mas nunca bloqueia execução.",
        "* **PROMOTE** = decisão para promover o gate a enforcement (threshold "
        f"{IV_THRESHOLD}) — a flip no router é uma mudança deliberada e revista, "
        "fora do âmbito deste watchdog.",
        "",
    ]
    REPORT_PATH.write_text("\n".join(rows), encoding="utf-8")
    log(f"report written: {REPORT_PATH}")


def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            log("state file corrupt — starting fresh")
    return {"triggered": False, "runs": []}


def save_state(state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def do_recheck(state: Dict[str, Any], force: bool) -> bool:
    """Run the comparison + decision. Returns True if a run was executed."""
    n_closed, n_high, n_low = iv_decision_count()
    log(f"closed trades com decisão IV: {n_closed}/{TARGET_CLOSED} "
        f"(high_iv={n_high}, low_iv={n_low})")
    if not force and n_closed < TARGET_CLOSED:
        pct = (n_closed / TARGET_CLOSED) * 100
        log(f"abaixo do gate — a saltar ({pct:.0f}% lá)")
        return False

    report = run_comparison()
    if report is None:
        log("recheck aborted — no comparison report")
        return False
    v = verdict(report)
    write_report(report, v, n_closed, n_high, n_low)
    run = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_closed": n_closed,
        "n_high_closed": n_high,
        "n_low_closed": n_low,
        "verdict": v["status"],
        "detail": v["detail"],
        "slices": report["slices"],
        "report_path": str(REPORT_PATH),
    }
    state["runs"].append(run)
    if not force:
        state["triggered"] = True
    save_state(state)
    log(f"recheck complete: n={n_closed} (high={n_high}, low={n_low}) -> {v['status']}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="single check, then exit")
    ap.add_argument("--force", action="store_true",
                    help="run the comparison now regardless of n (smoke test / manual)")
    ap.add_argument("--hours", type=float, default=CHECK_HOURS, help="check interval hours")
    args = ap.parse_args()

    log(f"=== iv gate shadow recheck watchdog "
        f"(target {TARGET_CLOSED} closed com decisão IV, threshold {IV_THRESHOLD}, "
        f"check a cada {args.hours:.0f}h) ===")
    state = load_state()
    if state.get("triggered"):
        log(f"already triggered ({state['runs'][-1]['ts'] if state['runs'] else '?'}) — "
            "monitor em watch-only mode (use --force para re-runs manuais)")
    if args.once:
        do_recheck(state, force=args.force)
        return 0
    while True:
        if not state.get("triggered"):
            do_recheck(state, force=False)
        else:
            n_closed, _h, _l = iv_decision_count()
            log(f"watch-only: n={n_closed} closed com decisão IV — recheck já consumido")
        time.sleep(args.hours * 3600)


if __name__ == "__main__":
    raise SystemExit(main())
