"""Single-instance guard — only one main.py process per project."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, TextIO

_lock_file: Optional[TextIO] = None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_instance_lock(lock_path: Path) -> None:
    """Acquire an exclusive instance lock or exit with a clear message."""
    global _lock_file
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        try:
            stale_pid = int(lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            stale_pid = 0
        if stale_pid and stale_pid != os.getpid() and _pid_alive(stale_pid):
            print(
                f"[FATAL] Another bot instance is already running (PID {stale_pid}).\n"
                f"        Run stop.bat first, then start only one main.py.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(
            "[FATAL] Bot lock file exists — another instance may be starting. "
            "Run stop.bat and retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    os.write(fd, str(os.getpid()).encode("ascii"))
    _lock_file = os.fdopen(fd, "w", encoding="utf-8")


def release_instance_lock(lock_path: Path) -> None:
    """Release lock on graceful shutdown."""
    global _lock_file
    if _lock_file is not None:
        try:
            _lock_file.close()
        except OSError:
            pass
        _lock_file = None
    try:
        if lock_path.exists():
            lock_path.unlink()
    except OSError:
        pass
