"""Monte Carlo bootstrap for backtest performance confidence intervals.

Given a list of closed trade PnLs, we resample with replacement
``iterations`` times to estimate the distribution of:
  * total return (sum of PnL per draw)
  * max drawdown (peak-to-trough of equity curve per draw)
  * Sharpe ratio (mean / std of trade returns per draw, no annualization)
  * win rate (fraction of wins per draw)

The 5/25/50/75/95th percentiles of each metric form the confidence
intervals reported back to the caller. We use ``numpy.random.default_rng``
for reproducible draws (seed parameter).

Usage:

    from src.backtest.monte_carlo import run_monte_carlo, MCResult
    result: MCResult = run_monte_carlo(trade_pnls, iterations=10000)
    print(result.total_return_ci_95)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)


# ── Percentile + CI dataclass ───────────────────────────────────────


@dataclass(frozen=True)
class PercentileCI:
    """Confidence interval given by the 5/25/50/75/95th percentiles.

    Lower / upper bound the 90% CI (5%–95%). Median is the 50th
    percentile. P25 and P75 bracket the inter-quartile range.
    """

    p05: float
    p25: float
    p50: float
    p75: float
    p95: float
    mean: float
    std: float
    n: int

    def as_dict(self) -> Dict[str, float]:
        return {
            "p05": float(self.p05),
            "p25": float(self.p25),
            "p50": float(self.p50),
            "p75": float(self.p75),
            "p95": float(self.p95),
            "mean": float(self.mean),
            "std": float(self.std),
            "n": int(self.n),
        }

    @property
    def ci_90(self) -> tuple[float, float]:
        return float(self.p05), float(self.p95)

    @property
    def ci_50(self) -> tuple[float, float]:
        return float(self.p25), float(self.p75)


@dataclass(frozen=True)
class MCResult:
    """Aggregate Monte Carlo result across all four metrics."""

    iterations: int
    seed: int
    n_trades: int
    total_return: PercentileCI
    max_drawdown: PercentileCI
    sharpe: PercentileCI
    win_rate: PercentileCI
    sample_size: int = 0
    extra: Dict[str, PercentileCI] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "iterations": int(self.iterations),
            "seed": int(self.seed),
            "n_trades": int(self.n_trades),
            "sample_size": int(self.sample_size),
            "total_return": self.total_return.as_dict(),
            "max_drawdown": self.max_drawdown.as_dict(),
            "sharpe": self.sharpe.as_dict(),
            "win_rate": self.win_rate.as_dict(),
            "extra": {k: v.as_dict() for k, v in self.extra.items()},
        }

    def summary(self) -> str:
        """Return a one-line per-metric summary string."""
        def fmt(ci: PercentileCI, name: str) -> str:
            return (
                f"{name:<14} p50={ci.p50:>10.4f}  "
                f"90%CI=[{ci.p05:>10.4f}, {ci.p95:>10.4f}]  "
                f"mean={ci.mean:>10.4f} std={ci.std:>10.4f}"
            )
        return "\n".join([
            fmt(self.total_return, "total_return"),
            fmt(self.max_drawdown, "max_drawdown"),
            fmt(self.sharpe, "sharpe"),
            fmt(self.win_rate, "win_rate"),
        ])


# ── Per-draw metric computation ────────────────────────────────────


def _max_drawdown(equity: np.ndarray) -> float:
    """Peak-to-trough drawdown of an equity curve, as a *positive* number.

    A 10% drawdown returns 0.10.
    """
    if equity.size == 0:
        return 0.0
    running_peak = np.maximum.accumulate(equity)
    # Avoid divide-by-zero on the first element (running_peak == 0).
    safe_peak = np.where(running_peak > 0, running_peak, 1.0)
    drawdown = (running_peak - equity) / safe_peak
    return float(drawdown.max())


def _sharpe(returns: np.ndarray) -> float:
    """Per-draw Sharpe (no annualization). Returns 0.0 on degenerate input."""
    if returns.size < 2:
        return 0.0
    std = float(returns.std(ddof=1))
    if std <= 0.0 or not math.isfinite(std):
        return 0.0
    return float(returns.mean() / std)


def _per_draw_metrics(
    pnls: np.ndarray,
    rng: np.random.Generator,
    iterations: int,
) -> Dict[str, np.ndarray]:
    """Run ``iterations`` bootstrap draws. Returns arrays of metric values."""
    n = pnls.size
    if n == 0:
        empty = np.zeros(iterations, dtype=np.float64)
        return {
            "total_return": empty.copy(),
            "max_drawdown": empty.copy(),
            "sharpe": empty.copy(),
            "win_rate": empty.copy(),
        }
    indices = rng.integers(0, n, size=(iterations, n))
    sampled = pnls[indices]  # shape (iterations, n)

    total_return = sampled.sum(axis=1)
    cum_equity = np.cumsum(sampled, axis=1)
    # cum_equity is trade-level PnL running total; max drawdown is
    # peak-to-trough of that curve. Equivalent to the running-PnL
    # version of a peak-to-trough equity calculation.
    running_peak = np.maximum.accumulate(cum_equity, axis=1)
    safe_peak = np.where(running_peak > 0, running_peak, 1.0)
    drawdown = (running_peak - cum_equity) / safe_peak
    max_dd = drawdown.max(axis=1)
    # Per-draw mean / std → Sharpe
    mean = sampled.mean(axis=1)
    std = sampled.std(axis=1, ddof=1)
    sharpe = np.where(std > 1e-12, mean / np.where(std > 1e-12, std, 1.0), 0.0)
    win_rate = (sampled > 0).sum(axis=1) / n

    return {
        "total_return": total_return.astype(np.float64),
        "max_drawdown": max_dd.astype(np.float64),
        "sharpe": sharpe.astype(np.float64),
        "win_rate": win_rate.astype(np.float64),
    }


def _percentile_ci(samples: np.ndarray) -> PercentileCI:
    return PercentileCI(
        p05=float(np.percentile(samples, 5)),
        p25=float(np.percentile(samples, 25)),
        p50=float(np.percentile(samples, 50)),
        p75=float(np.percentile(samples, 75)),
        p95=float(np.percentile(samples, 95)),
        mean=float(samples.mean()) if samples.size else 0.0,
        std=float(samples.std(ddof=1)) if samples.size > 1 else 0.0,
        n=int(samples.size),
    )


# ── Public entry point ─────────────────────────────────────────────


def run_monte_carlo(
    trade_pnls: Sequence[float],
    iterations: int = 10_000,
    seed: int = 0,
) -> MCResult:
    """Bootstrap-resample *trade_pnls* and return CI bands for 4 metrics.

    Parameters
    ----------
    trade_pnls :
        List of closed-trade PnL values (USD). At least 2 trades are
        required for a meaningful bootstrap.
    iterations :
        Number of bootstrap draws (default 10,000). Use 1,000 for a
        quick pass or 50,000 for a final report.
    seed :
        RNG seed (numpy.random.default_rng) — set for reproducible
        results across runs.

    Notes
    -----
    Returns a degenerate :class:`MCResult` (all percentiles = 0.0)
    if there are fewer than 2 trades; this is a deliberate choice so
    downstream callers don't have to special-case empty inputs.
    """
    pnls = np.asarray(list(trade_pnls), dtype=np.float64)
    if pnls.size < 2:
        logger.warning(
            "Monte Carlo bootstrap: %d trades (< 2) — returning degenerate result",
            pnls.size,
        )
        empty = PercentileCI(0, 0, 0, 0, 0, 0, 0, 0)
        return MCResult(
            iterations=0,
            seed=seed,
            n_trades=int(pnls.size),
            total_return=empty,
            max_drawdown=empty,
            sharpe=empty,
            win_rate=empty,
        )

    iters = max(1, int(iterations))
    rng = np.random.default_rng(int(seed))
    metrics = _per_draw_metrics(pnls, rng, iters)

    return MCResult(
        iterations=iters,
        seed=int(seed),
        n_trades=int(pnls.size),
        total_return=_percentile_ci(metrics["total_return"]),
        max_drawdown=_percentile_ci(metrics["max_drawdown"]),
        sharpe=_percentile_ci(metrics["sharpe"]),
        win_rate=_percentile_ci(metrics["win_rate"]),
        sample_size=int(pnls.size),
    )


# ── Optional CLI ───────────────────────────────────────────────────


def _cli(argv: Optional[Iterable[str]] = None) -> int:
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(
        description="Monte Carlo bootstrap of a backtest trade-PnL series",
    )
    p.add_argument(
        "--trades",
        type=int,
        default=100,
        help="Number of synthetic trades to generate (default 100)",
    )
    p.add_argument(
        "--iterations",
        type=int,
        default=10_000,
        help="Number of bootstrap draws (default 10000)",
    )
    p.add_argument(
        "--win-rate",
        type=float,
        default=0.55,
        help="Synthetic win rate (default 0.55)",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=0,
        help="RNG seed (default 0)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print the full MCResult as JSON",
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    # Build a synthetic PnL series with the requested win rate
    rng = np.random.default_rng(args.seed)
    pnls: List[float] = []
    for _ in range(args.trades):
        if rng.random() < args.win_rate:
            pnls.append(float(rng.normal(50.0, 10.0)))
        else:
            pnls.append(float(rng.normal(-30.0, 8.0)))

    result = run_monte_carlo(pnls, iterations=args.iterations, seed=args.seed)
    if args.json:
        json.dump(result.as_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(result.summary())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
