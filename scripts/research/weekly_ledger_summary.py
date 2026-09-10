"""Weekly digest of the overnight research ledger.

Scans ``data/research/overnight_experiments/*.json`` artifacts produced in
the trailing 7 days and appends a compact digest entry to
``docs/OVERNIGHT_WEEKLY_DIGEST.md`` (append-only — same discipline as the
research log: entries are never edited, only added).

Runs standalone or via ``--if-due`` from ``overnight_nightly.py`` — the
state file ``data/research/weekly_digest_state.json`` records the last ISO
week written so the nightly caller can fire it idempotently.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ARTIFACT_DIR = ROOT / "data" / "research" / "overnight_experiments"
DIGEST_PATH = ROOT / "docs" / "OVERNIGHT_WEEKLY_DIGEST.md"
STATE_PATH = ROOT / "data" / "research" / "weekly_digest_state.json"
WEEK_MS = 7 * 86_400_000


def _load_artifacts(since_ms: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not ARTIFACT_DIR.exists():
        return out
    for p in sorted(ARTIFACT_DIR.glob("*.json")):
        if p.name == "NIGHTLY_STATUS.json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            ts = datetime.strptime(p.name[:15], "%Y%m%d_%H%M%S")
            data["_ts_ms"] = int(
                ts.replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            data["_ts_ms"] = int(p.stat().st_mtime * 1000)
        if data["_ts_ms"] >= since_ms:
            data["_file"] = p.name
            out.append(data)
    return out


def _cell_line(r: Dict[str, Any]) -> str:
    agg = r.get("aggregate_variant") or {}
    return (
        f"{r.get('tag', '?')}: {r.get('verdict', '?')} "
        f"(net={agg.get('pnl', '?'):+} n={agg.get('n', '?')} "
        f"PF={agg.get('pf', '?')}, alpha_eff={r.get('alpha_eff', '?')})"
        if isinstance(agg.get("pnl"), (int, float)) else
        f"{r.get('tag', '?')}: {r.get('verdict', '?')}"
    )


def build_digest(now_ms: int) -> str:
    arts = _load_artifacts(now_ms - WEEK_MS)
    iso = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    week = f"{iso.isocalendar().year}-W{iso.isocalendar().week:02d}"
    lines = [f"## {week} — digest {iso:%Y-%m-%d %H:%M} UTC", ""]
    if not arts:
        lines.append("No experiment artifacts this week.")
        lines.append("")
        return "\n".join(lines)

    verdicts: Dict[str, int] = {}
    runs = 0
    lines.append(f"**{len(arts)} sessions** this week:")
    lines.append("")
    for a in arts:
        fam = a.get("family", "?")
        results = a.get("results") or []
        n_runs = sum(len(r.get("variant_cells") or []) for r in results)
        n_runs += sum(
            len(r.get("baseline_cells") or []) for r in results[:1])
        runs += n_runs
        corr = a.get("family_correction") or {}
        corr_note = (
            f" α_eff={corr['alpha_eff']}"
            if corr.get("alpha_eff") else ""
        )
        lines.append(f"### {a['_file']} — {fam}{corr_note}")
        for r in results:
            v = str(r.get("verdict", "?"))
            verdicts[v] = verdicts.get(v, 0) + 1
            lines.append(f"- {_cell_line(r)}")
        lines.append("")
    hist = ", ".join(f"{k}={v}" for k, v in sorted(verdicts.items()))
    lines.append(f"**Totals:** {runs} runs | verdicts: {hist}")
    lines.append("")
    return "\n".join(lines)


def append_digest(text: str) -> None:
    DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DIGEST_PATH.exists():
        DIGEST_PATH.write_text(
            "# Overnight Weekly Digest\n\n"
            "Auto-generated weekly rollup of experiment artifacts. "
            "Append-only — entries are never edited.\n\n",
            encoding="utf-8",
        )
    with DIGEST_PATH.open("a", encoding="utf-8") as fh:
        fh.write(text)


def _iso_week(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--if-due", action="store_true",
                    help="only write if this ISO week has no digest yet")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if args.if_due and STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if st.get("last_week") == _iso_week(now_ms):
                print(f"weekly digest already written for {_iso_week(now_ms)}")
                return 0
        except Exception:
            pass

    global WEEK_MS
    if args.days != 7:
        WEEK_MS = args.days * 86_400_000
    text = build_digest(now_ms)
    append_digest(text)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({
        "last_week": _iso_week(now_ms),
        "written_ms": now_ms,
    }, indent=2), encoding="utf-8")
    print(f"weekly digest appended -> {DIGEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
