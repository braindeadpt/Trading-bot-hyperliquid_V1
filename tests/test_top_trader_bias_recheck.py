"""Unit tests for scripts/top_trader_bias_recheck.py.

Pins the ≥20-date trigger contract so the auto re-run gate is exercised
before the tracker actually accumulates 20 dates.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.top_trader_bias_recheck import (  # noqa: E402
    TARGET_DATES,
    best_candidate,
    bias_date_count,
    load_state,
    save_state,
    verdict,
)


def _make_db(path: Path, ts_ms: list) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE top_trader_bias_samples (timestamp_ms INTEGER NOT NULL)")
    conn.executemany(
        "INSERT INTO top_trader_bias_samples (timestamp_ms) VALUES (?)",
        [(int(t),) for t in ts_ms],
    )
    conn.commit()
    conn.close()


def _cell(feature="tt_bias_level", horizon="24h", ic=0.01, survives=False,
          is_control=False, **extra) -> dict:
    d = {
        "feature": feature, "horizon": horizon, "ic": ic, "survives": survives,
        "is_control": is_control, "p_raw": 0.01, "n_dates": 20, "fdr_reject": survives,
    }
    d.update(extra)
    return d


class TestBiasDateCount:
    def test_counts_distinct_utc_dates(self, tmp_path):
        day = 86_400_000
        db = tmp_path / "bias.db"
        # 3 distinct UTC days, several samples each.
        _make_db(db, [day, day + 60_000, 2 * day, 2 * day + 60_000, 3 * day])
        n_dates, n_samples, mn, mx = bias_date_count(db=db)
        assert n_dates == 3
        assert n_samples == 5
        assert mn == day
        assert mx == 3 * day

    def test_missing_db_returns_zeroes(self, tmp_path):
        n_dates, n_samples, mn, mx = bias_date_count(db=tmp_path / "nope.db")
        assert (n_dates, n_samples, mn, mx) == (0, 0, None, None)


class TestVerdict:
    def test_pass_when_candidate_survives(self):
        v = verdict([_cell(survives=True)], n_dates=20)
        assert v.startswith("GATE PASS")

    def test_fail_when_no_survivor_at_target_dates(self):
        v = verdict([_cell(survives=False)], n_dates=20)
        assert v.startswith("GATE FAIL")

    def test_inconclusive_below_target_dates(self):
        v = verdict([_cell(survives=True)], n_dates=19)
        assert v.startswith("INCONCLUSIVE")

    def test_controls_ignored(self):
        # only the leaky control survives — must not count as a pass
        v = verdict([_cell(feature="control", is_control=True, survives=True)], n_dates=20)
        assert v.startswith("GATE FAIL")


class TestBestCandidate:
    def test_ignores_controls(self):
        cells = [
            _cell(feature="ctrl", is_control=True, ic=0.9),
            _cell(feature="tt_bias_delta_1h", ic=0.03),
            _cell(feature="tt_bias_level", ic=0.02),
        ]
        best = best_candidate(cells)
        assert best is not None and best["feature"] == "tt_bias_delta_1h"

    def test_empty_returns_none(self):
        assert best_candidate([]) is None


class TestStateRoundtrip:
    def test_save_then_load(self, tmp_path):
        p = tmp_path / "state.json"
        state = {"triggered": True, "runs": [{"ts": "t", "verdict": "GATE PASS"}]}
        save_state(state, path=p)
        assert load_state(path=p) == state

    def test_load_missing_returns_fresh(self, tmp_path):
        assert load_state(path=tmp_path / "missing.json") == {"triggered": False, "runs": []}


class TestJsonContract:
    """The probe's machine output must feed the recheck verdict unchanged."""

    def test_probe_json_feeds_verdict(self, tmp_path):
        from scripts.feature_screening_top_trader_bias import write_json
        from scripts.top_trader_bias_recheck import load_result

        cells = [
            _cell(feature="tt_bias_delta_1h", ic=0.05, survives=True),
            _cell(feature="tt_bias_level", ic=0.01, survives=False),
        ]
        meta = {"n_dates": 20, "n_bias": 100}
        out = tmp_path / "probe.json"
        write_json(cells, meta, out)

        data = load_result(out)
        assert data["meta"]["n_dates"] == 20
        assert verdict(data["cells"], data["meta"]["n_dates"]).startswith("GATE PASS")

    def test_nan_coerced_to_null(self, tmp_path):
        from scripts.feature_screening_top_trader_bias import write_json

        cells = [_cell(ic=float("nan"))]
        out = tmp_path / "nan.json"
        write_json(cells, {"n_dates": 20}, out)
        import json
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["cells"][0]["ic"] is None


class TestTargetContract:
    def test_target_is_20_dates(self):
        assert TARGET_DATES == 20
