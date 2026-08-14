"""Unit tests for scripts/feed_age_creep_recheck.py.

Pins the staircase rule: a contracted feed is "creeping" when the last N
recorded daily max-ages are non-decreasing, grew a meaningful fraction of
its own silence threshold, and sit above a quiet-level floor. A drop
re-arms; too little data stays quiet; a broken DB degrades to empty.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import feed_age_creep_recheck as creep  # noqa: E402

DAY_MS = 86_400_000
MAX_SIL = 3600.0  # 1h threshold


def _seed_days(db, feed: str, ages, samples=None):
    """Seed one row per day ending today, ascending by day."""
    now = int(time.time() * 1000)
    today = now - (now % DAY_MS)
    samples = samples or [5] * len(ages)
    rows = [
        (feed, today - (len(ages) - 1 - i) * DAY_MS, ages[i], samples[i])
        for i in range(len(ages))
    ]
    db.save_feed_age_history(rows)
    return today


class TestDetectCreepingAge:
    def test_rising_staircase_is_creeping(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        _seed_days(db, "liquidation_okx", [600, 900, 1200, 1600, 2100])
        out = creep.detect_creeping_age(
            {"liquidation_okx": MAX_SIL}, db=db
        )
        assert "liquidation_okx" in out
        d = out["liquidation_okx"]
        assert d["creeping"] is True
        assert d["days"] == 5
        assert d["first_max_age_sec"] == 600.0
        assert d["last_max_age_sec"] == 2100.0
        # 1500s growth on a 3600s threshold
        assert d["growth_sec"] == 1500.0
        assert d["growth_frac"] == pytest.approx(1500 / 3600, abs=1e-4)

    def test_drop_breaks_the_staircase(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        _seed_days(db, "liquidation_okx", [600, 900, 1200, 800, 2100])
        assert creep.detect_creeping_age(
            {"liquidation_okx": MAX_SIL}, db=db
        ) == {}

    def test_flat_or_noise_is_not_creeping(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        _seed_days(db, "liquidation_okx", [600, 610, 590, 620, 600])
        assert creep.detect_creeping_age(
            {"liquidation_okx": MAX_SIL}, db=db
        ) == {}

    def test_growth_below_frac_of_threshold_is_not_creeping(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        # 600->680 = 80s = 2.2% of 3600s — a wobble, not a staircase
        _seed_days(db, "liquidation_okx", [600, 620, 640, 660, 680])
        assert creep.detect_creeping_age(
            {"liquidation_okx": MAX_SIL}, db=db
        ) == {}

    def test_last_day_below_level_floor_is_not_creeping(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        # growth 4500s = 20.8% of 21600s passes, but last day 5000s = 23%
        # < 25% floor: the feed isn't actually quiet for a meaningful part
        # of the day, so it's not "creeping toward" anything.
        _seed_days(db, "liquidation_okx", [500, 1600, 2700, 3800, 5000])
        out = creep.detect_creeping_age(
            {"liquidation_okx": 21600.0}, db=db
        )
        assert out == {}

    def test_insufficient_days_stays_quiet(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        _seed_days(db, "liquidation_okx", [600, 900, 1200])
        assert creep.detect_creeping_age(
            {"liquidation_okx": MAX_SIL}, db=db
        ) == {}

    def test_scale_is_per_feed_threshold(self, tmp_path):
        """The same staircase can creep for a 1h-threshold feed but not for a
        6h-threshold feed — the growth is judged against each feed's own
        max_silence."""
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        ages = [600, 900, 1200, 1600, 2100]
        _seed_days(db, "liquidation_okx", ages)
        _seed_days(db, "funding_hl", ages)
        out = creep.detect_creeping_age(
            {"liquidation_okx": 3600.0, "funding_hl": 21600.0}, db=db
        )
        assert "liquidation_okx" in out  # growth 41.7% of 1h
        assert "funding_hl" not in out  # growth 6.9% of 6h

    def test_undersampled_days_are_excluded(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        # last day only 1 sample (fresh restart partial day) -> excluded,
        # leaving only 4 usable rows < min_days -> quiet
        _seed_days(db, "liquidation_okx", [600, 900, 1200, 1600, 2100],
                   samples=[5, 5, 5, 5, 1])
        assert creep.detect_creeping_age(
            {"liquidation_okx": MAX_SIL}, db=db
        ) == {}

    def test_contracts_not_present_are_ignored(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        _seed_days(db, "liquidation_okx", [600, 900, 1200, 1600, 2100])
        # only funding_hl contracted — the creeping liquidation_okx feed is
        # not part of this deployment, so it must NOT be flagged
        assert creep.detect_creeping_age(
            {"funding_hl": MAX_SIL}, db=db
        ) == {}

    def test_broken_db_degrades_to_empty(self, tmp_path):
        class BoomDB:
            def load_feed_age_history(self, *a, **k):
                raise RuntimeError("db broken")

        assert creep.detect_creeping_age(
            {"liquidation_okx": MAX_SIL}, db=BoomDB()
        ) == {}

    def test_empty_contracts_returns_empty(self, tmp_path):
        assert creep.detect_creeping_age({}, db=object()) == {}


class TestWriteReport:
    def test_report_lists_creeping_feeds(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        _seed_days(db, "liquidation_okx", [600, 900, 1200, 1600, 2100])
        detected = creep.detect_creeping_age(
            {"liquidation_okx": MAX_SIL}, db=db
        )
        out = tmp_path / "rep.md"
        p = creep.write_report(detected, {"liquidation_okx": MAX_SIL}, path=out)
        assert p is not None
        text = out.read_text(encoding="utf-8")
        assert "liquidation_okx" in text
        assert "CREEP_MIN_DAYS" not in text  # constants, not values in prose
        assert "5" in text  # days column


class TestResolveContracts:
    def test_returns_contracted_feeds(self):
        """Real config: only deployment-contracted feeds, with the opt-in
        exclusions (no binance_perp / liquidation_binance unless opted in)."""
        contracts = creep.resolve_contracts()
        assert isinstance(contracts, dict) and contracts
        assert "liquidation_binance" not in contracts
        assert "binance_perp" not in contracts
        for feed, max_sil in contracts.items():
            assert max_sil > 0
