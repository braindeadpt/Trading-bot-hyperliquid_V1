"""Liquidation flush recheck — automatic re-run when the real feed reaches 30d.

The v2 evidence (`data/backtests/liquidation_flush_shadow_v2_20260813_043848.json`)
approached the baseline gate on the REAL source (okx+bybit) with n=46,
PF 2.35, avg +6.98 bps — but that sample was only ~4 days old, and the
same cell is negative on the proxy source. The decision rule in
`docs/LIQUIDATION_FLUSH_SHADOW_LIVE.md` says: re-run when the real feed
accumulates 30 days.

This watchdog makes that re-run automatic and idempotent:

  * Every CHECK_HOURS (default 6h) it measures the span of real
    liquidation events in `data/live/bot.db` (okx+bybit only).
  * When span >= 30 days and no re-run has been recorded yet, it launches
    `scripts/liquidation_flush_shadow.py` (the full v2 simulation sweep),
    locates the freshly-written JSON, extracts the REAL ETH p90/30m/fade
    cell, and writes a comparison report against the v2 baseline.
  * The result is persisted in the state file (gitignored), so a restart
    never re-triggers a completed run. `--force` runs the comparison now
    without marking the 30d trigger as consumed (for smoke tests / manual
    checkpoints).

Comparison targets (from the v2 evidence):

  * Baseline v2 cell: n=46, WR 50.0%, PF 2.353, avg +6.98 bps.
  * The live/shadow evidence so far: see docs/LIQUIDATION_FLUSH_SHADOW_LIVE.md.

Verdict rule for the report:

  * n >= 30 and avg >= +3 bps and PF > 1.2  -> edge confirmed OOS, promote.
  * n >= 30 and (avg < 0 or PF < 1)         -> hypothesis dead, kill.
  * otherwise                               -> inconclusive.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "live" / "bot.db"
STATE_DIR = ROOT / "data" / "research"
STATE_PATH = STATE_DIR / "liquidation_flush_recheck_state.json"
SIM_SCRIPT = ROOT / "scripts" / "liquidation_flush_shadow.py"
REPORT_PATH = ROOT / "docs" / "LIQUIDATION_FLUSH_RECHECK_RESULT.md"

TARGET_DAYS = 30
CHECK_HOURS = 6
REAL_SOURCES = ("okx", "bybit")

# v2 baseline cell — from data/backtests/liquidation_flush_shadow_v2_20260813_043848.json
BASELINE = {
    "n": 46,
    "win_rate": 50.0,
    "profit_factor": 2.353,
    "avg_net_bps": 6.98,
    "net_bps": 321.0,
}

# current live evidence from the shadow-live harness (docs/LIQUIDATION_FLUSH_SHADOW_LIVE.md)
LIVE_EVIDENCE = {
    "n": 47,
    "win_rate": 48.9,
    "profit_factor": 2.23,
    "avg_net_bps": 6.37,
    "note": "shadow-live backfill 08-09..08-13 (simulation parity), 7d paper run started 08-13",
}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def real_span_days() -> float:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()
    ph = ",".join("?" * len(REAL_SOURCES))
    cur.execute(
        f"SELECT MIN(timestamp_ms), MAX(timestamp_ms), COUNT(*) FROM liquidation_events "
        f"WHERE source IN ({ph})",
        REAL_SOURCES,
    )
    mn, mx, cnt = cur.fetchone()
    conn.close()
    if mn is None or mx is None:
        return 0.0, 0
    return (mx - mn) / 86_400_000.0, cnt


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


def run_simulation() -> Optional[Path]:
    """Run the v2 sweep and return the path of the freshly-written JSON."""
    log("launching scripts/liquidation_flush_shadow.py (full sweep)")
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, str(SIM_SCRIPT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1800,
    )
    elapsed = time.time() - t0
    if proc.returncode != 0:
        log(f"simulation failed rc={proc.returncode} after {elapsed:.0f}s:\n{proc.stderr[-2000:]}")
        return None
    # The script prints the JSON path as the last non-empty line ("JSON: <path>").
    json_line = next(
        (ln.strip() for ln in reversed(proc.stdout.splitlines()) if ln.strip().startswith("JSON:")),
        None,
    )
    if json_line is None:
        log(f"could not locate JSON line in output (rc=0, {elapsed:.0f}s)\n{proc.stdout[-2000:]}")
        return None
    p = Path(json_line.split("JSON:", 1)[1].strip())
    log(f"simulation done in {elapsed:.0f}s -> {p}")
    return p if p.exists() else None


def extract_cell(results: List[Dict[str, Any]], source: str) -> Optional[Dict[str, Any]]:
    for x in results:
        if (x.get("source") == source and x.get("symbol") == "ETH"
                and x.get("threshold") == "p90" and x.get("hold_min") == 30
                and x.get("direction") == "fade" and x.get("sl_pct") is None):
            return x
    return None


def verdict(cell: Dict[str, Any]) -> str:
    n, pf, avg = cell["n"], cell["profit_factor"], cell["avg_net_bps"]
    if n >= 30 and avg >= 3.0 and pf > 1.2:
        return "CONFIRMED — promote to strategy proposal"
    if n >= 30 and (avg < 0 or pf < 1):
        return "DEAD — kill the hypothesis"
    return "INCONCLUSIVE — insufficient sample or marginal edge"


def write_report(cell: Dict[str, Any], span_days: float, n_events: int,
                 sim_path: Optional[Path]) -> None:
    rows = [
        "# Liquidation Flush Recheck — 30-day real-feed comparison",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by "
        f"`scripts/liquidation_flush_recheck.py`._",
        "",
        f"**Real feed span at trigger: {span_days:.1f} days ({n_events} okx/bybit events).**",
        "",
        "## The cell under test",
        "",
        "| Parameter | Value |",
        "|---|---|",
        "| Symbol | ETH |",
        "| Threshold | p90 of dominant-minute notional (recomputed on this sample) |",
        "| Direction | fade |",
        "| Hold | 30 min |",
        "| Stop-loss | none (no-op in v2) |",
        "",
        "## Comparison",
        "",
        "| Metric | v2 baseline (08-09..08-13) | recheck (30d) | delta |",
        "|---|---|---|---|",
        f"| n | {BASELINE['n']} | {cell['n']} | {cell['n'] - BASELINE['n']:+d} |",
        f"| win rate | {BASELINE['win_rate']:.1f}% | {cell['win_rate']:.1f}% | {cell['win_rate'] - BASELINE['win_rate']:+.1f}pp |",
        f"| profit factor | {BASELINE['profit_factor']:.3f} | {cell['profit_factor']:.3f} | {cell['profit_factor'] - BASELINE['profit_factor']:+.3f} |",
        f"| avg net | {BASELINE['avg_net_bps']:+.2f} bps | {cell['avg_net_bps']:+.2f} bps | {cell['avg_net_bps'] - BASELINE['avg_net_bps']:+.2f} bps |",
        f"| total net | {BASELINE['net_bps']:+.0f} bps | {cell['net_bps']:+.0f} bps | {cell['net_bps'] - BASELINE['net_bps']:+.0f} bps |",
        "",
        "## Verdict",
        "",
        f"**{verdict(cell)}**",
        "",
        "## Context",
        "",
        f"* Live/shadow evidence so far: n={LIVE_EVIDENCE['n']}, WR {LIVE_EVIDENCE['win_rate']}%, "
        f"PF {LIVE_EVIDENCE['profit_factor']}, avg {LIVE_EVIDENCE['avg_net_bps']:+.2f} bps "
        f"({LIVE_EVIDENCE['note']}).",
        f"* Simulation JSON: `{sim_path}`." if sim_path else "",
        "* Caveats: okx/bybit feed, not Hyperliquid; 30 days still modest for regime diversity.",
        "",
    ]
    REPORT_PATH.write_text("\n".join(rows), encoding="utf-8")
    log(f"report written: {REPORT_PATH}")


def do_recheck(state: Dict[str, Any], force: bool) -> bool:
    """Run the sweep + comparison. Returns True if a run was executed."""
    span, n_events = real_span_days()
    log(f"real feed span: {span:.2f} days ({n_events} events)")
    if not force and span < TARGET_DAYS:
        log(f"below {TARGET_DAYS}d target — skipping ({(span / TARGET_DAYS) * 100:.0f}% there)")
        return False

    sim_path = run_simulation()
    if sim_path is None:
        log("recheck aborted — no simulation JSON produced")
        return False
    data = json.loads(sim_path.read_text(encoding="utf-8"))
    cell = extract_cell(data["results"], "real")
    if cell is None:
        log("recheck aborted — REAL ETH p90/30m/fade cell not found in results")
        return False

    write_report(cell, span, n_events, sim_path)
    run = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "span_days": round(span, 2),
        "events": n_events,
        "sim_json": str(sim_path),
        "cell": cell,
        "verdict": verdict(cell),
    }
    state["runs"].append(run)
    if not force:
        state["triggered"] = True
    save_state(state)
    log(f"recheck complete: n={cell['n']} PF={cell['profit_factor']} avg={cell['avg_net_bps']:+.2f}bps "
        f"-> {run['verdict']}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="single check, then exit")
    ap.add_argument("--force", action="store_true",
                    help="run the comparison now regardless of span (smoke test / manual checkpoint)")
    ap.add_argument("--hours", type=float, default=CHECK_HOURS, help="check interval hours")
    args = ap.parse_args()

    log("=== liquidation flush recheck watchdog starting "
        f"(target {TARGET_DAYS}d real span, check every {args.hours:.0f}h) ===")
    state = load_state()
    if state.get("triggered"):
        log(f"already triggered at 30d ({state['runs'][-1]['ts'] if state['runs'] else '?'}) — "
            "monitor in watch-only mode (use --force for manual re-runs)")
    if args.once:
        do_recheck(state, force=args.force)
        return
    while True:
        if not state.get("triggered"):
            do_recheck(state, force=False)
        else:
            span, n_events = real_span_days()
            log(f"watch-only: span {span:.2f}d — recheck already consumed")
        time.sleep(args.hours * 3600)


if __name__ == "__main__":
    main()
