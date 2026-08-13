"""Run the full pre-push trio in one command: pytest battery + security audit + config_hash.

Suites (see pytest.ini markers):
  unit                 - fast, no network, no cross-module wiring
  integration_offline  - multi-component (OMS, reconciliation, engine boot,
                          walk-forward, shutdown), mocks only, no network
  network              - requires real HTTP/WebSocket calls (opt-in, not run by CI)
  testnet_live         - requires a live Hyperliquid testnet connection (opt-in)

Default CI run = unit + integration_offline. Pass --network or --testnet-live
to additionally include those suites (they hit real endpoints).

After the pytest battery passes, the same two stages as
``scripts/run_pre_push_gate.py`` run automatically — the static security
audit and the config_hash-vs-Fase-10-frozen-manifest check — so a single
command gives the full trio (CI + audit + hash). Pass --skip-audit /
--skip-hash to opt out of a stage (the pre-push gate does exactly this,
since it runs those stages itself and must not double-run them).

Exit codes mirror the gate: 0 all green, non-zero on the first failing stage
(pytest rc / audit rc / 1 for hash drift / 2 for a missing frozen manifest).
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
# (the same check main.py runs at startup). Must stay byte-identical to the
# snippet in scripts/run_pre_push_gate.py — both replicate the startup assert.
#
# Reads the manifest JSON directly instead of importing phase10_preregister:
# that module's import chain pulls numpy/pandas through src.utils.helpers,
# which costs 10-20s+ in a cold subprocess (and is flaky under AV scans).
# The manifest is read exactly as load_preregister_manifest() reads it
# (DEFAULT_PATH, relative to cwd), so the check is behaviour-identical, just
# cheap.
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


def _banner(label: str) -> None:
    print(f"\n{'=' * 70}\n>>> {label}\n{'=' * 70}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="also run the network suite")
    parser.add_argument("--testnet-live", action="store_true", help="also run the testnet_live suite")
    parser.add_argument(
        "--fail-on-high",
        action="store_true",
        help="security audit fails on HIGH findings too (default: CRITICAL only)",
    )
    parser.add_argument(
        "--skip-audit",
        action="store_true",
        help="run only the CI battery (audit + hash run separately)",
    )
    parser.add_argument(
        "--skip-hash",
        action="store_true",
        help="skip the config_hash vs frozen-manifest check (runs separately)",
    )
    args = parser.parse_args()

    sep = os.pathsep
    env = {
        **os.environ,
        "PYTHONPATH": f"{ROOT}{sep}{ROOT / 'src'}",
    }

    # Stage 1: pytest battery
    marker_expr = "unit or integration_offline"
    if args.network:
        marker_expr += " or network"
    if args.testnet_live:
        marker_expr += " or testnet_live"

    print(f">>> Running pytest -m \"{marker_expr}\"")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", marker_expr, "-v"],
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    if result.returncode != 0:
        print(f"\nCI TEST FAILURES (pytest exit code {result.returncode})")
        return result.returncode
    print("\nAll CI tests passed.")

    # Stage 2: security audit — same module + flags as the pre-push gate and
    # main.py --audit (fails on CRITICAL findings by default).
    if not args.skip_audit:
        _banner("Security audit (security.audit)")
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
        audit_result = subprocess.run(audit_cmd, cwd=str(ROOT), env=env, check=False)
        if audit_result.returncode != 0:
            print("\n[FAIL] Security audit FAILED - review findings before commit/push.")
            return audit_result.returncode
        print("[PASS] Security audit PASSED")

    # Stage 3: config_hash vs the Fase 10 frozen manifest — replicates the
    # startup assert in main.py (assert_phase10_preregister) so a drift that
    # would refuse to boot is caught before commit/push.
    if not args.skip_hash:
        _banner("config_hash vs Fase 10 frozen manifest")
        hash_result = subprocess.run(
            [sys.executable, "-c", _HASH_CHECK_SNIPPET],
            cwd=str(ROOT),
            env=env,
            check=False,
        )
        if hash_result.returncode != 0:
            print(
                "\n[FAIL] config_hash check FAILED - settings.yaml drifted from the "
                "Fase 10 frozen window. Restore it or re-freeze before commit/push."
            )
            return hash_result.returncode
        print("[PASS] config_hash matches the frozen Fase 10 manifest")

    print("\n[PASS] CI + security audit + config_hash - all green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
