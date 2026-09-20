"""tracemalloc-based memory leak profiler.

Opt-in via ``BOT_MEM_PROF=1`` env var. When enabled, snapshots Python heap
allocations every ``interval_sec`` and appends the top growing allocation
sites (diff vs previous snapshot) plus GC object-count deltas to
``logs/mem_trace.log``. Designed to answer "which code path is leaking"
after the bot runs for hours — diffs against the previous snapshot surface
steady growers regardless of baseline size.

tracemalloc overhead is small (~a few % CPU, bounded frames) and acceptable
for paper/research runs; keep it disabled on mainnet.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import os
import tracemalloc
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class MemProfiler:
    """Periodic tracemalloc snapshot differ."""

    def __init__(
        self,
        interval_sec: float = 300.0,
        out_path: Path | str = Path("logs") / "mem_trace.log",
        top_n: int = 25,
        nframes: int = 15,
    ) -> None:
        self._interval = max(60.0, float(interval_sec))
        self._out = Path(out_path)
        self._top_n = int(top_n)
        self._nframes = int(nframes)
        self._prev: Optional[tracemalloc.Snapshot] = None
        self._prev_counts: Optional[Counter] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        if not tracemalloc.is_tracing():
            tracemalloc.start(self._nframes)
        self._running = True
        self._out.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._loop(), name="mem_profiler")
        logger.info(
            "MemProfiler started (interval=%ss, out=%s)", self._interval, self._out
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("mem_profiler tick failed: %s", exc)

    async def _tick(self) -> None:
        # Snapshot + object counts off the event loop thread — both walk the
        # whole heap and can take ~100ms+ on a big process.
        snap, counts, traced_cur, traced_peak, rss_mb = await asyncio.to_thread(
            self._collect
        )
        lines = [
            "",
            f"=== {datetime.now(timezone.utc).isoformat()} "
            f"traced={traced_cur/1e6:.0f}MB peak={traced_peak/1e6:.0f}MB "
            f"rss={rss_mb:.0f}MB ===",
        ]
        if self._prev is not None:
            diff = snap.compare_to(self._prev, "lineno")
            pos = [s for s in diff if s.size_diff > 0][: self._top_n]
            lines.append("--- top growing alloc sites (vs prev tick) ---")
            for s in pos:
                lines.append(
                    f"+{s.size_diff/1024:.0f}KB ({s.count_diff:+d} obj) "
                    f"total={s.size/1024:.0f}KB @ {s.traceback[0]}"
                )
            if not pos:
                lines.append("(no positive growth)")
        else:
            top = snap.statistics("lineno")[: self._top_n]
            lines.append("--- baseline top alloc sites ---")
            for s in top:
                lines.append(f"{s.size/1024:.0f}KB ({s.count} obj) @ {s.traceback[0]}")

        if self._prev_counts is not None:
            growth = counts - self._prev_counts
            if growth:
                lines.append("--- top growing object types ---")
                for tname, delta in growth.most_common(15):
                    lines.append(f"+{delta:>8} {tname}")
        self._prev = snap
        self._prev_counts = counts
        try:
            with self._out.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as exc:
            logger.debug("mem_profiler write failed: %s", exc)

    def _collect(self):
        snap = tracemalloc.take_snapshot()
        counts: Counter = Counter()
        for obj in gc.get_objects():
            counts[type(obj).__name__] += 1
        traced_cur, traced_peak = tracemalloc.get_traced_memory()
        return snap, counts, traced_cur, traced_peak, _rss_mb()


def _rss_mb() -> float:
    try:
        import resource  # Unix only

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except ImportError:
        pass
    try:  # Windows without psutil
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        k32.K32GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD,
        ]
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        if k32.K32GetProcessMemoryInfo(
            k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb
        ):
            return pmc.WorkingSetSize / 1e6
    except Exception:  # noqa: BLE001
        pass
    return -1.0


def enabled() -> bool:
    return (os.environ.get("BOT_MEM_PROF") or "").strip() == "1"


def maybe_start(
    interval_sec: float = 300.0,
    out_path: Path | str = Path("logs") / "mem_trace.log",
) -> Optional[MemProfiler]:
    """Start the profiler iff BOT_MEM_PROF=1. Returns None when disabled."""
    if not enabled():
        return None
    prof = MemProfiler(interval_sec=interval_sec, out_path=out_path)
    prof.start()
    return prof
