"""Run CI test battery with clear failure reporting.

Suites (see pytest.ini markers):
  unit                 - fast, no network, no cross-module wiring
  integration_offline  - multi-component (OMS, reconciliation, engine boot,
                          walk-forward, shutdown), mocks only, no network
  network              - requires real HTTP/WebSocket calls (opt-in, not run by CI)
  testnet_live         - requires a live Hyperliquid testnet connection (opt-in)

Default CI run = unit + integration_offline. Pass --network or --testnet-live
to additionally include those suites (they hit real endpoints).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="also run the network suite")
    parser.add_argument("--testnet-live", action="store_true", help="also run the testnet_live suite")
    args = parser.parse_args()

    marker_expr = "unit or integration_offline"
    if args.network:
        marker_expr += " or network"
    if args.testnet_live:
        marker_expr += " or testnet_live"

    sep = os.pathsep
    env = {
        **os.environ,
        "PYTHONPATH": f"{ROOT}{sep}{ROOT / 'src'}",
    }

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
