"""Signal + portfolio math — Arm A frozen (prereg §Regra); Arm B descriptive.

Conventions (canonical readings of the frozen text, pinned here):
  * daily log returns  r_t = ln(C_t / C_{t-1})  at 00:00 UTC closes.
  * beta_i      = cov(r_i, r_btc) / var(r_btc)  over the last BETA_WINDOW_D
                returns (OLS slope with intercept).
  * resid15_i   = sum of the last RESID_WINDOW_D daily residuals
                (r_i - beta_i * r_btc).
  * vol30_i     = sample std (ddof=1) of the last VOL30_WINDOW_D daily
                log returns.
  * signal s_i  = resid15_i / vol30_i.
  * legs        = top / bottom floor(QUINTILE_FRAC * n) of the universe
                ranked by s, BTC excluded from legs only.
  * weights     = (1/vol30)-normalized inside each leg; leg gross solves
                beta-neutrality vs BTC with GROSS_TOTAL = 1.0:
                g_long * B_long = g_short * B_short. If a leg's unit-beta
                is <= BETA_EPS the solve is impossible -> GROSS_LEG/2 split
                and flag 'beta_neutral_unsolvable'.
  * Arm B score = mean(pct_rank(s_A), pct_rank(-vol30)) — pct_rank is the
                fractional rank in [0,1] (lowest=0, highest=1).

All functions are pure/numpy; inputs are per-symbol aligned close arrays.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from src.resmom_shadow import spec


def log_returns(closes: np.ndarray) -> np.ndarray:
    c = np.asarray(closes, dtype=float)
    c = c[np.isfinite(c) & (c > 0)]
    if len(c) < 2:
        return np.array([], dtype=float)
    return np.diff(np.log(c))


def ols_beta(r_i: np.ndarray, r_b: np.ndarray) -> Optional[float]:
    """OLS slope of r_i on r_b over the last BETA_WINDOW_D points."""
    n = spec.BETA_WINDOW_D
    if len(r_i) < n or len(r_b) < n:
        return None
    x = np.asarray(r_b[-n:], dtype=float)
    y = np.asarray(r_i[-n:], dtype=float)
    if not (np.all(np.isfinite(x)) and np.all(np.isfinite(y))):
        return None
    xd = x - x.mean()
    denom = float((xd * xd).sum())
    if denom <= 0:
        return None
    return float((xd * (y - y.mean())).sum() / denom)


def resid15(r_i: np.ndarray, r_b: np.ndarray, beta: float) -> Optional[float]:
    """Sum of the last RESID_WINDOW_D residuals r_i - beta*r_b."""
    n = spec.RESID_WINDOW_D
    if len(r_i) < n or len(r_b) < n:
        return None
    resid = np.asarray(r_i[-n:]) - beta * np.asarray(r_b[-n:])
    return float(resid.sum())


def vol30(r_i: np.ndarray) -> Optional[float]:
    if len(r_i) < spec.VOL30_WINDOW_D:
        return None
    v = float(np.std(r_i[-spec.VOL30_WINDOW_D:], ddof=1))
    return v if np.isfinite(v) else None


def pct_ranks(values: Dict[str, float]) -> Dict[str, float]:
    """Fractional rank in [0,1] — lowest 0.0, highest 1.0, ties share rank."""
    keys = sorted(values)
    if len(keys) <= 1:
        return {k: 0.5 for k in keys}
    order = sorted(keys, key=lambda k: values[k])
    ranks: Dict[str, float] = {}
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = (i + j) / 2.0 / (len(order) - 1)
        for k in order[i:j + 1]:
            ranks[k] = r
        i = j + 1
    return ranks


@dataclass
class SymbolSignal:
    symbol: str
    beta: float
    resid15: float
    vol30: float
    s: float


@dataclass
class Leg:
    symbols: List[str] = field(default_factory=list)
    weights: Dict[str, float] = field(default_factory=dict)   # 1/vol-normalized (sum 1)
    unit_beta: float = 0.0                                     # Σ w·beta


@dataclass
class Portfolio:
    """Signed gross weights: longs positive, shorts negative, sum|w| = 1."""
    weights: Dict[str, float]
    gross_long: float
    gross_short: float
    flags: List[str] = field(default_factory=list)


def compute_signals(returns: Dict[str, np.ndarray],
                    r_btc: np.ndarray) -> Dict[str, SymbolSignal]:
    """s_i per symbol — None-able inputs already filtered upstream."""
    out: Dict[str, SymbolSignal] = {}
    for sym, r_i in returns.items():
        b = ols_beta(r_i, r_btc)
        if b is None:
            continue
        r15 = resid15(r_i, r_btc, b)
        v = vol30(r_i)
        if r15 is None or v is None or v <= 0:
            continue
        out[sym] = SymbolSignal(sym, b, r15, v, r15 / v)
    return out


def arm_b_scores(signals: Dict[str, SymbolSignal]) -> Dict[str, float]:
    """Braço B: mean of s_A's pct rank and -vol30's pct rank (descriptive)."""
    s_a = {k: v.s for k, v in signals.items()}
    neg_v = {k: -v.vol30 for k, v in signals.items()}
    ra, rv = pct_ranks(s_a), pct_ranks(neg_v)
    return {k: (ra[k] + rv[k]) / 2.0 for k in s_a}


def _leg(symbols: List[str], signals: Dict[str, SymbolSignal]) -> Leg:
    leg = Leg(symbols=list(symbols))
    inv = {s: 1.0 / signals[s].vol30 for s in symbols}
    tot = sum(inv.values())
    leg.weights = {s: inv[s] / tot for s in symbols} if tot > 0 else {}
    leg.unit_beta = sum(leg.weights[s] * signals[s].beta for s in symbols)
    return leg


def build_portfolio(ranked: List[str], signals: Dict[str, SymbolSignal],
                    n_universe: int) -> Portfolio:
    """ranked: symbols sorted by score desc (BTC already excluded here).

    n_universe: size of the eligible universe *including* BTC — the
    quintile floor uses it (floor(0.2*n), n=25 -> 5).
    """
    n_legs = max(1, int(spec.QUINTILE_FRAC * n_universe))
    n_legs = min(n_legs, len(ranked) // 2) or 1
    longs = [s for s in ranked[:n_legs]]
    shorts = [s for s in ranked[::-1][:n_legs]]
    leg_l, leg_s = _leg(longs, signals), _leg(shorts, signals)

    flags: List[str] = []
    bl, bs = leg_l.unit_beta, leg_s.unit_beta
    # beta-neutral needs g_L*BL = g_S*BS — solvable only when both unit
    # betas share a sign (same sign: g_L = BS/(BL+BS) lands in (0,1);
    # mixed signs can never cancel) and the leg betas are non-degenerate
    solvable = (bl * bs > 0
                and abs(bl + bs) > spec.BETA_EPS)
    if solvable:
        g_l = abs(bs / (bl + bs)) * spec.GROSS_TOTAL
        g_s = spec.GROSS_TOTAL - g_l
    else:
        g_l = g_s = spec.GROSS_TOTAL * spec.GROSS_LEG
        flags.append("beta_neutral_unsolvable")

    w: Dict[str, float] = {}
    for s, wl in leg_l.weights.items():
        w[s] = wl * g_l
    for s, ws in leg_s.weights.items():
        w[s] = w.get(s, 0.0) - ws * g_s
    return Portfolio(weights=w, gross_long=g_l, gross_short=g_s, flags=flags)


def weights_equal(a: Dict[str, float], b: Dict[str, float],
                  tol: float = 1e-9) -> bool:
    """Parity check helper — same symbol set, weights within tol."""
    if set(a) != set(b):
        return False
    return all(abs(a[k] - b[k]) <= tol for k in a)
