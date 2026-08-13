"""Per-feed daily max-age history → research DB.

Why: a feed whose ``age`` keeps growing between resets (each beat resets the
clock, so it never trips ``degraded``) is a delivery path that is slowly
failing — the fstream-outage lesson. Recording the daily max age per feed
lets us see the trend: a max that creeps up day over day is the early
fingerprint of a feed dying between the 50%/90% fire-once warnings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

UTC_DAY_MS = 86_400_000
SAMPLE_BUCKET_MS = 600_000  # 10 minutes — intraday sparkline resolution


def utc_day_start_ms(ts_ms: int) -> int:
    """Start of the UTC day containing ``ts_ms`` (floor to 00:00 UTC)."""
    return (int(ts_ms) // UTC_DAY_MS) * UTC_DAY_MS


def sample_bucket_start_ms(ts_ms: int) -> int:
    """Start of the 10-minute intraday bucket containing ``ts_ms``."""
    return (int(ts_ms) // SAMPLE_BUCKET_MS) * SAMPLE_BUCKET_MS


def age_pct(age_sec: float, max_silence_sec: float) -> float:
    """% of silence threshold reached by ``age_sec`` (cap at 999 for safety)."""
    if max_silence_sec <= 0:
        return 0.0
    return min(999.0, (float(age_sec) / float(max_silence_sec)) * 100.0)


def creeping_age_detector(
    daily: Sequence[Tuple[int, float]],
    *,
    min_days: int = 3,
    growth_min_sec: float = 600.0,
) -> Optional[Dict[str, Any]]:
    """Detect a feed whose daily max age is consistently growing.

    ``daily`` = ascending (day_start_ms, max_age_sec) rows. Fits a least
    squares slope over the window and flags when the slope is positive and
    the last day's max exceeds the first by ``growth_min_sec`` — the
    fingerprint of a feed getting quieter between resets.

    Returns None when there is not enough history or no consistent growth.
    """
    if len(daily) < min_days:
        return None
    xs = [float(i) for i in range(len(daily))]
    ys = [float(max_age) for _, max_age in daily]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    slope = num / den if den else 0.0
    growth = ys[-1] - ys[0]
    if slope > 0.0 and growth >= growth_min_sec:
        return {
            "days": n,
            "first_max_sec": ys[0],
            "last_max_sec": ys[-1],
            "growth_sec": growth,
            "slope_sec_per_day": slope,
            "creeping": True,
        }
    return None


class FeedAgeRecorder:
    """Background task: sample feed silence ages → research DB daily maxes.

    Samples the ``FeedSilenceMonitor`` snapshot on an interval, tracks the
    running max age per feed within the current UTC day bucket, and flushes
    the completed day to the research DB when the bucket rolls over. A final
    flush of the partial current day happens on ``stop()`` so a restart never
    loses the in-progress day.
    """

    def __init__(
        self,
        db: Any,
        snapshot_fn: Callable[[], Dict[str, Dict[str, Any]]],
        *,
        interval_sec: float = 300.0,
    ) -> None:
        self._db = db
        self._snapshot_fn = snapshot_fn
        self._interval = max(30.0, float(interval_sec))
        self._running: Dict[str, Tuple[int, float, int]] = {}  # feed -> (day, max, samples)
        # feed -> (bucket_ms, max_pct, samples) — intraday sparkline data
        self._running_samples: Dict[str, Tuple[int, float, int]] = {}
        self._task: Optional[asyncio.Task] = None
        self._running_flag = False
        self._last_error: Optional[str] = None
        self._flushed_days = 0
        self._flushed_buckets = 0
        self._last_sample_ms: Optional[int] = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running_flag = True
        self._task = asyncio.create_task(self._loop(), name="feed_age_recorder")
        logger.info(
            "FeedAgeRecorder started — sampling every %.0fs → %s",
            self._interval,
            getattr(self._db, "db_path", "?"),
        )

    async def stop(self) -> None:
        self._running_flag = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Flush the partial current day/buckets so a restart doesn't lose them.
        self.flush()
        self.flush_samples()

    async def _loop(self) -> None:
        while self._running_flag:
            try:
                self.sample()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("FeedAgeRecorder sample failed: %s", exc)
            try:
                await asyncio.wait_for(
                    asyncio.sleep(self._interval),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                pass

    def sample(self, now_ms: Optional[int] = None) -> None:
        """Track running daily max + intraday bucket max pct per feed."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        self._last_sample_ms = int(now)
        day = utc_day_start_ms(now)
        bucket = sample_bucket_start_ms(now)
        # Roll over FIRST: flush any completed (previous) day bucket before
        # the new day's samples overwrite the running state.
        stale = [f for f, (d, _, _) in self._running.items() if d < day]
        if stale:
            self.flush(day_start_ms=day - UTC_DAY_MS)
        # Same for intraday buckets — flush completed buckets, keep current.
        stale_buckets = [
            f for f, (b, _, _) in self._running_samples.items() if b < bucket
        ]
        if stale_buckets:
            self.flush_samples(bucket_start_ms=bucket - SAMPLE_BUCKET_MS)
        snapshot = self._snapshot_fn() or {}
        for feed, st in snapshot.items():
            age = st.get("age_sec")
            if age is None:
                continue  # never seen this process — not a reset-creep signal
            cur = self._running.get(feed)
            if cur is None or cur[0] != day:
                self._running[feed] = (day, float(age), 1)
            else:
                self._running[feed] = (day, max(cur[1], float(age)), cur[2] + 1)
            pct = age_pct(float(age), float(st.get("max_silence_sec") or 0.0))
            cur_s = self._running_samples.get(feed)
            if cur_s is None or cur_s[0] != bucket:
                self._running_samples[feed] = (bucket, pct, 1)
            else:
                self._running_samples[feed] = (bucket, max(cur_s[1], pct), cur_s[2] + 1)

    def flush(self, day_start_ms: Optional[int] = None) -> int:
        """Persist running maxes.

        With ``day_start_ms`` given, flush only that bucket (rollover path).
        Without it, flush every bucket currently tracked — the partial
        current day at shutdown, whatever its wall-clock bucket is.
        """
        if not self._running:
            return 0
        if day_start_ms is not None:
            targets = {int(day_start_ms)}
        else:
            targets = {day_ms for _, (day_ms, _, _) in self._running.items()}
        rows = [
            (feed, day_ms, max_age, samples)
            for feed, (day_ms, max_age, samples) in self._running.items()
            if day_ms in targets
        ]
        if not rows:
            return 0
        try:
            n = self._db.save_feed_age_history(rows)
            self._flushed_days += n
            # Drop flushed buckets from the running state.
            self._running = {
                f: v for f, v in self._running.items() if v[0] not in targets
            }
            return n
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.warning("FeedAgeRecorder flush failed: %s", exc)
            return 0

    def flush_samples(self, bucket_start_ms: Optional[int] = None) -> int:
        """Persist intraday bucket maxes.

        With ``bucket_start_ms`` given, flush only that bucket (rollover
        path). Without it, flush every bucket tracked — the partial current
        bucket at shutdown. Also prunes stale intraday rows (48h retention)
        so the sparkline table stays bounded.
        """
        if not self._running_samples:
            return 0
        if bucket_start_ms is not None:
            targets = {int(bucket_start_ms)}
        else:
            targets = {b for _, (b, _, _) in self._running_samples.items()}
        rows = [
            (feed, bucket_ms, max_pct, samples)
            for feed, (bucket_ms, max_pct, samples) in self._running_samples.items()
            if bucket_ms in targets
        ]
        n = 0
        if rows:
            try:
                n = self._db.save_feed_age_samples(rows)
                self._flushed_buckets += n
                self._running_samples = {
                    f: v
                    for f, v in self._running_samples.items()
                    if v[0] not in targets
                }
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("FeedAgeRecorder flush_samples failed: %s", exc)
                return 0
        try:
            self._db.prune_feed_age_samples(now_ms=self._last_sample_ms)
        except Exception as exc:  # noqa: BLE001
            logger.warning("FeedAgeRecorder prune failed: %s", exc)
        return n

    def status(self) -> dict:
        return {
            "interval_sec": self._interval,
            "running": self._running_flag,
            "feeds_tracked": len(self._running),
            "flushed_days": self._flushed_days,
            "flushed_buckets": self._flushed_buckets,
            "last_error": self._last_error,
        }

    def load_sparkline(
        self,
        feed: str,
        now_ms: Optional[int] = None,
        hours: int = 24,
    ) -> List[Tuple[int, float]]:
        """Intraday max-pct series for ``feed`` over the last ``hours``.

        Returns ascending (bucket_ms, max_pct) from the research DB — the
        sparkline data for the dashboard panel.
        """
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        start = now - int(hours) * 3_600_000
        return [
            (int(b), float(p))
            for b, p, _ in self._db.load_feed_age_samples(feed, start, now)
        ]


def start_feed_age_recorder_from_config(
    cfg: Any,
    snapshot_fn: Callable[[], Dict[str, Dict[str, Any]]],
) -> Optional[FeedAgeRecorder]:
    """Build the recorder from ``research.feed_age_history``; None when off."""
    research = cfg.get("research", {}) or {}
    section = research.get("feed_age_history", {}) or {}
    if not bool(section.get("enabled", True)):
        return None
    from src.data.research_database import ResearchDatabase

    db = ResearchDatabase(ResearchDatabase.resolve_path(cfg))
    interval_sec = float(section.get("interval_sec", 300.0))
    return FeedAgeRecorder(db, snapshot_fn, interval_sec=interval_sec)
