"""Re-screen BASIS + LIQUIDATION features on data-valid windows only.

Critical context (2026-08-09 contamination audit):
  * ``binance_perp_prices`` real coverage ends 2026-06-29 (fstream dead).
  * ``liquidation_events`` in bot.db are 100% ``source='proxy'`` (candle
    heuristic backfill) — there are ZERO ``source='binance'`` force-order
    rows. A "valid liq window" is therefore still PROXY data, not real
    liquidations. Report that explicitly.

Usage:
  python scripts/feature_screening_basis_liq_valid.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.feature_screening import (  # noqa: E402
    CANDIDATE_FEATURES,
    CONTROL_NEGS,
    CONTROL_POS,
    HORIZONS,
    FDR_ALPHA,
    SYMBOLS_DEFAULT,
    benjamini_hochberg,
    build_feature_frame,
    screen_one,
    survives_top_gate,
    write_report,
    _connect,
)
from dataclasses import asdict

BASIS_FEATURES = ["basis", "basis_z_7d", "basis_velocity_1h"]
# Proxy-derived liq features (NOT real Binance force-orders)
LIQ_FEATURES = [
    "liq_notional_15m",
    "liq_notional_1h",
    "liq_side_imbalance_1h",
    "bars_since_liq_cluster",
]

# Windows (UTC) where feeds exist in bot.db
BASIS_START = int(datetime(2026, 5, 30, tzinfo=timezone.utc).timestamp() * 1000)
BASIS_END = int(datetime(2026, 6, 29, 12, 13, tzinfo=timezone.utc).timestamp() * 1000)
LIQ_START = int(datetime(2026, 6, 8, tzinfo=timezone.utc).timestamp() * 1000)
LIQ_END = BASIS_END

MIN_N_POWERED = 500  # soft floor; below → INCONCLUSIVE for family


def _run_family(
    df: pd.DataFrame,
    features: Sequence[str],
    label: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    feats = list(features) + [CONTROL_POS, *CONTROL_NEGS]
    for feat in feats:
        for h_name, h_bars in HORIZONS.items():
            cell = screen_one(df, feat, h_name, h_bars)
            d = asdict(cell)
            d["family"] = label
            rows.append(d)
            print(
                f"  [{label}] {feat:28s} {h_name:4s} IC={cell.ic_agg:+.4f} "
                f"p={cell.p_raw:.2e} n={cell.n_eff} mono={cell.mono:+.2f}",
                flush=True,
            )
    # FDR within family candidates only
    cand_idx = [i for i, r in enumerate(rows) if not r["is_control"]]
    pvals = [rows[i]["p_raw"] for i in cand_idx]
    rejected, qvals = benjamini_hochberg(pvals, alpha=FDR_ALPHA)
    for j, i in enumerate(cand_idx):
        rows[i]["q_fdr"] = float(qvals[j]) if np.isfinite(qvals[j]) else float("nan")
        rows[i]["fdr_reject"] = bool(rejected[j])
    for r in rows:
        if r["is_control"]:
            r["q_fdr"] = float("nan")
            r["fdr_reject"] = False
        r["survives"] = survives_top_gate(r)
    return rows


def main() -> int:
    db = ROOT / "data" / "live" / "bot.db"
    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = ROOT / "docs" / "FEATURE_SCREENING_BASIS_LIQ_VALID.md"

    t0 = time.time()
    con = _connect(db)
    try:
        full = build_feature_frame(con, SYMBOLS_DEFAULT)
    finally:
        con.close()

    basis_df = full[
        (full["timestamp_ms"] >= BASIS_START) & (full["timestamp_ms"] <= BASIS_END)
    ].copy()
    liq_df = full[
        (full["timestamp_ms"] >= LIQ_START) & (full["timestamp_ms"] <= LIQ_END)
    ].copy()

    # Drop rows where basis is NaN (no bn_perp) for basis family — do NOT
    # forward-fill obsolete June prices into later bars (that was the bug).
    basis_cov = float(basis_df["basis"].notna().mean()) if len(basis_df) else 0.0
    liq_cov = float((liq_df["liq_notional_15m"].fillna(0) != 0).mean()) if len(liq_df) else 0.0

    print(f"Basis window rows={len(basis_df)} basis_non_nan={basis_cov:.1%}", flush=True)
    print(f"Liq window rows={len(liq_df)} nonzero_liq_bars={liq_cov:.1%}", flush=True)

    all_rows: List[Dict[str, Any]] = []
    all_rows.extend(_run_family(basis_df, BASIS_FEATURES, "BASIS_valid"))
    all_rows.extend(_run_family(liq_df, LIQ_FEATURES, "LIQ_proxy_valid"))

    # Verdict helpers
    def family_verdict(family: str, min_n: int = MIN_N_POWERED) -> Dict[str, Any]:
        cand = [r for r in all_rows if r.get("family") == family and not r["is_control"]]
        ns = [r["n_eff"] for r in cand if np.isfinite(r.get("n_eff", float("nan")))]
        max_n = max(ns) if ns else 0
        tops = [r for r in cand if r.get("survives")]
        if max_n < min_n:
            return {
                "verdict": "INCONCLUSIVE",
                "reason": f"max n_eff={max_n} < {min_n} (underpowered valid window)",
                "top": [],
            }
        if not tops:
            return {
                "verdict": "NO_TOP",
                "reason": "powered sample but no FDR+mono+stab+symbol survivors",
                "top": [],
            }
        return {"verdict": "HAS_TOP", "reason": f"{len(tops)} survivors", "top": tops}

    basis_v = family_verdict("BASIS_valid")
    liq_v = family_verdict("LIQ_proxy_valid")

    lines = [
        "# Feature Screening — BASIS & LIQUIDATIONS (valid windows only)",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Contamination context",
        "",
        "- Original full-window TOP exclusion of BASIS/LIQ is **invalid**: "
        "`merge_asof` silently forward-filled the last 2026-06-29 Binance perp "
        "price across ~40 subsequent days, and liq features saw zeros after "
        "the event table ended.",
        "- **`liquidation_events` are 100% `source='proxy'`** (candle+OI "
        "heuristic). Zero Binance `@forceOrder` rows exist in `bot.db`. This "
        "re-screen therefore measures the *proxy*, not real liquidations.",
        "",
        "## Windows",
        "",
        f"| family | start | end | rows | coverage note |",
        f"|---|---|---|---:|---|",
        f"| BASIS | 2026-05-30 | 2026-06-29 | {len(basis_df)} | "
        f"basis non-NaN {basis_cov:.0%} |",
        f"| LIQ (proxy) | 2026-06-08 | 2026-06-29 | {len(liq_df)} | "
        f"nonzero liq bars {liq_cov:.0%} |",
        "",
        "## Verdicts",
        "",
        f"- **BASIS:** `{basis_v['verdict']}` — {basis_v['reason']}",
        f"- **LIQ (proxy):** `{liq_v['verdict']}` — {liq_v['reason']}",
        "",
        "INCONCLUSIVE ≠ 'no edge'. Real liquidations remain untestable until "
        "`source='binance'` events exist.",
        "",
        "## Survivors (if any)",
        "",
    ]
    for fam, v in (("BASIS", basis_v), ("LIQ_proxy", liq_v)):
        tops = v.get("top") or []
        if not tops:
            lines.append(f"### {fam}: none")
            lines.append("")
            continue
        lines.append(f"### {fam}")
        lines.append("| feature | h | IC | q_FDR | mono | stab | sym | n |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
        for r in sorted(tops, key=lambda x: abs(x.get("ic_agg") or 0), reverse=True)[:10]:
            lines.append(
                f"| {r['feature']} | {r['horizon']} | {r['ic_agg']:.4f} | "
                f"{r.get('q_fdr', float('nan')):.3f} | {r['mono']:.2f} | "
                f"{r['same_sign_periods']}/3 | {r['same_sign_symbols']}/4 | "
                f"{r['n_eff']} |"
            )
        lines.append("")

    lines.append("## Full candidate cells")
    lines.append("")
    lines.append("| family | feature | h | IC | p_raw | q_FDR | FDR | mono | n |")
    lines.append("|---|---|---|---:|---:|---:|:---:|---:|---:|")
    cand = [r for r in all_rows if not r["is_control"]]
    cand.sort(key=lambda r: abs(r.get("ic_agg") or 0), reverse=True)
    for r in cand:
        lines.append(
            f"| {r.get('family')} | {r['feature']} | {r['horizon']} | "
            f"{r['ic_agg']:.4f} | {r['p_raw']:.2e} | "
            f"{r.get('q_fdr', float('nan')):.3f} | "
            f"{'Y' if r.get('fdr_reject') else 'n'} | {r['mono']:.2f} | "
            f"{r['n_eff']} |"
        )

    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    payload = {
        "meta": {
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": round(time.time() - t0, 1),
            "basis_window": [BASIS_START, BASIS_END],
            "liq_window": [LIQ_START, LIQ_END],
            "basis_verdict": basis_v,
            "liq_verdict": {k: v for k, v in liq_v.items() if k != "top"},
            "note": "liq family is proxy-only; zero binance forceOrder rows in DB",
        },
        "rows": all_rows,
    }
    (out_dir / f"feature_screening_basis_liq_valid_{stamp}.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    (out_dir / "feature_screening_basis_liq_valid_latest.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    print(f"Wrote {report}", flush=True)
    print(f"BASIS={basis_v['verdict']} LIQ={liq_v['verdict']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
