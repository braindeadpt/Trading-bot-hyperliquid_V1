#!/usr/bin/env python3
"""Full-depth L2 feature screening — gated on ≥30 valid recording days.

Measurement only. Refuses to run until ``scripts/l2_recording_audit.py``
reports ``ready_for_screen=True``. Does not build strategies.

Usage:
  python scripts/feature_screening_l2_depth.py
  python scripts/feature_screening_l2_depth.py --force   # override readiness (debug)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.l2_recording_audit import audit_l2  # noqa: E402

OUT = ROOT / "data" / "research" / "paper_oos_90d" / "l2_depth_screen_latest.json"
DOC = ROOT / "docs" / "FEATURE_SCREENING_L2_DEPTH.md"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Run even if <30 days")
    args = ap.parse_args()

    audit = audit_l2(write=True)
    if not audit.get("ready_for_screen") and not args.force:
        payload = {
            "status": "DEFERRED",
            "reason": "insufficient_l2_history",
            "audit": audit,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "next": "Keep recorder running; re-run when ready_for_screen=true",
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps(payload, indent=2))
        print(
            f"\nDEFERRED: need >={audit.get('min_days_for_screen')} days "
            f"(have {audit.get('n_days')}). Not fishing early."
        )
        return 2

    # Placeholder for the future FDR screen — intentionally not implemented
    # until history is ready so we do not invent features on thin samples.
    payload = {
        "status": "NOT_IMPLEMENTED_YET" if args.force else "READY_BUT_SCREEN_PENDING",
        "audit": audit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "planned_features": [
            "microprice",
            "imbalance_by_level",
            "depth_slope",
            "queue_depletion",
            "ofi",
            "resiliency",
        ],
        "horizons_sec": [1, 5, 10, 30, 60],
        "controls": ["FDR", "date_cluster_bootstrap", "tier0_cost", "AS"],
        "note": (
            "Screen implementation lands after first 30 valid days without "
            "changing this gate. Do not build MM/directional from raw L2 yet."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
