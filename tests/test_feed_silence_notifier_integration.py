"""Integration-offline: engine wires feed-silence layers -> notifier.

Early (50%) and cadence (p99) stay in the monitor state (dashboard table
+ logs) and do **not** page Telegram/Discord. Imminent (90%) — the last
checkpoint before the outage — pages at error severity, exactly once per
silence episode (fire-once ``warned_90_pct``, reset on ``beat()``). A real
outage — ``check()`` crossing 100% → ``FEED SILENT`` — sends the final
alert. Builds a bare TradingEngine (``__new__``, no ``__init__``) with a
real ``FeedSilenceMonitor`` and a fake notifier.
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


def _messages(notifier) -> list:
    return [msg for msg, _ in notifier.alerts]


def test_early_is_log_only_not_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crossing 50% updates monitor state but does not page Telegram."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        engine, mon, notifier = _bare_engine(clock, monkeypatch)

        episode_start = int(clock["t"] * 1000)
        mon.beat("liquidation_okx", timestamp_ms=episode_start)

        clock["t"] += 0.4 * 3600.0
        await _refresh(engine)
        assert notifier.alerts == []
        assert mon.snapshot()["liquidation_okx"]["warn_level"] == "none"

        clock["t"] += 0.15 * 3600.0  # 55%
        await _refresh(engine)
        assert notifier.alerts == []
        assert mon.snapshot()["liquidation_okx"]["warn_level"] == "early"

    asyncio.run(scenario())


def test_imminent_pages_telegram_once_per_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At 90% the imminent alert pages the notifier at error severity, exactly
    once per silence episode (fire-once warned_90_pct, reset on beat)."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        engine, mon, notifier = _bare_engine(clock, monkeypatch)

        mon.beat("liquidation_okx", timestamp_ms=int(clock["t"] * 1000))
        clock["t"] += 0.95 * 3600.0  # 95%
        await _refresh(engine)
        assert mon.snapshot()["liquidation_okx"]["warn_level"] == "imminent"
        assert len(notifier.alerts) == 1
        msg, level = notifier.alerts[0]
        assert "FEED QUIET (imminent)" in msg
        assert level == "error"

        # the same episode continuing does not re-fire
        clock["t"] += 0.01 * 3600.0
        await _refresh(engine)
        assert len(notifier.alerts) == 1

        # a beat resets the fire-once flag -> a new episode pages again
        mon.beat("liquidation_okx", timestamp_ms=int(clock["t"] * 1000))
        clock["t"] += 0.95 * 3600.0
        await _refresh(engine)
        assert len(notifier.alerts) == 2
        assert "FEED QUIET (imminent)" in notifier.alerts[-1][0]

    asyncio.run(scenario())


def test_degraded_owns_the_telegram_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a feed degrades, FEED SILENT is the error sent — no early/cadence page."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        engine, mon, notifier = _bare_engine(clock, monkeypatch)

        mon.beat("liquidation_okx", timestamp_ms=int(clock["t"] * 1000))
        clock["t"] += 1.1 * 3600.0
        await _refresh(engine)

        assert mon.snapshot()["liquidation_okx"]["degraded"] is True
        assert any("FEED SILENT" in msg for msg in _messages(notifier))
        assert not any("FEED QUIET" in msg for msg in _messages(notifier))
        assert not any("FEED CADENCE" in msg for msg in _messages(notifier))

    asyncio.run(scenario())


def test_outage_episode_pages_imminent_then_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One outage episode escalates: imminent (90%) pages first, FEED SILENT
    (100%) owns the final alert — never the reverse order."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        engine, mon, notifier = _bare_engine(clock, monkeypatch)
        # production gates repeat FEED SILENT with a 1h cooldown
        mon._alert_cooldown_sec = 3600.0

        mon.beat("liquidation_okx", timestamp_ms=int(clock["t"] * 1000))
        clock["t"] += 0.95 * 3600.0  # 95% -> imminent
        await _refresh(engine)
        assert len(notifier.alerts) == 1
        assert "FEED QUIET (imminent)" in notifier.alerts[0][0]

        clock["t"] += 0.15 * 3600.0  # 110% -> degraded
        await _refresh(engine)
        assert len(notifier.alerts) == 2
        assert "FEED SILENT" in notifier.alerts[-1][0]

        # still degraded inside the cooldown: no further pages
        clock["t"] += 0.5 * 3600.0
        await _refresh(engine)
        assert len(notifier.alerts) == 2

    asyncio.run(scenario())


def test_feed_silent_respects_cooldown_before_realert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded feed re-alerts only after alert_cooldown_sec elapses — never
    on every refresh (the monitor gates repeats with time.monotonic())."""
    import time as _time

    async def scenario() -> None:
        clock = {"t": 1_000_000.0, "mono_offset": 0.0}
        engine, mon, notifier = _bare_engine(clock, monkeypatch)
        # production gating: 1h cooldown between FEED SILENT repeats.
        mon._alert_cooldown_sec = 3600.0
        # Cooldown compares time.monotonic(); wrap the REAL monotonic so the
        # asyncio event loop keeps ticking while the monitor sees an offset
        # we control (advancing it simulates an hour passing between checks).
        real_mono = _time.monotonic

        def fake_monotonic() -> float:
            return real_mono() + clock["mono_offset"]

        monkeypatch.setattr(
            "src.data.market_data_health.time.monotonic", fake_monotonic
        )

        mon.beat("liquidation_okx", timestamp_ms=int(clock["t"] * 1000))
        clock["t"] += 1.1 * 3600.0  # 110% -> degraded
        await _refresh(engine)
        assert sum("FEED SILENT" in m for m, _ in notifier.alerts) == 1

        # refresh again while still degraded, BEFORE the cooldown elapses:
        # the refresh itself must not re-page.
        clock["t"] += 0.5 * 3600.0
        await _refresh(engine)
        assert sum("FEED SILENT" in m for m, _ in notifier.alerts) == 1

        # refresh AFTER the cooldown elapses -> the re-alert fires (once).
        clock["mono_offset"] += 3601.0  # monotonic passes the 1h cooldown
        await _refresh(engine)
        assert sum("FEED SILENT" in m for m, _ in notifier.alerts) == 2

        # and again it is quiet until the next cooldown window
        clock["t"] += 0.25 * 3600.0
        await _refresh(engine)
        assert sum("FEED SILENT" in m for m, _ in notifier.alerts) == 2

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


def test_cadence_is_log_only_not_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gap exceeding historical p99 flags cadence on the monitor, no Telegram."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        engine, mon, notifier = _bare_engine(clock, monkeypatch)
        mon._cadence_min_samples = 3

        t = 1_000_000
        mon.beat("liquidation_okx", timestamp_ms=t)
        for _ in range(6):
            t += 60_000
            mon.beat("liquidation_okx", timestamp_ms=t)
        assert mon.snapshot()["liquidation_okx"]["cadence_samples"] == 6

        clock["t"] = (t + 10 * 60_000) / 1000.0
        await _refresh(engine)
        assert notifier.alerts == []
        assert mon.snapshot()["liquidation_okx"]["warned_cadence"] is True
        assert mon.snapshot()["liquidation_okx"]["degraded"] is False

        clock["t"] += 60.0
        await _refresh(engine)
        assert notifier.alerts == []

        t2 = int(clock["t"] * 1000)
        mon.beat("liquidation_okx", timestamp_ms=t2)
        assert mon.snapshot()["liquidation_okx"]["warned_cadence"] is False
        clock["t"] += 30.0
        await _refresh(engine)
        assert notifier.alerts == []

    asyncio.run(scenario())
