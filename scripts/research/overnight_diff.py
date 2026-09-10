"""Compare two overnight session artifacts side by side.

Answers the question "what changed between run A and run B" — used when a
family is re-run after a data fix (e.g. a gap filled, new candles landed,
a harness bug corrected) to show per-cell verdict/net/n/PF movement.

Usage:
    python scripts/research/overnight_diff.py <artifact_a.json> <artifact_b.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ARTIFACT_DIR = ROOT / "data" / "research" / "overnight_experiments"


def _load(ref: str) -> Dict[str, Any]:
    p = Path(ref)
    if not p.exists():
        p = ARTIFACT_DIR / ref
    if not p.exists() and not ref.endswith(".json"):
        p = ARTIFACT_DIR / f"{ref}.json"
    if not p.exists():
        raise SystemExit(f"artifact not found: {ref}")
    return json.loads(p.read_text(encoding="utf-8"))


def _fmt_agg(a: Dict[str, Any]) -> str:
    return (f"net={a.get('pnl', 0.0):+.2f} n={a.get('n', 0)} "
            f"PF={a.get('pf', 0.0)}")


def diff_sessions(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    lines = [
        f"A: family={a.get('family')} span={a.get('span', {}).get('start')}.."
        f"{a.get('span', {}).get('end')}",
        f"B: family={b.get('family')} span={b.get('span', {}).get('start')}.."
        f"{b.get('span', {}).get('end')}",
        "",
    ]
    res_a = {r["tag"]: r for r in a.get("results") or []}
    res_b = {r["tag"]: r for r in b.get("results") or []}
    tags = list(dict.fromkeys(list(res_a) + list(res_b)))
    for tag in tags:
        ra, rb = res_a.get(tag), res_b.get(tag)
        if ra is None:
            lines.append(f"  {tag}: only in B -> {rb['verdict']} "
                         f"({_fmt_agg(rb.get('aggregate_variant') or {})})")
            continue
        if rb is None:
            lines.append(f"  {tag}: only in A -> {ra['verdict']} "
                         f"({_fmt_agg(ra.get('aggregate_variant') or {})})")
            continue
        va, vb = ra["verdict"], rb["verdict"]
        mark = "  " if va == vb else "!!"
        aa = ra.get("aggregate_variant") or {}
        bb = rb.get("aggregate_variant") or {}
        dp = (bb.get("pnl") or 0.0) - (aa.get("pnl") or 0.0)
        dn = (bb.get("n") or 0) - (aa.get("n") or 0)
        lines.append(
            f"{mark} {tag}: {va} -> {vb} | Δnet={dp:+.2f} Δn={dn:+d}")
        lines.append(f"     A {_fmt_agg(aa)}")
        lines.append(f"     B {_fmt_agg(bb)}")
        ng_a = (ra.get("noise_gate") or {})
        ng_b = (rb.get("noise_gate") or {})
        if ng_a.get("evaluated") or ng_b.get("evaluated"):
            lines.append(
                f"     noise p: {ng_a.get('p_value', '-')} -> "
                f"{ng_b.get('p_value', '-')} "
                f"(alpha {ng_a.get('alpha', '-')} -> {ng_b.get('alpha', '-')})")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("a")
    ap.add_argument("b")
    args = ap.parse_args()
    print(diff_sessions(_load(args.a), _load(args.b)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
