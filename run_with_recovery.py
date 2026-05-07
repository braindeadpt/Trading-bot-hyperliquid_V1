"""Entry point that wraps the bot with crash recovery (Task 5.2).

Usage:
    python run_with_recovery.py --mode paper
    python run_with_recovery.py --mode live
    python run_with_recovery.py --mode testnet

The wrapper will:
  1. Detect crashes (non-zero exit code)
  2. Capture the last few log lines as crash reason
  3. Restart the bot in paper mode (safety fallback)
  4. Limit to 3 restarts with 30-second cooldown
  5. Log all crashes to logs/crashes.log
"""

from __future__ import annotations

import argparse
import sys

from src.utils.crash_recovery import CrashRecovery


def main() -> None:
    parser = argparse.ArgumentParser(description="Hyperliquid Bot with Crash Recovery")
    parser.add_argument(
        "--mode",
        choices=["paper", "testnet", "live"],
        default="paper",
        help="Trading mode (default: paper)",
    )
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=3,
        help="Maximum crash restarts (default: 3)",
    )
    parser.add_argument(
        "--cooldown",
        type=int,
        default=30,
        help="Cooldown seconds between restarts (default: 30)",
    )
    args = parser.parse_args()

    recovery = CrashRecovery(
        max_restarts=args.max_restarts,
        cooldown_seconds=args.cooldown,
        fallback_mode="paper",
    )

    cmd = (sys.executable, "main.py", "--mode", args.mode)
    exit_code = recovery.run(cmd)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
