"""Unit tests: ChecklistMeta ignores proxy liquidations; FeedSilenceMonitor alerts."""

from __future__ import annotations

import inspect
import time

import pytest

from src.data.market_data_health import FeedSilenceMonitor
from src.strategies.checklist_meta import ChecklistMeta

pytestmark = pytest.mark.unit


def test_checklist_meta_requires_real_provenance_in_source() -> None:
    src = inspect.getsource(ChecklistMeta.on_data)
    assert "is_real_liquidation_source" in src
    assert "liq_long_squeeze" in src


def test_feed_silence_monitor_alerts_after_threshold() -> None:
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"binance_perp": 10.0},
    )
    mon.beat("binance_perp", timestamp_ms=1_000_000)
    alerts = mon.check(now_ms=1_000_000 + 11_000)
    assert alerts, alerts
    assert mon.any_degraded
    assert mon.snapshot()["binance_perp"]["degraded"] is True


def test_feed_silence_stale_event_ts_false_alarm_pattern() -> None:
    """OKX REST bootstrap uses event timestamps up to 6h old.

    Beating silence with those stamps trips FEED SILENT at restart.
    Engine must beat with receive time instead (see engine liquidation cb).
    """
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 6 * 3600.0},
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    now = int(time.time() * 1000)
    stale = now - 6 * 3600 * 1000
    mon.beat("liquidation_okx", stale)
    alerts = mon.check(now_ms=now)
    assert alerts and "quiet for" in alerts[0]

    mon.beat("liquidation_okx", now)
    assert mon.check(now_ms=now) == []
    assert mon.snapshot()["liquidation_okx"]["degraded"] is False


def test_feed_silence_contracts_exclude_blocked_binance_feeds() -> None:
    """Not-contracted feeds (binance_perp without LeadLag, fstream-blocked
    liquidation_binance) must not appear in the silence contract, so the
    dashboard ``degraded`` state only reflects feeds that deliver here."""
    from src.core.engine import feed_silence_contracts
    from src.utils.config import Config

    cfg = Config({
        "market_data": {"feed_silence": {"enabled": True}},
        "strategy": {"lead_lag": {"enabled": False, "auto_enable": False}},
    })
    feeds = feed_silence_contracts(cfg)
    assert "binance_perp" not in feeds
    assert "liquidation_binance" not in feeds
    # Always-contracted feeds stay contracted
    for name in ("liquidation_okx", "liquidation_bybit", "funding_cex",
                 "funding_hl", "taker_split"):
        assert name in feeds, name


def test_feed_silence_contracts_lead_lag_contracts_binance_perp() -> None:
    from src.core.engine import feed_silence_contracts
    from src.utils.config import Config

    cfg = Config({
        "market_data": {},
        "strategy": {"lead_lag": {"enabled": True, "auto_enable": False}},
    })
    feeds = feed_silence_contracts(cfg)
    assert "binance_perp" in feeds


def test_feed_silence_monitor_drops_uncontracted_defaults() -> None:
    """FeedSilenceMonitor registers class-level default feeds even when
    omitted from ``feeds`` — the engine drops non-contracted ones, so
    ``snapshot()``/``any_degraded`` only reflect feeds that deliver here."""
    from src.core.engine import feed_silence_contracts
    from src.data.market_data_health import FeedSilenceMonitor
    from src.utils.config import Config

    cfg = Config({
        "market_data": {"feed_silence": {"enabled": True}},
        "strategy": {"lead_lag": {"enabled": False, "auto_enable": False}},
    })
    contracts = feed_silence_contracts(cfg)
    mon = FeedSilenceMonitor(feeds=contracts)
    # Same drop step the TradingEngine applies after construction.
    for fname in list(mon._enabled_feeds):
        if fname not in contracts:
            mon.disable_feed(fname)
    snap = mon.snapshot()
    assert "binance_perp" not in snap
    assert "liquidation_binance" not in snap
    assert "liquidation_okx" in snap
    assert "funding_cex" in snap


