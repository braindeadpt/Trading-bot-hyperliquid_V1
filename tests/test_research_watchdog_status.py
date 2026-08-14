"""Unit tests for src/research/research_watchdog_status.py.

Verifies the dashboard payload shapes all three watchdogs (bias, flush,
IV gate shadow) and keeps their thresholds consistent with the scripts
they report on.
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
        wd, "run_iv_comparison",
        lambda: {
            "slices": {
                "high_iv": {"n": 3, "n_closed": 3, "n_open": 0,
                             "net_pnl_usd": 12.0, "win_rate": 0.67,
                             "avg_pnl_usd": 4.0, "median_pnl_usd": 3.0,
                             "best_usd": 9.0, "worst_usd": 0.5},
                "low_iv": {"n": 4, "n_closed": 4, "n_open": 0,
                            "net_pnl_usd": -8.0, "win_rate": 0.25,
                            "avg_pnl_usd": -2.0, "median_pnl_usd": -1.5,
                            "best_usd": 2.0, "worst_usd": -7.0},
                "unknown": {"n": 0, "n_closed": 0, "n_open": 0,
                             "net_pnl_usd": 0.0, "win_rate": None,
                             "avg_pnl_usd": None, "median_pnl_usd": None,
                             "best_usd": None, "worst_usd": None},
            },
        },
    )
    monkeypatch.setattr(
        wd, "load_shared_state",
        lambda: {
            "top_trader_bias": {"triggered": False, "runs": []},
            "liquidation_flush": {
                "triggered": True,
                "runs": [{"ts": "2026-08-13T00:00:00", "verdict": "INCONCLUSIVE — marginal"}],
            },
            "iv_gate_shadow": {
                "triggered": False,
                "runs": [{"ts": "2026-08-13T01:00:00", "verdict": "INCONCLUSIVE"}],
            },
            "feed_age_creep": {
                "triggered": True,
                "runs": [{"ts": "2026-08-13T02:00:00", "verdict": "CREEP DETECTED",
                           "feed": "liquidation_okx"}],
                "feeds_alerted": {"liquidation_okx": 1},
            },
        },
    )
    monkeypatch.setattr(wd, "resolve_creep_contracts",
                        lambda: {"funding_hl": 3600.0, "liquidation_okx": 21600.0})
    monkeypatch.setattr(
        wd, "detect_creeping_age",
        lambda contracts: {
            "liquidation_okx": {
                "creeping": True,
                "days": 5,
                "first_max_age_sec": 600.0,
                "last_max_age_sec": 2100.0,
                "growth_sec": 1500.0,
                "growth_frac": 0.417,
                "last_day_start_ms": 1_752_000_000_000,
            }
        },
    )


def _by_id(payload):
    return {w.get("id"): w for w in payload["watchdogs"]}


def test_four_watchdogs_present(_stub_metrics):
    payload = wd.build_research_watchdogs_payload()
    by_id = _by_id(payload)
    assert set(by_id) == {
        "top_trader_bias", "liquidation_flush", "iv_gate_shadow",
        "feed_age_creep",
    }
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
    from scripts.iv_gate_shadow_recheck import TARGET_CLOSED

    assert wd.BIAS_TARGET_DATES == TARGET_DATES == 20
    assert wd.FLUSH_TARGET_DAYS == TARGET_DAYS == 30
    assert wd.IV_TARGET_CLOSED == TARGET_CLOSED == 30


def test_iv_gate_progress_and_trigger(_stub_metrics):
    by_id = _by_id(wd.build_research_watchdogs_payload())
    iv = by_id["iv_gate_shadow"]
    assert iv["current"] == 7
    assert iv["target"] == 30
    assert iv["progress_pct"] == pytest.approx(23.3, abs=0.1)
    assert iv["samples"] == 7  # n_high + n_low
    assert iv["triggered"] is False
    assert iv["last_run"]["verdict"] == "INCONCLUSIVE"
    assert iv["unit"] == "trades"
    assert iv["report_path"] == "docs/IV_GATE_SHADOW_RECHECK_RESULT.md"


def test_iv_gate_projected_decision_before_trigger(_stub_metrics):
    """The panel projects PROMOTE/REJECT from the CURRENT slices before the
    n>=30 trigger fires — high_iv +12.00 / low_iv -8.00 points to PROMOTE,
    flagged provisional (n=7 < 30)."""
    by_id = _by_id(wd.build_research_watchdogs_payload())
    iv = by_id["iv_gate_shadow"]
    proj = iv["projected"]
    assert proj["status"] == "PROMOTE"
    assert proj["provisional"] is True
    assert proj["n_closed"] == 7
    assert proj["high_net_usd"] == 12.0
    assert proj["low_net_usd"] == -8.0
    assert "high_iv" in proj["detail"] and "low_iv" in proj["detail"]


def test_iv_gate_projected_reject(_stub_metrics, monkeypatch):
    """Slices that do not confirm the backtest direction project REJECT."""
    monkeypatch.setattr(
        wd, "run_iv_comparison",
        lambda: {
            "slices": {
                "high_iv": {"n": 10, "n_closed": 10, "n_open": 0,
                             "net_pnl_usd": -5.0, "win_rate": 0.2,
                             "avg_pnl_usd": -0.5, "median_pnl_usd": -1.0,
                             "best_usd": 2.0, "worst_usd": -6.0},
                "low_iv": {"n": 10, "n_closed": 10, "n_open": 0,
                            "net_pnl_usd": 9.0, "win_rate": 0.6,
                            "avg_pnl_usd": 0.9, "median_pnl_usd": 1.0,
                            "best_usd": 4.0, "worst_usd": -1.0},
                "unknown": {"n": 0, "n_closed": 0, "n_open": 0,
                             "net_pnl_usd": 0.0, "win_rate": None,
                             "avg_pnl_usd": None, "median_pnl_usd": None,
                             "best_usd": None, "worst_usd": None},
            },
        },
    )
    by_id = _by_id(wd.build_research_watchdogs_payload())
    proj = by_id["iv_gate_shadow"]["projected"]
    assert proj["status"] == "REJECT"
    assert proj["provisional"] is True
    assert proj["n_closed"] == 20


def test_iv_gate_projected_na_on_broken_db(_stub_metrics, monkeypatch):
    """A broken comparison report degrades to an N/A projection, never an error."""
    monkeypatch.setattr(wd, "run_iv_comparison", lambda: None)
    by_id = _by_id(wd.build_research_watchdogs_payload())
    iv = by_id["iv_gate_shadow"]
    assert iv["projected"]["status"] == "N/A"
    assert iv["current"] == 0
    assert iv["progress_pct"] == 0.0


def test_broken_builder_is_isolated(_stub_metrics, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(wd, "_bias_watchdog", boom)
    payload = wd.build_research_watchdogs_payload()
    by_id = _by_id(payload)
    assert by_id["top_trader_bias"]["error"] == "db down"
    # flush watchdog still healthy
    assert by_id["liquidation_flush"]["current"] == 12.3


def test_creeping_age_payload(_stub_metrics):
    """The creep watchdog reports the live creeping feeds + the episode state."""
    by_id = _by_id(wd.build_research_watchdogs_payload())
    creep_wd = by_id["feed_age_creep"]
    assert creep_wd["current"] == 1
    assert creep_wd["target"] == 0
    assert creep_wd["progress_pct"] == 0.0
    assert creep_wd["min_days"] == 5
    assert creep_wd["triggered"] is True
    assert creep_wd["samples"] == 1  # alert episodes so far
    assert creep_wd["last_run"]["verdict"] == "CREEP DETECTED"
    feeds = creep_wd["feeds"]
    assert len(feeds) == 1
    assert feeds[0]["feed"] == "liquidation_okx"
    assert feeds[0]["days"] == 5
    assert feeds[0]["growth_frac"] == pytest.approx(0.417)
    assert feeds[0]["last_max_age_sec"] == 2100.0
    assert creep_wd["report_path"] == "docs/FEED_AGE_CREEP_RECHECK_RESULT.md"


def test_creeping_age_quiet_when_nothing_detected(_stub_metrics, monkeypatch):
    monkeypatch.setattr(wd, "detect_creeping_age", lambda contracts: {})
    by_id = _by_id(wd.build_research_watchdogs_payload())
    creep_wd = by_id["feed_age_creep"]
    assert creep_wd["current"] == 0
    assert creep_wd["feeds"] == []
    # episode state persists even while quiet (triggered stays sticky)
    assert creep_wd["triggered"] is True


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

    def test_endpoint_returns_all_watchdog_ids(self):
        r = self.client.get("/api/research_watchdogs")
        assert r.status_code == 200
        ids = {w.get("id") for w in r.get_json()["watchdogs"]}
        assert ids == {
            "top_trader_bias", "liquidation_flush", "iv_gate_shadow",
            "feed_age_creep",
        }


class TestResearchWatchdogsTemplate:
    """The panel markup renders the projected IV decision."""

    pytestmark = pytest.mark.unit

    def test_panel_renders_projected_iv_decision(self) -> None:
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        assert "w.id === \"iv_gate_shadow\"" in html
        assert "w.projected" in html
        assert "pr.status === \"PROMOTE\"" in html
        assert "→ " in html and "(proj)" in html
        assert "high_net_usd" in html
        assert "low_net_usd" in html

    def test_panel_renders_feed_age_creep(self) -> None:
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        # the creep watchdog renders the creeping feed names in red
        assert "w.id === \"feed_age_creep\"" in html
        assert "CREEP ATIVO" in html
        assert "w.feeds" in html
        assert "growth_frac" in html
        assert "sem creep — ok" in html
