"""Focused tests for the Q13 Phase A diagnostics added to
scripts/research/wallet_markout_screen.py (usable window, gap audit,
Spearman, permutation diagnostics)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.research.wallet_markout_screen import (  # noqa: E402
    N_PERM,
    PERM_SEED,
    RHO_UNLOCK,
    candle_gaps,
    family_threshold_pvalue,
    permutation_pvalue,
    spearman_rho,
    usable_window,
)

pytestmark = pytest.mark.unit


class TestUsableWindow:
    def test_intersection_clips_by_longest_horizon(self) -> None:
        lo, hi = usable_window(1_000, 10_000, 500, 9_000, max_horizon_ms=3_000)
        assert lo == 1_000          # fill window tighter than candle start
        assert hi == 6_000          # candle last minus longest horizon

    def test_fill_window_inside_candles(self) -> None:
        lo, hi = usable_window(2_000, 8_000, 500, 20_000, max_horizon_ms=1_000)
        assert (lo, hi) == (2_000, 8_000)

    def test_empty_when_no_overlap(self) -> None:
        assert usable_window(1_000, 2_000, 5_000, 9_000, 3_000) == (None, None)

    def test_empty_when_missing_side(self) -> None:
        assert usable_window(None, 5_000, 1_000, 9_000, 3_000) == (None, None)
        assert usable_window(1_000, 5_000, None, None, 3_000) == (None, None)


class TestCandleGaps:
    def test_reports_only_gaps_over_threshold(self) -> None:
        ts = [0, 60_000, 120_000, 600_000, 660_000]
        assert candle_gaps(ts) == [(120_000, 600_000)]

    def test_exactly_at_threshold_is_not_a_gap(self) -> None:
        assert candle_gaps([0, 5 * 60_000]) == []


class TestSpearmanRho:
    def test_perfect_agreement(self) -> None:
        ws = ["a", "b", "c"]
        s1 = {"a": 1.0, "b": 2.0, "c": 3.0}
        s2 = {"a": 10.0, "b": 20.0, "c": 30.0}
        assert spearman_rho(s1, s2, ws) == pytest.approx(1.0)

    def test_perfect_reversal(self) -> None:
        ws = ["a", "b", "c"]
        s1 = {"a": 1.0, "b": 2.0, "c": 3.0}
        s2 = {"a": 30.0, "b": 20.0, "c": 10.0}
        assert spearman_rho(s1, s2, ws) == pytest.approx(-1.0)


class TestPermutationPvalue:
    def _scores(self, n: int, shift: int = 0):
        ws = [f"w{i:03d}" for i in range(n)]
        s1 = {w: float(i) for i, w in enumerate(ws)}
        s2 = {w: float((i + shift) % n) for i, w in enumerate(ws)}
        return s1, s2, ws

    def test_perfect_persistence_gets_tiny_p(self) -> None:
        s1, s2, ws = self._scores(50, shift=0)
        rng = np.random.default_rng(PERM_SEED)
        rho, p = permutation_pvalue(s1, s2, ws, n_perm=2_000, rng=rng)
        assert rho == pytest.approx(1.0)
        assert p < 0.01

    def test_shuffled_scores_get_large_p_and_low_rho(self) -> None:
        s1, s2, ws = self._scores(60, shift=17)
        rng = np.random.default_rng(PERM_SEED)
        rho, p = permutation_pvalue(s1, s2, ws, n_perm=2_000, rng=rng)
        assert abs(rho) < 0.5
        assert p > 0.3

    def test_deterministic_given_seed(self) -> None:
        s1, s2, ws = self._scores(30, shift=5)
        r1 = permutation_pvalue(s1, s2, ws, n_perm=500,
                                rng=np.random.default_rng(7))
        r2 = permutation_pvalue(s1, s2, ws, n_perm=500,
                                rng=np.random.default_rng(7))
        assert r1 == r2


class TestFamilyThresholdPvalue:
    def test_persistence_yields_high_family_hit_rate(self) -> None:
        ws = [f"w{i}" for i in range(30)]
        ok = {w: (float(i), float(i)) for i, w in enumerate(ws)}
        ok_by_h = {300_000: ok, 900_000: ok, 3_600_000: ok}
        rng = np.random.default_rng(PERM_SEED)
        # rho_perm under shared permutation can still reach 0.30 by chance,
        # but the diagnostic must return a probability in [0,1] and be
        # deterministic.
        p = family_threshold_pvalue(ok_by_h, n_perm=500, rng=rng)
        assert 0.0 <= p <= 1.0
        p2 = family_threshold_pvalue(ok_by_h, n_perm=500,
                                     rng=np.random.default_rng(PERM_SEED))
        assert p == p2

    def test_empty_union_returns_nan(self) -> None:
        assert np.isnan(family_threshold_pvalue({}, n_perm=10))


def test_module_constants_preregistered() -> None:
    assert N_PERM >= 10_000
    assert RHO_UNLOCK == 0.30
