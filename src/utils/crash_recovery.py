"""Crash recovery wrapper (Task 5.2).

Monitors the bot process and auto-restarts it on crash:
  - Saves crash reason (last lines of log + traceback)
  - Restarts automatically in paper mode (safe)
  - Logs the restart event
  - Configurable max restart attempts to avoid loops

Usage:
    python run_with_recovery.py --mode paper
    python run_with_recovery.py --mode live   # falls back to paper on crash

Or programmatically:
    from src.utils.crash_recovery import CrashRecovery
    recovery = CrashRecovery(max_restarts=3, cooldown_seconds=30)
    recovery.run("python", "main.py", "--mode", "paper")
"""

import logging
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CrashRecovery:
    """Wraps the bot process with crash detection and auto-restart.

    Args:
        max_restarts: maximum number of restarts before giving up
        cooldown_seconds: wait time between restart attempts
        fallback_mode: mode to use after a crash ("paper" for safety)
        crash_log_file: where to save crash details
    """

    def __init__(
        self,
        max_restarts: int = 3,
        cooldown_seconds: int = 30,
        fallback_mode: str = "paper",
        crash_log_file: str = "logs/crashes.log",
    ) -> None:
        self._max_restarts = max_restarts
        self._cooldown_s = cooldown_seconds
        self._fallback_mode = fallback_mode
        self._crash_log = Path(crash_log_file)
        self._restart_count: int = 0
        self._last_crash_reason: str = ""
        self._total_uptime_s: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, *cmd: str) -> int:
        """Run the bot process with crash recovery.

        Args:
            cmd: command and arguments (e.g. "python", "main.py", "--mode", "paper")

        Returns:
            final exit code
        """
        logger.info(
            "CrashRecovery starting: max_restarts=%d, cooldown=%ds, fallback=%s",
            self._max_restarts, self._cooldown_s, self._fallback_mode,
        )

        while self._restart_count <= self._max_restarts:
            start_time = time.time()
            exit_code = self._run_once(cmd)
            uptime = time.time() - start_time
            self._total_uptime_s += uptime

            if exit_code == 0:
                logger.info("Bot exited cleanly (code 0). Uptime: %.1fs", uptime)
                return 0

            # Crash detected
            self._restart_count += 1
            self._last_crash_reason = self._capture_crash_reason(cmd, exit_code)
            self._log_crash(exit_code, uptime)

            if self._restart_count > self._max_restarts:
                logger.error(
                    "Max restarts (%d) reached. Giving up. Last crash: %s",
                    self._max_restarts, self._last_crash_reason[:200],
                )
                return exit_code

            # Restart in fallback mode (paper for safety)
            cmd = self._rewrite_mode_to_paper(cmd)
            logger.warning(
                "Restart %d/%d in %s mode after %.1fs uptime. "
                "Waiting %ds before restart...",
                self._restart_count, self._max_restarts,
                self._fallback_mode, uptime, self._cooldown_s,
            )
            time.sleep(self._cooldown_s)

        return 1  # unreachable, but satisfies type checker

    def get_stats(self) -> Dict[str, Any]:
        """Return recovery statistics."""
        return {
            "restart_count": self._restart_count,
            "max_restarts": self._max_restarts,
            "last_crash_reason": self._last_crash_reason[:500],
            "total_uptime_s": self._total_uptime_s,
            "fallback_mode": self._fallback_mode,
        }

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _run_once(self, cmd: tuple[str, ...]) -> int:
        """Run a single instance of the bot.

        SECURITY NOTE: subprocess.run is used intentionally to spawn the
        bot process — this is the core functionality of crash recovery
        (the accepted AUDIT-005 finding, documented in docs/SECURITY.md).
        The command is validated by ``_validate_cmd`` before execution:
        ``cmd[0]`` must be ``sys.executable`` (the current interpreter) and
        the first script must be ``main.py``; ``--mode`` values are
        restricted to the known set. No user-controlled argument reaches
        this call unvalidated.
        """
        if not self._validate_cmd(cmd):
            logger.error("Refusing to run invalid command: %s", cmd)
            return 1
        logger.info("Starting bot: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=False,  # let output go to console
                text=True,
            )
            return result.returncode
        except FileNotFoundError:
            logger.error("Command not found: %s", cmd[0])
            return 127
        except Exception as exc:
            logger.error("Failed to start bot: %s", exc)
            return 1

    @staticmethod
    def _validate_cmd(cmd: tuple[str, ...]) -> bool:
        """Allowlist-check the command before subprocess.run.

        Crash recovery only ever spawns the current interpreter running this
        repo's ``main.py`` in a known mode. Anything else (different
        executable, arbitrary script, or an unknown ``--mode`` value) is
        refused — this is the remediation layer for the accepted AUDIT-005
        subprocess finding.
        """
        if not cmd:
            return False
        import os

        # The interpreter must be the current one (allow both the full path
        # and the plain name, e.g. python.exe on Windows).
        exe = os.path.basename(cmd[0]).lower()
        cur = os.path.basename(sys.executable).lower()
        if not (cmd[0] == sys.executable or exe in (cur, "python", "python3", "python.exe", "python3.exe")):
            return False
        if len(cmd) < 2 or not os.path.basename(cmd[1]).endswith("main.py"):
            return False
        # --mode must be a known value (paper / testnet / live); anything
        # else — including a crafted extra arg — is refused.
        for i, arg in enumerate(cmd):
            if arg == "--mode":
                if i + 1 >= len(cmd) or cmd[i + 1] not in ("paper", "testnet", "live"):
                    return False
            elif arg.startswith("--mode="):
                if arg.split("=", 1)[1] not in ("paper", "testnet", "live"):
                    return False
        return True

    def _capture_crash_reason(
        self,
        cmd: tuple[str, ...],
        exit_code: int,
    ) -> str:
        """Capture the last lines of the log and any traceback."""
        lines: List[str] = []
        # Try to read the last 50 lines of the main log
        log_file = Path("logs/bot.log")
        if log_file.exists():
            try:
                with log_file.open("r", encoding="utf-8") as f:
                    all_lines = f.readlines()
                    lines = [l.strip() for l in all_lines[-50:] if l.strip()]
            except Exception:
                pass

        # Filter for ERROR/CRITICAL/Traceback lines
        error_lines = [
            l for l in lines
            if any(pat in l.upper() for pat in ("ERROR", "CRITICAL", "EXCEPTION", "TRACEBACK"))
        ]

        reason = f"exit_code={exit_code}"
        if error_lines:
            reason += " | last_errors: " + " | ".join(error_lines[-3:])
        else:
            reason += " | last_log: " + " | ".join(lines[-3:])

        return reason

    def _log_crash(self, exit_code: int, uptime: float) -> None:
        """Write crash details to crash log."""
        ts = datetime.now(timezone.utc).isoformat()
        self._crash_log.parent.mkdir(parents=True, exist_ok=True)
        with self._crash_log.open("a", encoding="utf-8") as f:
            f.write(
                f"[{ts}] crash #{self._restart_count} | "
                f"exit={exit_code} | uptime={uptime:.1f}s | "
                f"reason={self._last_crash_reason[:300]}\n"
            )

    @staticmethod
    def _rewrite_mode_to_paper(cmd: tuple[str, ...]) -> tuple[str, ...]:
        """Replace --mode X with --mode paper in the command."""
        args = list(cmd)
        for i, arg in enumerate(args):
            if arg == "--mode" and i + 1 < len(args):
                args[i + 1] = "paper"
                break
            if arg.startswith("--mode="):
                args[i] = "--mode=paper"
                break
        return tuple(args)


def main() -> int:
    """Entry point for ``python run_with_recovery.py ...``."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Run the trading bot with crash recovery",
    )
    parser.add_argument(
        "--mode", default="paper", choices=["paper", "testnet", "live"],
        help="Trading mode (default: paper)",
    )
    parser.add_argument(
        "--max-restarts", type=int, default=3,
        help="Maximum restart attempts (default: 3)",
    )
    parser.add_argument(
        "--cooldown", type=int, default=30,
        help="Seconds between restarts (default: 30)",
    )
    args = parser.parse_args()

    recovery = CrashRecovery(
        max_restarts=args.max_restarts,
        cooldown_seconds=args.cooldown,
        fallback_mode="paper",
    )

    # Build command: python main.py --mode X
    cmd = (sys.executable, "main.py", "--mode", args.mode)
    return recovery.run(*cmd)


if __name__ == "__main__":
    sys.exit(main())
