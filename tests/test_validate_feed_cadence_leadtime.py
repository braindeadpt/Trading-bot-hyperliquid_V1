"""Tests for the cadence lead-time validator (scripts/validate_feed_cadence_leadtime.py).

The walk-forward simulation must reproduce the production rule exactly
(shared ``cadence_percentile``) and answer the operational question:
does ``FEED CADENCE`` anticipate the 6h silence, and by how much?
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_feed_cadence_leadtime import (  # noqa: E402
    cross_check_with_real,
    simulate_cadence_fires,
)
from src.data.market_data_health import (  # noqa: E402
    FeedSilenceState,
    cadence_percentile,
)

H = 3600.0
MIN_SAMPLES = 100
GAP_HISTORY = 4000
MAX_SIL = 6 * H  # liquidation_okx 6h threshold


def _ts(secs: List[float], start: float = 1_000_000.0) -> List[int]:
    """Event timestamps from cumulative gaps (sec): first arg is the gap
    to the NEXT event."""
    ts = [int(start)]
    for g in secs:
        ts.append(ts[-1] + int(g * 1000))
    return ts


class TestCadencePercentileShared:
    pytestmark = pytest.mark.unit

    """cadence_percentile is the single source of truth: the monitor state
    delegates to it, so the validator measures the production rule."""

    def test_module_function_matches_state_method(self) -> None:
        st = FeedSilenceState(feed="x")
        st.gaps.extend([30.0, 45.0, 60.0, 90.0, 120.0])
        assert cadence_percentile(st.gaps, 0.99, 5) == 120.0
        assert st.cadence_percentile_sec(0.99, 5) == 120.0

    def test_nearest_rank_p99(self) -> None:
        gaps = [float(i) for i in range(1, 201)]  # 1..200
        # nearest-rank: idx = int(0.99 * 200) = 198 -> ordered[198] = 199
        assert cadence_percentile(gaps, 0.99, 100) == 199.0

    def test_none_below_min_samples(self) -> None:
        assert cadence_percentile([30.0] * 50, 0.99, 100) is None


class TestSimulateCadenceFires:
    pytestmark = pytest.mark.unit

    def test_cold_start_degrades_without_fire(self) -> None:
        """A 6h+ gap with fewer than min_samples recorded -> MISS (the
        detector was still learning — the honest answer)."""
        ts = _ts([30.0, 45.0] + [MAX_SIL + 60.0])
        rep = simulate_cadence_fires(
            ts, min_samples=MIN_SAMPLES, gap_history=GAP_HISTORY,
            max_silence_sec=MAX_SIL, now_ms=ts[-1] + 60_000,
        )
        assert rep["gaps"] == 3
        assert rep["fires_total"] == 0
        assert rep["degradations"] == 1
        assert rep["misses"] == 1
        assert rep["anticipated"] == 0

    def test_steady_cadence_no_fires_no_degradation(self) -> None:
        ts = _ts([30.0] * 150)
        rep = simulate_cadence_fires(
            ts, min_samples=MIN_SAMPLES, gap_history=GAP_HISTORY,
            max_silence_sec=MAX_SIL, now_ms=ts[-1] + 30_000,
        )
        assert rep["gaps"] == 150
        assert rep["fires_total"] == 0
        assert rep["degradations"] == 0

    def test_gap_over_p99_recovers_is_unconfirmed(self) -> None:
        """A gap above the rolling p99 that ends before 6h is a warning
        without a real silence (unconfirmed fire)."""
        ts = _ts([30.0] * 120 + [600.0] + [30.0] * 5)
        rep = simulate_cadence_fires(
            ts, min_samples=MIN_SAMPLES, gap_history=GAP_HISTORY,
            max_silence_sec=MAX_SIL, now_ms=ts[-1] + 30_000,
        )
        assert rep["fires_total"] == 1
        assert rep["unconfirmed"] == 1
        assert rep["confirmed"] == 0
        assert rep["degradations"] == 0

    def test_gap_over_p99_reaching_6h_confirmed_with_lead(self) -> None:
        """A gap that crosses the p99 AND reaches 6h: the cadence fire
        precedes FEED SILENT by (6h - p99) — the lead time."""
        ts = _ts([30.0] * 120 + [MAX_SIL + 60.0])
        rep = simulate_cadence_fires(
            ts, min_samples=MIN_SAMPLES, gap_history=GAP_HISTORY,
            max_silence_sec=MAX_SIL, now_ms=ts[-1] + 60_000,
        )
        assert rep["degradations"] == 1
        assert rep["anticipated"] == 1
        assert rep["misses"] == 0
        f = rep["fires"][0]
        assert f["confirmed"] is True
        assert f["p99_sec"] == 30.0  # p99 of 120 uniform 30s gaps
        assert f["lead_sec"] == pytest.approx(MAX_SIL - 30.0)
        assert rep["avg_lead_sec"] == pytest.approx(MAX_SIL - 30.0)

    def test_fire_time_is_start_plus_p99(self) -> None:
        ts = _ts([30.0] * 120 + [MAX_SIL + 60.0])
        rep = simulate_cadence_fires(
            ts, min_samples=MIN_SAMPLES, gap_history=GAP_HISTORY,
            max_silence_sec=MAX_SIL, now_ms=ts[-1] + 60_000,
        )
        f = rep["fires"][0]
        # gap index 120 -> started at ts[120]; fire 30s into it
        assert f["gap_index"] == 120
        assert f["start_ms"] == ts[120]
        assert f["fired_at_ms"] == ts[120] + 30_000

    def test_open_gap_ongoing_degradation(self) -> None:
        """The open tail (last event -> now) is evaluated: a 6h+ open gap
        counts as an in-progress degradation with a lead."""
        ts = _ts([30.0] * 120)
        now = ts[-1] + int((MAX_SIL + 60.0) * 1000)
        rep = simulate_cadence_fires(
            ts, min_samples=MIN_SAMPLES, gap_history=GAP_HISTORY,
            max_silence_sec=MAX_SIL, now_ms=now,
        )
        assert rep["degradations"] == 1
        assert rep["anticipated"] == 1
        assert rep["fires"][0]["confirmed"] is True

    def test_gap_history_cap_excludes_old_spikes(self) -> None:
        """With a small gap_history, the old 30s baseline falls out of the
        window so the burst's p99 (500s) is seen sooner — the burst stops
        re-firing. With a long history the spike takes ~1% of samples to
        move the nearest-rank p99, so every burst gap fires."""
        ts = _ts([30.0] * 100 + [500.0] * 5 + [60.0])
        rep = simulate_cadence_fires(
            ts, min_samples=10, gap_history=10,
            max_silence_sec=MAX_SIL, now_ms=ts[-1] + 60_000,
        )
        # only the first burst gap fires off the stale 30s baseline; the
        # recent window (p99=500) silences the rest of the burst + the tail
        assert rep["fires_total"] == 1
        rep2 = simulate_cadence_fires(
            ts, min_samples=10, gap_history=4000,
            max_silence_sec=MAX_SIL, now_ms=ts[-1] + 60_000,
        )
        # long history: 2 spikes are needed to move the nearest-rank p99
        # above 30 -> two burst gaps fire before the p99 catches up
        assert rep2["fires_total"] == 2

    def test_negative_gap_clamped(self) -> None:
        """Out-of-order/duplicate timestamps (clock skew) clamp to 0-gaps,
        matching the monitor's 'record only gaps >= 0' contract."""
        ts = [1_000_000, 1_000_030_000, 1_000_000_000]  # second event earlier
        rep = simulate_cadence_fires(
            ts, min_samples=1, gap_history=10,
            max_silence_sec=MAX_SIL, now_ms=ts[-1] + 30_000,
        )
        assert rep["gaps"] == 2
        assert rep["fires_total"] == 0


