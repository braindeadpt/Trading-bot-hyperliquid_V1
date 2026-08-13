"""Run the full pre-commit/pre-push gate: CI battery + security audit.

One command to run before commit/push, combining the two validations that
keep the repo green:

  1. CI battery  - `scripts/run_ci_tests.py` (unit + integration_offline;
                    optionally --network / --testnet-live).
  2. Security    - `python main.py --audit` (static audit; fails on CRITICAL
                    findings; --fail-on-high also fails on HIGH).

Returns non-zero (and stops early) if either stage fails. Safe to wire into
a git pre-commit/pre-push hook:

    # .git/hooks/pre-push
    python scripts/run_pre_push_gate.py || exit 1

Exit codes:
  0  all stages passed
  1  CI or audit failed
  2  audit unreachable (main.py --audit broken)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _run(cmd: list, label: str) -> int:
    print(f"\n{'=' * 70}\n>>> {label}\n{'=' * 70}")
    result = subprocess.run(cmd, cwd=str(ROOT), check=False)
    if result.returncode != 0:
        print(f"\n[FAIL] {label} FAILED (exit {result.returncode})")
        return result.returncode
    print(f"\n[PASS] {label} PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network",
        action="store_true",
        help="also run the network suite (real HTTP/WS calls)",
    )
    parser.add_argument(
        "--testnet-live",
        action="store_true",
        help="also run the testnet_live suite (real testnet orders)",
    )
    parser.add_argument(
        "--fail-on-high",
        action="store_true",
        help="security audit fails on HIGH findings too (default: CRITICAL only)",
    )
    parser.add_argument(
        "--skip-audit",
        action="store_true",
        help="run only the CI battery (audit runs separately)",
    )
    args = parser.parse_args()

    # Stage 1: CI battery
    ci_cmd = [sys.executable, str(ROOT / "scripts" / "run_ci_tests.py")]
    if args.network:
        ci_cmd.append("--network")
    if args.testnet_live:
        ci_cmd.append("--testnet-live")
    ci_rc = _run(ci_cmd, "CI battery (pytest)")
    if ci_rc != 0:
        print("\n[BLOCKED] GATE BLOCKED: CI failures. Fix before commit/push.")
        return ci_rc

    # Stage 2: security audit. Call the audit module directly (main.py's
    # --audit fast-path only forwards --src-dir, not --fail-on-high).
    if not args.skip_audit:
        sep = os.pathsep
        audit_env = {
            **os.environ,
            "PYTHONPATH": f"{ROOT}{sep}{ROOT / 'src'}",
        }
        audit_cmd = [
            sys.executable, "-c",
            "import sys; "
            "sys.stdout.reconfigure(encoding='utf-8', errors='replace'); "
            "from security.audit import main; raise SystemExit(main())",
            "--src-dir", str(ROOT / "src"),
        ]
        if args.fail_on_high:
            audit_cmd.append("--fail-on-high")
        audit_cmd.append("-v")
        print(f"\n{'=' * 70}\n>>> Security audit (security.audit)\n{'=' * 70}")
        audit_result = subprocess.run(
            audit_cmd, cwd=str(ROOT), env=audit_env, check=False,
        )
        if audit_result.returncode != 0:
            print("\n[FAIL] Security audit FAILED - GATE BLOCKED: review findings before commit/push.")
            return audit_result.returncode
        print("\n[PASS] Security audit PASSED")

    print("\n[PASS] PRE-PUSH GATE PASSED - safe to commit/push.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
