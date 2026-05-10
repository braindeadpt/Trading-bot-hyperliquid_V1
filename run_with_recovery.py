#!/usr/bin/env python3
"""
Crash recovery wrapper for the Hyperliquid trading bot.

Runs main.py as a subprocess and automatically restarts it on crash,
with configurable back-off and optional notification hooks.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RECOVERY_CONFIG = {
    "max_restarts": 10,
    "initial_backoff_sec": 3,
    "max_backoff_sec": 300,
    "backoff_multiplier": 2.0,
    "crash_log_lines": 50,
    "bot_script": "main.py",
    "bot_args": ["--mode", "paper"],
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("recovery")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# ---------------------------------------------------------------------------
# Crash analysis helpers
# ---------------------------------------------------------------------------

def analyze_crash(log_path: Path) -> Dict[str, Any]:
    """Read the last N lines of bot.log and extract crash context."""
    result: Dict[str, Any] = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "log_tail": [],
        "traceback": [],
        "error_type": None,
        "error_message": None,
    }
    if not log_path.exists():
        return result
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        tail = lines[-RECOVERY_CONFIG["crash_log_lines"] :]
        result["log_tail"] = tail
        # Look for traceback
        in_tb = False
        for line in tail:
            if line.strip().startswith("Traceback (most recent call last):"):
                in_tb = True
            if in_tb:
                result["traceback"].append(line)
            if in_tb and line.strip() and not line.startswith(" ") and not line.startswith("Traceback"):
                # Likely the error line
                parts = line.split(":", 1)
                if len(parts) == 2:
                    result["error_type"] = parts[0].strip()
                    result["error_message"] = parts[1].strip()
                break
    except Exception as exc:
        logger.warning("Failed to read crash log: %s", exc)
    return result

# ---------------------------------------------------------------------------
# Main recovery loop
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the bot in a subprocess, restarting on crash."""
    restart_count = 0
    backoff = RECOVERY_CONFIG["initial_backoff_sec"]
    bot_root = Path(__file__).resolve().parent
    log_path = bot_root / "logs" / "bot.log"
    fatal_log = bot_root / "logs" / "fatal_errors.log"

    # Ensure logs directory exists
    log_path.parent.mkdir(parents=True, exist_ok=True)

    while restart_count < RECOVERY_CONFIG["max_restarts"]:
        logger.info(
            "=== Starting bot (attempt %d/%d) ===",
            restart_count + 1,
            RECOVERY_CONFIG["max_restarts"],
        )
        start_time = time.time()

        # Run bot as subprocess (isolated, so a crash doesn't kill the watcher)
        proc = subprocess.Popen(
            [sys.executable, str(bot_root / RECOVERY_CONFIG["bot_script"])]
            + RECOVERY_CONFIG["bot_args"],
            cwd=str(bot_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # Wait for bot to finish (or crash)
        try:
            stdout, _ = proc.communicate(timeout=None)
        except KeyboardInterrupt:
            logger.info("Ctrl+C pressed — shutting down bot...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            logger.info("Recovery stopped by user.")
            return

        exit_code = proc.returncode
        run_time = time.time() - start_time

        if exit_code == 0:
            logger.info("Bot exited cleanly (code 0). Run time: %.1fs", run_time)
            break  # Normal shutdown, don't restart

        # Crash detected
        crash_info = analyze_crash(log_path)
        logger.error(
            "Bot crashed (exit code %d) after %.1fs. Error: %s — %s",
            exit_code,
            run_time,
            crash_info.get("error_type", "Unknown"),
            crash_info.get("error_message", "No message"),
        )

        # Write to fatal_errors.log
        try:
            with open(fatal_log, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 60}\n")
                f.write(f"CRASH at {datetime.utcnow().isoformat()}Z\n")
                f.write(f"Exit code: {exit_code} | Run time: {run_time:.1f}s\n")
                f.write(f"Error: {crash_info.get('error_type')}: {crash_info.get('error_message')}\n")
                f.write(f"Traceback:\n")
                for line in crash_info.get("traceback", []):
                    f.write(line + "\n")
                f.write(f"\nLog tail ({len(crash_info.get('log_tail', []))} lines):\n")
                for line in crash_info.get("log_tail", []):
                    f.write(line + "\n")
        except Exception as exc:
            logger.warning("Failed to write fatal log: %s", exc)

        restart_count += 1
        if restart_count >= RECOVERY_CONFIG["max_restarts"]:
            logger.error("Max restarts (%d) reached — giving up.", RECOVERY_CONFIG["max_restarts"])
            break

        # Backoff before restart
        logger.info("Restarting in %.1f seconds (back-off)...", backoff)
        time.sleep(backoff)
        backoff = min(
            backoff * RECOVERY_CONFIG["backoff_multiplier"],
            RECOVERY_CONFIG["max_backoff_sec"],
        )

    logger.info("Recovery loop ended.")


if __name__ == "__main__":
    main()
