"""Unit tests for scripts/research_watchdog_supervisor.py.

Pins the unified supervisor contract: ONE shared state file (with migration
from the legacy per-watchdog files) and both gate triggers (bias >= 20 dates,
flush >= 30 days) firing exactly once.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import research_watchdog_supervisor as sup  # noqa: E402


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


class TestSharedState:
    def test_save_then_load_roundtrip(self, tmp_path):
        p = tmp_path / "state.json"
        state = sup.fresh_state()
        state["top_trader_bias"]["triggered"] = True
        state["top_trader_bias"]["runs"] = [{"ts": "t", "verdict": "GATE PASS"}]
        sup.save_shared_state(state, path=p)
        loaded = sup.load_shared_state(path=p)
        assert loaded == state
        assert loaded["liquidation_flush"] == {"triggered": False, "runs": []}

    def test_load_missing_returns_fresh(self, tmp_path):
        state = sup.load_shared_state(path=tmp_path / "missing.json")
        assert state == sup.fresh_state()

    def test_corrupt_shared_falls_back_to_fresh(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("{not json", encoding="utf-8")
        assert sup.load_shared_state(path=p) == sup.fresh_state()

    def test_legacy_migration(self, tmp_path):
        legacy_bias = tmp_path / "top_trader_bias_recheck_state.json"
        legacy_flush = tmp_path / "liquidation_flush_recheck_state.json"
        _write_json(legacy_bias, {"triggered": True, "runs": [{"ts": "b1"}]})
        _write_json(legacy_flush, {"triggered": False, "runs": []})

        # Point the supervisor at the legacy files via a temp state dir.
        sup.LEGACY_STATE_PATHS = {  # type: ignore[attr-defined]
            "top_trader_bias": legacy_bias,
            "liquidation_flush": legacy_flush,
        }
        shared_path = tmp_path / "research_watchdogs_state.json"
        try:
            state = sup.load_shared_state(path=shared_path)
            assert state["top_trader_bias"]["triggered"] is True
            assert state["top_trader_bias"]["runs"] == [{"ts": "b1"}]
            assert state["liquidation_flush"]["triggered"] is False
            # migrated shared file persisted
            assert shared_path.exists()
            assert sup.load_shared_state(path=shared_path)["top_trader_bias"]["triggered"] is True
        finally:
            sup.LEGACY_STATE_PATHS = {  # type: ignore[attr-defined]
                "top_trader_bias": Path("data/research/top_trader_bias_recheck_state.json"),
                "liquidation_flush": Path("data/research/liquidation_flush_recheck_state.json"),
            }


def _shared_with_flush_state() -> Dict[str, Any]:
    return sup.fresh_state()


class TestBiasGate:
    def test_skips_below_target_dates(self, monkeypatch, tmp_path):
        shared = _shared_with_flush_state()
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "bias_date_count", lambda: (19, 5000, 1, 2))
            ran = sup.check_bias(shared, force=False)
            assert ran is False
            assert shared["top_trader_bias"]["triggered"] is False
            assert shared["top_trader_bias"]["runs"] == []
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_fires_at_target_dates_and_is_idempotent(self, monkeypatch, tmp_path):
        shared = _shared_with_flush_state()
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "bias_date_count", lambda: (20, 6000, 1, 2))
            probe_out = tmp_path / "probe.json"
            _write_json(probe_out, {
                "cells": [{
                    "feature": "tt_bias_delta_1h", "horizon": "24h", "ic": 0.05,
                    "survives": True, "is_control": False, "n_dates": 20,
                }],
                "meta": {"n_dates": 20},
            })
            monkeypatch.setattr(sup, "run_bias_probe", lambda json_out=None: probe_out)
            monkeypatch.setattr(sup, "write_bias_report", lambda *a, **k: None)

            assert sup.check_bias(shared, force=False) is True
            assert shared["top_trader_bias"]["triggered"] is True
            run = shared["top_trader_bias"]["runs"][-1]
            assert run["verdict"].startswith("GATE PASS")
            assert run["n_survived"] == 1

            # second call: watch-only, no re-fire
            assert sup.check_bias(shared, force=False) is False
            assert len(shared["top_trader_bias"]["runs"]) == 1
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_force_reruns_without_consuming_trigger(self, monkeypatch, tmp_path):
        shared = _shared_with_flush_state()
        shared["top_trader_bias"]["triggered"] = True
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "bias_date_count", lambda: (20, 6000, 1, 2))
            probe_out = tmp_path / "probe.json"
            _write_json(probe_out, {"cells": [], "meta": {"n_dates": 20}})
            monkeypatch.setattr(sup, "run_bias_probe", lambda json_out=None: probe_out)
            monkeypatch.setattr(sup, "write_bias_report", lambda *a, **k: None)

            assert sup.check_bias(shared, force=True) is True
            # --force runs but does NOT consume the trigger
            assert shared["top_trader_bias"]["triggered"] is True
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")


class TestFlushGate:
    def test_skips_below_30_days(self, monkeypatch, tmp_path):
        shared = _shared_with_flush_state()
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "real_span_days", lambda: (12.0, 10000))
            assert sup.check_flush(shared, force=False) is False
            assert shared["liquidation_flush"]["triggered"] is False
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_fires_at_30_days(self, monkeypatch, tmp_path):
        shared = _shared_with_flush_state()
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "real_span_days", lambda: (31.0, 20000))
            sim_path = tmp_path / "sim.json"
            _write_json(sim_path, {"results": [{
                "source": "real", "symbol": "ETH", "threshold": "p90",
                "hold_min": 30, "direction": "fade", "sl_pct": None,
                "n": 40, "win_rate": 52.0, "profit_factor": 1.5,
                "avg_net_bps": 4.5, "net_bps": 180.0,
            }]})
            monkeypatch.setattr(sup, "run_flush_simulation", lambda: sim_path)
            monkeypatch.setattr(sup, "write_flush_report", lambda *a, **k: None)

            assert sup.check_flush(shared, force=False) is True
            assert shared["liquidation_flush"]["triggered"] is True
            run = shared["liquidation_flush"]["runs"][-1]
            assert run["verdict"].startswith("CONFIRMED")
            assert run["cell"]["n"] == 40

            assert sup.check_flush(shared, force=False) is False
            assert len(shared["liquidation_flush"]["runs"]) == 1
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")


class TestIvGateGate:
    def test_skips_below_target_closed(self, monkeypatch, tmp_path):
        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "iv_decision_count", lambda: (10, 4, 6))
            assert sup.check_iv_gate(shared, force=False) is False
            assert shared["iv_gate_shadow"]["triggered"] is False
            assert shared["iv_gate_shadow"]["runs"] == []
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_fires_at_target_and_is_idempotent(self, monkeypatch, tmp_path):
        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "iv_decision_count", lambda: (30, 20, 10))
            report = {
                "slices": {
                    "high_iv": {"n": 20, "n_closed": 20, "n_open": 0,
                                 "net_pnl_usd": 50.0, "win_rate": 0.6,
                                 "avg_pnl_usd": 2.5, "median_pnl_usd": 1.0,
                                 "best_usd": 10.0, "worst_usd": -2.0},
                    "low_iv": {"n": 10, "n_closed": 10, "n_open": 0,
                                "net_pnl_usd": -30.0, "win_rate": 0.2,
                                "avg_pnl_usd": -3.0, "median_pnl_usd": -1.0,
                                "best_usd": 1.0, "worst_usd": -8.0},
                    "unknown": {"n": 0, "n_closed": 0, "n_open": 0,
                                 "net_pnl_usd": 0.0, "win_rate": None,
                                 "avg_pnl_usd": None, "median_pnl_usd": None,
                                 "best_usd": None, "worst_usd": None},
                },
            }
            monkeypatch.setattr(sup, "run_iv_comparison", lambda: report)
            monkeypatch.setattr(sup, "write_iv_report", lambda *a, **k: None)

            assert sup.check_iv_gate(shared, force=False) is True
            assert shared["iv_gate_shadow"]["triggered"] is True
            run = shared["iv_gate_shadow"]["runs"][-1]
            assert run["verdict"] == "PROMOTE"
            assert run["n_closed"] == 30
            assert run["n_high_closed"] == 20
            assert run["n_low_closed"] == 10

            # second call: watch-only, no re-fire
            assert sup.check_iv_gate(shared, force=False) is False
            assert len(shared["iv_gate_shadow"]["runs"]) == 1
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_reject_keeps_shadow(self, monkeypatch, tmp_path):
        """high_iv not positive => REJECT: never silently enforce."""
        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "iv_decision_count", lambda: (40, 20, 20))
            report = {
                "slices": {
                    "high_iv": {"n": 20, "n_closed": 20, "n_open": 0,
                                 "net_pnl_usd": -10.0, "win_rate": 0.2,
                                 "avg_pnl_usd": -0.5, "median_pnl_usd": -1.0,
                                 "best_usd": 5.0, "worst_usd": -8.0},
                    "low_iv": {"n": 20, "n_closed": 20, "n_open": 0,
                                "net_pnl_usd": 10.0, "win_rate": 0.6,
                                "avg_pnl_usd": 0.5, "median_pnl_usd": 1.0,
                                "best_usd": 8.0, "worst_usd": -2.0},
                    "unknown": {"n": 0, "n_closed": 0, "n_open": 0,
                                 "net_pnl_usd": 0.0, "win_rate": None,
                                 "avg_pnl_usd": None, "median_pnl_usd": None,
                                 "best_usd": None, "worst_usd": None},
                },
            }
            monkeypatch.setattr(sup, "run_iv_comparison", lambda: report)
            monkeypatch.setattr(sup, "write_iv_report", lambda *a, **k: None)
            notified: list = []
            monkeypatch.setattr(sup, "notify_iv_promote", lambda r, v: notified.append(v["status"]))

            assert sup.check_iv_gate(shared, force=False) is True
            assert shared["iv_gate_shadow"]["runs"][-1]["verdict"] == "REJECT"
            # REJECT must NOT fire the promote alert.
            assert notified == []
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_promote_fires_alert_once_with_exact_diff(self, monkeypatch, tmp_path):
        """PROMOTE notifies the operator with the exact diff (slices + threshold
        + report path); the alert fires once per run — never on watch-only."""
        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "iv_decision_count", lambda: (30, 20, 10))
            report = {
                "slices": {
                    "high_iv": {"n": 20, "n_closed": 20, "n_open": 0,
                                 "net_pnl_usd": 50.0, "win_rate": 0.6,
                                 "avg_pnl_usd": 2.5, "median_pnl_usd": 1.0,
                                 "best_usd": 10.0, "worst_usd": -2.0},
                    "low_iv": {"n": 10, "n_closed": 10, "n_open": 0,
                                "net_pnl_usd": -30.0, "win_rate": 0.2,
                                "avg_pnl_usd": -3.0, "median_pnl_usd": -1.0,
                                "best_usd": 1.0, "worst_usd": -8.0},
                    "unknown": {"n": 0, "n_closed": 0, "n_open": 0,
                                 "net_pnl_usd": 0.0, "win_rate": None,
                                 "avg_pnl_usd": None, "median_pnl_usd": None,
                                 "best_usd": None, "worst_usd": None},
                },
            }
            monkeypatch.setattr(sup, "run_iv_comparison", lambda: report)
            monkeypatch.setattr(sup, "write_iv_report", lambda *a, **k: None)
            notified: list = []

            def _fake_notify(rpt, run):
                notified.append({
                    "n_closed": run["n_closed"],
                    "verdict": run["verdict"],
                    "threshold": run.get("threshold"),
                    "report_path": run.get("report_path"),
                    "hi_net": rpt["slices"]["high_iv"]["net_pnl_usd"],
                    "lo_net": rpt["slices"]["low_iv"]["net_pnl_usd"],
                })

            monkeypatch.setattr(sup, "notify_iv_promote", _fake_notify)

            assert sup.check_iv_gate(shared, force=False) is True
            assert len(notified) == 1
            assert notified[0]["verdict"] == "PROMOTE"
            assert notified[0]["n_closed"] == 30
            assert notified[0]["threshold"] == 66.7
            assert "IV_GATE_SHADOW_RECHECK_RESULT.md" in notified[0]["report_path"]
            assert notified[0]["hi_net"] == 50.0
            assert notified[0]["lo_net"] == -30.0

            # Watch-only second call: no re-run, no second alert.
            assert sup.check_iv_gate(shared, force=False) is False
            assert len(notified) == 1
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_promote_without_notifier_does_not_break(self, monkeypatch, tmp_path):
        """A missing/broken notifier must not take the gate down."""
        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "iv_decision_count", lambda: (30, 20, 10))
            report = {
                "slices": {
                    "high_iv": {"n": 20, "n_closed": 20, "n_open": 0,
                                 "net_pnl_usd": 50.0, "win_rate": 0.6,
                                 "avg_pnl_usd": 2.5, "median_pnl_usd": 1.0,
                                 "best_usd": 10.0, "worst_usd": -2.0},
                    "low_iv": {"n": 10, "n_closed": 10, "n_open": 0,
                                "net_pnl_usd": -30.0, "win_rate": 0.2,
                                "avg_pnl_usd": -3.0, "median_pnl_usd": -1.0,
                                "best_usd": 1.0, "worst_usd": -8.0},
                    "unknown": {"n": 0, "n_closed": 0, "n_open": 0,
                                 "net_pnl_usd": 0.0, "win_rate": None,
                                 "avg_pnl_usd": None, "median_pnl_usd": None,
                                 "best_usd": None, "worst_usd": None},
                },
            }
            monkeypatch.setattr(sup, "run_iv_comparison", lambda: report)
            monkeypatch.setattr(sup, "write_iv_report", lambda *a, **k: None)
            monkeypatch.setattr(sup, "build_alert_notifier", lambda: None)

            assert sup.check_iv_gate(shared, force=False) is True
            assert shared["iv_gate_shadow"]["runs"][-1]["verdict"] == "PROMOTE"
            # The gate still completed and persisted its run.
            assert len(shared["iv_gate_shadow"]["runs"]) == 1
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")


class TestCreepingAgeGate:
    """Edge-triggered per feed: alerts once when an episode starts, stays
    quiet while it continues, and re-arms when the feed recovers."""

    def _stub_detect(self, monkeypatch, creeping=None):
        creeping = creeping or {}
        monkeypatch.setattr(
            sup, "resolve_contracts",
            lambda: {"funding_hl": 3600.0, "liquidation_okx": 21600.0},
        )
        monkeypatch.setattr(
            sup, "detect_creeping_age", lambda contracts: dict(creeping)
        )
        monkeypatch.setattr(sup, "write_creep_report", lambda *a, **k: None)

    def _creeping(self, feed="liquidation_okx"):
        return {
            feed: {
                "creeping": True,
                "days": 5,
                "first_max_age_sec": 600.0,
                "last_max_age_sec": 2100.0,
                "growth_sec": 1500.0,
                "growth_frac": 0.417,
                "last_day_start_ms": 1_752_000_000_000,
            }
        }

    def test_fires_once_per_episode_and_rearms_on_recovery(
        self, monkeypatch, tmp_path
    ):
        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        notified: list = []
        try:
            monkeypatch.setattr(sup, "notify_creeping_age",
                                lambda f, d, r: notified.append((f, r["verdict"])))
            self._stub_detect(monkeypatch, self._creeping())

            # episode starts -> one alert + run persisted
            assert sup.check_creeping_age(shared, force=False) is True
            assert notified == [("liquidation_okx", "CREEP DETECTED")]
            assert shared["feed_age_creep"]["triggered"] is True
            assert len(shared["feed_age_creep"]["runs"]) == 1
            assert shared["feed_age_creep"]["feeds_alerted"]["liquidation_okx"] \
                == 1_752_000_000_000

            # same episode continuing -> no re-alert, no new run
            assert sup.check_creeping_age(shared, force=False) is False
            assert len(notified) == 1
            assert len(shared["feed_age_creep"]["runs"]) == 1

            # recovery -> episode closed, feed dropped from the alerted map
            self._stub_detect(monkeypatch, {})
            assert sup.check_creeping_age(shared, force=False) is False
            assert shared["feed_age_creep"]["feeds_alerted"] == {}

            # re-creep -> new episode alerts again
            self._stub_detect(monkeypatch, self._creeping())
            assert sup.check_creeping_age(shared, force=False) is True
            assert len(notified) == 2
            assert len(shared["feed_age_creep"]["runs"]) == 2
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_alert_has_warning_severity_and_feed_detail(
        self, monkeypatch, tmp_path
    ):
        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        captured: list = []
        try:
            class _FakeNotifier:
                async def send(self, msg, level="info", *, force=False):
                    captured.append((msg, level))

            monkeypatch.setattr(sup, "build_alert_notifier",
                                lambda: _FakeNotifier())
            self._stub_detect(monkeypatch, self._creeping())

            assert sup.check_creeping_age(shared, force=False) is True
            assert len(captured) == 1
            msg, level = captured[0]
            assert level == "warning"
            assert "FEED AGE CREEP" in msg
            assert "liquidation_okx" in msg
            assert "+42%" in msg  # growth_frac 0.417 -> 41.7 -> 42
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_no_notifier_does_not_break(self, monkeypatch, tmp_path):
        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "build_alert_notifier", lambda: None)
            self._stub_detect(monkeypatch, self._creeping())
            assert sup.check_creeping_age(shared, force=False) is True
            assert shared["feed_age_creep"]["runs"][-1]["verdict"] == "CREEP DETECTED"
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_shared_state_roundtrip_preserves_feeds_alerted(self, tmp_path):
        p = tmp_path / "state.json"
        state = sup.fresh_state()
        state["feed_age_creep"]["feeds_alerted"] = {"liquidation_okx": 123}
        state["feed_age_creep"]["triggered"] = True
        sup.save_shared_state(state, path=p)
        loaded = sup.load_shared_state(path=p)
        assert loaded["feed_age_creep"]["feeds_alerted"] == {"liquidation_okx": 123}
        assert loaded["feed_age_creep"]["triggered"] is True


class TestCadenceGate:
    """Edge-triggered per feed, like the creep gate: alerts once when a feed
    turns DEGRADING (recent median > its own historical p99), stays quiet
    while the episode continues, re-arms on recovery."""

    def _stub_diagnostic(self, monkeypatch, degrading=None):
        feeds = {}
        for f in (degrading or []):
            feeds[f] = {
                "status": "DEGRADING",
                "recent_median_sec": 600.0,
                "hist_p99_sec": 45.0,
                "hist_p95_sec": 30.0,
                "latest_gap_sec": 650.0,
                "trend_sec_per_gap": 8.0,
            }
        monkeypatch.setattr(
            sup, "cadence_diagnostic",
            lambda: {"now_ms": 1_752_000_000_000, "feeds": feeds},
        )
        # the markdown report write is a side artifact — tests never touch
        # the real docs/ path.
        monkeypatch.setattr(sup, "write_cadence_report", lambda *a, **k: None)

    def test_fires_once_per_episode_and_rearms_on_recovery(
        self, monkeypatch, tmp_path
    ):
        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        notified: list = []
        try:
            monkeypatch.setattr(sup, "notify_cadence_degrading",
                                lambda f, d, r: notified.append((f, r["verdict"])))
            self._stub_diagnostic(monkeypatch, ["liquidation_okx"])

            # episode starts -> one alert + run persisted
            assert sup.check_cadence_degrading(shared, force=False) is True
            assert notified == [("liquidation_okx", "DEGRADING")]
            assert shared["feed_cadence"]["triggered"] is True
            assert len(shared["feed_cadence"]["runs"]) == 1
            assert shared["feed_cadence"]["feeds_alerted"]["liquidation_okx"] \
                == 1_752_000_000_000

            # same episode continuing -> no re-alert, no new run
            assert sup.check_cadence_degrading(shared, force=False) is False
            assert len(notified) == 1
            assert len(shared["feed_cadence"]["runs"]) == 1

            # recovery (verdict no longer DEGRADING) -> episode closed
            self._stub_diagnostic(monkeypatch, [])
            assert sup.check_cadence_degrading(shared, force=False) is False
            assert shared["feed_cadence"]["feeds_alerted"] == {}

            # re-degrading -> new episode alerts again
            self._stub_diagnostic(monkeypatch, ["liquidation_okx"])
            assert sup.check_cadence_degrading(shared, force=False) is True
            assert len(notified) == 2
            assert len(shared["feed_cadence"]["runs"]) == 2
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_alert_has_warning_severity_and_feed_detail(
        self, monkeypatch, tmp_path
    ):
        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        captured: list = []
        try:
            class _FakeNotifier:
                async def send(self, msg, level="info", *, force=False):
                    captured.append((msg, level))

            monkeypatch.setattr(sup, "build_alert_notifier",
                                lambda: _FakeNotifier())
            self._stub_diagnostic(monkeypatch, ["liquidation_okx"])

            assert sup.check_cadence_degrading(shared, force=False) is True
            assert len(captured) == 1
            msg, level = captured[0]
            assert level == "warning"
            assert "FEED CADENCE DEGRADING" in msg
            assert "liquidation_okx" in msg
            assert "10.0m" in msg  # 600s median
            assert "0.8m" in msg  # 45s p99
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_no_notifier_does_not_break(self, monkeypatch, tmp_path):
        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        try:
            monkeypatch.setattr(sup, "build_alert_notifier", lambda: None)
            self._stub_diagnostic(monkeypatch, ["liquidation_okx"])
            assert sup.check_cadence_degrading(shared, force=False) is True
            assert shared["feed_cadence"]["runs"][-1]["verdict"] == "DEGRADING"
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")

    def test_shared_state_roundtrip_preserves_feeds_alerted(self, tmp_path):
        p = tmp_path / "state.json"
        state = sup.fresh_state()
        state["feed_cadence"]["feeds_alerted"] = {"liquidation_okx": 123}
        state["feed_cadence"]["triggered"] = True
        sup.save_shared_state(state, path=p)
        loaded = sup.load_shared_state(path=p)
        assert loaded["feed_cadence"]["feeds_alerted"] == {"liquidation_okx": 123}
        assert loaded["feed_cadence"]["triggered"] is True

    def test_real_diagnostic_drives_alert(self, monkeypatch, tmp_path):
        """End-to-end: a real temp DB with okx events (fast history, slow
        recent) -> the diagnostic says DEGRADING -> the watchdog alerts."""
        import sqlite3
        import time as _time

        db_path = tmp_path / "live.db"
        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE TABLE liquidation_events (symbol TEXT, timestamp_ms INTEGER, "
            "notional_usd REAL, side TEXT, source TEXT)"
        )
        now = int(_time.time() * 1000)
        cutoff = now - 48 * 3600_000
        ts = cutoff - 199 * 30_000  # history: 200 events every 30s
        for _ in range(200):
            con.execute(
                "INSERT INTO liquidation_events VALUES ('OKX', ?, 1.0, 'buy', 'okx')",
                (ts,),
            )
            ts += 30_000
        for _ in range(60):  # recent: 60 events every 600s
            con.execute(
                "INSERT INTO liquidation_events VALUES ('OKX', ?, 1.0, 'buy', 'okx')",
                (ts,),
            )
            ts += 600_000
        con.commit()
        con.close()

        shared = sup.fresh_state()
        sup.STATE_PATH = tmp_path / "state.json"
        notified: list = []
        try:
            monkeypatch.setattr(sup, "notify_cadence_degrading",
                                lambda f, d, r: notified.append(f))
            monkeypatch.setattr(sup, "CADENCE_DEFAULT_DB", db_path)
            monkeypatch.setattr(sup, "feed_silence_contracts",
                                lambda cfg: {"liquidation_okx": 6 * 3600.0})
            monkeypatch.setattr(sup, "write_cadence_report", lambda *a, **k: None)

            assert sup.check_cadence_degrading(shared, force=False) is True
            assert notified == ["liquidation_okx"]
            run = shared["feed_cadence"]["runs"][-1]
            assert run["status"] == "DEGRADING"
            assert run["recent_median_sec"] == 600.0
            assert run["hist_p99_sec"] == 30.0
        finally:
            sup.STATE_PATH = Path("data/research/research_watchdogs_state.json")
