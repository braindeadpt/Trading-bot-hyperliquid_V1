"""Run CI test battery with clear failure reporting."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TESTS = [
    "tests/test_critical_fixes.py",
    "tests/test_portfolio_dashboard.py",
    "tests/test_phase2.py",
    "tests/test_phase3.py",
    "tests/test_phase4.py",
    "tests/test_phase5_live_auth.py",
    "tests/test_volatility_breakout.py",
    "tests/test_market_data_funding.py",
    "tests/test_market_data_phase4.py",
]


def main() -> int:
    sep = os.pathsep
    env = {
        **os.environ,
        "PYTHONPATH": f"{ROOT}{sep}{ROOT / 'src'}",
    }
    failed: list[str] = []

    for rel in TESTS:
        path = ROOT / rel
        print(f"\n>>> Running {rel}")
        result = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.returncode != 0:
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
            failed.append(rel)
            print(f"!!! FAILED {rel} (exit {result.returncode})")

    print("\n>>> Running unittest tests.test_basic")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_basic"],
        cwd=str(ROOT),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        failed.append("tests.test_basic")

    if failed:
        print("\nCI TEST FAILURES:")
        for name in failed:
            print(f"  - {name}")
        return 1

    print("\nAll CI tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
