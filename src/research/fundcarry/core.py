"""Pure functions for the funding-carry Phase 1 backtest.

Frozen by notes/fundcarry-prereg-2026-11-21.md (+ operational definitions in
the Phase-1 result note, fixed before the verdict was computed). No I/O, no
network. Reuses the ResMom spec machinery (universe / beta / vol / weights /
percentile / Sharpe) by import — nothing in src/resmom_shadow is modified.
"""
from __future__ import annotations

import hashlib
import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

MS_D = 86_400_000
MS_H = 3_600_000

# ── frozen constants (prereg) ────────────────────────────────────────────────
CAP_PER_COIN = 0.10            # max |weight| per leg as share of GROSS_TOTAL
GROSS_TOTAL = 1.0
SIGNAL_WINDOW_D = 7            # s = -mean(hourly funding, last 7 days)
WINDOW_D = 30                  # non-overlapping windows
MIN_WINDOWS = 4                # < 4 -> INCONCLUSIVE
N_NULLS = 200
NULL_SEED_TAG = "fundcarry-null"   # sha256(f"{tag}|{week_ms}|{i}") as ResMom
SIGNFLIP_EXACT_MAX_K = 12
SIGNFLIP_SAMPLES = 200_000
SIGNFLIP_SEED = 20261121
MIN_FUNDING_OBS = 120          # of 168 hourly points needed for a signal


# ── per-coin cap with proportional redistribution ───────────────────────────
def apply_cap(weights: Dict[str, float], cap: float = CAP_PER_COIN,
              max_iter: int = 1000) -> Dict[str, float]:
    """Cap |w| at `cap` per name; redistribute the excess over the other
    names of the SAME SIDE proportionally to their current |w| (which are
    1/vol-proportional), iterating until nothing exceeds the cap.

    If every name on a side is at the cap the remaining excess cannot be
    placed and is DROPPED (the side's gross falls below its pre-cap value).
    """
    out: Dict[str, float] = {}
    for sign in (1.0, -1.0):
        side = {s: abs(w) for s, w in weights.items()
                if w != 0 and (w > 0) == (sign > 0)}
        capped: set = set()
        for _ in range(max_iter):
            excess = 0.0
            for s in list(side):
                if s not in capped and side[s] > cap + 1e-15:
                    excess += side[s] - cap
                    side[s] = cap
                    capped.add(s)
            if excess <= 1e-15:
                break
            free = [s for s in side if s not in capped]
            tot = sum(side[s] for s in free)
            if not free or tot <= 0:
                break                       # nowhere to put it -> dropped
            for s in free:
                side[s] += excess * side[s] / tot
        for s, w in side.items():
            out[s] = sign * w
    return out


# ── 30-day non-overlapping windows ──────────────────────────────────────────
def split_windows(week_ms: Sequence[int], first_ms: int, last_end_ms: int,
                  days: int = WINDOW_D) -> List[dict]:
    """Assign each weekly rebalance to window floor((t-first)/days).

    A window [first+k*days, first+(k+1)*days) is COMPLETE iff its end is
    <= last_end_ms (the exit time of the last closed week). Returns a list
    of {"k","start_ms","end_ms","idx":[week indices],"complete"}.
    """
    span = days * MS_D
    groups: Dict[int, List[int]] = {}
    for i, t in enumerate(week_ms):
        if t < first_ms:
            continue
        groups.setdefault(int((t - first_ms) // span), []).append(i)
    out = []
    for k in sorted(groups):
        start = first_ms + k * span
        out.append({"k": k, "start_ms": start, "end_ms": start + span,
                    "idx": groups[k],
                    "complete": start + span <= last_end_ms})
    return out


# ── sign-flip test ──────────────────────────────────────────────────────────
def sign_flip_p(values: Sequence[float], seed: int = SIGNFLIP_SEED,
                exact_max_k: int = SIGNFLIP_EXACT_MAX_K,
                n_samples: int = SIGNFLIP_SAMPLES) -> Tuple[float, str]:
    """One-sided p for H0: mean = 0 against mean > 0.

    p = fraction of sign patterns whose sum >= observed (ties count).
    Exact over all 2^K patterns for K <= exact_max_k; otherwise a seeded
    Monte-Carlo with the observed pattern included: (count+1)/(n+1).
    """
    v = np.asarray(values, dtype=float)
    k = len(v)
    obs = float(v.sum())
    tol = 1e-12 * max(1.0, float(np.abs(v).sum()))
    if k == 0:
        return 1.0, "empty"
    if k <= exact_max_k:
        n = 1 << k
        bits = ((np.arange(n)[:, None] >> np.arange(k)[None, :]) & 1)
        sums = ((2.0 * bits - 1.0) * v[None, :]).sum(axis=1)
        return float((sums >= obs - tol).sum()) / n, "exact"
    rng = np.random.default_rng(seed)
    cnt = 0
    done = 0
    chunk = 20_000
    while done < n_samples:
        m = min(chunk, n_samples - done)
        sg = rng.integers(0, 2, size=(m, k)) * 2.0 - 1.0
        cnt += int(((sg * v[None, :]).sum(axis=1) >= obs - tol).sum())
        done += m
    return (cnt + 1) / (n_samples + 1), "monte_carlo"


# ── signal ──────────────────────────────────────────────────────────────────
def carry_score(events: Sequence[Tuple[int, float]], cutoff_ms: int,
                window_d: int = SIGNAL_WINDOW_D,
                min_obs: int = MIN_FUNDING_OBS) -> Optional[float]:
    """s = -mean(hourly funding in [cutoff-window_d, cutoff)); None if the
    window holds fewer than `min_obs` observations. `events` = (ts_ms, rate)
    sorted by ts."""
    t0 = cutoff_ms - window_d * MS_D
    rates = [r for t, r in events if t0 <= t < cutoff_ms]
    if len(rates) < min_obs:
        return None
    return -float(sum(rates) / len(rates))


def null_seed(week_ms: int, i: int) -> str:
    return hashlib.sha256(
        f"{NULL_SEED_TAG}|{week_ms}|{i}".encode()).hexdigest()


def spearman(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    if len(a) < 3:
        return None
    from scipy.stats import spearmanr
    r = spearmanr(a, b).statistic
    return None if r is None or not math.isfinite(r) else float(r)
