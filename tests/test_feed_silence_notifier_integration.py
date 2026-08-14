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


def _bare_engine(
    clock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    warn_fraction: float = 0.5,
    on_alert=None,
):
    """Bare TradingEngine with the attrs _refresh_market_data_health needs.

    ``warn_fraction`` defaults to 0.5 for the existing tests; the env-driven
    test passes ``feed_silence_warn_fraction()`` (exactly what the engine's
    constructor does) to prove the env reaches the monitor. ``on_alert`` is
    forwarded to the monitor — the engine wires it to the research DB.
    """
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
        warn_fraction=warn_fraction,
        on_alert=on_alert,
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


def test_engine_forwards_feed_silent_to_on_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine's refresh forwards every FEED SILENT emission to the
    monitor's on_alert sink (the research-DB audit trail) — same path as the
    notifier page, so the recorded history matches what the operator saw."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        recorded: list = []
        engine, mon, _notifier = _bare_engine(
            clock, monkeypatch,
            on_alert=lambda f, t, ms, m: recorded.append((f, t, ms, m)),
        )
        mon._alert_cooldown_sec = 0.0  # degraded re-fires per refresh (test only)

        mon.beat("liquidation_okx", timestamp_ms=int(clock["t"] * 1000))
        clock["t"] += 1.1 * 3600.0  # 110% -> degraded
        await _refresh(engine)
        assert recorded and recorded[0][:2] == ("liquidation_okx", "degraded")
        assert "FEED SILENT" in recorded[0][3]
        assert recorded[0][2] == int(clock["t"] * 1000)  # fired_ms

        # each emission is appended — a repeat after cooldown is its own row
        n = len(recorded)
        clock["t"] += 1.1 * 3600.0
        await _refresh(engine)
        assert len(recorded) == n + 1
        assert recorded[-1][1] == "degraded"

    asyncio.run(scenario())


def test_warned_50_pct_true_in_same_refresh_as_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot flag warned_50_pct is True in the SAME refresh where the
    early warning is emitted — the Alerted column (reads the snapshot) never
    shows 'early' before the alert fires, nor hides it after."""

    async def scenario() -> None:
        clock = {"t": 1_000_000.0}
        recorded: list = []
        engine, mon, _notifier = _bare_engine(
            clock, monkeypatch,
            on_alert=lambda f, t, ms, m: recorded.append((f, t, ms, m)),
        )

        mon.beat("liquidation_okx", timestamp_ms=int(clock["t"] * 1000))
        clock["t"] += 0.40 * 3600.0  # 40% — below the 0.5 early level
        await _refresh(engine)
        assert mon.snapshot()["liquidation_okx"]["warned_50_pct"] is False
        assert not any(r[1] == "early" for r in recorded)

        # cross the early level in ONE refresh: the emission and the flag
        # must land atomically — the column reads the snapshot right after.
        clock["t"] += 0.15 * 3600.0  # 55%
        await _refresh(engine)
        snap = mon.snapshot()["liquidation_okx"]
        assert any(r[1] == "early" for r in recorded)  # the warning was emitted
        assert snap["warned_50_pct"] is True  # in the SAME refresh
        assert snap["warn_level"] == "early"

        # fire-once: a later refresh keeps the flag True (the column keeps
        # showing the episode's alert) but does NOT re-emit.
        n_early = len([r for r in recorded if r[1] == "early"])
        clock["t"] += 0.10 * 3600.0
        await _refresh(engine)
        assert mon.snapshot()["liquidation_okx"]["warned_50_pct"] is True
        assert len([r for r in recorded if r[1] == "early"]) == n_early

    asyncio.run(scenario())


def test_warn_fraction_env_moves_early_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FEED_SILENCE_WARN_FRACTION moves the early threshold through the
    engine's notifier pipeline: the SAME age flips from 'none' to 'early'
    when the env goes 0.5 -> 0.3, and the early level never pages — a more
    sensitive threshold must not spam the notifier."""
    from src.core.engine import feed_silence_warn_fraction

    async def scenario() -> None:
        # default env: 40% of the 1h threshold is below the 0.5 early level
        assert feed_silence_warn_fraction() == 0.5
        clock_a = {"t": 1_000_000.0}
        engine_a, mon_a, notif_a = _bare_engine(
            clock_a, monkeypatch,
            warn_fraction=feed_silence_warn_fraction(),
        )
        mon_a.beat("liquidation_okx", timestamp_ms=int(clock_a["t"] * 1000))
        clock_a["t"] += 0.40 * 3600.0  # 40%
        await _refresh(engine_a)
        assert mon_a.snapshot()["liquidation_okx"]["warn_fraction"] == 0.5
        assert mon_a.snapshot()["liquidation_okx"]["warn_level"] == "none"
        assert notif_a.alerts == []

        # env 0.3: the SAME 40% age is now 'early' — the threshold moved
        monkeypatch.setenv("FEED_SILENCE_WARN_FRACTION", "0.3")
        assert feed_silence_warn_fraction() == 0.3
        clock_b = {"t": 2_000_000.0}
        engine_b, mon_b, notif_b = _bare_engine(
            clock_b, monkeypatch,
            warn_fraction=feed_silence_warn_fraction(),
        )
        mon_b.beat("liquidation_okx", timestamp_ms=int(clock_b["t"] * 1000))
        clock_b["t"] += 0.40 * 3600.0  # 40%
        await _refresh(engine_b)
        assert mon_b.snapshot()["liquidation_okx"]["warn_fraction"] == 0.3
        assert mon_b.snapshot()["liquidation_okx"]["warn_level"] == "early"
        # early is log-only by contract: the moved threshold must NOT page
        assert notif_b.alerts == []

        # the paging level is env-agnostic: imminent still pages at 90%
        clock_b["t"] += 0.55 * 3600.0  # 95%
        await _refresh(engine_b)
        assert any(
            "FEED QUIET (imminent)" in m for m, _ in notif_b.alerts
        )

    asyncio.run(scenario())


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
