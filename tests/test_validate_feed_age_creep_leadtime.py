"""Unit tests for scripts/validate_feed_age_creep_leadtime.py.

Pins the lead-time semantics: an episode is anticipated when the production
staircase rule fired (and stayed active) before the first degraded day;
same-day when the fire lands on the degraded day itself; MISS otherwise. A
recovery re-arms the detector, so a stale fire after a recovery does not
count. Fires with no later degradation are "unconfirmed".
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import validate_feed_age_creep_leadtime as va  # noqa: E402

DAY_MS = 86_400_000
MAX_SIL = 3600.0  # 1h threshold


def _rows(ages, samples=None):
    """Ascending daily rows ending today."""
    now = int(time.time() * 1000)
    today = now - (now % DAY_MS)
    samples = samples or [5] * len(ages)
    return [
        (today - (len(ages) - 1 - i) * DAY_MS, float(ages[i]), samples[i])
        for i in range(len(ages))
    ]


def _seed(db, feed, ages, samples=None):
    db.save_feed_age_history(
        [(feed, d, a, s) for d, a, s in _rows(ages, samples)]
    )


class TestDegradedDaysAndEpisodes:
    def test_episodes_group_consecutive_days(self):
        rows = _rows([500, 4000, 4100, 300, 5000, 400])
        episodes = va.degradation_episodes(rows, MAX_SIL)
        assert episodes == [(rows[1][0], rows[2][0]), (rows[4][0], rows[4][0])]

    def test_no_degradation_no_episodes(self):
        assert va.degradation_episodes(_rows([500, 600, 700]), MAX_SIL) == []


class TestWalkForwardFires:
    def test_fire_on_staircase_completion(self):
        rows = _rows([600, 900, 1200, 1600, 2100, 2200])
        fires = va.walk_forward_fires(rows, MAX_SIL)
        assert len(fires) == 1
        assert fires[0][0] == rows[4][0]  # first day the 5-day rule completes

    def test_recovery_rearms(self):
        # staircase completes at d4, drops at d5 (recovery), re-creeps later
        rows = _rows([600, 900, 1200, 1600, 2100, 300, 800, 1300, 1800, 2300])
        fires = va.walk_forward_fires(rows, MAX_SIL)
        assert len(fires) == 2
        assert fires[0][0] == rows[4][0]
        assert fires[1][0] == rows[9][0]

    def test_no_fire_without_staircase(self):
        assert va.walk_forward_fires(_rows([500, 400, 500, 400, 500]), MAX_SIL) == []


class TestAnalyseFeed:
    def test_anticipated_with_lead(self):
        # staircase completes at d4 (fire), keeps creeping, degrades at d8
        rows = _rows([600, 900, 1200, 1600, 2100, 2200, 2400, 2600, 5000])
        a = va.analyse_feed(rows, MAX_SIL)
        assert a["episodes"] == 1
        assert a["anticipated"] == 1
        assert a["same_day"] == 0
        assert a["misses"] == 0
        assert a["lead_days"] == [4]  # d8 - d4
        assert a["avg_lead_days"] == 4.0
        assert a["episode_details"][0]["bucket"] == "anticipated"

    def test_same_day_fire(self):
        # the 5th day both completes the staircase AND degrades
        rows = _rows([600, 900, 1200, 1600, 4000])
        a = va.analyse_feed(rows, MAX_SIL)
        assert a["episodes"] == 1
        assert a["anticipated"] == 0
        assert a["same_day"] == 1
        assert a["misses"] == 0
        assert a["lead_days"] == [0]
        assert a["episode_details"][0]["bucket"] == "same-day"

    def test_miss_without_staircase(self):
        rows = _rows([500, 400, 500, 400, 500, 4000])
        a = va.analyse_feed(rows, MAX_SIL)
        assert a["episodes"] == 1
        assert a["misses"] == 1
        assert a["anticipated"] == 0
        assert a["episode_details"][0]["bucket"] == "miss"
        assert a["episode_details"][0]["lead_days"] is None

    def test_stale_fire_after_recovery_is_miss(self):
        # fire at d4, recovery at d5, degrade at d6 without re-creeping
        rows = _rows([600, 900, 1200, 1600, 2100, 300, 4000])
        a = va.analyse_feed(rows, MAX_SIL)
        assert a["misses"] == 1
        assert a["anticipated"] == 0
        # the d4 fire exists but the episode closed before the degradation
        assert a["unconfirmed_fires"] == 1

    def test_fire_without_confirmation(self):
        rows = _rows([600, 900, 1200, 1600, 2100, 300, 400, 300, 400, 300])
        a = va.analyse_feed(rows, MAX_SIL)
        assert a["episodes"] == 0
        assert a["unconfirmed_fires"] == 1

    def test_two_episodes_each_measured(self):
        rows = _rows([
            600, 900, 1200, 1600, 2100,  # staircase -> fire d4
            4000, 4100,                   # degraded episode 1
            300, 400, 300,                # recovery (re-arm)
            600, 900, 1200, 1600,         # staircase again -> fire d13
            5000,                         # degraded episode 2
        ])
        a = va.analyse_feed(rows, MAX_SIL)
        assert a["episodes"] == 2
        assert a["anticipated"] == 2
        # episode 2: fire at d13 (5-day window d9..d13), degrade at d14
        assert a["lead_days"] == [1, 1]
        assert a["avg_lead_days"] == 1.0

    def test_insufficient_history_before_first_episode(self):
        # only 3 days before the degradation: the 5-day rule cannot fire
        rows = _rows([600, 900, 5000])
        a = va.analyse_feed(rows, MAX_SIL)
        assert a["episodes"] == 1
        assert a["misses"] == 1  # no way to anticipate with < min_days history

    def test_no_degradation_at_all(self):
        a = va.analyse_feed(_rows([600, 900, 1200, 1600, 2100]), MAX_SIL)
        assert a["episodes"] == 0
        assert a["unconfirmed_fires"] == 1


class TestAnalyseAll:
    def test_empty_db_degrades_gracefully(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        report = va.analyse_all(
            {"funding_hl": MAX_SIL, "liquidation_okx": 21600.0},
            db=db,
        )
        assert report["feeds"]["funding_hl"]["rows"] == 0
        assert report["feeds"]["liquidation_okx"]["episodes"] == 0

    def test_contracts_filter_feeds(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        _seed(db, "liquidation_okx",
              [600, 900, 1200, 1600, 2100, 5000])
        report = va.analyse_all({"funding_hl": MAX_SIL}, db=db)
        # only the contracted feed is analysed; liquidation_okx is absent
        assert "liquidation_okx" not in report["feeds"]
        assert report["feeds"]["funding_hl"]["rows"] == 0

    def test_end_to_end_via_db(self, tmp_path):
        from src.data.research_database import ResearchDatabase

        db = ResearchDatabase(tmp_path / "r.db")
        _seed(db, "funding_hl",
              [600, 900, 1200, 1600, 2100, 2200, 2400, 2600, 5000])
        report = va.analyse_all({"funding_hl": MAX_SIL}, db=db)
        a = report["feeds"]["funding_hl"]
        assert a["anticipated"] == 1
        assert a["avg_lead_days"] == 4.0


class TestRenderMarkdown:
    def test_report_renders_table_and_totals(self, tmp_path):
        report = {
            "min_days": 5, "min_growth_frac": 0.15, "min_level_frac": 0.25,
            "feeds": {
                "funding_hl": {
                    "rows": 9, "degraded_days": 1, "episodes": 1,
                    "anticipated": 1, "same_day": 0, "misses": 0,
                    "lead_days": [4], "avg_lead_days": 4.0,
                    "episode_details": [{
                        "start_day_ms": 1_752_000_000_000,
                        "end_day_ms": 1_752_000_000_000,
                        "fire_day_ms": 1_751_654_400_000,
                        "lead_days": 4, "bucket": "anticipated",
                    }],
                    "unconfirmed": [],
                }
            },
        }
        md = va.render_markdown(report)
        assert "`funding_hl`" in md
        assert "**Total: 1/1 episódios antecipados (100%)" in md
        assert "lead médio 4.0d" in md

    def test_empty_report_notes_nothing_to_validate(self):
        md = va.render_markdown({
            "min_days": 5, "min_growth_frac": 0.15, "min_level_frac": 0.25,
            "feeds": {},
        })
        assert "Sem episódios de degradação na janela" in md
