"""Re-classify portfolio battery under three-condition gate (no re-sim)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.baseline_signal_gate import (  # noqa: E402
    MIN_TRADES,
    _render_portfolio_md,
    gate_verdict,
)
from scripts.baseline_portfolio_scan import tier_note  # noqa: E402

OUT = ROOT / "data" / "backtests" / "parity_diag"


def _load_battery() -> tuple[dict, Path]:
    dated = sorted(OUT.glob("baseline_portfolio_battery_2*.json"), reverse=True)
    path = dated[0] if dated else OUT / "baseline_portfolio_battery_latest.json"
    return json.loads(path.read_text(encoding="utf-8")), path


def _fold_for(name: str, fk: str, battery: dict, smf: dict | None) -> dict:
    if name == "SmartMoneyFlow" and smf:
        for k, v in (smf.get("folds") or {}).items():
            if str(k).startswith(fk) or str(k) == fk:
                return v
    fr = (((battery.get("strategies") or {}).get(name) or {}).get("folds") or {}).get(fk)
    if fr:
        return fr
    return {
        "skipped": True,
        "n_trades": 0,
        "strategy_engine": {"n_trades": 0, "profit_factor": 0.0, "expectancy": 0.0},
        "baselines": {},
    }


def main() -> int:
    battery, src = _load_battery()
    smf_path = OUT / "baseline_gate_SmartMoneyFlow.json"
    smf = json.loads(smf_path.read_text(encoding="utf-8")) if smf_path.exists() else None

    harness_path = OUT / "baseline_harness_validation.json"
    harness = (
        json.loads(harness_path.read_text(encoding="utf-8"))
        if harness_path.exists()
        else None
    )

    names: list[str] = []
    seen: set[str] = set()
    for row in battery.get("table") or []:
        if row["strategy"] not in seen:
            names.append(row["strategy"])
            seen.add(row["strategy"])
    if smf and "SmartMoneyFlow" not in seen:
        names.append("SmartMoneyFlow")

    table: list[dict] = []
    for name in names:
        tn = tier_note(name)
        for fk in ("W2", "W3"):
            fr = _fold_for(name, fk, battery, smf)
            if fr.get("skipped") and not fr.get("baselines"):
                n = int(
                    fr.get("n_trades")
                    or (fr.get("strategy_engine") or {}).get("n_trades")
                    or 0
                )
                # scan-skip rows often use threshold 15
                gate = {
                    "verdict": "INCONCLUSIVE",
                    "reason": (
                        f"INCONCLUSIVE (underpowered / not runnable in replay): "
                        f"n_trades={n}<{MIN_TRADES}"
                    ),
                    "n_trades": n,
                    "pf": None,
                    "expectancy": None,
                    "pf_percentile": None,
                    "failed_conditions": [f"n_trades={n}<{MIN_TRADES}"],
                }
                b1p = b2p = b3p = None
                pf = exp = None
            else:
                if "strategy_engine" not in fr and "checklist_meta_engine" in fr:
                    fr = dict(fr)
                    fr["strategy_engine"] = fr["checklist_meta_engine"]
                gate = gate_verdict(fr)
                b1p = b2p = b3p = None
                if fr.get("baselines"):
                    b1p = fr["baselines"]["B1_random_direction"]["vs_real_fast"][
                        "profit_factor"
                    ]["percentile"]
                    b2p = fr["baselines"]["B2_random_timing"]["vs_real_fast"][
                        "profit_factor"
                    ]["percentile"]
                    b3p = fr["baselines"]["B3_random_both"]["vs_real_fast"][
                        "profit_factor"
                    ]["percentile"]
                eng = fr.get("strategy_engine") or {}
                pf = eng.get("profit_factor")
                exp = eng.get("expectancy")

            table.append(
                {
                    "strategy": name,
                    "fold": fk,
                    "n_trades": int(gate.get("n_trades") or 0),
                    "B1_pf_pctile": b1p,
                    "B2_pf_pctile": b2p,
                    "B3_pf_pctile": b3p,
                    "pf": pf,
                    "expectancy": exp,
                    "verdict": gate["verdict"],
                    "failed_conditions": gate.get("failed_conditions") or [],
                    "reason": gate.get("reason"),
                    "tier": tn["tier"],
                    "conservative_test": tn["conservative_vs_strategy"],
                }
            )

    payload = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "reclassified_from": str(src),
        "smf_artifact": str(smf_path) if smf else None,
        "seeds": battery.get("seeds", 200),
        "min_trades": MIN_TRADES,
        "criterion": "B1≥p95 AND n≥30 AND expectancy>0 (PF>1)",
        "table": table,
        "strategies": battery.get("strategies"),
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_json = OUT / f"baseline_portfolio_battery_reclass_{stamp}.json"
    latest = OUT / "baseline_portfolio_battery_latest.json"
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    md = _render_portfolio_md(payload, harness)
    # Append portfolio status board
    not_validatable = [
        "CVDOrderFlow",
        "SpotPerpCarry",
        "FundingMomentum",
        "OrderBookScalper",
        "FundingArbitrage",
        "LeadLag",
        "LiquidationCatcher",
    ]
    md += "\n## Portfolio status board (reclassified)\n\n"
    md += (
        "Criterion: PASS ⇔ B1≥p95 **and** n_trades≥30 **and** expectancy>0 (PF>1).\n"
        "INCONCLUSIVO = not tested (never use alone to kill a strategy).\n\n"
    )
    md += (
        "| Strategy | Fold | n | PF | B1 %ile | Verdict | Failed conditions |\n"
        "|----------|------|--:|---:|--------:|---------|-------------------|\n"
    )
    for row in table:
        failed = "; ".join(row["failed_conditions"]) if row["failed_conditions"] else "—"
        md += (
            f"| {row['strategy']} | {row['fold']} | {row['n_trades']} | "
            f"{row.get('pf')} | {row.get('B1_pf_pctile')} | **{row['verdict']}** | {failed} |\n"
        )
    md += (
        "\n### Not validatable in current candle replay (0 trades → not promotable)\n\n"
        + ", ".join(not_validatable)
        + "\n\n"
        "Tier-B feed gaps that hurt the strategy (not the baselines) make the test "
        "**conservative** vs the strategy — declare that on any future powered run.\n"
    )
    md += (
        "\n### SmartMoneyFlow W3 (why percentile alone is insufficient)\n\n"
        "B1 PF percentile **96** with engine PF **~0.27** / negative expectancy: "
        "beats random losers in an adverse regime but is still unprofitable. "
        "Under the three-condition gate this is **FAIL** (`not_profitable`), not PASS.\n"
    )

    report = OUT / "BASELINE_PORTFOLIO_GATE_REPORT.md"
    report.write_text(md, encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {latest}")
    print(f"Wrote {report}")
    passes = sum(1 for r in table if r["verdict"] == "PASS")
    fails = sum(1 for r in table if r["verdict"] == "FAIL")
    incon = sum(1 for r in table if r["verdict"] == "INCONCLUSIVE")
    print(f"PASS={passes} FAIL={fails} INCONCLUSIVE={incon}")
    for r in table:
        if r["verdict"] != "INCONCLUSIVE":
            print(
                f"  {r['strategy']}/{r['fold']}: {r['verdict']} "
                f"n={r['n_trades']} PF={r.get('pf')} B1={r.get('B1_pf_pctile')} "
                f"failed={r['failed_conditions']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
