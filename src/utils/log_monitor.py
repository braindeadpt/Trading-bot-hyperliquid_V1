"""Auto-log monitoring (Task 5.1).

Heartbeat that checks the log file every 15 minutes for new ERROR/CRITICAL
entries and alerts when new fatal errors are detected.

Usage:
    monitor = LogMonitor("logs/bot.log", check_interval_minutes=15)
    monitor.start()  # runs in background thread
"""

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

logger = logging.getLogger(__name__)


class LogMonitor:
    """Monitors a log file for new ERROR/CRITICAL entries.

    Runs in a background thread and calls an alert callback when new
    errors are detected.  Tracks which lines have already been seen so
    each error is only reported once.
    """

    # Patterns that indicate a fatal/critical error
    FATAL_PATTERNS = ("ERROR", "CRITICAL", "FATAL", "EXCEPTION", "Traceback")

    def __init__(
        self,
        log_file: str,
        check_interval_minutes: int = 15,
        alert_callback: Optional[Callable[[List[str]], None]] = None,
    ) -> None:
        self._log_file = Path(log_file)
        self._check_interval_s = check_interval_minutes * 60
        self._alert_callback = alert_callback or self._default_alert
        self._last_position: int = 0
        self._last_size: int = 0
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._error_history: List[str] = []  # for deduplication

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background monitoring thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("LogMonitor started — checking %s every %d min",
                    self._log_file, self._check_interval_s // 60)

    def stop(self) -> None:
        """Stop the background thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            logger.info("LogMonitor stopped")

    def check_now(self) -> List[str]:
        """Immediate check — returns new fatal lines.

        Useful for testing or manual triggering.
        """
        return self._scan_for_new_errors()

    def get_stats(self) -> Dict[str, Any]:
        """Return monitoring statistics."""
        return {
            "log_file": str(self._log_file),
            "running": self._running,
            "last_position": self._last_position,
            "errors_detected": len(self._error_history),
            "last_errors": self._error_history[-10:],
        }

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Background loop."""
        # Initial check — skip existing errors
        self._scan_for_new_errors()
        while self._running:
            time.sleep(self._check_interval_s)
            if not self._running:
                break
            new_errors = self._scan_for_new_errors()
            if new_errors:
                self._alert_callback(new_errors)

    def _scan_for_new_errors(self) -> List[str]:
        """Read the log file from last known position and return new
        ERROR/CRITICAL lines.
        """
        if not self._log_file.exists():
            return []

        current_size = self._log_file.stat().st_size
        if current_size < self._last_size:
            # Log rotated — reset position
            self._last_position = 0
        self._last_size = current_size

        if current_size == 0:
            return []

        try:
            with self._log_file.open("r", encoding="utf-8") as f:
                f.seek(self._last_position)
                new_lines = f.readlines()
                self._last_position = f.tell()
        except (IOError, UnicodeDecodeError) as exc:
            logger.warning("LogMonitor: failed to read %s: %s", self._log_file, exc)
            return []

        # Filter for ERROR/CRITICAL/fatal lines
        errors = []
        for line in new_lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Check if any fatal pattern is present
            upper = stripped.upper()
            if any(pat in upper for pat in self.FATAL_PATTERNS):
                # Deduplicate — skip exact duplicates
                if stripped not in self._error_history:
                    errors.append(stripped)
                    self._error_history.append(stripped)
                    # Keep history bounded
                    if len(self._error_history) > 1000:
                        self._error_history = self._error_history[-500:]

        if errors:
            logger.warning("LogMonitor: detected %d new error(s)", len(errors))
        return errors

    def _default_alert(self, errors: List[str]) -> None:
        """Default alert handler — logs loudly."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        logger.error(
            "[LOG-MONITOR ALERT %s] %d new error(s) detected:",
            ts, len(errors),
        )
        for i, err in enumerate(errors[:5], 1):
            logger.error("  [%d] %s", i, err[:200])
        if len(errors) > 5:
            logger.error("  ... and %d more", len(errors) - 5)
