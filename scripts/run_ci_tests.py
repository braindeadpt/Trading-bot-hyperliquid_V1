"""Run CI test battery with clear failure reporting."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TESTS = [
    "tests/test_critical_fixes.py",
    "tests/test_phase2.py",
    "tests/test_phase3.py",
    "tests/test_phase4.py",
    "tests/test_phase5_live_auth.py",
    "tests/test_volatility_breakout.py",
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
        )
        if result.returncode != 0:
            failed.append(rel)
            print(f"!!! FAILED {rel} (exit {result.returncode})")

    print("\n>>> Running unittest tests.test_basic")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_basic"],
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    if result.returncode != 0:
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
