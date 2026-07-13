"""Fase 10 frozen-window gate CLI — read-only check against data/live/bot.db.

Usage:
    python scripts/phase10_check_gate.py                 # create manifest if missing, then check
    python scripts/phase10_check_gate.py --no-register    # only check; fail if manifest missing
    python scripts/phase10_check_gate.py --json           # machine-readable output

Exit code 0 only when every gate criterion is a real PASS. INSUFFICIENT_DATA
and FAIL both produce a non-zero exit code, since the gate is not met either
way — this script is meant to gate CI/ops decisions, not just report status.

Never writes to data/live/bot.db (opened via sqlite3 mode=ro URI). The only
write this script performs is the (idempotent, first-write-wins) Fase 10
preregister manifest under data/research/phase10/.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.research.phase10_gate_metrics import (  # noqa: E402
    Phase10GateMetricsError,
    build_gate_report,
    format_report_text,
)
from src.research.phase10_preregister import (  # noqa: E402
    persist_preregister_manifest,
)
from src.utils.config import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/settings.yaml", help="Path to settings.yaml")
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="Do not create the preregister manifest if missing (fail instead).",
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    args = parser.parse_args()

    config = load_config(args.config)

    if not args.no_register:
        persist_preregister_manifest(config)

    try:
        report = build_gate_report(config=config)
    except Phase10GateMetricsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_report_text(report))

    return 0 if report["gate_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
