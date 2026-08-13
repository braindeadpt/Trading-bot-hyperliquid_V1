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

    monkeypatch.delenv("FEED_SILENCE_WARN_FRACTION", raising=False)
    assert feed_silence_warn_fraction() == 0.5
