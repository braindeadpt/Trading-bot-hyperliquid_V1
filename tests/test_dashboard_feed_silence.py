"""Tests for the dashboard Feed Silence panel (threshold vs age per feed).

The panel lives under System Health and shows, per contracted feed: age,
max-silence threshold, % of threshold (age / max) and degraded state — so
silences are *anticipated* before the 6h/1h/12h thresholds trip. The
endpoint (`/api/market_data_health`) already carries `feed_silence` from the
engine's `FeedSilenceMonitor.snapshot()`; these tests pin the payload shape
and that the template renders the columns.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEMPLATE_PATH = ROOT / "src" / "dashboard" / "templates" / "index.html"


class _SilenceStub:
    def __init__(self, entries):
        self._entries = entries
        self.any_degraded = any(e.get("degraded") for e in entries.values())

    def snapshot(self):
        return self._entries


class _HealthSummaryStub:
    def to_dict(self):
        return {"overall": "green", "symbols": {"BTC": {"symbol": "BTC", "status": "green"}}}


class _EngineStub:
    def __init__(self, silence, summary=None, health=None):
        self._feed_silence = silence
        self._market_data_health_summary = summary
        self._market_data_health = health or {}


class TestFeedSilencePayload:
    pytestmark = pytest.mark.integration_offline

    def setup_method(self) -> None:
        import src.dashboard.web as web

        self._web = web
        self._orig_engine = web._engine
        web._engine = None
        self._app, self._sio, _ = web.create_app({"mode": "paper"})
        self._client = self._app.test_client()

    def teardown_method(self) -> None:
        self._web._engine = self._orig_engine

    def test_no_engine_returns_empty_feeds(self) -> None:
        r = self._client.get("/api/market_data_health")
        assert r.status_code == 200
        d = r.get_json()
        assert d["feeds"] == []
        assert d["overall"] == "red"

    def test_payload_carries_feed_silence_snapshot(self) -> None:
        silence = _SilenceStub(
            {
                "funding_hl": {
                    "last_event_ms": 1_000,
                    "age_sec": 30.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                    "warned_50_pct": True,
                    "warned_50_at_ms": 1_752_000_000_000,
                    "warned_90_pct": False,
                    "warned_90_at_ms": None,
                    "early_count_today": 2,
                    "imminent_count_today": 1,
                    "warn_fraction": 0.3,
                    "imminent_fraction": 0.8,
                },
                "liquidation_okx": {
                    "last_event_ms": 1_000,
                    "age_sec": 5400.0,
                    "max_silence_sec": 21600.0,
                    "degraded": False,
                    "warned_50_pct": True,
                    "warned_90_pct": False,
                },
                "liquidation_bybit": {
                    "last_event_ms": 1_000,
                    "age_sec": 22000.0,
                    "max_silence_sec": 21600.0,
                    "degraded": True,
                    "warned_50_pct": True,
                    "warned_90_pct": True,
                },
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        r = self._client.get("/api/market_data_health")
        assert r.status_code == 200
        d = r.get_json()
        assert d["feed_silence_degraded"] is True
        # bybit is already degraded, okx only early-warned -> not "imminent"
        assert d["feed_silence_imminent"] is False
        fs = d["feed_silence"]
        assert fs["funding_hl"]["age_sec"] == 30.0
        assert fs["funding_hl"]["max_silence_sec"] == 3600.0
        assert fs["funding_hl"]["degraded"] is False
        # the effective early/imminent fractions flow through the snapshot
        assert fs["funding_hl"]["warn_fraction"] == 0.3
        assert fs["funding_hl"]["imminent_fraction"] == 0.8
        # when each alert fired also passes through (None until it fires)
        assert fs["funding_hl"]["warned_50_at_ms"] == 1_752_000_000_000
        assert fs["funding_hl"]["warned_90_at_ms"] is None
        # daily episode counters (UTC day) pass through per feed
        assert fs["funding_hl"]["early_count_today"] == 2
        assert fs["funding_hl"]["imminent_count_today"] == 1
        # fire-once flags pass through the snapshot (funding_hl already fired
        # early at 30% warn_fraction, hence its persisted timestamp)
        assert fs["funding_hl"]["warned_50_pct"] is True
        assert fs["liquidation_okx"]["warned_50_pct"] is True
        assert fs["liquidation_okx"]["warned_90_pct"] is False
        # bybit is past its 6h threshold -> degraded
        assert fs["liquidation_bybit"]["degraded"] is True
        assert fs["liquidation_bybit"]["warned_90_pct"] is True

    def test_all_healthy_no_degraded_flag(self) -> None:
        silence = _SilenceStub(
            {
                "funding_hl": {
                    "last_event_ms": 1_000,
                    "age_sec": 15.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                }
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        assert d["feed_silence_degraded"] is False
        assert d["feed_silence_imminent"] is False

    def test_imminent_flag_when_feed_past_90pct_not_degraded(self) -> None:
        """A feed at >=90% of its threshold (fire-once warned_90_pct set, still
        healthy) flips the global imminent flag that the header consumes."""
        silence = _SilenceStub(
            {
                "funding_hl": {
                    "last_event_ms": 1_000,
                    "age_sec": 30.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                    "warned_50_pct": True,
                    "warned_90_pct": True,
                }
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        assert d["feed_silence_degraded"] is False
        assert d["feed_silence_imminent"] is True

    def test_payload_carries_warn_level_per_feed(self) -> None:
        """Each feed's JSON carries its own warn_level (none/early/imminent/
        degraded) — the escalation level the panel's State column renders."""
        silence = _SilenceStub(
            {
                "funding_hl": {
                    "last_event_ms": 1_000,
                    "age_sec": 30.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                    "warned_50_pct": False,
                    "warned_90_pct": False,
                    "warn_level": "none",
                },
                "liquidation_okx": {
                    "last_event_ms": 1_000,
                    "age_sec": 1980.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                    "warned_50_pct": True,
                    "warned_90_pct": False,
                    "warn_level": "early",
                },
                "liquidation_bybit": {
                    "last_event_ms": 1_000,
                    "age_sec": 3564.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                    "warned_50_pct": True,
                    "warned_90_pct": True,
                    "warn_level": "imminent",
                },
                "funding_binance": {
                    "last_event_ms": 1_000,
                    "age_sec": 7200.0,
                    "max_silence_sec": 3600.0,
                    "degraded": True,
                    "warned_50_pct": True,
                    "warned_90_pct": True,
                    "warn_level": "degraded",
                },
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        fs = d["feed_silence"]
        assert fs["funding_hl"]["warn_level"] == "none"
        assert fs["liquidation_okx"]["warn_level"] == "early"
        assert fs["liquidation_bybit"]["warn_level"] == "imminent"
        assert fs["funding_binance"]["warn_level"] == "degraded"

    def test_payload_warn_level_from_real_monitor(self) -> None:
        """End-to-end: a real FeedSilenceMonitor wired to the endpoint — the
        escalation level computed by the monitor (early -> imminent) flows
        into the payload JSON, not just a stub pass-through."""
        from src.data.market_data_health import FeedSilenceMonitor

        mon = FeedSilenceMonitor(
            alert_cooldown_sec=0.0,
            feeds={"liquidation_okx": 3600.0},
        )
        for name in list(mon._enabled_feeds):
            if name != "liquidation_okx":
                mon.disable_feed(name)
        mon.beat("liquidation_okx", timestamp_ms=1_000_000)
        mon.check_early_warnings(now_ms=1_000_000 + int(0.5 * 3600_000))
        self._web._engine = _EngineStub(mon, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        assert d["feed_silence"]["liquidation_okx"]["warn_level"] == "early"
        # the real monitor's effective fractions are on the wire (defaults)
        assert d["feed_silence"]["liquidation_okx"]["warn_fraction"] == 0.5
        assert d["feed_silence"]["liquidation_okx"]["imminent_fraction"] == 0.9

        # escalate to 90% -> the same endpoint now reports imminent
        # (bust the endpoint TTL cache so the refresh re-snapshots the monitor)
        mon.check_early_warnings(now_ms=1_000_000 + int(0.9 * 3600_000))
        self._web._ttl_clear()
        d = self._client.get("/api/market_data_health").get_json()
        assert d["feed_silence"]["liquidation_okx"]["warn_level"] == "imminent"
        assert d["feed_silence"]["liquidation_okx"]["degraded"] is False

    def test_imminent_flag_false_when_feed_degraded(self) -> None:
        """Once the feed degrades, imminent yields to degraded (never both)."""
        silence = _SilenceStub(
            {
                "liquidation_okx": {
                    "last_event_ms": 1_000,
                    "age_sec": 22000.0,
                    "max_silence_sec": 21600.0,
                    "degraded": True,
                    "warned_50_pct": True,
                    "warned_90_pct": True,
                }
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        assert d["feed_silence_degraded"] is True
        assert d["feed_silence_imminent"] is False

    def test_engine_without_silence_monitor_omits_key(self) -> None:
        self._web._engine = _EngineStub(None, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        assert "feed_silence" not in d

    def test_payload_carries_sparkline_series(self, monkeypatch, tmp_path) -> None:
        """Sparkline data comes from the research DB feed_age_samples table."""
        import src.data.research_database as rdb_mod
        from src.data.research_database import ResearchDatabase

        rdb = ResearchDatabase(tmp_path / "research.db")
        now = int(time.time() * 1000)
        bucket = now - (now % 600_000)
        rdb.save_feed_age_samples(
            [
                ("funding_hl", bucket - 600_000, 20.0, 2),
                ("funding_hl", bucket, 55.0, 3),
            ]
        )

        def fake_research_db(*a, **k):
            return rdb

        monkeypatch.setattr(rdb_mod, "ResearchDatabase", fake_research_db)
        silence = _SilenceStub(
            {
                "funding_hl": {
                    "last_event_ms": 1_000,
                    "age_sec": 15.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                }
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        spark = d["feed_silence_spark"]["funding_hl"]
        assert len(spark) == 2
        assert spark[1] == [float(bucket), 55.0]

    def test_payload_carries_daily_max_age_series(self, monkeypatch, tmp_path) -> None:
        """The 14d column reads feed_age_history (daily max age per feed)."""
        import src.data.research_database as rdb_mod
        from src.data.research_database import ResearchDatabase

        rdb = ResearchDatabase(tmp_path / "research.db")
        now = int(time.time() * 1000)
        day = now - (now % 86_400_000)
        rdb.save_feed_age_history(
            [
                ("funding_hl", day - 2 * 86_400_000, 300.0, 4),
                ("funding_hl", day - 86_400_000, 1200.0, 5),
                ("funding_hl", day, 2700.0, 3),
            ]
        )

        def fake_research_db(*a, **k):
            return rdb

        monkeypatch.setattr(rdb_mod, "ResearchDatabase", fake_research_db)
        silence = _SilenceStub(
            {
                "funding_hl": {
                    "last_event_ms": 1_000,
                    "age_sec": 15.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                }
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        daily = d["feed_silence_daily"]["funding_hl"]
        assert len(daily) == 3
        assert [p[1] for p in daily] == [300.0, 1200.0, 2700.0]
        # ascending by UTC day
        assert daily[0][0] < daily[1][0] < daily[2][0]

    def test_payload_carries_creep_verdict_per_feed(self, monkeypatch, tmp_path) -> None:
        """A rising daily-max staircase (production rule) flags the feed in
        feed_silence_creep — the badge source."""
        import src.data.research_database as rdb_mod
        from src.data.research_database import ResearchDatabase

        rdb = ResearchDatabase(tmp_path / "research.db")
        now = int(time.time() * 1000)
        day = now - (now % 86_400_000)
        rdb.save_feed_age_history(
            [
                ("funding_hl", day - 4 * 86_400_000, 600.0, 5),
                ("funding_hl", day - 3 * 86_400_000, 900.0, 5),
                ("funding_hl", day - 2 * 86_400_000, 1200.0, 5),
                ("funding_hl", day - 86_400_000, 1600.0, 5),
                ("funding_hl", day, 2100.0, 5),
            ]
        )

        def fake_research_db(*a, **k):
            return rdb

        monkeypatch.setattr(rdb_mod, "ResearchDatabase", fake_research_db)
        silence = _SilenceStub(
            {
                "funding_hl": {
                    "last_event_ms": 1_000,
                    "age_sec": 15.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                }
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        cr = d["feed_silence_creep"]["funding_hl"]
        assert cr["creeping"] is True
        assert cr["days"] == 5
        assert cr["growth_frac"] == pytest.approx(1500 / 3600, abs=1e-4)
        assert cr["last_max_age_sec"] == 2100.0

    def test_creep_quiet_for_flat_feed(self, monkeypatch, tmp_path) -> None:
        """No staircase -> the feed is absent from feed_silence_creep (no badge)."""
        import src.data.research_database as rdb_mod
        from src.data.research_database import ResearchDatabase

        rdb = ResearchDatabase(tmp_path / "research.db")
        now = int(time.time() * 1000)
        day = now - (now % 86_400_000)
        rdb.save_feed_age_history(
            [
                ("funding_hl", day - 4 * 86_400_000, 600.0, 5),
                ("funding_hl", day - 3 * 86_400_000, 610.0, 5),
                ("funding_hl", day - 2 * 86_400_000, 590.0, 5),
                ("funding_hl", day - 86_400_000, 620.0, 5),
                ("funding_hl", day, 600.0, 5),
            ]
        )

        def fake_research_db(*a, **k):
            return rdb

        monkeypatch.setattr(rdb_mod, "ResearchDatabase", fake_research_db)
        silence = _SilenceStub(
            {
                "funding_hl": {
                    "last_event_ms": 1_000,
                    "age_sec": 15.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                }
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        assert d["feed_silence_creep"] == {}

    def test_creep_best_effort_on_missing_db(self, monkeypatch) -> None:
        """A broken research DB degrades to an empty creep map, never an error."""
        import src.data.research_database as rdb_mod

        def boom(*a, **k):
            raise RuntimeError("db broken")

        monkeypatch.setattr(rdb_mod, "ResearchDatabase", boom)
        silence = _SilenceStub(
            {
                "funding_hl": {
                    "last_event_ms": 1_000,
                    "age_sec": 15.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                }
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        assert d["feed_silence_creep"] == {}

    def test_daily_series_best_effort_on_missing_db(self, monkeypatch) -> None:
        """A broken research DB degrades to empty daily series, never an error."""
        import src.data.research_database as rdb_mod

        def boom(*a, **k):
            raise RuntimeError("db broken")

        monkeypatch.setattr(rdb_mod, "ResearchDatabase", boom)
        silence = _SilenceStub(
            {
                "funding_hl": {
                    "last_event_ms": 1_000,
                    "age_sec": 15.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                }
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        assert d["feed_silence_daily"] == {}  # best-effort empty

    def test_sparkline_best_effort_on_missing_db(self, monkeypatch) -> None:
        """A broken research DB degrades to empty series, never an error."""
        import src.data.research_database as rdb_mod

        def boom(*a, **k):
            raise RuntimeError("db broken")

        monkeypatch.setattr(rdb_mod, "ResearchDatabase", boom)
        silence = _SilenceStub(
            {
                "funding_hl": {
                    "last_event_ms": 1_000,
                    "age_sec": 15.0,
                    "max_silence_sec": 3600.0,
                    "degraded": False,
                }
            }
        )
        self._web._engine = _EngineStub(silence, summary=_HealthSummaryStub())
        d = self._client.get("/api/market_data_health").get_json()
        assert d["feed_silence_spark"] == {}  # best-effort empty


class TestFeedSilenceTemplate:
    """The panel markup + JS must expose the threshold-vs-age columns."""

    pytestmark = pytest.mark.unit

    def test_template_has_feed_silence_panel(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "Feed Silence" in html
        assert 'id="feed-silence-tbody"' in html

    def test_template_has_threshold_vs_age_columns(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # the 7 columns: Feed, Age/exp gap, Threshold, % of threshold, 24h %, Alerted, State
        assert ">Age / exp gap</th>" in html
        assert ">Threshold</th>" in html
        assert "% of threshold" in html
        assert ">24h %</th>" in html
        assert ">Alerted</th>" in html
        assert ">State</th>" in html

    def test_template_renders_expected_gap_from_cadence(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # the Age cell shows the historical p99 cadence as "exp ≤ …"
        assert "cadence_p95_sec" in html
        assert "cadence_p99_sec" in html
        assert "cadence_samples" in html
        assert "exp ≤ " in html
        assert "fmtDur" in html
        assert "learning…" in html

    def test_template_has_render_and_poll_wiring(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "function renderFeedSilence(" in html
        assert "function sparklineSvg(" in html
        assert "feed_silence_spark" in html
        assert "sparklineSvg(sparks[feed] || [], 92, 20)" in html
        assert 'authFetch("/api/market_data_health")' in html

    def test_template_uses_warned_flags_for_alerted_column(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # the JS must read the fire-once flags and render early/imminent
        assert "st.warned_50_pct" in html
        assert "st.warned_90_pct" in html
        assert "alerted50" in html
        assert "alerted90" in html
        assert "alertedTxt" in html

    def test_template_header_turns_amber_on_imminent(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # the whole header bar must change color when any contracted feed
        # crosses ~90% of its silence threshold (before degrading)
        assert "feed_silence_imminent" in html
        assert "state-imminent" in html
        assert "state-degraded" in html
        assert "hdr.classList.toggle" in html
        assert "warned_90_pct && !st.degraded" in html

    def test_template_has_14d_daily_max_age_column(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # 8 columns now: the extra one renders the daily max-age sparkline
        assert ">14d max age</th>" in html
        assert 'colspan="8"' in html
        assert "feed_silence_daily" in html
        assert "function sparklineAbs(" in html
        assert "sparklineAbs(daily[feed] || [], 92, 20, max)" in html

    def test_template_renders_creeping_badge(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # the feed row shows a 'creeping' pill when feed_silence_creep flags it
        assert "feed_silence_creep" in html
        assert "pill-creep" in html
        assert "pill-creep-hi" in html
        assert "creepBadge" in html
        assert ">creeping</span>" in html
        assert "cr.growth_frac" in html

    def test_template_sparkline_hover_tooltip(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # both sparklines carry per-bucket data for the hover tooltip
        assert "data-tips=" in html
        assert 'data-tip-format="pct"' in html
        assert 'data-tip-format="abs"' in html
        assert "encodeURIComponent(JSON.stringify" in html
        # the delegated mousemove tooltip: nearest bucket + exact value
        assert "svg[data-tips]" in html
        assert "_sparkTipEl" in html
        assert "closest(\"svg[data-tips]\")" in html
        assert "fmtTime(ts) + \" · \" + v.toFixed(1) + \"% do threshold\"" in html
        assert "fmtDate(ts) + \" · max \" + fmtDur(v)" in html

    def test_template_shows_fractions_in_alerted_column(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # the Alerted column renders the effective thresholds, e.g.
        # "early @ 30%" and "imminent @ 80%" (configurable, not assumed)
        assert "st.warn_fraction" in html
        assert "earlyFrac" in html
        assert "st.imminent_fraction" in html
        assert "imminentFrac" in html
        assert '"early @ " + earlyFrac + "%"' in html
        assert '"imminent @ " + imminentFrac + "%"' in html

    def test_template_shows_alert_fired_timestamp(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # the Alerted column appends WHEN each alert fired (e.g. 'early 14:32')
        assert "function fmtHM(" in html
        assert "st.warned_50_at_ms" in html
        assert "st.warned_90_at_ms" in html
        assert "earlyAt" in html
        assert "imminentAt" in html
        assert "fmtHM(st.warned_50_at_ms)" in html
        assert "fmtHM(st.warned_90_at_ms)" in html
        # tooltip with full precision
        assert "early disparou a " in html
        assert "imminent disparou a " in html

    def test_template_shows_daily_episode_counters(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # the Alerted cell appends how many episodes already alerted today
        assert "st.early_count_today" in html
        assert "st.imminent_count_today" in html
        assert "epsToday" in html
        assert "todayTxt" in html
        assert "× hoje" in html
        # tooltip carries the per-level breakdown
        assert "epsTitle" in html
        assert "episódio(s) early" in html
        assert "episódio(s) imminent" in html

    def test_template_cadence_badge_and_gap_comparison(self) -> None:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        # the cadence badge + Age cell compare the rolling p99 vs the gap
        assert "pill-cadence" in html
        assert "cadenceBad" in html
        assert "cadenceBadge" in html
        assert "gapOverP99" in html
        assert "gapOverP95" in html
        assert "st.warned_cadence" in html
        # when breached, the Age cell spells out the comparison
        assert 'gap " + fmtDur(age) + " > p99 " + fmtDur(p99)' in html
        # tooltip carries the sample count and the fire-once flag state
        assert "alerta cadence emitido" in html
        assert "(n=" in html
        assert "cadência a degradar" in html
