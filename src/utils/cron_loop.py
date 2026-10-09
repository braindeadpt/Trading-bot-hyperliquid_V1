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

import calendar
import sys
import time
from typing import Callable


def ms_to_next_boundary(now_ms: int, interval_ms: int, offset_ms: int = 0) -> int:
    """Milliseconds until the next multiple of ``interval_ms`` + ``offset_ms``.

    ``offset_ms`` shifts the boundary phase — e.g. interval 10800s + offset
    1200s replicates cron ``20 */3 * * *`` (:20 of every third hour).

    ``now`` exactly on a boundary waits a full interval (the boundary that
    already started is not "next").
    """
    if interval_ms <= 0:
        raise ValueError(f"interval_ms must be > 0, got {interval_ms}")
    rem = (now_ms - offset_ms) % interval_ms
    return interval_ms - rem if rem else interval_ms


def sleep_to_next_boundary(
    interval_s: float,
    offset_s: float = 0.0,
    *,
    local: bool = False,
    _sleep: Callable[[float], None] = time.sleep,
    _now: Callable[[], float] = time.time,
) -> float:
    """Sleep until the next multiple of ``interval_s`` + ``offset_s``.

    ``local=True`` aligns to multiples of *local* wall-clock time — the
    same semantics as a cron schedule (pm2's cron_restart fired on server
    local time), so the phase survives DST changes like cron's did.
    Default (False) aligns to UTC.

    Returns the seconds slept (injectable clock/sleep for tests).
    """
    interval_ms = int(interval_s * 1000)
    offset_ms = int(offset_s * 1000)
    if local:
        # timegm(localtime(t)) = local wall-clock read as an epoch ("local
        # epoch") — multiples land on the same local slots a cron
        # expression would hit, and the phase follows DST automatically
        now_ms = calendar.timegm(time.localtime(_now())) * 1000
    else:
        now_ms = int(_now() * 1000)
    wait_s = ms_to_next_boundary(now_ms, interval_ms, offset_ms) / 1000.0
    _sleep(wait_s)
    return wait_s


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if a != "--local"]
    local = len(argv) != len(sys.argv) - 1
    if len(argv) not in (1, 2):
        print("usage: python -m src.utils.cron_loop <interval_s> [offset_s] [--local]",
              file=sys.stderr)
        raise SystemExit(2)
    iv = float(argv[0])
    off = float(argv[1]) if len(argv) == 2 else 0.0
    waited = sleep_to_next_boundary(iv, off, local=local)
    print(f"aligned to {iv:g}s boundary +{off:g}s"
          f"{' (local)' if local else ''} (slept {waited:.1f}s)")
