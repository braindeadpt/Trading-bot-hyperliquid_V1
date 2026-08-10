#!/usr/bin/env python3
"""Weekly paper/OOS evidence snapshot (read-only vs bot.db).

Combines Phase10 gate progress, shadow net scoreboards, and L2 audit summary.
Never writes to bot.db. Writes under data/research/paper_oos_90d/weekly/.

Usage:
  python scripts/paper_oos_weekly_report.py
  python scripts/paper_oos_weekly_report.py --json-only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.phase10_gate_metrics import (  # noqa: E402
    Phase10GateMetricsError,
    build_gate_report,
)
from src.research.shadow_outcome_evaluator import run_evaluation  # noqa: E402
from src.research.shadow_panel import build_shadow_panel_payload  # noqa: E402
from src.utils.config import load_config  # noqa: E402

OUT_DIR = ROOT / "data" / "research" / "paper_oos_90d" / "weekly"


def _l2_audit_summary() -> Dict[str, Any]:
    try:
        from scripts.l2_recording_audit import audit_l2  # type: ignore

        return audit_l2(write=False)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def build_report(config_path: Path) -> Dict[str, Any]:
    cfg = load_config(config_path)
    p08 = cfg.get("strategy.phase08", {}) or {}
    shadow_names = [str(s) for s in (p08.get("shadow_strategies") or [])]
    exec_names = [str(s) for s in (p08.get("execution_strategies") or [])]

    phase10: Dict[str, Any]
    try:
        phase10 = build_gate_report(config=cfg)
    except Phase10GateMetricsError as exc:
        phase10 = {"error": str(exc), "gate_met": False}

    shadow = run_evaluation(since_days=14.0, config=cfg, persist=False)
    panel = build_shadow_panel_payload(
        shadow_names=shadow_names, config=cfg, evaluate=False
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol": "paper-oos-90d-v1",
        "fees": {
            "taker_fee_pct": cfg.get("risk.taker_fee_pct"),
            "maker_fee_pct": cfg.get("execution.maker_orders.maker_fee_pct"),
            "commission_pct": cfg.get("backtest.commission_pct"),
        },
        "execution_strategies": exec_names,
        "shadow_strategies": shadow_names,
        "paper_only": bool(p08.get("paper_only", True)),
        "phase10_gate": phase10,
        "shadow_14d": {
            "n_decisions_loaded": shadow.get("n_decisions_loaded"),
            "strategies": shadow.get("strategies"),
            "disclaimer": shadow.get("disclaimer"),
        },
        "shadow_panel": panel,
        "l2_audit": _l2_audit_summary(),
        "reminders": [
            "Mid-window snapshots are observational only — verdict at day 90.",
            "Do not promote without baseline_signal_gate PASS.",
            "Net shadow without funding_coverage_ok is INCONCLUSIVE, never PASS.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    report = build_report(Path(args.config))
    if not args.json_only:
        print("=== Paper OOS weekly snapshot ===")
        print(f"generated: {report['generated_at']}")
        print(f"execution: {report['execution_strategies']}")
        print(f"fees: {report['fees']}")
        p10 = report.get("phase10_gate") or {}
        print(f"phase10 gate_met: {p10.get('gate_met')} error={p10.get('error')}")
        print(f"shadow decisions (14d): {report['shadow_14d'].get('n_decisions_loaded')}")
        l2 = report.get("l2_audit") or {}
        print(f"l2 audit ok={l2.get('ok')} days={l2.get('n_days')} note={l2.get('note')}")
        print()
        for r in report["reminders"]:
            print(f"- {r}")

    if not args.no_write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = OUT_DIR / f"weekly_{stamp}.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        latest = OUT_DIR / "weekly_latest.json"
        latest.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {path}")

    if args.json_only:
        print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
