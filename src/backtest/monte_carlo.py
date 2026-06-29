"""Monte Carlo bootstrap for backtest trade lists.

Resamples the closed-trade PnL sequence with replacement (N iterations) and
computes the distribution of profit factor, Sharpe, maximum drawdown, and
final PnL. This gives confidence intervals around point estimates from a
single backtest — far more robust than a single PF/Sharpe number when the
underlying trade count is small (n < 50).

Usage:
    from src.backtest.monte_carlo import bootstrap_metrics

    trades = [{"pnl_usd": 12.3, ...}, ...]
    mc = bootstrap_metrics(trades, initial_capital=10_000.0, n_iter=1000)
    print(mc.pf_p05, mc.pf_median, mc.sharpe_p05)
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class MCMetrics:
    """Bootstrap distribution summary."""
    n_trades: int
    n_iter: int
    # Profit factor
    pf_median: float
    pf_p05: float
    pf_p95: float
    # Sharpe (per-trade returns, annualised by sqrt(252) -- per-trade proxy)
    sharpe_median: float
    sharpe_p05: float
    sharpe_p95: float
    # Max drawdown (% of peak equity)
    max_dd_median: float
    max_dd_p95: float
    # Final PnL
    pnl_median: float
    pnl_p05: float
    pnl_p95: float
    # Probability of profitability (PF > 1 in bootstrap)
    prob_profitable: float

    def as_dict(self) -> Dict[str, float]:
        return {
            "n_trades": self.n_trades,
            "n_iter": self.n_iter,
            "pf_median": self.pf_median,
            "pf_p05": self.pf_p05,
            "pf_p95": self.pf_p95,
            "sharpe_median": self.sharpe_median,
            "sharpe_p05": self.sharpe_p05,
            "sharpe_p95": self.sharpe_p95,
            "max_dd_median": self.max_dd_median,
            "max_dd_p95": self.max_dd_p95,
            "pnl_median": self.pnl_median,
            "pnl_p05": self.pnl_p05,
            "pnl_p95": self.pnl_p95,
            "prob_profitable": self.prob_profitable,
        }


def _metrics_for_sequence(
    pnls: Sequence[float], initial_capital: float
) -> Dict[str, float]:
    """Compute PF, Sharpe, MaxDD, final PnL for an ordered trade sequence."""
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    equity = initial_capital
    peak = equity
    max_dd = 0.0
    returns: List[float] = []
    for p in pnls:
        if equity > 0:
            r = p / equity
        else:
            r = 0.0
        returns.append(r)
        equity += p
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd

    if returns:
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / max(1, len(returns) - 1)
        std_r = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean_r / std_r) * math.sqrt(252.0) if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    final_pnl = sum(pnls)

    return {
        "pf": pf if pf != float("inf") else 99.0,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "pnl": final_pnl,
    }


def bootstrap_metrics(
    trades: List[Dict[str, Any]],
    initial_capital: float = 10_000.0,
    n_iter: int = 1000,
    seed: Optional[int] = 42,
) -> MCMetrics:
    """Bootstrap trade PnL sequence with replacement.

    Args:
        trades: list of trade dicts with "pnl_usd" key.
        initial_capital: starting capital for equity curve / DD computation.
        n_iter: number of bootstrap iterations.
        seed: RNG seed for reproducibility.

    Returns:
        MCMetrics with median + 5/95 percentiles.
    """
    pnls = [float(t.get("pnl_usd", 0.0)) for t in trades]
    n = len(pnls)

    if n == 0:
        return MCMetrics(
            n_trades=0, n_iter=n_iter,
            pf_median=0.0, pf_p05=0.0, pf_p95=0.0,
            sharpe_median=0.0, sharpe_p05=0.0, sharpe_p95=0.0,
            max_dd_median=0.0, max_dd_p95=0.0,
            pnl_median=0.0, pnl_p05=0.0, pnl_p95=0.0,
            prob_profitable=0.0,
        )

    rng = random.Random(seed)
    pfs: List[float] = []
    sharpes: List[float] = []
    max_dds: List[float] = []
    pnls_final: List[float] = []

    for _ in range(n_iter):
        sample = [pnls[rng.randrange(n)] for _ in range(n)]
        m = _metrics_for_sequence(sample, initial_capital)
        pfs.append(m["pf"])
        sharpes.append(m["sharpe"])
        max_dds.append(m["max_dd"])
        pnls_final.append(m["pnl"])

    pfs.sort()
    sharpes.sort()
    max_dds.sort()
    pnls_final.sort()

    def pct(sorted_list: List[float], p: float) -> float:
        if not sorted_list:
            return 0.0
        idx = max(0, min(len(sorted_list) - 1, int(p * (len(sorted_list) - 1))))
        return sorted_list[idx]

    prob_profit = sum(1 for pf in pfs if pf > 1.0) / len(pfs)

    return MCMetrics(
        n_trades=n,
        n_iter=n_iter,
        pf_median=pct(pfs, 0.5),
        pf_p05=pct(pfs, 0.05),
        pf_p95=pct(pfs, 0.95),
        sharpe_median=pct(sharpes, 0.5),
        sharpe_p05=pct(sharpes, 0.05),
        sharpe_p95=pct(sharpes, 0.95),
        max_dd_median=pct(max_dds, 0.5),
        max_dd_p95=pct(max_dds, 0.95),
        pnl_median=pct(pnls_final, 0.5),
        pnl_p05=pct(pnls_final, 0.05),
        pnl_p95=pct(pnls_final, 0.95),
        prob_profitable=prob_profit,
    )
