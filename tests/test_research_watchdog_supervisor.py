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
    return {
        "top_trader_bias": {"triggered": False, "runs": []},
        "liquidation_flush": {"triggered": False, "runs": []},
    }


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
