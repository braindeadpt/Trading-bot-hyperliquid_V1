"""Run the full pre-commit/pre-push gate: CI battery + security audit + config_hash.

One command to run before commit/push, combining the three validations that
keep the repo green:

  1. CI battery  - `scripts/run_ci_tests.py` (unit + integration_offline;
                    optionally --network / --testnet-live). Since
                    run_ci_tests.py itself runs the full trio (CI + audit +
                    hash), the gate passes --skip-audit --skip-hash so each
                    check runs exactly once — in this gate's own stages.
  2. Security    - static audit (`security.audit`); fails on CRITICAL
                    findings; --fail-on-high also fails on HIGH.
  3. config_hash - effective `config/settings.yaml` hash must equal the
                    Fase 10 frozen manifest hash (the same assert main.py
                    runs at startup — a drift here means the bot would
                    refuse to start).

Returns non-zero (and stops early) if any stage fails. Safe to wire into
a git pre-commit/pre-push hook:

    # .git/hooks/pre-push
    python scripts/run_pre_push_gate.py || exit 1

Exit codes:
  0  all stages passed
  1  CI / audit / hash failed
  2  stage unreachable (broken import / missing manifest)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Compare the effective config hash against the Fase 10 frozen manifest,
# mirroring src/research/phase10_preregister.assert_config_matches_preregister
# (the same check main.py runs at startup). Exit 1 on drift, 2 on missing
# manifest, 0 when hashes match.
#
# Reads the manifest JSON directly instead of importing phase10_preregister:
# that module's import chain pulls numpy/pandas through src.utils.helpers,
# which costs 10-20s+ in a cold subprocess (and is flaky under AV scans).
# The manifest is read exactly as load_preregister_manifest() reads it
# (DEFAULT_PATH, relative to cwd), so the check is behaviour-identical, just
# cheap. Must stay byte-identical to the snippet in scripts/run_ci_tests.py.
_HASH_CHECK_SNIPPET = (
    "import json, sys; "
    "sys.stdout.reconfigure(encoding='utf-8', errors='replace'); "
    "from pathlib import Path; "
    "from src.utils.config import load_config, compute_config_hash; "
    "cfg = load_config(); "
    "p = Path('data/research/phase10/phase10_preregister.json'); "
    "m = json.loads(p.read_text(encoding='utf-8')) if p.exists() else None; "
    "sys.exit(2 if m is None else 0 if compute_config_hash(cfg) == m.get('config_hash') else 1)"
)


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
    parser.add_argument(
        "--skip-hash",
        action="store_true",
        help="skip the config_hash vs frozen-manifest check (hash runs separately)",
    )
    args = parser.parse_args()

    # Stage 1: CI battery. run_ci_tests.py itself runs the full trio (CI +
    # audit + hash); pass --skip-audit --skip-hash so this gate's own audit /
    # hash stages below run each check exactly once instead of doubling work.
    ci_cmd = [
        sys.executable, str(ROOT / "scripts" / "run_ci_tests.py"),
        "--skip-audit", "--skip-hash",
    ]
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

    # Stage 3: config_hash vs Fase 10 frozen manifest. Replicates the startup
    # assert in main.py (assert_phase10_preregister) so a drift that would
    # refuse to boot is caught before commit/push.
    if not args.skip_hash:
        sep = os.pathsep
        hash_env = {
            **os.environ,
            "PYTHONPATH": f"{ROOT}{sep}{ROOT / 'src'}",
        }
        hash_cmd = [
            sys.executable, "-c", _HASH_CHECK_SNIPPET,
        ]
        print(f"\n{'=' * 70}\n>>> config_hash vs Fase 10 frozen manifest\n{'=' * 70}")
        hash_result = subprocess.run(
            hash_cmd, cwd=str(ROOT), env=hash_env, check=False,
        )
        if hash_result.returncode != 0:
            print(
                "\n[FAIL] config_hash check FAILED - GATE BLOCKED: settings.yaml "
                "drifted from the Fase 10 frozen window. Restore it or re-freeze "
                "before commit/push."
            )
            return hash_result.returncode
        print("\n[PASS] config_hash matches the frozen Fase 10 manifest")

    print("\n[PASS] PRE-PUSH GATE PASSED - safe to commit/push.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
