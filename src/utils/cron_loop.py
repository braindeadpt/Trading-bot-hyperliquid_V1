"""Wall-clock aligned sleep for pm2-supervised aux loops.

pm2's ``cron_restart`` proved unreliable on this host — the scheduler
double-fired (deregister+register 1s apart) and lost the job's timer
registration twice (jev-judge 2026-10-07 08:10 → silent 50h) and SIGKILLed
an in-flight outcome-eval run (2026-10-09 00:20 → orphaned eval lock,
boards frozen ~14h). The ``deploy/macos/run_*.sh`` wrappers now loop
internally and align each pass to a wall-clock boundary via this module:

    ./.venv/bin/python -m src.utils.cron_loop 300   # sleep to next :05 mark

A pass that starts on the boundary trains every run to the same minute,
so heartbeat panels see a stable cadence instead of drift.
"""

from __future__ import annotations

import sys
import time
from typing import Callable


def ms_to_next_boundary(now_ms: int, interval_ms: int) -> int:
    """Milliseconds until the next strict multiple of ``interval_ms``.

    ``now`` exactly on a boundary waits a full interval (the boundary that
    already started is not "next").
    """
    if interval_ms <= 0:
        raise ValueError(f"interval_ms must be > 0, got {interval_ms}")
    rem = now_ms % interval_ms
    return interval_ms - rem if rem else interval_ms


def sleep_to_next_boundary(
    interval_s: float,
    *,
    _sleep: Callable[[float], None] = time.sleep,
    _now: Callable[[], float] = time.time,
) -> float:
    """Sleep until the next wall-clock multiple of ``interval_s`` seconds.

    Returns the seconds slept (injectable clock/sleep for tests).
    """
    interval_ms = int(interval_s * 1000)
    wait_s = ms_to_next_boundary(int(_now() * 1000), interval_ms) / 1000.0
    _sleep(wait_s)
    return wait_s


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m src.utils.cron_loop <interval_s>",
              file=sys.stderr)
        raise SystemExit(2)
    iv = float(sys.argv[1])
    waited = sleep_to_next_boundary(iv)
    print(f"aligned to {iv:g}s boundary (slept {waited:.1f}s)")