def test_feed_silence_contracts_liquidation_binance_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.engine import feed_silence_contracts
    from src.utils.config import Config

    monkeypatch.setenv("LIQUIDATION_BINANCE_CONTRACTED", "true")
    cfg = Config({
        "market_data": {"feed_silence": {"enabled": True}},
        "strategy": {"lead_lag": {"enabled": False, "auto_enable": False}},
    })
    feeds = feed_silence_contracts(cfg)
    assert "liquidation_binance" in feeds


def test_feed_silence_never_seen_waits_for_process_uptime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-seen alerts must use age since monitor start, not boot monotonic.

    On Windows, time.monotonic() is often ~system uptime (days/years). Using it
    raw caused every restart to Telegram FEED SILENT within seconds.
    """
    clock = {"t": 1_000_000.0}

    def fake_mono() -> float:
        return clock["t"]

    monkeypatch.setattr(
        "src.data.market_data_health.time.monotonic", fake_mono
    )
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"binance_perp": 3600.0, "liquidation_binance": 6 * 3600.0},
    )
    # Constructor merges into defaults — isolate the two feeds under test.
    for name in list(mon._enabled_feeds):
        if name not in ("binance_perp", "liquidation_binance"):
            mon.disable_feed(name)
    # Immediate check — wall mono is huge vs threshold, but uptime is ~0
    assert mon.check() == []
    assert not mon.any_degraded

    # Just under 1h process uptime — still quiet for 1h feed
    clock["t"] = 1_000_000.0 + 3599.0
    assert mon.check() == []

    # Cross 1h — only binance_perp (not 6h binance liq)
    clock["t"] = 1_000_000.0 + 3601.0
    alerts = mon.check()
    assert len(alerts) == 1
    assert "binance_perp" in alerts[0]
    assert "never produced" in alerts[0]
    assert mon.snapshot()["binance_perp"]["degraded"] is True
    assert mon.snapshot()["liquidation_binance"]["degraded"] is False


def test_feed_silence_early_warning_at_50_pct_before_degrade() -> None:
    """A contracted feed crossing 50% of max_silence alerts once (early),
    before it degrades; beat() resets the fire-once flag."""
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    mon.beat("liquidation_okx", timestamp_ms=1_000_000)

    # 40% of 1h — no early warning yet
    assert mon.check_early_warnings(now_ms=1_000_000 + int(0.4 * 3600_000)) == []
    assert mon.snapshot()["liquidation_okx"]["degraded"] is False

    # 50% — early warning fires once
    warns = mon.check_early_warnings(now_ms=1_000_000 + int(0.5 * 3600_000))
    assert len(warns) == 1
    assert "FEED QUIET (early)" in warns[0]
    assert "liquidation_okx" in warns[0]
    assert "50%" in warns[0]

    # Fire-once: same age, no second warning
    assert mon.check_early_warnings(now_ms=1_000_000 + int(0.6 * 3600_000)) == []

    # beat() resets the flag — a new silence episode warns again
    mon.beat("liquidation_okx", timestamp_ms=1_000_000 + int(0.6 * 3600_000))
    warns2 = mon.check_early_warnings(now_ms=1_000_000 + int(1.1 * 3600_000))
    assert len(warns2) == 1


def test_feed_silence_imminent_warning_at_90_pct() -> None:
    """A contracted feed crossing 90% of max_silence fires a second-level
    imminent warning (degrade imminent), independent of the 50% fire-once
    flag; snapshot reports the escalation level."""
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    mon.beat("liquidation_okx", timestamp_ms=1_000_000)

    # 50% — early warning fires; level = early
    warns = mon.check_early_warnings(now_ms=1_000_000 + int(0.5 * 3600_000))
    assert len(warns) == 1
    assert "FEED QUIET (early)" in warns[0]
    snap = mon.snapshot()["liquidation_okx"]
    assert snap["warn_level"] == "early"
    assert snap["warned_50_pct"] is True
    assert snap["warned_90_pct"] is False

    # 70% — neither level fires (fire-once at 50% already consumed)
    assert mon.check_early_warnings(now_ms=1_000_000 + int(0.7 * 3600_000)) == []

    # 90% — imminent warning fires independently, escalate to 90%
    warns2 = mon.check_early_warnings(now_ms=1_000_000 + int(0.9 * 3600_000))
    assert len(warns2) == 1
    assert "FEED QUIET (imminent)" in warns2[0]
    assert "90%" in warns2[0]
    snap = mon.snapshot()["liquidation_okx"]
    assert snap["warn_level"] == "imminent"
    assert snap["warned_50_pct"] is True
    assert snap["warned_90_pct"] is True

    # Fire-once at imminent: same age, no second warning
    assert mon.check_early_warnings(now_ms=1_000_000 + int(0.95 * 3600_000)) == []

    # Not degraded yet (threshold is 1h)
    assert mon.snapshot()["liquidation_okx"]["degraded"] is False

    # beat() resets both flags — a new silence episode re-warns in order:
    # early first (50%), then imminent (90%)
    beat_at = 1_000_000 + int(0.95 * 3600_000)
    mon.beat("liquidation_okx", timestamp_ms=beat_at)
    warns3 = mon.check_early_warnings(now_ms=beat_at + int(0.6 * 3600_000))
    assert len(warns3) == 1 and "FEED QUIET (early)" in warns3[0]
    warns4 = mon.check_early_warnings(now_ms=beat_at + int(0.9 * 3600_000))
    assert len(warns4) == 1 and "FEED QUIET (imminent)" in warns4[0]


def test_feed_silence_early_warning_skips_degraded() -> None:
    """Once a feed is degraded, check_early_warnings stays quiet (check() owns
    the degrade alert) and does not double-alert."""
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_bybit": 3600.0},
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_bybit":
            mon.disable_feed(name)
    mon.beat("liquidation_bybit", timestamp_ms=1_000_000)
    now = 1_000_000 + int(1.5 * 3600_000)  # 1.5h > 1h threshold
    deg_alerts = mon.check(now_ms=now)
    assert deg_alerts and mon.snapshot()["liquidation_bybit"]["degraded"] is True
    assert mon.check_early_warnings(now_ms=now) == []


def test_feed_silence_early_warning_never_seen_uses_uptime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A feed that never produced an event warns at 50% of threshold from
    monitor start (not wall-clock monotonic, which is huge on Windows)."""
    clock = {"t": 1_000_000.0}

    def fake_mono() -> float:
        return clock["t"]

    monkeypatch.setattr(
        "src.data.market_data_health.time.monotonic", fake_mono
    )
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"funding_hl": 3600.0},
    )
    for name in list(mon._enabled_feeds):
        if name != "funding_hl":
            mon.disable_feed(name)

    assert mon.check_early_warnings() == []
    clock["t"] = 1_000_000.0 + 1801.0  # > 50% of 1h, < 1h
    warns = mon.check_early_warnings()
    assert len(warns) == 1
    assert "never produced" in warns[0]
    assert mon.snapshot()["funding_hl"]["degraded"] is False  # not degraded yet


