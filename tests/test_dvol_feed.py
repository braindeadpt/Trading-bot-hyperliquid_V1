"""Unit tests for src/data/dvol_feed.py + ResearchDatabase dvol_daily.

Pins the canonical DVOL percentile math (which the offline IV-gate evidence
scripts now import) and the research-DB persistence path.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.dvol_feed import (  # noqa: E402
    DVOL_WINDOW_DAYS,
    IV_HIGH_PCT,
    DvolFeed,
    build_iv_percentile,
    classify_iv,
    current_dvol_percentile,
    dvol_currency_for,
    dvol_series_for,
    iv_pct_at,
    start_dvol_feed_from_config,
)
from src.data.research_database import ResearchDatabase  # noqa: E402
from src.utils.config import Config  # noqa: E402

DAY = 86_400_000


class TestBuildIvPercentile:
    def test_warmup_returns_none(self):
        closes = [(i * DAY, 50.0) for i in range(1, 10)]  # 9 < 20 closes
        series = build_iv_percentile(closes)
        assert all(p is None for _, p in series)

    def test_strictly_increasing_last_is_100(self):
        closes = [(i * DAY, float(i)) for i in range(1, 31)]
        series = build_iv_percentile(closes)
        assert series[-1][1] == 100.0

    def test_strictly_decreasing_last_is_min_rank(self):
        closes = [(i * DAY, float(31 - i)) for i in range(1, 31)]
        series = build_iv_percentile(closes)
        # last close is the minimum of its 30-day window -> rank 1/30
        assert abs(series[-1][1] - 100.0 / 30.0) < 1e-9


class TestIvPctAt:
    def test_previous_completed_day(self):
        series = [(i * DAY, 50.0) for i in range(1, 31)]
        # ts on day 31 -> last DVOL close strictly before day 30 (day 29)
        assert iv_pct_at(series, 31 * DAY) == 50.0

    def test_empty_returns_none(self):
        assert iv_pct_at([], 123) is None

    def test_before_first_returns_none(self):
        series = [(10 * DAY, 50.0)]
        assert iv_pct_at(series, 10 * DAY) is None


class TestDvolSeriesFor:
    def test_btc_eth_own_proxy_elsewhere(self):
        btc = [(1, 0.5)]
        eth = [(2, 0.7)]
        assert dvol_series_for("BTC", btc, eth) is btc
        assert dvol_series_for("ETH", btc, eth) is eth
        assert dvol_series_for("SOL", btc, eth) is btc
        assert dvol_series_for("HYPE", btc, eth) is btc


class TestDvolCurrencyFor:
    def test_btc_eth_native_elsewhere_proxy(self):
        assert dvol_currency_for("BTC") == "BTC"
        assert dvol_currency_for("ETH") == "ETH"
        assert dvol_currency_for("SOL") == "BTC"
        assert dvol_currency_for("HYPE") == "BTC"

    def test_lowercase_and_whitespace_normalized(self):
        assert dvol_currency_for(" btc ") == "BTC"
        assert dvol_currency_for("eth") == "ETH"


class TestClassifyIv:
    def test_none_is_unknown(self):
        assert classify_iv(None) == "unknown"

    def test_strictly_above_threshold_is_high(self):
        assert classify_iv(IV_HIGH_PCT) == "low_iv"  # not strictly above
        assert classify_iv(IV_HIGH_PCT + 0.01) == "high_iv"
        assert classify_iv(100.0) == "high_iv"
        assert classify_iv(0.0) == "low_iv"

    def test_custom_threshold(self):
        assert classify_iv(50.0, threshold=50.0) == "low_iv"
        assert classify_iv(50.1, threshold=50.0) == "high_iv"


class TestResearchDbDvolDaily:
    def test_save_load_and_upsert(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        assert rdb.save_dvol_daily([("BTC", 1000, 50.0), ("BTC", 1000 + DAY, 60.0)]) == 2
        assert rdb.load_dvol_daily("btc", 0, 10**15) == [(1000, 50.0), (1000 + DAY, 60.0)]
        # upsert replaces the same (currency, ts)
        rdb.save_dvol_daily([("BTC", 1000, 55.0)])
        assert rdb.load_dvol_daily("BTC", 0, 10**15) == [(1000, 55.0), (1000 + DAY, 60.0)]

    def test_empty_load(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        assert rdb.load_dvol_daily("BTC", 0, 10**15) == []


class TestCurrentDvolPercentile:
    def test_returns_rank_in_0_100(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        rdb.save_dvol_daily([("BTC", i * DAY, 50.0 + i) for i in range(1, 31)])
        pct = current_dvol_percentile("BTC", ts_ms=31 * DAY, db=rdb)
        assert pct is not None and 0.0 <= pct <= 100.0

    def test_no_data_returns_none(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        assert current_dvol_percentile("BTC", ts_ms=31 * DAY, db=rdb) is None


class TestDvolFeed:
    def test_fetch_once_persists(self, tmp_path, monkeypatch):
        rdb = ResearchDatabase(tmp_path / "research.db")
        feed = DvolFeed(rdb, ["BTC"], interval_sec=3600.0, lookback_days=60)
        fake = [(1000, 50.0), (1000 + DAY, 60.0)]

        async def fake_fetch(c, s, e):
            return fake

        monkeypatch.setattr("src.data.dvol_feed.fetch_dvol", fake_fetch)
        n = asyncio.run(feed._fetch_once())
        assert n == 2
        assert rdb.load_dvol_daily("BTC", 0, 10**15) == fake

    def test_status_fields(self, tmp_path):
        rdb = ResearchDatabase(tmp_path / "research.db")
        feed = DvolFeed(rdb, ["btc", "ETH"], interval_sec=7200.0, lookback_days=45)
        st = feed.status()
        assert st["currencies"] == ["BTC", "ETH"]
        assert st["interval_sec"] == 7200.0
        assert st["lookback_days"] == 45


class TestHashNeutral:
    """dvol_feed must not change the frozen Fase-10 config_hash."""

    def test_dvol_feed_excluded_from_config_hash(self):
        from src.utils.config import compute_config_hash

        with_feed = {
            "research": {
                "dvol_feed": {"enabled": True, "currencies": ["BTC"]},
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
        """Adding dvol_feed to settings.yaml/DEFAULT_CONFIG stays hash-neutral."""
        from src.utils.config import compute_config_hash, load_config

        cfg = load_config(str(ROOT / "config" / "settings.yaml"))
        assert compute_config_hash(cfg) == "9456c6eb877b2391"


class TestFactory:
    def test_disabled_returns_none(self):
        cfg = Config({"research": {"dvol_feed": {"enabled": False}}})
        assert start_dvol_feed_from_config(cfg) is None

    def test_enabled_returns_feed(self, tmp_path):
        cfg = Config({
            "research": {
                "dvol_feed": {"enabled": True, "currencies": ["BTC"]},
                "database": {"path": str(tmp_path / "r.db")},
            }
        })
        feed = start_dvol_feed_from_config(cfg)
        assert feed is not None
        assert feed._currencies == ["BTC"]

    def test_window_days_default(self):
        assert DVOL_WINDOW_DAYS == 30
