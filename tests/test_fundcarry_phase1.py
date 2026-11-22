"""Unit tests for src/research/fundcarry/core.py (funding-carry Phase 1)."""
from __future__ import annotations

import pytest

from src.research.fundcarry import core

MS_D, MS_H = core.MS_D, core.MS_H
pytestmark = pytest.mark.unit


# ── 10% cap with proportional redistribution ────────────────────────────────
def test_cap_noop_when_under_cap():
    w = {"A": 0.1, "B": 0.09, "C": -0.08}
    assert core.apply_cap(w) == pytest.approx(w)


def test_cap_redistributes_to_same_side_proportionally():
    w = {"A": 0.20, "B": 0.05, "C": 0.05, "S1": -0.30, "S2": -0.10}
    out = core.apply_cap(w, cap=0.10)
    assert out["A"] == pytest.approx(0.10)
    # excess 0.10 split B:C = 1:1 -> +0.05 each, hits cap exactly
    assert out["B"] == pytest.approx(0.10) and out["C"] == pytest.approx(0.10)
    assert out["S1"] == pytest.approx(-0.10) and out["S2"] == pytest.approx(-0.10)
    # long side excess does not leak into shorts and vice versa
    assert sum(v for v in out.values() if v > 0) == pytest.approx(0.30)


def test_cap_iterates_when_redistribution_exceeds_again():
    w = {"a": 0.20, "b": 0.09, "c": 0.05, "d": 0.04, "e": 0.02}
    out = core.apply_cap(w, cap=0.10)
    assert all(abs(v) <= 0.10 + 1e-12 for v in out.values())
    assert sum(out.values()) == pytest.approx(0.40)       # mass preserved
    assert out["a"] == pytest.approx(0.10) and out["b"] == pytest.approx(0.10)
    # c,d,e keep their pre-redistribution ratio 0.05:0.04:0.02 on the leftover
    r = out["c"] / out["d"], out["d"] / out["e"]
    assert r[0] == pytest.approx(0.05 / 0.04) and r[1] == pytest.approx(2.0)


def test_cap_drops_unplaceable_excess_when_all_at_cap():
    out = core.apply_cap({"a": 0.30, "b": 0.30, "c": -0.05}, cap=0.10)
    assert out["a"] == pytest.approx(0.10) and out["b"] == pytest.approx(0.10)
    assert out["c"] == pytest.approx(-0.05)               # short side untouched


# ── 30-day non-overlapping windows ──────────────────────────────────────────
def test_windows_non_overlapping_and_complete_flag():
    first = 1_700_000_000_000 // MS_D * MS_D
    weeks = [first + 7 * i * MS_D for i in range(14)]       # 14 weeks = 98 d
    last_end = weeks[-1] + 7 * MS_D                          # day 98
    wins = core.split_windows(weeks, first, last_end)
    seen = [i for w in wins for i in w["idx"]]
    assert sorted(seen) == list(range(14))                    # each week once
    assert len(seen) == len(set(seen))
    for w in wins:
        assert w["end_ms"] - w["start_ms"] == 30 * MS_D
        for i in w["idx"]:
            assert w["start_ms"] <= weeks[i] < w["end_ms"]
    # windows are contiguous, start at the first rebalance
    assert wins[0]["start_ms"] == first
    for a, b in zip(wins, wins[1:]):
        assert b["start_ms"] == a["end_ms"]
    # 98 d -> 3 complete windows (90 d) + one incomplete
    assert [w["complete"] for w in wins] == [True, True, True, False]


# ── sign-flip test ──────────────────────────────────────────────────────────
def test_sign_flip_exact_small():
    p, m = core.sign_flip_p([1.0, 1.0, 1.0])
    assert m == "exact" and p == pytest.approx(1 / 8)
    p, _ = core.sign_flip_p([1.0, -1.0])         # sums: 2,0,0,-2 ; obs 0
    assert p == pytest.approx(3 / 4)             # ties count


def test_sign_flip_symmetry_and_range():
    p_pos, _ = core.sign_flip_p([0.3, 0.1, 0.2, 0.05])
    p_neg, _ = core.sign_flip_p([-0.3, -0.1, -0.2, -0.05])
    assert p_pos == pytest.approx(1 / 16)
    assert p_neg == pytest.approx(1.0)
    assert 0 < p_pos < p_neg <= 1


def test_sign_flip_monte_carlo_seeded_and_close_to_exact():
    vals = [0.05, 0.02, -0.01, 0.04, 0.03, 0.01, 0.02, 0.06, -0.02, 0.03,
            0.01, 0.02, 0.04]                          # K=13 -> sampled
    p1, m1 = core.sign_flip_p(vals, n_samples=40_000)
    p2, _ = core.sign_flip_p(vals, n_samples=40_000)
    assert m1 == "monte_carlo" and p1 == p2           # seeded -> repeatable
    pe, me = core.sign_flip_p(vals, exact_max_k=13)    # exact reference
    assert me == "exact" and abs(p1 - pe) < 0.01


# ── signal ──────────────────────────────────────────────────────────────────
def _hourly(start, hours, rate_fn):
    return [(start + h * MS_H + 48, rate_fn(h)) for h in range(hours)]


def test_carry_score_is_minus_mean_of_last_7d():
    cutoff = 1_700_000_000_000 // MS_D * MS_D
    ev = _hourly(cutoff - 14 * MS_D, 14 * 24, lambda h: 0.0001 if h < 7 * 24
                 else 0.0003)            # old 7d low, last 7d high
    assert core.carry_score(ev, cutoff) == pytest.approx(-0.0003)


def test_carry_score_excludes_cutoff_and_future_and_needs_obs():
    cutoff = 1_700_000_000_000 // MS_D * MS_D
    ev = _hourly(cutoff - 7 * MS_D, 7 * 24, lambda h: 0.0002)
    ev += [(cutoff, 9.0), (cutoff + MS_H, 9.0)]          # not before cutoff
    assert core.carry_score(ev, cutoff) == pytest.approx(-0.0002)
    assert core.carry_score(ev[:50], cutoff) is None       # < 120 obs


def test_negative_funding_gives_positive_score_long_candidate():
    cutoff = 1_700_000_000_000 // MS_D * MS_D
    ev = _hourly(cutoff - 7 * MS_D, 7 * 24, lambda h: -0.0004)
    assert core.carry_score(ev, cutoff) > 0