def test_feed_silence_cadence_tracks_gaps_and_fires_above_p99() -> None:
    """beat() records inter-event gaps; check_cadence fires when the current
    gap exceeds the historical p99 — catching subtle thinning before the 6h
    silence threshold."""
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 6 * 3600.0},
        cadence_min_samples=50,
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)

    # Build history: 60 beats, mostly 1-minute gaps.
    t0 = 1_000_000
    mon.beat("liquidation_okx", timestamp_ms=t0)
    t = t0
    for i in range(1, 60):
        gap_ms = 60_000  # 1 min
        if i % 10 == 0:
            gap_ms = 5 * 60_000  # occasional 5-min gap
        t += gap_ms
        mon.beat("liquidation_okx", timestamp_ms=t)
    snap = mon.snapshot()["liquidation_okx"]
    assert snap["cadence_samples"] == 59
    assert snap["cadence_p99_sec"] is not None
    assert snap["cadence_p95_sec"] is not None
    # p95 <= p99 always
    assert snap["cadence_p95_sec"] <= snap["cadence_p99_sec"]

    # Current gap of 10 min (> p99 of ~5 min) fires the cadence alert.
    now = t + 10 * 60_000
    alerts = mon.check_cadence(now_ms=now)
    assert len(alerts) == 1
    assert "FEED CADENCE" in alerts[0]
    assert "liquidation_okx" in alerts[0]
    assert "p99" in alerts[0]

    # Fire-once: same episode, no second alert.
    assert mon.check_cadence(now_ms=now + 60_000) == []
    assert mon.snapshot()["liquidation_okx"]["warned_cadence"] is True
    # Not degraded — the 6h threshold is far away.
    assert mon.snapshot()["liquidation_okx"]["degraded"] is False

    # beat() resets the fire-once flag; a new normal gap clears the alert.
    mon.beat("liquidation_okx", timestamp_ms=now + 60_000)
    assert mon.snapshot()["liquidation_okx"]["warned_cadence"] is False
    # Short gap now — no alert.
    assert mon.check_cadence(now_ms=now + 120_000) == []


