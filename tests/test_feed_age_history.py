"""Unit tests for src/data/feed_age_history.py + ResearchDatabase feed_age_history.

Pins the daily max-age-per-feed persistence (the signal for feeds whose age
grows between resets) and the creeping-age detector.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.feed_age_history import (  # noqa: E402
    FeedAgeRecorder,
    creeping_age_detector,
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
    def _snap(self, ages: dict):
        return {
            feed: {"age_sec": age, "degraded": False, "max_silence_sec": 3600.0}
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
        assert compute_config_hash(cfg) == "9456c6eb877b2391"


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
