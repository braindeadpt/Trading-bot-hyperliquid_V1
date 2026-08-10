"""Unit tests: ChecklistMeta ignores proxy liquidations; FeedSilenceMonitor alerts."""

from __future__ import annotations

import inspect

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