def test_feed_silence_cadence_quiet_without_history() -> None:
    """A feed with too few recorded gaps never fires the cadence alert — a
    cold-start feed has no baseline to compare against."""
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 6 * 3600.0},
        cadence_min_samples=50,
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    t0 = 1_000_000
    mon.beat("liquidation_okx", timestamp_ms=t0)
    for i in range(1, 5):  # only 4 gaps << 50 min_samples
        mon.beat("liquidation_okx", timestamp_ms=t0 + i * 60_000)
    now = t0 + 4 * 60_000 + 10 * 60_000
    assert mon.check_cadence(now_ms=now) == []
    assert mon.snapshot()["liquidation_okx"]["cadence_p99_sec"] is None


def test_feed_silence_cadence_skips_degraded_and_never_seen() -> None:
    """Degraded feeds and never-seen feeds are skipped by check_cadence."""
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 6 * 3600.0, "funding_hl": 3600.0},
        cadence_min_samples=3,
    )
    for name in list(mon._enabled_feeds):
        if name not in ("liquidation_okx", "funding_hl"):
            mon.disable_feed(name)
    t0 = 1_000_000
    # funding_hl never seen — skipped.
    # liquidation_okx builds 5 gaps of 1 min then goes quiet.
    mon.beat("liquidation_okx", timestamp_ms=t0)
    t = t0
    for i in range(1, 6):
        t += 60_000
        mon.beat("liquidation_okx", timestamp_ms=t)
    now = t + 10 * 60_000  # 10 min > p99(~1m)
    alerts = mon.check_cadence(now_ms=now)
    assert len(alerts) == 1
    assert "liquidation_okx" in alerts[0]
    assert not any("funding_hl" in a for a in alerts)
    # Degrade it, then cadence goes quiet.
    degrade_now = t + 6 * 3600 * 1000 + 1  # 6h after the last beat
    mon.check(now_ms=degrade_now)
    assert mon.snapshot()["liquidation_okx"]["degraded"] is True
    assert mon.check_cadence(now_ms=degrade_now + 1) == []


def test_feed_silence_warn_fraction_constructor_override() -> None:
    """warn_fraction set at construction moves the early-warning threshold.

    Default 0.5 fires early at 50% of threshold; a constructor override of
    0.2 fires at 20% (earlier, more conservative).
    """
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
        warn_fraction=0.2,
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    mon.beat("liquidation_okx", timestamp_ms=1_000_000)

    # 10% of 1h — below the 20% override: no warning
    assert mon.check_early_warnings(now_ms=1_000_000 + int(0.1 * 3600_000)) == []
    # 30% of 1h — past 20%: early warning fires
    warns = mon.check_early_warnings(now_ms=1_000_000 + int(0.3 * 3600_000))
    assert len(warns) == 1
    assert "FEED QUIET (early)" in warns[0]
    assert "20%" in warns[0]
    # the effective warn_fraction is exposed in the snapshot
    assert mon.snapshot()["liquidation_okx"]["warn_fraction"] == 0.2