class TestCrossCheckWithReal:
    pytestmark = pytest.mark.unit

    def test_matches_real_alert_to_simulated_fire(self) -> None:
        ts = _ts([30.0] * 120 + [MAX_SIL + 60.0])
        rep = simulate_cadence_fires(
            ts, min_samples=MIN_SAMPLES, gap_history=GAP_HISTORY,
            max_silence_sec=MAX_SIL, now_ms=ts[-1] + 60_000,
        )
        real = [rep["fires"][0]["fired_at_ms"]]
        cc = cross_check_with_real(rep, real, tolerance_sec=900.0)
        assert cc["real_alerts"] == 1
        assert cc["matched_real"] == 1
        assert cc["matched_sim"] == 1

    def test_real_alert_outside_tolerance_not_matched(self) -> None:
        ts = _ts([30.0] * 120 + [MAX_SIL + 60.0])
        rep = simulate_cadence_fires(
            ts, min_samples=MIN_SAMPLES, gap_history=GAP_HISTORY,
            max_silence_sec=MAX_SIL, now_ms=ts[-1] + 60_000,
        )
        far = rep["fires"][0]["fired_at_ms"] + int(3 * 3600 * 1000)
        cc = cross_check_with_real(rep, [far], tolerance_sec=900.0)
        assert cc["matched_real"] == 0

    def test_empty_sim_or_real(self) -> None:
        cc = cross_check_with_real({}, [])
        assert cc["real_alerts"] == 0
        assert cc["matched_real"] == 0
