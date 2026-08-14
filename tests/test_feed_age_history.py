"""Unit tests for src/data/feed_age_history.py + ResearchDatabase feed_age_history.

Pins the daily max-age-per-feed persistence (the signal for feeds whose age
grows between resets) and the creeping-age detector.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.feed_age_history import (  # noqa: E402
    SAMPLE_BUCKET_MS,
    FeedAgeRecorder,
    age_pct,
    creeping_age_detector,
    sample_bucket_start_ms,
    start_feed_age_recorder_from_config,
    utc_day_start_ms,
)
from src.data.research_database import ResearchDatabase  # noqa: E402

DAY = 86_400_000


class TestDayBucket:
    def test_utc_midnight_floors(self):
        assert utc_day_start_ms(0) == 0
        assert utc_day_start_ms(1) == 0
        assert utc_day_start_ms(DAY - 1) == 0
        assert utc_day_start_ms(DAY) == DAY
        assert utc_day_start_ms(DAY + 123) == DAY
        assert utc_day_start_ms(3 * DAY + 5) == 3 * DAY


class TestResearchDb:
    def test_save_load(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        rows = [
            ("liquidation_okx", 0, 120.0, 12),
            ("liquidation_okx", DAY, 300.0, 8),
            ("funding_hl", 0, 15.0, 20),
        ]
        assert rdb.save_feed_age_history(rows) == 3
        got = rdb.load_feed_age_history("liquidation_okx", 0, 10**15)
        assert got == [(0, 120.0, 12), (DAY, 300.0, 8)]
        assert rdb.load_feed_age_history("funding_hl", 0, 10**15) == [(0, 15.0, 20)]
        assert rdb.load_feed_age_history("missing", 0, 10**15) == []

    def test_upsert_takes_max_and_adds_samples(self, tmp_path):
        """A later sample for the same (feed, day) raises the max to the true
        peak and accumulates samples — the mid-day quiet episode is not lost."""
        rdb = ResearchDatabase(tmp_path / "research.db")
        rdb.save_feed_age_history([("liquidation_okx", 0, 120.0, 12)])
        rdb.save_feed_age_history([("liquidation_okx", 0, 60.0, 4)])  # lower — ignored
        rdb.save_feed_age_history([("liquidation_okx", 0, 250.0, 3)])  # higher — kept
        assert rdb.load_feed_age_history("liquidation_okx", 0, 10**15) == [
            (0, 250.0, 19)
        ]

    def test_empty_save_and_load(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        assert rdb.save_feed_age_history([]) == 0
        assert rdb.load_feed_age_history("x", 0, 10**15) == []
        assert rdb.load_latest_feed_age("x") is None

    def test_latest_feed_age(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        rdb.save_feed_age_history([("liquidation_okx", 0, 120.0, 12)])
        rdb.save_feed_age_history([("liquidation_okx", DAY, 300.0, 8)])
        assert rdb.load_latest_feed_age("liquidation_okx") == (DAY, 300.0, 8)


class TestFeedSilenceAlerts:
    def test_save_load_roundtrip_and_feed_filter(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        rows = [
            ("liquidation_okx", "early", 1_000, "FEED QUIET (early): ..."),
            ("liquidation_okx", "imminent", 2_000, "FEED QUIET (imminent): ..."),
            ("funding_hl", "degraded", 3_000, "FEED SILENT: ..."),
        ]
        assert rdb.save_feed_silence_alerts(rows) == 3
        assert rdb.load_feed_silence_alerts() == rows
        assert rdb.load_feed_silence_alerts(feed="liquidation_okx") == rows[:2]
        assert rdb.load_feed_silence_alerts(feed="funding_hl") == rows[2:]
        assert rdb.load_feed_silence_alerts(feed="missing") == []

    def test_window_filter_and_ordering(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        rdb.save_feed_silence_alerts([
            ("liquidation_okx", "early", 1_000, "m1"),
            ("liquidation_okx", "imminent", 5_000, "m2"),
            ("liquidation_okx", "degraded", 9_000, "m3"),
        ])
        got = rdb.load_feed_silence_alerts(start_ms=2_000, end_ms=6_000)
        assert got == [("liquidation_okx", "imminent", 5_000, "m2")]

    def test_append_only_keeps_every_emission(self, tmp_path):
        """Each emission is a row — a repeat FEED SILENT after the cooldown
        is its own audit entry, never merged away."""
        rdb = ResearchDatabase(tmp_path / "research.db")
        rdb.save_feed_silence_alerts([("liquidation_okx", "degraded", 1_000, "a")])
        rdb.save_feed_silence_alerts([("liquidation_okx", "degraded", 4_000, "b")])
        assert rdb.load_feed_silence_alerts() == [
            ("liquidation_okx", "degraded", 1_000, "a"),
            ("liquidation_okx", "degraded", 4_000, "b"),
        ]

    def test_empty_save(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        assert rdb.save_feed_silence_alerts([]) == 0
        assert rdb.load_feed_silence_alerts() == []


class TestIntradaySamples:
    def test_bucket_start_floors_to_10min(self):
        assert sample_bucket_start_ms(0) == 0
        assert sample_bucket_start_ms(1) == 0
        assert sample_bucket_start_ms(SAMPLE_BUCKET_MS - 1) == 0
        assert sample_bucket_start_ms(SAMPLE_BUCKET_MS) == SAMPLE_BUCKET_MS
        assert sample_bucket_start_ms(SAMPLE_BUCKET_MS + 123) == SAMPLE_BUCKET_MS

    def test_age_pct(self):
        assert age_pct(0.0, 3600.0) == 0.0
        assert age_pct(1800.0, 3600.0) == 50.0
        assert age_pct(3600.0, 3600.0) == 100.0
        assert age_pct(999999.0, 100.0) == 999.0  # capped
        assert age_pct(100.0, 0.0) == 0.0  # no threshold -> 0

    def test_save_load_and_upsert_max(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        rows = [
            ("funding_hl", 0, 10.0, 3),
            ("funding_hl", SAMPLE_BUCKET_MS, 55.0, 2),
        ]
        assert rdb.save_feed_age_samples(rows) == 2
        assert rdb.load_feed_age_samples("funding_hl", 0, 10**15) == [
            (0, 10.0, 3),
            (SAMPLE_BUCKET_MS, 55.0, 2),
        ]
        # upsert raises the max and accumulates samples
        rdb.save_feed_age_samples([("funding_hl", SAMPLE_BUCKET_MS, 20.0, 1)])
        rdb.save_feed_age_samples([("funding_hl", SAMPLE_BUCKET_MS, 70.0, 4)])
        assert rdb.load_feed_age_samples("funding_hl", 0, 10**15) == [
            (0, 10.0, 3),
            (SAMPLE_BUCKET_MS, 70.0, 7),
        ]

    def test_prune_removes_stale_buckets(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        now = sample_bucket_start_ms(int(time.time() * 1000))
        old = sample_bucket_start_ms(now - 60 * 86_400_000)  # 60 days ago
        rdb.save_feed_age_samples([("funding_hl", old, 99.0, 1)])
        rdb.save_feed_age_samples([("funding_hl", now, 5.0, 1)])
        n = rdb.prune_feed_age_samples(now_ms=now)
        assert n == 1
        assert rdb.load_feed_age_samples("funding_hl", 0, 10**15) == [(now, 5.0, 1)]


class TestCreepingDetector:
    def test_not_enough_days_returns_none(self):
        daily = [(0, 100.0), (DAY, 120.0)]
        assert creeping_age_detector(daily) is None

    def test_flat_history_returns_none(self):
        daily = [(0, 100.0), (DAY, 105.0), (2 * DAY, 110.0)]
        assert creeping_age_detector(daily) is None  # growth < 600s min

    def test_consistent_growth_flags_creeping(self):
        daily = [(0, 60.0), (DAY, 300.0), (2 * DAY, 900.0), (3 * DAY, 2400.0)]
        res = creeping_age_detector(daily)
        assert res is not None
        assert res["creeping"] is True
        assert res["days"] == 4
        assert res["growth_sec"] == 2400.0 - 60.0
        assert res["slope_sec_per_day"] > 0.0

    def test_decreasing_history_returns_none(self):
        daily = [(0, 3000.0), (DAY, 500.0), (2 * DAY, 100.0), (3 * DAY, 50.0)]
        assert creeping_age_detector(daily) is None


class TestFeedAgeRecorder:
    def _snap(self, ages: dict, max_silence: float = 3600.0):
        return {
            feed: {
                "age_sec": age,
                "degraded": False,
                "max_silence_sec": max_silence,
            }
            for feed, age in ages.items()
        }

    def test_sample_tracks_daily_max(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        ages = {"liquidation_okx": 100.0}
        rec = FeedAgeRecorder(rdb, lambda: self._snap(ages))
        rec.sample(now_ms=DAY + 1000)
        ages["liquidation_okx"] = 80.0  # lower — max unchanged
        rec.sample(now_ms=DAY + 2000)
        ages["liquidation_okx"] = 300.0  # higher — max raised
        rec.sample(now_ms=DAY + 3000)
        assert rec._running["liquidation_okx"] == (DAY, 300.0, 3)

    def test_sample_skips_never_seen(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        rec = FeedAgeRecorder(
            rdb, lambda: self._snap({"liquidation_okx": None, "funding_hl": 5.0})
        )
        rec.sample(now_ms=1000)
        assert "liquidation_okx" not in rec._running
        assert rec._running["funding_hl"] == (0, 5.0, 1)

    def test_rollover_flushes_completed_day(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        ages = {"liquidation_okx": 200.0}
        rec = FeedAgeRecorder(rdb, lambda: self._snap(ages))
        rec.sample(now_ms=DAY + 1000)
        # Next day: bucket rolls — previous day flushed to DB, new day tracked.
        rec.sample(now_ms=2 * DAY + 1000)
        assert rdb.load_feed_age_history("liquidation_okx", 0, 10**15) == [
            (DAY, 200.0, 1)
        ]
        assert rec._running["liquidation_okx"][0] == 2 * DAY

    def test_flush_and_stop_persist_partial_day(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        rec = FeedAgeRecorder(rdb, lambda: self._snap({"funding_hl": 42.0}))
        rec.sample(now_ms=1234)
        n = rec.flush()
        assert n == 1
        assert rdb.load_feed_age_history("funding_hl", 0, 10**15) == [(0, 42.0, 1)]
        assert rec._running == {}

    def test_start_stop_loop(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        rec = FeedAgeRecorder(
            rdb, lambda: self._snap({"liquidation_okx": 77.0}), interval_sec=30.0
        )
        asyncio.run(rec.start())
        assert rec.status()["running"] is True
        asyncio.run(rec.stop())
        assert rec.status()["running"] is False
        # stop flushed the partial current day
        assert rdb.load_feed_age_history("liquidation_okx", 0, 10**15) != []

    def test_status_fields(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        rec = FeedAgeRecorder(rdb, lambda: {}, interval_sec=120.0)
        st = rec.status()
        assert st["interval_sec"] == 120.0
        assert st["running"] is False
        assert st["feeds_tracked"] == 0

    def test_sample_tracks_intraday_bucket_pct(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        ages = {"liquidation_okx": 1800.0}  # 50% of 1h
        rec = FeedAgeRecorder(
            rdb,
            lambda: self._snap(ages, max_silence=3600.0),
        )
        bucket = SAMPLE_BUCKET_MS
        rec.sample(now_ms=bucket + 1000)
        assert rec._running_samples["liquidation_okx"] == (bucket, 50.0, 1)
        # higher pct within same bucket raises the max
        ages["liquidation_okx"] = 2700.0  # 75%
        rec.sample(now_ms=bucket + 2000)
        assert rec._running_samples["liquidation_okx"] == (bucket, 75.0, 2)

    def test_sample_uses_feed_max_silence_for_pct(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        ages = {"funding_hl": 1800.0}
        rec = FeedAgeRecorder(rdb, lambda: self._snap(ages, max_silence=3600.0))
        rec.sample(now_ms=1000)
        assert rec._running_samples["funding_hl"][1] == 50.0

    def test_intraday_rollover_flushes_completed_bucket(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        ages = {"funding_hl": 900.0}
        rec = FeedAgeRecorder(
            rdb,
            lambda: self._snap(ages, max_silence=3600.0),
        )
        rec.sample(now_ms=SAMPLE_BUCKET_MS + 1000)
        # Next bucket: completed bucket flushed, new one tracked.
        rec.sample(now_ms=2 * SAMPLE_BUCKET_MS + 1000)
        assert rdb.load_feed_age_samples("funding_hl", 0, 10**15) == [
            (SAMPLE_BUCKET_MS, 25.0, 1)
        ]
        assert rec._running_samples["funding_hl"][0] == 2 * SAMPLE_BUCKET_MS

    def test_flush_samples_and_load_sparkline(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        ages = {"funding_hl": 1200.0}
        rec = FeedAgeRecorder(
            rdb,
            lambda: self._snap(ages, max_silence=3600.0),
        )
        bucket = 2 * SAMPLE_BUCKET_MS
        rec.sample(now_ms=bucket + 500)
        n = rec.flush_samples()
        assert n == 1
        series = rec.load_sparkline("funding_hl", now_ms=bucket + 500, hours=24)
        assert series[0][0] == bucket
        assert series[0][1] == pytest.approx(1200.0 / 3600.0 * 100.0)
        assert rec._running_samples == {}


class TestHashNeutral:
    """feed_age_history must not change the frozen Fase-10 config_hash."""

    def test_feed_age_history_excluded_from_config_hash(self):
        from src.utils.config import compute_config_hash

        with_feed = {
            "research": {
                "feed_age_history": {"enabled": True, "interval_sec": 300.0},
                "ws_microstructure_enabled": True,
            },
            "risk": {"initial_capital": 10000.0},
        }
        without_feed = {
            "research": {"ws_microstructure_enabled": True},
            "risk": {"initial_capital": 10000.0},
        }
        assert compute_config_hash(with_feed) == compute_config_hash(without_feed)

    def test_production_config_matches_frozen_hash(self):
        from src.utils.config import compute_config_hash, load_config

        cfg = load_config(str(ROOT / "config" / "settings.yaml"))
        assert compute_config_hash(cfg) == "4984555298afe7c8"


class TestStartFromConfig:
    def test_disabled_returns_none(self):
        cfg = {"research": {"feed_age_history": {"enabled": False}}}
        assert start_feed_age_recorder_from_config(cfg, lambda: {}) is None

    def test_enabled_builds_recorder(self, tmp_path):
        from src.utils.config import Config

        cfg = Config(
            {
                "research": {
                    "database": {"path": str(tmp_path / "research.db")},
                    "feed_age_history": {"enabled": True, "interval_sec": 60.0},
                }
            }
        )
        rec = start_feed_age_recorder_from_config(cfg, lambda: {})
        assert rec is not None
        assert rec._interval == 60.0
        assert str(rec._db.db_path) == str(tmp_path / "research.db")
