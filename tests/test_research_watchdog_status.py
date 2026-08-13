"""Unit tests for src/research/research_watchdog_status.py.

Verifies the dashboard payload shapes both watchdogs and keeps their
thresholds consistent with the scripts they report on.
"""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research import research_watchdog_status as wd  # noqa: E402


@pytest.fixture
def _stub_metrics(monkeypatch):
    """Deterministic metric/state inputs — no live DB reads."""
    monkeypatch.setattr(
        wd, "bias_date_count",
        lambda: (15, 7000, 100, 200),
    )
    monkeypatch.setattr(
        wd, "real_span_days",
        lambda: (12.3, 10000),
    )
    monkeypatch.setattr(
        wd, "load_shared_state",
        lambda: {
            "top_trader_bias": {"triggered": False, "runs": []},
            "liquidation_flush": {
                "triggered": True,
                "runs": [{"ts": "2026-08-13T00:00:00", "verdict": "INCONCLUSIVE — marginal"}],
            },
        },
    )


def _by_id(payload):
    return {w.get("id"): w for w in payload["watchdogs"]}


def test_two_watchdogs_present(_stub_metrics):
    payload = wd.build_research_watchdogs_payload()
    by_id = _by_id(payload)
    assert set(by_id) == {"top_trader_bias", "liquidation_flush"}
    assert payload["generated_ms"] > 0


def test_bias_progress_and_trigger(_stub_metrics):
    by_id = _by_id(wd.build_research_watchdogs_payload())
    bias = by_id["top_trader_bias"]
    assert bias["current"] == 15
    assert bias["target"] == 20
    assert bias["progress_pct"] == 75.0
    assert bias["samples"] == 7000
    assert bias["triggered"] is False
    assert bias["last_run"] is None
    assert bias["unit"] == "datas"


def test_flush_progress_and_trigger(_stub_metrics):
    by_id = _by_id(wd.build_research_watchdogs_payload())
    flush = by_id["liquidation_flush"]
    assert flush["current"] == 12.3
    assert flush["target"] == 30
    assert flush["progress_pct"] == 41.0
    assert flush["samples"] == 10000
    assert flush["triggered"] is True
    assert flush["last_run"]["verdict"] == "INCONCLUSIVE — marginal"
    assert flush["unit"] == "dias"


def test_progress_clamped_at_100(_stub_metrics, monkeypatch):
    monkeypatch.setattr(wd, "bias_date_count", lambda: (50, 7000, 100, 200))
    by_id = _by_id(wd.build_research_watchdogs_payload())
    assert by_id["top_trader_bias"]["progress_pct"] == 100.0


def test_thresholds_match_scripts():
    from scripts.top_trader_bias_recheck import TARGET_DATES
    from scripts.liquidation_flush_recheck import TARGET_DAYS

    assert wd.BIAS_TARGET_DATES == TARGET_DATES == 20
    assert wd.FLUSH_TARGET_DAYS == TARGET_DAYS == 30


def test_broken_builder_is_isolated(_stub_metrics, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(wd, "_bias_watchdog", boom)
    payload = wd.build_research_watchdogs_payload()
    by_id = _by_id(payload)
    assert by_id["top_trader_bias"]["error"] == "db down"
    # flush watchdog still healthy
    assert by_id["liquidation_flush"]["current"] == 12.3


class TestResearchWatchdogsEndpoint:
    """The /api/research_watchdogs REST endpoint serves both watchdogs."""

    pytestmark = pytest.mark.integration_offline

    def setup_method(self):
        import src.dashboard.web as web

        self._web = web
        self._orig_engine = web._engine
        web._engine = None
        self.app, self.sio, _ = web.create_app({"mode": "paper"})
        self.client = self.app.test_client()

    def teardown_method(self):
        self._web._engine = self._orig_engine

    def test_endpoint_returns_both_watchdog_ids(self):
        r = self.client.get("/api/research_watchdogs")
        assert r.status_code == 200
        ids = {w.get("id") for w in r.get_json()["watchdogs"]}
        assert ids == {"top_trader_bias", "liquidation_flush"}
