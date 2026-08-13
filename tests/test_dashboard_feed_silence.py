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
                    "warned_50_pct": False,
                    "warned_90_pct": False,
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
        fs = d["feed_silence"]
        assert fs["funding_hl"]["age_sec"] == 30.0
        assert fs["funding_hl"]["max_silence_sec"] == 3600.0
        assert fs["funding_hl"]["degraded"] is False
        # fire-once flags pass through the snapshot
        assert fs["funding_hl"]["warned_50_pct"] is False
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
        # the 7 columns: Feed, Age, Threshold, % of threshold, 24h %, Alerted, State
        assert ">Age</th>" in html
        assert ">Threshold</th>" in html
        assert "% of threshold" in html
        assert ">24h %</th>" in html
        assert ">Alerted</th>" in html
        assert ">State</th>" in html

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
