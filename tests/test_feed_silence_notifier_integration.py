"""Integration-offline: engine wires check_early_warnings -> notifier.

Builds a bare TradingEngine (``__new__``, no ``__init__``) with a real
``FeedSilenceMonitor`` and a fake notifier that records ``send_alert``
calls, then drives ``_refresh_market_data_health()`` through a
controllable clock to verify the **fire-once per episode** contract:

- crossing 50% of max_silence sends exactly one ``warning`` alert;
- a later refresh at the same episode sends nothing (fire-once);
- a ``beat()`` starts a new episode and the warning fires again.

The imminent (90%) level is also pinned: it escalates to ``error`` and is
independent of the early flag. ``_notify`` schedules the notifier call as
an asyncio task, so each scenario runs under ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

pytestmark = pytest.mark.integration_offline

MAX_SILENCE_SEC = 3600.0


class _FakeNotifier:
    """Records (message, level) pairs instead of sending Telegram/Discord."""

    def __init__(self) -> None:
        self.alerts: list = []

    async def send_alert(self, message: str, level: str = "info") -> None:
        self.alerts.append((message, level))


def _bare_engine(clock, monkeypatch: pytest.MonkeyPatch):
    """Bare TradingEngine with the attrs _refresh_market_data_health needs."""
    from src.core.engine import TradingEngine
    from src.data.market_data_health import (
        FeedSilenceMonitor,
        MarketDataHealthSummary,
        MarketDataHealthTracker,
    )

    monkeypatch.setattr("src.core.engine.time.time", lambda: clock["t"])
    monkeypatch.setattr("src.data.market_data_health.time.time", lambda: clock["t"])

    engine = TradingEngine.__new__(TradingEngine)
    engine._symbols = []
    engine._latest_agg_funding = {}
    engine._hl_predicted = {}
    engine._latest_ctx = {}
    engine._min_exchanges_green = 2
    engine._market_data_health = {}
    engine._health_tracker = MarketDataHealthTracker(window_sec=3600.0)
    engine._market_data_health_summary = MarketDataHealthSummary()
    engine._md_red_since = None
    engine._feed_health_evaluated = False
    engine._feed_health_ready = False
    engine._feed_silence_enabled = True

    mon = FeedSilenceMonitor(
        alert_cooldown_sec=0.0,
        feeds={"liquidation_okx": MAX_SILENCE_SEC},
    )
    for name in list(mon._enabled_feeds):
        if name != "liquidation_okx":
            mon.disable_feed(name)
    engine._feed_silence = mon

    notifier = _FakeNotifier()
    engine._notifier = notifier
    engine._notify_tasks = set()
    engine._notify_max_pending = 100
    engine._notify_concurrency = 4
    engine._notify_sema = None
    return engine, mon, notifier


async def _refresh(engine) -> None:
    """Call the engine's health refresh and let scheduled notify tasks run."""
    engine._refresh_market_data_health()
    await asyncio.sleep(0.01)


def _levels(notifier) -> list:
    return [level for _, level in notifier.alerts]


def _messages(notifier) -> list:
    return [msg for msg, _ in notifier.alerts]


def test_early_warning_sent_exactly_once_per_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crossing 50% of max_silence sends one warning alert; the same episode
    stays quiet on later refreshes; a beat starts a new episode that warns
    again."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        engine, mon, notifier = _bare_engine(clock, monkeypatch)

        # Episode 1: feed beats at t=0 of the episode.
        episode_start = int(clock["t"] * 1000)
        mon.beat("liquidation_okx", timestamp_ms=episode_start)

        # 40% of 1h — no warning yet.
        clock["t"] += 0.4 * 3600.0
        await _refresh(engine)
        assert notifier.alerts == []
        assert mon.snapshot()["liquidation_okx"]["warn_level"] == "none"

        # 55% of 1h — early warning fires exactly once.
        clock["t"] += 0.15 * 3600.0
        await _refresh(engine)
        assert len(notifier.alerts) == 1
        assert _levels(notifier) == ["warning"]
        assert "FEED QUIET (early)" in _messages(notifier)[0]
        assert "liquidation_okx" in _messages(notifier)[0]
        assert mon.snapshot()["liquidation_okx"]["warn_level"] == "early"

        # Same episode, later refresh — fire-once, nothing new.
        clock["t"] += 0.2 * 3600.0  # 75% of 1h
        await _refresh(engine)
        assert len(notifier.alerts) == 1

        # Episode 2: beat resets the fire-once flags.
        episode2_start = int(clock["t"] * 1000)
        mon.beat("liquidation_okx", timestamp_ms=episode2_start)
        assert mon.snapshot()["liquidation_okx"]["warn_level"] == "none"
        clock["t"] += 0.6 * 3600.0  # 60% of 1h in the new episode
        await _refresh(engine)
        assert len(notifier.alerts) == 2
        assert _levels(notifier) == ["warning", "warning"]
        assert "FEED QUIET (early)" in _messages(notifier)[1]

    asyncio.run(scenario())


