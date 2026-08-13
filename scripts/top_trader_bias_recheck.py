"""Top-trader bias screening recheck — auto re-run at ≥20 dates of samples.

The screening probe (`docs/FEATURE_SCREENING_TOP_TRADER_BIAS.md`) requires
re-running once `top_trader_bias_samples` covers ≥20 distinct UTC dates
(≈3 weeks of ~60s polling). Below that the strict bootstrap gate is
structurally unreachable (see `survives_strict` in
`feature_screening_24m_candles.py`), so an early run only yields
"directional evidence, not a decision".

This watchdog makes that re-run automatic and idempotent:

  * Every CHECK_HOURS (default 6h) it counts distinct UTC dates in
    `data/research/hyperliquid.db -> top_trader_bias_samples`.
  * When dates >= 20 and no re-run has been recorded yet, it launches
    `scripts/feature_screening_top_trader_bias.py --json-out`, reads the
    machine-readable output, and writes a decision report against the gate.
  * The result is persisted in the state file (gitignored), so a restart
    never re-triggers a completed run. `--force` runs the screening now
    without marking the trigger as consumed (smoke tests / manual checks).

Verdict rule for the report:

  * any candidate cell survives the strict gate -> GATE PASS (promote).
  * none survives (dates >= 20)                 -> GATE FAIL (kill signal).
  * otherwise (dates < 20)                      -> INCONCLUSIVE.
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
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
BIAS_DB = ROOT / "data" / "research" / "hyperliquid.db"
STATE_DIR = ROOT / "data" / "research"
STATE_PATH = STATE_DIR / "top_trader_bias_recheck_state.json"
PROBE_SCRIPT = ROOT / "scripts" / "feature_screening_top_trader_bias.py"
REPORT_PATH = ROOT / "docs" / "TOP_TRADER_BIAS_RECHECK_RESULT.md"
JSON_OUT = ROOT / "data" / "backtests" / "top_trader_bias_screening_latest.json"

TARGET_DATES = 20
CHECK_HOURS = 6


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def bias_date_count(
    db: Optional[Path] = None,
) -> Tuple[int, int, Optional[int], Optional[int]]:
    """(n_dates, n_samples, min_ms, max_ms) — distinct UTC dates in the samples.

    Dates are UTC-day indices (`timestamp_ms // 86_400_000`), matching the
    probe's `date` column (`ts.dt.strftime("%Y-%m-%d")` in UTC).
    """
    db_path = db or BIAS_DB
    if not db_path.exists():
        return 0, 0, None, None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COUNT(DISTINCT (timestamp_ms / 86400000)), COUNT(*), "
            "MIN(timestamp_ms), MAX(timestamp_ms) FROM top_trader_bias_samples"
        )
        row = cur.fetchone()
    except sqlite3.Error:
        return 0, 0, None, None
    finally:
        conn.close()
    if row is None or row[0] is None:
        return 0, 0, None, None
    return int(row[0]), int(row[1]), int(row[2] or 0), int(row[3] or 0)


def load_state(path: Optional[Path] = None) -> Dict[str, Any]:
    state_path = path or STATE_PATH
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            log("state file corrupt — starting fresh")
    return {"triggered": False, "runs": []}


def save_state(state: Dict[str, Any], path: Optional[Path] = None) -> None:
    state_path = path or STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def run_probe(json_out: Optional[Path] = None) -> Optional[Path]:
    """Run the screening probe and return the path of the written JSON."""
    out = json_out or JSON_OUT
    log(f"launching {PROBE_SCRIPT.name} --json-out {out}")
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, str(PROBE_SCRIPT), "--json-out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1800,
    )
    elapsed = time.time() - t0
    if proc.returncode != 0:
        log(f"probe failed rc={proc.returncode} after {elapsed:.0f}s:\n{proc.stderr[-2000:]}")
        return None
    if not out.exists():
        log(f"probe rc=0 but no JSON written ({elapsed:.0f}s)\n{proc.stdout[-2000:]}")
        return None
    log(f"probe done in {elapsed:.0f}s -> {out}")
    return out


def load_result(json_path: Path) -> Dict[str, Any]:
    return json.loads(json_path.read_text(encoding="utf-8"))


def verdict(cells: List[Dict[str, Any]], n_dates: int) -> str:
    """Decide promote / kill / inconclusive from the screening cells."""
    candidates = [c for c in cells if not c.get("is_control")]
    survived = [c for c in candidates if c.get("survives")]
    if n_dates < TARGET_DATES:
        return "INCONCLUSIVE — amostra ainda < 20 datas"
    if survived:
        names = ", ".join(
            f"`{c['feature']}`@{c['horizon']}" for c in survived
        )
        return f"GATE PASS — {len(survived)} célula(s) sobrevive(m): {names}"
    return "GATE FAIL — nenhuma célula sobreviveu ao gate estrito"


def best_candidate(cells: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    cands = [c for c in cells if not c.get("is_control")]
    if not cands:
        return None
    return max(cands, key=lambda c: abs(float(c.get("ic") or 0.0)))


def write_report(
    cells: List[Dict[str, Any]],
    meta: Dict[str, Any],
    *,
    n_dates: int,
    n_samples: int,
    v: str,
) -> None:
    cands = sorted(
        (c for c in cells if not c.get("is_control")),
        key=lambda c: -abs(float(c.get("ic") or 0.0)),
    )
    rows = [
        "# Top-Trader Bias Recheck — screening gate at ≥20 dates",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by "
        f"`scripts/top_trader_bias_recheck.py`._",
        "",
        f"**Trigger: `top_trader_bias_samples` covers {n_dates} datas "
        f"({n_samples} amostras, meta n_dates={meta.get('n_dates')}).**",
        "",
        "## Candidate cells (sorted by |IC|)",
        "",
        "| feature | h | IC | p_boot | n_dates | FDR | survives |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in cands:
        rows.append(
            f"| `{c['feature']}` | {c['horizon']} | "
            f"{float(c.get('ic') or 0.0):.3f} | "
            f"{float(c.get('p_raw') or 0.0):.2e} | "
            f"{c.get('n_dates')} | "
            f"{'Y' if c.get('fdr_reject') else 'n'} | "
            f"{'**SIM**' if c.get('survives') else 'não'} |"
        )
    rows += [
        "",
        "## Verdict",
        "",
        f"**{v}**",
        "",
        "## Context",
        "",
        "* Trigger: `top_trader_bias_samples` cobrir ≥20 datas (≈3 semanas de polling).",
        "* Below 20 datas the bootstrap gate is structurally unreachable — see ",
        "  `docs/FEATURE_SCREENING_TOP_TRADER_BIAS.md`.",
        "",
    ]
    REPORT_PATH.write_text("\n".join(rows), encoding="utf-8")
    log(f"report written: {REPORT_PATH}")


def do_recheck(state: Dict[str, Any], force: bool) -> bool:
    """Run the screening probe + decision report. True if a run executed."""
    n_dates, n_samples, _mn, _mx = bias_date_count()
    log(f"bias dates: {n_dates}/{TARGET_DATES} ({n_samples} samples)")
    if not force and n_dates < TARGET_DATES:
        log(f"below {TARGET_DATES} dates target — skipping "
            f"({n_dates / TARGET_DATES * 100:.0f}% there)")
        return False

    json_path = run_probe()
    if json_path is None:
        log("recheck aborted — no screening JSON produced")
        return False
    data = load_result(json_path)
    cells = data.get("cells") or []
    meta = data.get("meta") or {}
    v = verdict(cells, int(meta.get("n_dates") or n_dates))
    write_report(cells, meta, n_dates=n_dates, n_samples=n_samples, v=v)

    best = best_candidate(cells)
    run = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_dates": int(meta.get("n_dates") or n_dates),
        "n_samples": n_samples,
        "verdict": v,
        "n_survived": sum(1 for c in cells if not c.get("is_control") and c.get("survives")),
        "best_feature": best.get("feature") if best else None,
        "best_ic": round(float(best.get("ic") or 0.0), 4) if best else None,
        "json": str(json_path),
    }
    state["runs"].append(run)
    if not force:
        state["triggered"] = True
    save_state(state)
    log(f"recheck complete: {run['n_dates']} datas, "
        f"{run['n_survived']} survived -> {v}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="single check, then exit")
    ap.add_argument("--force", action="store_true",
                    help="run the screening now regardless of dates (smoke test / manual)")
    ap.add_argument("--hours", type=float, default=CHECK_HOURS, help="check interval hours")
    args = ap.parse_args()

    log("=== top-trader bias screening recheck watchdog starting "
        f"(target {TARGET_DATES} datas, check every {args.hours:.0f}h) ===")
    state = load_state()
    if state.get("triggered"):
        last = state["runs"][-1]["ts"] if state["runs"] else "?"
        log(f"already triggered at {TARGET_DATES} datas ({last}) — "
            "monitor in watch-only mode (use --force for manual re-runs)")
    if args.once:
        do_recheck(state, force=args.force)
        return
    while True:
        if not state.get("triggered"):
            do_recheck(state, force=False)
        else:
            n_dates, n_samples, _mn, _mx = bias_date_count()
            log(f"watch-only: {n_dates} datas — recheck already consumed "
                f"({n_samples} samples)")
        time.sleep(args.hours * 3600)


if __name__ == "__main__":
    main()
