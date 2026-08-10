"""Re-gate ChecklistMeta with w_liquidation=0 (proxy contamination control).

Compares against the recorded FAIL (B1 48/43). Does not write settings.yaml.

Usage:
  python scripts/baseline_gate_cm_no_liq.py --seeds 100 --folds W2,W3
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.baseline_signal_gate import gate_verdict, run_fold_strategy  # noqa: E402
from src.data.database import Database  # noqa: E402
from src.utils.config import load_config  # noqa: E402

OUT = ROOT / "data" / "backtests" / "parity_diag"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=100)
    ap.add_argument("--folds", default="W2,W3")
    ap.add_argument(
        "--db",
        type=Path,
        default=ROOT / "data" / "live" / "bot_ruleset_validate.db",
    )
    args = ap.parse_args()
    folds = [f.strip() for f in args.folds.split(",") if f.strip()]

    cfg = load_config(ROOT / "config" / "settings.yaml")
    strat = cfg.get("strategy")
    if not isinstance(strat, dict) or "checklist_meta" not in strat:
        raise SystemExit("strategy.checklist_meta missing from config")
    # Mutate nested dict in place (Config may be read-only at top level)
    cm = strat["checklist_meta"]
    if not isinstance(cm, dict):
        raise SystemExit("checklist_meta is not a dict")
    prior_w = cm.get("w_liquidation")
    cm["w_liquidation"] = 0.0
    print(
        f"Patched strategy.checklist_meta.w_liquidation {prior_w} → 0.0 (in-memory)",
        flush=True,
    )

    db_path = args.db if args.db.exists() else ROOT / "data" / "live" / "bot.db"
    db = Database(str(db_path))
    symbols = list(cfg.get("assets") or ["BTC", "ETH", "SOL", "HYPE"])

    results = {}
    for fold in folds:
        print(f"=== ChecklistMeta w_liq=0 fold {fold} seeds={args.seeds} ===", flush=True)
        fr = run_fold_strategy(
            fold, cfg, db, symbols, "ChecklistMeta", args.seeds
        )
        gv = gate_verdict(fr)
        results[fold] = {"fold_result": fr, "gate": gv}
        print(
            f"  verdict={gv.get('verdict')} details={gv}",
            flush=True,
        )

    # Load prior FAIL for comparison if present
    prior_path = OUT / "baseline_gate_ChecklistMeta.json"
    prior = None
    if prior_path.exists():
        prior = json.loads(prior_path.read_text(encoding="utf-8"))

    payload = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "variant": "ChecklistMeta_w_liquidation_0",
        "seeds": args.seeds,
        "folds": results,
        "prior_path": str(prior_path) if prior else None,
        "interpretation": (
            "If B1/PF remain FAIL-class (powered, B1<<95 or PF<1), the "
            "demotion verdict stands — liquidation proxy contamination was "
            "immaterial at gate level (same order as OIR M1≈M2≈M3)."
        ),
    }
    # Attach compact prior numbers if available
    if prior:
        payload["prior_compact"] = {
            k: {
                "verdict": (prior.get("folds") or prior.get("results") or {})
                .get(k, {})
                .get("gate", {})
                .get("verdict")
                if isinstance(prior.get("folds") or prior.get("results"), dict)
                else None
            }
            for k in folds
        }

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "baseline_gate_ChecklistMeta_no_liq.json"
    # Store without huge fold dumps if too large — keep gate summaries
    slim = {
        "created_utc": payload["created_utc"],
        "variant": payload["variant"],
        "seeds": args.seeds,
        "interpretation": payload["interpretation"],
        "folds": {
            k: {"gate": v["gate"]}
            for k, v in results.items()
        },
    }
    # Try to extract B1 from fold_result for readability
    for k, v in results.items():
        fr = v["fold_result"]
        eng = fr.get("strategy_engine") or fr.get("checklist_meta_engine") or {}
        slim["folds"][k]["engine_n"] = eng.get("n_trades")
        slim["folds"][k]["engine_pf"] = eng.get("profit_factor")
        b1 = fr.get("b1") or {}
        slim["folds"][k]["b1_percentile"] = b1.get("percentile") or v["gate"].get(
            "b1_percentile"
        )

    out.write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out}", flush=True)

    # Material-change heuristic
    print("\n=== COMPARISON NOTE ===", flush=True)
    print(
        "Prior CM: W2 B1≈48 n=146; W3 B1≈43 n=215 — FAIL. "
        "If this run stays FAIL with similar B1, contamination did not drive the verdict.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