def test_imminent_warning_escalates_to_error_once_per_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At 90% the imminent warning fires independently of the early one and
    escalates to error level — exactly once per episode."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        engine, mon, notifier = _bare_engine(clock, monkeypatch)

        mon.beat("liquidation_okx", timestamp_ms=int(clock["t"] * 1000))

        # 50% — early fires.
        clock["t"] += 0.5 * 3600.0
        await _refresh(engine)
        assert _levels(notifier) == ["warning"]

        # 95% — imminent fires once as error, in addition to the early alert.
        clock["t"] += 0.45 * 3600.0
        await _refresh(engine)
        assert len(notifier.alerts) == 2
        assert _levels(notifier) == ["warning", "error"]
        assert "FEED QUIET (imminent)" in _messages(notifier)[1]
        assert mon.snapshot()["liquidation_okx"]["warn_level"] == "imminent"

        # Same episode, still below degrade — nothing new.
        clock["t"] += 0.02 * 3600.0  # 97% of 1h, still < 100%
        await _refresh(engine)
        assert len(notifier.alerts) == 2

    asyncio.run(scenario())


def test_degraded_owns_final_alert_not_early_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a feed degrades, check_early_warnings goes quiet and the degrade
    alert is the error sent — no double-alert on the same episode."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        engine, mon, notifier = _bare_engine(clock, monkeypatch)

        mon.beat("liquidation_okx", timestamp_ms=int(clock["t"] * 1000))
        clock["t"] += 1.1 * 3600.0  # past the 1h threshold -> degraded
        await _refresh(engine)

        assert mon.snapshot()["liquidation_okx"]["degraded"] is True
        assert any("FEED SILENT" in msg for msg in _messages(notifier))
        # No FEED QUIET (early/imminent) — degraded feeds skip early warnings.
        assert not any("FEED QUIET" in msg for msg in _messages(notifier))

    asyncio.run(scenario())


def test_disabled_feed_silence_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With feed_silence disabled, the engine never consults the monitor."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        engine, mon, notifier = _bare_engine(clock, monkeypatch)
        engine._feed_silence_enabled = False

        mon.beat("liquidation_okx", timestamp_ms=int(clock["t"] * 1000))
        clock["t"] += 0.6 * 3600.0
        await _refresh(engine)
        assert notifier.alerts == []

    asyncio.run(scenario())


def test_cadence_alert_sent_once_per_episode_via_notifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gap exceeding the historical p99 sends one warning via the notifier,
    fire-once per episode; a beat starts a new episode."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        engine, mon, notifier = _bare_engine(clock, monkeypatch)
        mon._cadence_min_samples = 3

        # Build cadence history: 6 beats, 1-min gaps.
        t = 1_000_000
        mon.beat("liquidation_okx", timestamp_ms=t)
        for _ in range(6):
            t += 60_000
            mon.beat("liquidation_okx", timestamp_ms=t)
        assert mon.snapshot()["liquidation_okx"]["cadence_samples"] == 6

        # 10-min gap > p99 (~1 min) — cadence alert fires via the engine.
        clock["t"] = (t + 10 * 60_000) / 1000.0
        await _refresh(engine)
        assert len(notifier.alerts) == 1
        assert _levels(notifier) == ["warning"]
        assert "FEED CADENCE" in _messages(notifier)[0]
        assert "liquidation_okx" in _messages(notifier)[0]
        assert mon.snapshot()["liquidation_okx"]["warned_cadence"] is True
        # Not degraded — 6h threshold far away.
        assert mon.snapshot()["liquidation_okx"]["degraded"] is False

        # Same episode, later refresh — fire-once, nothing new.
        clock["t"] += 60.0
        await _refresh(engine)
        assert len(notifier.alerts) == 1

        # New episode: beat resets, short gap stays quiet.
        t2 = int(clock["t"] * 1000)
        mon.beat("liquidation_okx", timestamp_ms=t2)
        assert mon.snapshot()["liquidation_okx"]["warned_cadence"] is False
        clock["t"] += 30.0  # 30s < p99
        await _refresh(engine)
        assert len(notifier.alerts) == 1

    asyncio.run(scenario())
