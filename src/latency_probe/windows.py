"""Window math in America/New_York (DST via zoneinfo).

A "session" is one window on one NY date:
  * "us-close": 19:45-20:15 NY, weekdays
  * "control":  15:00-15:30 NY, weekdays
Session id: "<YYYY-MM-DD>:<window>" — the readout clusters by it.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from src.latency_probe import spec

_NY = ZoneInfo(spec.NY_TZ)
_UTC = dt.timezone.utc


def _mins(hm: Tuple[int, int]) -> int:
    return hm[0] * 60 + hm[1]


def window_at(ts_ms: int) -> Optional[Tuple[str, str, int]]:
    """(window_name, session_id, window_end_utc_ms) or None."""
    d = dt.datetime.fromtimestamp(ts_ms / 1000, _UTC).astimezone(_NY)
    if d.weekday() not in spec.WEEKDAYS:
        return None
    minute = d.hour * 60 + d.minute + d.second / 60.0
    for name, start, end in (
            ("us-close", spec.US_CLOSE_START, spec.US_CLOSE_END),
            ("control", spec.CONTROL_START,
             (spec.CONTROL_START[0] + spec.CONTROL_MIN // 60,
              spec.CONTROL_START[1] + spec.CONTROL_MIN % 60))):
        if _mins(start) <= minute < _mins(end):
            end_local = d.replace(hour=end[0], minute=end[1],
                                  second=0, microsecond=0)
            end_ms = int(end_local.astimezone(_UTC).timestamp() * 1000)
            return name, f"{d.date().isoformat()}:{name}", end_ms
    return None


def next_window_start(ts_ms: int, horizon_d: int = 8
                      ) -> Optional[Tuple[int, str]]:
    """(start_utc_ms, window_name) of the next window after ts_ms."""
    d = dt.datetime.fromtimestamp(ts_ms / 1000, _UTC).astimezone(_NY)
    for day in range(horizon_d + 1):
        cand = d + dt.timedelta(days=day)
        if cand.weekday() not in spec.WEEKDAYS:
            continue
        for name, start in (("control", spec.CONTROL_START),
                            ("us-close", spec.US_CLOSE_START)):
            t = cand.replace(hour=start[0], minute=start[1],
                             second=0, microsecond=0)
            t_ms = int(t.astimezone(_UTC).timestamp() * 1000)
            if t_ms > ts_ms:
                return t_ms, name
    return None