def test_feed_silence_warn_fraction_default_still_50() -> None:
    """Default construction keeps the 50% threshold (message reflects it)."""
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    mon.beat("liquidation_okx", timestamp_ms=1_000_000)
    warns = mon.check_early_warnings(now_ms=1_000_000 + int(0.5 * 3600_000))
    assert len(warns) == 1
    assert "50%" in warns[0]
    assert mon.snapshot()["liquidation_okx"]["warn_fraction"] == 0.5


def test_feed_silence_warn_fraction_env_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine reads FEED_SILENCE_WARN_FRACTION and clamps it into (0,1)."""
    from src.core.engine import feed_silence_warn_fraction

    assert feed_silence_warn_fraction() == 0.5  # no env

    monkeypatch.setenv("FEED_SILENCE_WARN_FRACTION", "0.7")
    assert feed_silence_warn_fraction() == 0.7

    monkeypatch.setenv("FEED_SILENCE_WARN_FRACTION", "0.01")  # below floor
    assert feed_silence_warn_fraction() == 0.05

    monkeypatch.setenv("FEED_SILENCE_WARN_FRACTION", "0.99")  # above imminent
    assert feed_silence_warn_fraction() == 0.95

    monkeypatch.setenv("FEED_SILENCE_WARN_FRACTION", "not-a-float")
    assert feed_silence_warn_fraction() == 0.5


def test_feed_silence_imminent_fraction_constructor_override() -> None:
    """imminent_fraction set at construction moves the 90% checkpoint."""
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
        imminent_fraction=0.7,
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    mon.beat("liquidation_okx", timestamp_ms=1_000_000)

    # 60% of 1h — past early (50%) but below the 70% override: no imminent
    warns = mon.check_early_warnings(now_ms=1_000_000 + int(0.6 * 3600_000))
    assert len(warns) == 1
    assert "FEED QUIET (early)" in warns[0]
    assert "(imminent)" not in warns[0]
    # 80% of 1h — past 70%: imminent fires (early already consumed)
    warns = mon.check_early_warnings(now_ms=1_000_000 + int(0.8 * 3600_000))
    assert len(warns) == 1
    assert "FEED QUIET (imminent)" in warns[0]
    assert "70%" in warns[0]
    assert mon.snapshot()["liquidation_okx"]["imminent_fraction"] == 0.7


def test_feed_silence_imminent_fraction_default_still_90() -> None:
    """Default construction keeps the 90% checkpoint."""
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    assert mon.snapshot()["liquidation_okx"]["imminent_fraction"] == 0.9


def test_feed_silence_imminent_fraction_env_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine reads FEED_SILENCE_IMMINENT_FRACTION and clamps it to
    (0.5, 1.0) — hash-neutral (no BOT_ prefix), like the warn fraction."""
    from src.core.engine import feed_silence_imminent_fraction

    assert feed_silence_imminent_fraction() == 0.9  # no env

    monkeypatch.setenv("FEED_SILENCE_IMMINENT_FRACTION", "0.8")
    assert feed_silence_imminent_fraction() == 0.8

    monkeypatch.setenv("FEED_SILENCE_IMMINENT_FRACTION", "0.3")  # below floor
    assert feed_silence_imminent_fraction() == 0.5

    monkeypatch.setenv("FEED_SILENCE_IMMINENT_FRACTION", "1.0")  # valid bound
    assert feed_silence_imminent_fraction() == 1.0

    monkeypatch.setenv("FEED_SILENCE_IMMINENT_FRACTION", "1.5")  # out of (0,1]
    assert feed_silence_imminent_fraction() == 0.9  # rejected -> default

    monkeypatch.setenv("FEED_SILENCE_IMMINENT_FRACTION", "not-a-float")
    assert feed_silence_imminent_fraction() == 0.9


