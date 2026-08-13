#!/usr/bin/env python3
"""Join ``iv_gate_shadow`` decisions with executed trade PnL — high_iv vs low_iv in production.

The IV gate is shadow-only (never enforced): the backtest evidence is n=13 /
INCONCLUSIVE (docs/IV_HIGH_ONLY_AB_SPLIT.md). This script is the live
counterpart. Each time the regime router routes a trade it records an
``iv_gate_shadow`` decision (research DB) with the trailing-DVOL percentile /
class in the snapshot metadata — *before* touching execution. This script:

1. Loads every ``iv_gate_shadow`` decision from the research DB.
2. Loads executed trades from the live bot DB.
3. Joins decision → trade on (strategy, symbol, side) + nearest entry_time
   within a tolerance window (the decision is recorded in the same event-loop
   tick as ``_process_entry_signal``, so the gap is milliseconds in practice).
4. Compares the high_iv slice against the low_iv slice on realized PnL — the
   same comparison the backtest gate makes, on real fills.

Usage:
  python scripts/iv_gate_shadow_vs_pnl.py [--live-db PATH] [--research-db PATH]
  python scripts/iv_gate_shadow_vs_pnl.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# UTF-8 stdout so the Unicode report does not crash on cp1252 consoles (Windows).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.dvol_feed import IV_HIGH_PCT  # noqa: E402
from src.data.research_database import (  # noqa: E402
    DEFAULT_RESEARCH_DB_PATH,
    ResearchDatabase,
)
from src.utils.config import load_config  # noqa: E402

DEFAULT_LIVE_DB = Path("data") / "live" / "bot.db"
# Verdict gate: matches the n>=30 evidence gate used by the backtest A/Bs.
MIN_N_GATE = 30
BACKTEST_EVIDENCE = {
    "net_high_iv_only_usd": 42.99,
    "n": 13,
    "source": "docs/IV_HIGH_ONLY_AB_SPLIT.md (janelas independentes 30d)",
}


def _load_config() -> Dict[str, Any]:
    cfg_path = ROOT / "config" / "settings.yaml"
    if not cfg_path.exists():
        return {}
    try:
        cfg = load_config(str(cfg_path))
        return cfg if isinstance(cfg, dict) else {}
    except Exception:  # noqa: BLE001 — best-effort; CLI flags override anyway
        return {}


def resolve_db_paths(
    live_db: Optional[str] = None, research_db: Optional[str] = None
) -> Tuple[Path, Path]:
    """Resolve the live/research DB paths (CLI override → config → default)."""
    cfg = _load_config()
    live = live_db or cfg.get("database.path", str(DEFAULT_LIVE_DB))
    research = research_db or str(ResearchDatabase.resolve_path(cfg))
    return Path(live), Path(research)


def _resolve_dbs(args: argparse.Namespace) -> Tuple[Path, Path]:
    return resolve_db_paths(args.live_db, args.research_db)


def load_shadow_decisions(db_path: Path) -> List[Dict[str, Any]]:
    """All ``iv_gate_shadow`` rows with the IV class/percentile extracted."""
    if not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        try:
            rows = con.execute(
                "SELECT id, symbol, strategy, side, timestamp_ms, reason, snapshot_json "
                "FROM shadow_decisions WHERE variant = 'iv_gate_shadow' "
                "ORDER BY timestamp_ms ASC"
            ).fetchall()
        except sqlite3.OperationalError:
            # Fresh research DB without the table yet — zero decisions.
            rows = []
    finally:
        con.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        meta: Dict[str, Any] = {}
        try:
            snap = json.loads(r["snapshot_json"]) if r["snapshot_json"] else {}
            meta = snap.get("metadata", {}) if isinstance(snap, dict) else {}
        except (ValueError, TypeError):
            meta = {}
        iv_pct = meta.get("iv_percentile")
        out.append(
            {
                "id": int(r["id"]),
                "symbol": str(r["symbol"]),
                "strategy": str(r["strategy"]),
                "side": str(r["side"] or ""),
                "timestamp_ms": int(r["timestamp_ms"]),
                "reason": str(r["reason"] or ""),
                "iv_class": str(meta.get("iv_class") or _class_from_reason(r["reason"])),
                "iv_percentile": iv_pct,
                "iv_threshold": meta.get("iv_threshold", IV_HIGH_PCT),
            }
        )
    return out


def _class_from_reason(reason: Optional[str]) -> str:
    if not reason:
        return "unknown"
    r = str(reason)
    if "high_iv" in r:
        return "high_iv"
    if "low_iv" in r:
        return "low_iv"
    return "unknown"


def load_trades(db_path: Path) -> List[Dict[str, Any]]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        try:
            rows = con.execute(
                "SELECT id, symbol, side, entry_time, exit_time, pnl_usd, pnl_pct, "
                "strategy, status, exit_reason FROM trades ORDER BY entry_time ASC"
            ).fetchall()
        except sqlite3.OperationalError:
            # Fresh live DB without the trades table yet — zero trades.
            rows = []
    finally:
        con.close()
    return [
        {
            "id": int(r["id"]),
            "symbol": str(r["symbol"]),
            "side": str(r["side"] or ""),
            "entry_time": int(r["entry_time"]),
            "exit_time": r["exit_time"],
            "pnl_usd": r["pnl_usd"],
            "pnl_pct": r["pnl_pct"],
            "strategy": str(r["strategy"] or ""),
            "status": str(r["status"] or ""),
            "exit_reason": r["exit_reason"],
        }
        for r in rows
    ]


def join_decisions_to_trades(
    decisions: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    tolerance_ms: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """Attach each executed trade's IV class (nearest decision within tolerance).

    Returns (trades_with_iv, n_matched_decisions). A trade with no decision in
    the tolerance window is ``unknown`` (executed before DVOL was wired, or
    routed outside the shadow path). Each decision is consumed once — a routed
    decision that never filled (risk reject / no fill) has no trade row and is
    reported as coverage loss.
    """
    by_key: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for d in decisions:
        by_key.setdefault((d["strategy"], d["symbol"], d["side"]), []).append(d)

    used: set[int] = set()
    matched = 0
    for t in trades:
        cands = by_key.get((t["strategy"], t["symbol"], t["side"]), [])
        best: Optional[Dict[str, Any]] = None
        best_delta = tolerance_ms + 1
        for d in cands:
            if d["id"] in used:
                continue
            delta = abs(d["timestamp_ms"] - t["entry_time"])
            if delta <= tolerance_ms and delta < best_delta:
                best, best_delta = d, delta
        if best is not None:
            t["iv_class"] = best["iv_class"]
            t["iv_percentile"] = best["iv_percentile"]
            t["shadow_id"] = best["id"]
            t["shadow_ts_ms"] = best["timestamp_ms"]
            used.add(best["id"])
            matched += 1
        else:
            t["iv_class"] = "unknown"
            t["iv_percentile"] = None
            t["shadow_id"] = None
            t["shadow_ts_ms"] = None
    return trades, matched


def slice_stats(trades: List[Dict[str, Any]], iv_class: str) -> Dict[str, Any]:
    sl = [t for t in trades if t["iv_class"] == iv_class]
    closed = [t for t in sl if t["status"] == "closed" and t["pnl_usd"] is not None]
    pnls = [float(t["pnl_usd"]) for t in closed]
    n = len(sl)
    wins = sum(1 for p in pnls if p > 0)
    net = sum(pnls)
    # Average recorded IV percentile for the slice (sample distribution).
    pcts = [float(t["iv_percentile"]) for t in sl if t.get("iv_percentile") is not None]
    return {
        "class": iv_class,
        "n": n,
        "n_closed": len(closed),
        "n_open": n - len(closed),
        "win_rate": (wins / len(pnls) if pnls else None),
        "net_pnl_usd": net,
        "avg_pnl_usd": (net / len(pnls) if pnls else None),
        "median_pnl_usd": (sorted(pnls)[len(pnls) // 2] if pnls else None),
        "best_usd": (max(pnls) if pnls else None),
        "worst_usd": (min(pnls) if pnls else None),
        "n_pct": len(pcts),
        "avg_pct": (sum(pcts) / len(pcts) if pcts else None),
    }


def by_strategy(trades: List[Dict[str, Any]], iv_class: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for strat in sorted({t["strategy"] for t in trades}):
        out[strat] = slice_stats(
            [t for t in trades if t["strategy"] == strat], iv_class
        )
    return out


def verdict(hi: Dict[str, Any], lo: Dict[str, Any]) -> Dict[str, Any]:
    n_closed = (hi["n_closed"] or 0) + (lo["n_closed"] or 0)
    if n_closed < MIN_N_GATE:
        return {
            "status": "INCONCLUSIVE",
            "detail": (
                f"n={n_closed} closed trades with an IV decision < {MIN_N_GATE} "
                f"(gate de evidência) — amostra insuficiente para comparar com o "
                f"backtest (n={BACKTEST_EVIDENCE['n']}, +{BACKTEST_EVIDENCE['net_high_iv_only_usd']:.2f} USD)."
            ),
        }
    hi_net = hi["net_pnl_usd"]
    lo_net = lo["net_pnl_usd"]
    if hi_net > 0 >= lo_net:
        status = "CONSISTENTE"
        detail = "high_iv positivo e low_iv não-positivo — mesma direção do backtest."
    elif hi_net > lo_net:
        status = "CONSISTENTE (parcial)"
        detail = "high_iv > low_iv em PnL, mas low_iv também positivo."
    elif hi_net < lo_net:
        status = "CONTRADIZ"
        detail = "high_iv <= low_iv em PnL — o slice high_iv não vence na amostra real."
    else:
        status = "EMPATE"
        detail = "PnL idêntico entre slices."
    return {
        "status": status,
        "detail": detail,
        "hi_net_usd": hi_net,
        "lo_net_usd": lo_net,
    }


def fmt_pct(v: Optional[float]) -> str:
    return f"{v * 100:.0f}%" if v is not None else "—"


def fmt_money(v: Optional[float]) -> str:
    return f"{v:+.2f}" if v is not None else "—"


def _row(cls: str, s: Dict[str, Any]) -> str:
    return (
        f"| {cls} | {s['n']} | {s['n_closed']} | {s['n_open']} | "
        f"{fmt_pct(s['win_rate'])} | {fmt_money(s['net_pnl_usd'])} | "
        f"{fmt_money(s['avg_pnl_usd'])} | {fmt_money(s['median_pnl_usd'])} | "
        f"{fmt_money(s['best_usd'])} / {fmt_money(s['worst_usd'])} |"
    )


def build_report(
    *,
    live_db: Optional[str] = None,
    research_db: Optional[str] = None,
    tolerance_ms: int = 60_000,
    min_n: int = MIN_N_GATE,
) -> Dict[str, Any]:
    """Compute the full IV shadow vs PnL report (pure, reusable).

    Returns the report dict that ``main()`` persists/prints. Used by the
    recheck watchdog (``scripts/iv_gate_shadow_recheck.py``) so the join and
    slices live in exactly one place.
    """
    live, research = resolve_db_paths(live_db, research_db)
    decisions = load_shadow_decisions(research)
    trades = load_trades(live)
    min_n = max(1, min_n)

    if not trades:
        return {"error": "no_trades", "live_db": str(live), "research_db": str(research)}

    joined, matched = join_decisions_to_trades(decisions, trades, tolerance_ms)
    with_dec = sum(1 for t in joined if t["iv_class"] != "unknown")
    hi = slice_stats(joined, "high_iv")
    lo = slice_stats(joined, "low_iv")
    unk = slice_stats(joined, "unknown")
    v = verdict(hi, lo)
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "live_db": str(live),
        "research_db": str(research),
        "tolerance_ms": tolerance_ms,
        "min_n_gate": min_n,
        "n_decisions": len(decisions),
        "n_trades": len(trades),
        "matched_decisions": matched,
        "trades_with_decision": with_dec,
        "slices": {"high_iv": hi, "low_iv": lo, "unknown": unk},
        "per_strategy": {
            cls: by_strategy(joined, cls) for cls in ("high_iv", "low_iv", "unknown")
        },
        "verdict": v,
        "backtest_evidence": BACKTEST_EVIDENCE,
        "iv_high_pct": IV_HIGH_PCT,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live-db", help="live bot DB (default: config database.path)")
    ap.add_argument("--research-db", help="research DB (default: config research.database.path)")
    ap.add_argument("--tolerance-ms", type=int, default=60_000,
                    help="join window decision↔entry (default 60s)")
    ap.add_argument("--min-n", type=int, default=MIN_N_GATE,
                    help="closed-trade gate for the verdict (default 30)")
    ap.add_argument("--json", type=Path, help="write full report as JSON")
    args = ap.parse_args()

    live, research = _resolve_dbs(args)

    print("=" * 80)
    print("  IV gate shadow vs executed PnL — high_iv vs low_iv em produção")
    print(f"  live DB:      {live}")
    print(f"  research DB:  {research}")
    print("=" * 80)

    report = build_report(live_db=args.live_db, research_db=args.research_db,
                          tolerance_ms=args.tolerance_ms, min_n=args.min_n)
    if report.get("error") == "no_trades":
        print("Sem trades no live DB — nada a comparar.")
        return 0

    decisions_n = report["n_decisions"]
    trades_n = report["n_trades"]
    matched = report["matched_decisions"]
    with_dec = report["trades_with_decision"]
    joined_n = trades_n
    print(f"  decisões iv_gate_shadow: {decisions_n} | trades: {trades_n}")
    if not decisions_n:
        print(
            "Sem decisões iv_gate_shadow (DVOL ainda não wired no router, ou "
            "feed desligado). O slice high/low_iv fica vazio — só cobertura unknown."
        )

    # ── Coverage ──
    print(f"\n[1] Cobertura do join (decisão → trade, tol {args.tolerance_ms}ms)")
    print(f"    trades com decisão IV: {with_dec}/{joined_n} | "
          f"decisões consumidas: {matched}/{decisions_n} | "
          f"sem decisão (pré-DVOL/fora do shadow): {joined_n - with_dec}")

    # ── Slices ──
    print("\n[2] Slice high_iv vs low_iv (PnL realizado)")
    print("| classe | n | closed | open | WR | net | avg | mediana | best / worst |")
    print("|---|---|---|---|---|---|---|---|---|")
    hi, lo, unk = (report["slices"][k] for k in ("high_iv", "low_iv", "unknown"))
    print(_row("high_iv", hi))
    print(_row("low_iv", lo))
    print(_row("unknown", unk))
    print(f"\n    (threshold IV = {IV_HIGH_PCT}; classes do reason/metadata shadow)")

    # ── Per strategy ──
    print("\n[3] Por estratégia")
    for cls in ("high_iv", "low_iv", "unknown"):
        rows = report["per_strategy"][cls]
        if not any(s["n"] for s in rows.values()):
            continue
        print(f"  --- {cls} ---")
        for strat, s in sorted(rows.items()):
            if not s["n"]:
                continue
            print(f"    {strat:22} n={s['n']:3d} closed={s['n_closed']:3d} "
                  f"WR={fmt_pct(s['win_rate'])} net={fmt_money(s['net_pnl_usd'])} "
                  f"avg={fmt_money(s['avg_pnl_usd'])}")

    # ── Verdict ──
    v = report["verdict"]
    print("\n[4] Veredito")
    print(f"    {v['status']} — {v['detail']}")
    print(f"    Referência backtest: +{BACKTEST_EVIDENCE['net_high_iv_only_usd']:.2f} "
          f"USD (n={BACKTEST_EVIDENCE['n']}, {BACKTEST_EVIDENCE['source']}).")
    print(
        "    Nota: o gate IV é shadow-only por design (n de backtest INCONCLUSIVO) — "
        "este relatório é observacional, nunca bloqueia execução."
    )

    # ── Persist ──
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nJSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
