"""FeedSilenceMonitor boot-downtime grace — the runtime sibling of the
preflight's stale-since-downtime verdict (2026-09-09).

The boot preflight classifies an over-threshold feed as stale-since-downtime
when its age fits inside downtime+tolerance (the bot was off, not the feed
dying). The runtime monitor mirrors that verdict: while a stale feed's age
is still inside downtime+tolerance — i.e. for about DOWNTIME_TOLERANCE_SEC
after boot, before it has delivered a beat — early/imminent/degraded alerts
must NOT fire (the silence is explained by the boot). Once the window
passes, or a beat arrives, the normal never-seen escalation resumes
untouched. No boot context -> behavior is exactly the pre-grace behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.unit

from src.data.market_data_health import (  # noqa: E402
    DOWNTIME_TOLERANCE_SEC,
    FeedSilenceMonitor,
)

# Boot instant in the PAST (2025-06-05) — check() without an explicit
# now_ms falls back to the real wall clock, and a future boot instant
# would make the evidence timestamps future-dated (negative ages).
BOOT_AT = 1_750_000_000_000          # boot instant (the report's now_ms)
DOWNTIME = 12 * 3600.0               # inferred downtime (bot off 12h)
LAST_EVIDENCE = int(BOOT_AT - DOWNTIME * 1000)


# The never-seen escalation fires at warn_fraction*threshold of PROCESS
# UPTIME. The boot grace window is only DOWNTIME_TOLERANCE_SEC (~300s)
# wide, so an observable suppression needs a feed whose escalation starts
# inside that window — a small threshold (here 120s: early at 60s uptime,
# imminent at 108s, degraded at 121s). With the production 1h+ thresholds
# the escalation floors sit far beyond the window by design.
FAST_THRESHOLD = 120.0


def _mon(clock, monkeypatch, *, feeds=None, **kw):
    """Isolated monitor with a fake monotonic clock (uptime = clock - start)."""
    monkeypatch.setattr(
        "src.data.market_data_health.time.monotonic", lambda: clock["t"]
    )
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds=feeds or {"funding_hl": FAST_THRESHOLD},
        **kw,
    )
    for name in list(mon._enabled_feeds):
        if name not in (feeds or {"funding_hl": FAST_THRESHOLD}):
            mon.disable_feed(name)
    return mon


def test_no_boot_context_behavior_unchanged(monkeypatch) -> None:
    """Without boot context the never-seen escalation is exactly the old
    one: early at 50% uptime, imminent at 90%, degraded at 100% (1h feed)."""
    clock = {"t": 0.0}
    mon = _mon(clock, monkeypatch, feeds={"funding_hl": 3600.0})

    clock["t"] = 1800.0  # 30 min uptime -> early
    warns = mon.check_early_warnings(now_ms=BOOT_AT + 1800_000)
    assert len(warns) == 1 and "early" in warns[0]

    clock["t"] = 3240.0  # 54 min -> imminent
    warns = mon.check_early_warnings(now_ms=BOOT_AT + 3240_000)
    assert len(warns) == 1 and "imminent" in warns[0]

    clock["t"] = 3660.0  # 61 min -> degraded (never produced)
    alerts = mon.check(now_ms=BOOT_AT + 3_660_000)
    assert len(alerts) == 1 and "never produced" in alerts[0]
    assert mon.snapshot()["funding_hl"]["degraded"] is True


def test_stale_feed_suppressed_inside_boot_window(monkeypatch) -> None:
    """A feed the boot classified stale-since-downtime fires NOTHING while
    its age is still inside downtime+tolerance — even with process uptime
    already past every escalation threshold (here: 200s uptime vs 120s
    threshold — normally degraded by now)."""
    clock = {"t": 0.0}
    mon = _mon(
        clock, monkeypatch,
        boot_stale_latest_ms={"funding_hl": LAST_EVIDENCE},
        boot_at_ms=BOOT_AT,
        boot_downtime_sec=DOWNTIME,
    )

    clock["t"] = 200.0  # past early (60s) AND imminent (108s) AND degraded (121s)
    now = BOOT_AT + 200_000
    assert mon.check_early_warnings(now_ms=now) == []
    assert mon.check(now_ms=now) == []
    snap = mon.snapshot(now_ms=now)
    assert snap["funding_hl"]["degraded"] is False
    assert snap["funding_hl"]["warned_50_pct"] is False
    assert snap["funding_hl"]["warned_90_pct"] is False


def test_alerts_resume_after_window(monkeypatch) -> None:
    """Once age passes downtime+tolerance (about tolerance after boot), the
    silence is no longer explained by the bot being off and the normal
    never-seen escalation resumes from the uptime clock."""
    clock = {"t": 0.0}
    mon = _mon(
        clock, monkeypatch,
        boot_stale_latest_ms={"funding_hl": LAST_EVIDENCE},
        boot_at_ms=BOOT_AT,
        boot_downtime_sec=DOWNTIME,
    )

    # inside the window — quiet despite uptime past the degraded threshold
    clock["t"] = 200.0
    assert mon.check_early_warnings(now_ms=BOOT_AT + 200_000) == []
    assert mon.check(now_ms=BOOT_AT + 200_000) == []

    # window closed: age = downtime + tolerance + 1 -> not explained
    after = DOWNTIME + DOWNTIME_TOLERANCE_SEC + 1.0
    clock["t"] = after  # uptime ~12h — past every threshold
    now = BOOT_AT + int(after * 1000)
    warns = mon.check_early_warnings(now_ms=now)
    assert any("early" in w for w in warns), warns
    assert any("imminent" in w for w in warns), warns
    alerts = mon.check(now_ms=now)
    assert len(alerts) == 1 and "never produced" in alerts[0]


def test_beat_ends_window_immediately(monkeypatch) -> None:
    """A beat after boot switches the feed to the age-based path with fresh
    evidence — no alerts, the boot window is irrelevant from then on."""
    clock = {"t": 0.0}
    mon = _mon(
        clock, monkeypatch,
        boot_stale_latest_ms={"funding_hl": LAST_EVIDENCE},
        boot_at_ms=BOOT_AT,
        boot_downtime_sec=DOWNTIME,
    )
    clock["t"] = 100.0
    mon.beat("funding_hl", timestamp_ms=BOOT_AT + 100_000)
    now = BOOT_AT + 100_000
    assert mon.check_early_warnings(now_ms=now) == []
    assert mon.check(now_ms=now) == []
    # a later quiet period still escalates normally from the fresh beat
    warns = mon.check_early_warnings(now_ms=now + int(0.6 * FAST_THRESHOLD * 1000))
    assert len(warns) == 1 and "early" in warns[0]


def test_feed_not_in_boot_report_not_suppressed(monkeypatch) -> None:
    """Feeds absent from the boot's stale list are NOT suppressed — the
    grace applies only to the boot's stale verdict. Same window, two feeds:
    taker_split (no boot evidence) escalates, funding_hl (stale) is quiet."""
    clock = {"t": 0.0}
    mon = _mon(
        clock, monkeypatch,
        feeds={"funding_hl": FAST_THRESHOLD, "taker_split": FAST_THRESHOLD},
        boot_stale_latest_ms={"funding_hl": LAST_EVIDENCE},
        boot_at_ms=BOOT_AT,
        boot_downtime_sec=DOWNTIME,
    )
    clock["t"] = 200.0  # past taker's early (60s) AND imminent (108s)
    now = BOOT_AT + 200_000
    warns = mon.check_early_warnings(now_ms=now)
    assert any("taker_split" in w and "early" in w for w in warns), warns
    assert any("taker_split" in w and "imminent" in w for w in warns), warns
    assert not any("funding_hl" in w for w in warns)


def test_boot_window_requires_valid_context(monkeypatch) -> None:
    """Incomplete boot context (no boot_at_ms / zero downtime) never
    suppresses — missing report == unchanged behavior."""
    clock = {"t": 0.0}
    mon = _mon(
        clock, monkeypatch,
        boot_stale_latest_ms={"funding_hl": LAST_EVIDENCE},
        boot_at_ms=None,
        boot_downtime_sec=0.0,
    )
    clock["t"] = 200.0
    warns = mon.check_early_warnings(now_ms=BOOT_AT + 200_000)
    # early (60s) AND imminent (108s) both fire — never suppressed
    assert any("early" in w for w in warns), warns
    assert any("imminent" in w for w in warns), warns