def test_feed_silence_cadence_env_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FEED_CADENCE_MIN_SAMPLES / FEED_CADENCE_GAP_HISTORY tune the cadence
    detector hash-neutral (no BOT_ prefix); rejected values -> defaults."""
    from src.core.engine import (
        feed_silence_cadence_gap_history,
        feed_silence_cadence_min_samples,
    )

    assert feed_silence_cadence_min_samples() == 100  # no env
    assert feed_silence_cadence_gap_history() == 4000  # no env

    monkeypatch.setenv("FEED_CADENCE_MIN_SAMPLES", "50")
    assert feed_silence_cadence_min_samples() == 50
    monkeypatch.setenv("FEED_CADENCE_MIN_SAMPLES", "1")  # valid bound
    assert feed_silence_cadence_min_samples() == 1
    monkeypatch.setenv("FEED_CADENCE_MIN_SAMPLES", "0")  # not positive
    assert feed_silence_cadence_min_samples() == 100
    monkeypatch.setenv("FEED_CADENCE_MIN_SAMPLES", "-5")
    assert feed_silence_cadence_min_samples() == 100
    monkeypatch.setenv("FEED_CADENCE_MIN_SAMPLES", "abc")
    assert feed_silence_cadence_min_samples() == 100

    monkeypatch.setenv("FEED_CADENCE_GAP_HISTORY", "8000")
    assert feed_silence_cadence_gap_history() == 8000
    monkeypatch.setenv("FEED_CADENCE_GAP_HISTORY", "0")
    assert feed_silence_cadence_gap_history() == 4000
    monkeypatch.setenv("FEED_CADENCE_GAP_HISTORY", "-1")
    assert feed_silence_cadence_gap_history() == 4000
    monkeypatch.setenv("FEED_CADENCE_GAP_HISTORY", "abc")
    assert feed_silence_cadence_gap_history() == 4000


def test_feed_silence_cadence_snapshot_exposes_effective_config() -> None:
    """The snapshot carries the effective min_samples (the learning gate) and
    the constructor keeps gap history at least as deep as min_samples so the
    percentile can always be computed."""
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
        cadence_min_samples=50,
        cadence_gap_history=10,  # below min_samples -> clamped up
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    assert mon._cadence_gap_history == 50
    assert mon.snapshot()["liquidation_okx"]["cadence_min_samples"] == 50

    mon2 = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
    )
    for name in list(mon2._enabled_feeds):
        if name != "liquidation_okx":
            mon2.disable_feed(name)
    assert mon2.snapshot()["liquidation_okx"]["cadence_min_samples"] == 100


def test_feed_silence_cadence_snapshot_distribution() -> None:
    """The snapshot exposes the real gap distribution (p50/p95/p99) and where
    the CURRENT gap sits in it (percentile rank, no learning gate)."""
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
        cadence_min_samples=5,
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    base = 1_000_000
    mon.beat("liquidation_okx", timestamp_ms=base)
    for gap in (30, 60, 90, 120, 150):  # gaps: 30..150s
        base += gap * 1000
        mon.beat("liquidation_okx", timestamp_ms=base)
    now = base + 60_000  # age = 60s
    snap = mon.snapshot(now_ms=now)["liquidation_okx"]
    # nearest-rank over [30,60,90,120,150]: p50 idx=int(2.5)=2 -> 90
    assert snap["cadence_p50_sec"] == 90.0
    # p95/p99: idx=int(4.75)=4 -> 150
    assert snap["cadence_p95_sec"] == 150.0
    assert snap["cadence_p99_sec"] == 150.0
    assert snap["cadence_samples"] == 5
    # current 60s: gaps <= 60 are {30,60} -> 2/5 = 40%
    assert snap["cadence_pct_current"] == 40.0

    # before min_samples: percentiles are None (still learning) but the
    # descriptive percentile of the current gap works regardless
    mon2 = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
        cadence_min_samples=100,
    )
    for name in list(mon2._enabled_feeds):
        if name != "liquidation_okx":
            mon2.disable_feed(name)
    b2 = 1_000_000
    mon2.beat("liquidation_okx", timestamp_ms=b2)
    mon2.beat("liquidation_okx", timestamp_ms=b2 + 30_000)
    snap2 = mon2.snapshot(now_ms=b2 + 60_000)["liquidation_okx"]
    assert snap2["cadence_p50_sec"] is None  # still learning
    assert snap2["cadence_pct_current"] == 100.0  # age 30 >= the one gap

    # never-seen feed: no distribution at all
    mon3 = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
    )
    for name in list(mon3._enabled_feeds):
        if name != "liquidation_okx":
            mon3.disable_feed(name)
    snap3 = mon3.snapshot(now_ms=1_000_000)["liquidation_okx"]
    assert snap3["cadence_pct_current"] is None
    assert snap3["cadence_p50_sec"] is None


def test_feed_silence_on_alert_records_every_emission() -> None:
    """The on_alert sink receives (feed, alert_type, fired_ms, message) for
    every emitted early/imminent/degraded alert — the audit trail of WHEN
    each level fired, exactly once per episode (fire-once)."""
    recorded: list = []
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=7200.0,  # degraded re-alert gated by a 2h cooldown
        feeds={"liquidation_okx": 3600.0},
        on_alert=lambda f, t, ms, m: recorded.append((f, t, ms, m)),
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    mon.beat("liquidation_okx", timestamp_ms=1_000_000)

    # 55% -> early fires and is recorded; the state persists WHEN it fired
    fired_early = 1_000_000 + int(0.55 * 3600_000)
    mon.check_early_warnings(now_ms=fired_early)
    assert recorded[-1][:3] == ("liquidation_okx", "early", fired_early)
    snap = mon.snapshot()["liquidation_okx"]
    assert snap["warned_50_pct"] is True
    assert snap["warned_50_at_ms"] == fired_early
    assert snap["warned_90_at_ms"] is None
    n = len(recorded)
    # same episode continuing -> no second early emission
    mon.check_early_warnings(now_ms=1_000_000 + int(0.7 * 3600_000))
    assert len(recorded) == n

    # 95% -> imminent fires and is recorded; WHEN is persisted too
    fired_imminent = 1_000_000 + int(0.95 * 3600_000)
    mon.check_early_warnings(now_ms=fired_imminent)
    assert recorded[-1][:3] == ("liquidation_okx", "imminent", fired_imminent)
    snap = mon.snapshot()["liquidation_okx"]
    assert snap["warned_90_pct"] is True
    assert snap["warned_90_at_ms"] == fired_imminent
    # the early timestamp survives (both fired in this episode)
    assert snap["warned_50_at_ms"] == fired_early
    n = len(recorded)
    mon.check_early_warnings(now_ms=1_000_000 + int(0.99 * 3600_000))
    assert len(recorded) == n

    # 110% -> degraded (FEED SILENT) fires and is recorded
    mon.check(now_ms=1_000_000 + int(1.1 * 3600_000))
    assert recorded[-1][:3] == ("liquidation_okx", "degraded", 1_000_000 + int(1.1 * 3600_000))
    assert "FEED SILENT" in recorded[-1][3]
    assert len([r for r in recorded if r[1] == "degraded"]) == 1

    # fire-once per episode: repeat check at a later age records nothing new
    mon.check(now_ms=1_000_000 + int(2.0 * 3600_000))
    assert len([r for r in recorded if r[1] == "degraded"]) == 1

    # beat() resets the episode: flags AND the persisted timestamps
    mon.beat("liquidation_okx", timestamp_ms=2_000_000)
    snap = mon.snapshot()["liquidation_okx"]
    assert snap["warned_50_pct"] is False
    assert snap["warned_50_at_ms"] is None
    assert snap["warned_90_pct"] is False
    assert snap["warned_90_at_ms"] is None


def test_feed_silence_daily_episode_counters() -> None:
    """Daily counters count EPISODES (fire-once re-arms on beat), not checks,
    and roll over at the UTC day boundary — "episódios hoje" is a day."""
    from src.data.market_data_health import FeedSilenceMonitor

    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    day_a = 1_752_000_000_000  # any UTC day
    mon.beat("liquidation_okx", timestamp_ms=day_a)

    # episode 1: early at 55%, then imminent at 95% — same episode
    mon.check_early_warnings(now_ms=day_a + int(0.55 * 3600_000))
    assert mon.snapshot(now_ms=day_a)["liquidation_okx"]["early_count_today"] == 1
    # same episode continuing -> fire-once, no re-increment
    mon.check_early_warnings(now_ms=day_a + int(0.7 * 3600_000))
    mon.check_early_warnings(now_ms=day_a + int(0.95 * 3600_000))
    snap = mon.snapshot(now_ms=day_a)
    assert snap["liquidation_okx"]["early_count_today"] == 1
    assert snap["liquidation_okx"]["imminent_count_today"] == 1

    # beat re-arms -> a NEW episode on the same day counts again
    mon.beat("liquidation_okx", timestamp_ms=day_a + int(1.5 * 3600_000))
    mon.check_early_warnings(now_ms=day_a + int(2.05 * 3600_000))  # 55% again
    snap = mon.snapshot(now_ms=day_a + int(2.05 * 3600_000))
    assert snap["liquidation_okx"]["early_count_today"] == 2
    assert snap["liquidation_okx"]["imminent_count_today"] == 1

    # UTC day flips -> counters reset; new episodes count from zero
    day_b = day_a + 86_400_000
    snap = mon.snapshot(now_ms=day_b)
    assert snap["liquidation_okx"]["early_count_today"] == 0
    assert snap["liquidation_okx"]["imminent_count_today"] == 0
    mon.beat("liquidation_okx", timestamp_ms=day_b)
    mon.check_early_warnings(now_ms=day_b + int(0.95 * 3600_000))  # straight to imminent
    snap = mon.snapshot(now_ms=day_b + int(0.95 * 3600_000))
    assert snap["liquidation_okx"]["early_count_today"] == 1
    assert snap["liquidation_okx"]["imminent_count_today"] == 1


def test_feed_silence_on_alert_fires_cadence() -> None:
    """The cadence alert (gap > historical p99) is also recorded."""
    recorded: list = []
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
        cadence_min_samples=3,
        on_alert=lambda f, t, ms, m: recorded.append((f, t, ms, m)),
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    t = 1_000_000
    mon.beat("liquidation_okx", timestamp_ms=t)
    for _ in range(6):
        t += 60_000
        mon.beat("liquidation_okx", timestamp_ms=t)
    mon.check_cadence(now_ms=t + 10 * 60_000)
    assert len(recorded) == 1
    assert recorded[0][0] == "liquidation_okx"
    assert recorded[0][1] == "cadence"
    assert "FEED CADENCE" in recorded[0][3]


def test_feed_silence_on_alert_quiet_when_nothing_fires() -> None:
    """No emissions -> no callback calls."""
    recorded: list = []
    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
        on_alert=lambda f, t, ms, m: recorded.append((f, t, ms, m)),
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    mon.beat("liquidation_okx", timestamp_ms=1_000_000)
    mon.check(now_ms=1_000_000 + int(0.3 * 3600_000))
    mon.check_early_warnings(now_ms=1_000_000 + int(0.3 * 3600_000))
    assert recorded == []


def test_feed_silence_on_alert_broken_sink_does_not_break_flow() -> None:
    """A failing recorder must never break the monitor's own alert flow."""
    def boom(*a, **k):
        raise RuntimeError("db down")

    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": 3600.0},
        on_alert=boom,
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    mon.beat("liquidation_okx", timestamp_ms=1_000_000)
    warns = mon.check_early_warnings(now_ms=1_000_000 + int(0.95 * 3600_000))
    assert len(warns) == 2  # early + imminent still returned despite the sink
