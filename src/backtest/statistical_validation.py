"""Multiple-testing corrections and overfitting diagnostics (Phase 06).

**INTERNAL RESEARCH PROXIES ONLY** — not full academic implementations.

* ``deflated_sharpe_prob_proxy`` — simplified Bailey-style null adjustment;
  does not implement the complete Deflated Sharpe Ratio paper workflow.
* ``pbo_proxy`` — rank-comparison heuristic across IS/OOS trial scores;
  not Combinatorially Symmetric Cross-Validation (CSCV).

Use for relative ranking inside this codebase.  Do not cite as publication-grade
DSR/PBO without replacing these proxies with a validated implementation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

METHODOLOGY_NOTE = (
    "INTERNAL RESEARCH PROXY — not the full Bailey/López de Prado CSCV "
    "implementation. Use for relative ranking only."
)
DSR_PROXY_LABEL = "deflated_sharpe_prob_proxy"
PBO_PROXY_LABEL = "pbo_proxy"


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def expected_max_sharpe_null(
    n_trials: int,
    n_observations: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Expected maximum Sharpe under the null (multiple independent trials)."""
    if n_trials <= 1 or n_observations < 2:
        return 0.0
    euler_gamma = 0.5772156649
    z = (1.0 - euler_gamma) * _norm_cdf_inv_approx(
        1.0 - 1.0 / float(n_trials)
    ) + euler_gamma * _norm_cdf_inv_approx(1.0 - 1.0 / (float(n_trials) * math.e))
    denom = math.sqrt(max(1.0, n_observations - 1))
    sr0 = z / denom
    adj = 1.0 - skew * sr0 + ((kurtosis - 1.0) / 4.0) * (sr0 ** 2)
    if adj <= 0:
        return sr0
    return sr0 / math.sqrt(adj)


def _norm_cdf_inv_approx(p: float) -> float:
    """Rational approximation for inverse standard normal CDF."""
    p = min(max(p, 1e-12), 1.0 - 1e-12)
    if p < 0.5:
        return -_norm_cdf_inv_approx(1.0 - p)
    t = math.sqrt(-2.0 * math.log(1.0 - p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t)


def deflated_sharpe_prob_proxy(
    observed_sharpe: float,
    n_trials: int,
    n_observations: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Internal DSR proxy — probability Sharpe exceeds null after *n_trials*."""
    if n_observations < 2:
        return 0.0
    sr0 = expected_max_sharpe_null(n_trials, n_observations, skew, kurtosis)
    denom_inner = 1.0 - skew * sr0 + ((kurtosis - 1.0) / 4.0) * (sr0 ** 2)
    if denom_inner <= 0:
        return 0.0
    z = (observed_sharpe - sr0) * math.sqrt(n_observations - 1) / math.sqrt(denom_inner)
    return float(_norm_cdf(z))


def pbo_proxy(
    is_scores: Sequence[float],
    oos_scores: Sequence[float],
) -> float:
    """Internal PBO proxy — fraction of trials where OOS rank worsens vs IS."""
    if len(is_scores) != len(oos_scores) or len(is_scores) < 2:
        return 0.0
    is_arr = np.asarray(is_scores, dtype=float)
    oos_arr = np.asarray(oos_scores, dtype=float)
    is_rank = (-is_arr).argsort().argsort().astype(float)
    oos_rank = (-oos_arr).argsort().argsort().astype(float)
    return float(np.mean(oos_rank > is_rank))


# Backward-compatible aliases (deprecated names).
def deflated_sharpe_ratio(*args, **kwargs) -> float:
    return deflated_sharpe_prob_proxy(*args, **kwargs)


def probability_of_backtest_overfitting(*args, **kwargs) -> float:
    return pbo_proxy(*args, **kwargs)


@dataclass(frozen=True)
class MultipleTestingReport:
    """Summary of trial count and internal proxy significance metrics."""

    n_trials: int
    n_observations: int
    observed_sharpe: float
    deflated_sharpe_prob_proxy: float
    pbo_proxy: float
    is_scores: List[float]
    oos_scores: List[float]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "n_trials": float(self.n_trials),
            "n_observations": float(self.n_observations),
            "observed_sharpe": float(self.observed_sharpe),
            DSR_PROXY_LABEL: float(self.deflated_sharpe_prob_proxy),
            PBO_PROXY_LABEL: float(self.pbo_proxy),
            # Legacy keys retained for callers/tests.
            "deflated_sharpe_prob": float(self.deflated_sharpe_prob_proxy),
            "pbo": float(self.pbo_proxy),
            "methodology": METHODOLOGY_NOTE,
            "is_academic_implementation": False,
        }


def build_multiple_testing_report(
    *,
    n_trials: int,
    n_observations: int,
    observed_sharpe: float,
    is_scores: Optional[Sequence[float]] = None,
    oos_scores: Optional[Sequence[float]] = None,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> MultipleTestingReport:
    """Assemble internal DSR/PBO proxy report for walk-forward / grid-search."""
    is_list = list(is_scores or [])
    oos_list = list(oos_scores or [])
    dsr = deflated_sharpe_prob_proxy(
        observed_sharpe,
        max(1, n_trials),
        max(2, n_observations),
        skew=skew,
        kurtosis=kurtosis,
    )
    pbo = pbo_proxy(is_list, oos_list) if is_list and oos_list else 0.0
    return MultipleTestingReport(
        n_trials=max(1, n_trials),
        n_observations=max(2, n_observations),
        observed_sharpe=float(observed_sharpe),
        deflated_sharpe_prob_proxy=float(dsr),
        pbo_proxy=float(pbo),
        is_scores=is_list,
        oos_scores=oos_list,
    )
