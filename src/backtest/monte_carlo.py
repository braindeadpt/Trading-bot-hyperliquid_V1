"""Monte Carlo bootstrap for backtest trade lists.

Resamples closed-trade PnL sequences with replacement (IID) or by calendar-day
blocks (preserving intraday serial dependence).  Block bootstrap is preferred
for walk-forward / OOS validation when trade outcomes cluster by day or regime.
"""
from __future__ import annotations

import math
import random
from src.backtest.metrics import CRYPTO_MS_PER_YEAR
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple


@dataclass
class MCMetrics:
    """Bootstrap distribution summary."""
    n_trades: int
    n_iter: int
    bootstrap_mode: str = "iid"
    # Profit factor
    pf_median: float = 0.0
    pf_p05: float = 0.0
    pf_p95: float = 0.0
    # Sharpe (per-trade returns, crypto-calendar annualisation)
    sharpe_median: float = 0.0
    sharpe_p05: float = 0.0
    sharpe_p95: float = 0.0
    # Max drawdown (% of peak equity)
    max_dd_median: float = 0.0
    max_dd_p95: float = 0.0
    # Final PnL
    pnl_median: float = 0.0
    pnl_p05: float = 0.0
    pnl_p95: float = 0.0
    # Probability of profitability (PF > 1 in bootstrap)
    prob_profitable: float = 0.0
    block_count: int = 0

    def as_dict(self) -> Dict[str, float]:
        return {
            "n_trades": self.n_trades,
            "n_iter": self.n_iter,
            "bootstrap_mode": self.bootstrap_mode,
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
            "block_count": float(self.block_count),
        }

def _trade_exit_day(trade: Dict[str, Any]) -> str:
    """UTC calendar day for block grouping."""
    ts = int(trade.get("exit_time", trade.get("entry_time", 0)))
    return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _trade_regime_key(trade: Dict[str, Any]) -> Optional[str]:
    """Return a named regime key when metadata carries a valid regime label."""
    meta = trade.get("metadata") or {}
    regime = meta.get("regime") or meta.get("adx_regime")
    if regime is None:
        return None
    label = str(regime).strip().lower()
    if not label or label in {"none", "null", "nan"}:
        return None
    return f"regime:{label}"


def group_trades_into_blocks(
    trades: Sequence[Dict[str, Any]],
    mode: Literal["day", "regime"] = "day",
) -> List[List[float]]:
    """Group trade PnLs into non-overlapping blocks.

    * ``day`` — contiguous UTC calendar days (default; always preserves
      temporal contiguity).
    * ``regime`` — group by valid regime metadata; trades without a valid
      regime fall back to UTC day blocks (never weekday buckets).
    """
    buckets: Dict[str, List[float]] = defaultdict(list)
    for t in trades:
        if mode == "regime":
            key = _trade_regime_key(t) or f"day:{_trade_exit_day(t)}"
        else:
            key = f"day:{_trade_exit_day(t)}"
        buckets[key].append(float(t.get("pnl_usd", 0.0)))
    return list(buckets.values())


def _resample_blocks(
    blocks: Sequence[Sequence[float]],
    n_trades: int,
    rng: random.Random,
) -> List[float]:
    """Sample whole blocks with replacement until *n_trades* PnLs collected."""
    if not blocks:
        return []
    flat_blocks = [list(b) for b in blocks]
    sample: List[float] = []
    while len(sample) < n_trades:
        block = flat_blocks[rng.randrange(len(flat_blocks))]
        sample.extend(block)
    return sample[:n_trades]


def _empty_mc(n_iter: int, mode: str = "iid") -> MCMetrics:
    return MCMetrics(
        n_trades=0, n_iter=n_iter, bootstrap_mode=mode,
        pf_median=0.0, pf_p05=0.0, pf_p95=0.0,
        sharpe_median=0.0, sharpe_p05=0.0, sharpe_p95=0.0,
        max_dd_median=0.0, max_dd_p95=0.0,
        pnl_median=0.0, pnl_p05=0.0, pnl_p95=0.0,
        prob_profitable=0.0, block_count=0,
    )


def _pct(sorted_list: List[float], p: float) -> float:
    if not sorted_list:
        return 0.0
    idx = max(0, min(len(sorted_list) - 1, int(p * (len(sorted_list) - 1))))
    return sorted_list[idx]


def _run_bootstrap_iterations(
    pnls: Sequence[float],
    sampler,
    initial_capital: float,
    n_iter: int,
    mode: str,
    block_count: int,
    *,
    trades: Optional[Sequence[Dict[str, Any]]] = None,
) -> MCMetrics:
    pfs: List[float] = []
    sharpes: List[float] = []
    max_dds: List[float] = []
    pnls_final: List[float] = []
    n = len(pnls)
    ann = _trade_annualization_factor(trades or [], n)

    for _ in range(n_iter):
        sample = sampler()
        m = _metrics_for_sequence(sample, initial_capital, annualization_factor=ann)
        pfs.append(m["pf"])
        sharpes.append(m["sharpe"])
        max_dds.append(m["max_dd"])
        pnls_final.append(m["pnl"])

    pfs.sort()
    sharpes.sort()
    max_dds.sort()
    pnls_final.sort()
    prob_profit = sum(1 for pf in pfs if pf > 1.0) / len(pfs)

    return MCMetrics(
        n_trades=n,
        n_iter=n_iter,
        bootstrap_mode=mode,
        pf_median=_pct(pfs, 0.5),
        pf_p05=_pct(pfs, 0.05),
        pf_p95=_pct(pfs, 0.95),
        sharpe_median=_pct(sharpes, 0.5),
        sharpe_p05=_pct(sharpes, 0.05),
        sharpe_p95=_pct(sharpes, 0.95),
        max_dd_median=_pct(max_dds, 0.5),
        max_dd_p95=_pct(max_dds, 0.95),
        pnl_median=_pct(pnls_final, 0.5),
        pnl_p05=_pct(pnls_final, 0.05),
        pnl_p95=_pct(pnls_final, 0.95),
        prob_profitable=prob_profit,
        block_count=block_count,
    )


def _trade_annualization_factor(trades: Sequence[Dict[str, Any]], n_trades: int) -> float:
    """Annualisation sqrt factor from UTC span of trade exits (crypto 24/7)."""
    if n_trades < 2:
        return math.sqrt(365.25)
    ts = [
        int(t.get("exit_time", t.get("entry_time", 0)))
        for t in trades
        if int(t.get("exit_time", t.get("entry_time", 0))) > 0
    ]
    if len(ts) < 2:
        return math.sqrt(365.25)
    span_ms = max(ts) - min(ts)
    years = max(span_ms / CRYPTO_MS_PER_YEAR, 1.0 / 365.25)
    trades_per_year = n_trades / years
    return math.sqrt(max(1.0, trades_per_year))


def _metrics_for_sequence(
    pnls: Sequence[float],
    initial_capital: float,
    *,
    annualization_factor: float = math.sqrt(365.25),
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
        sharpe = (mean_r / std_r) * annualization_factor if std_r > 0 else 0.0
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
        return _empty_mc(n_iter, mode="iid")

    rng = random.Random(seed)
    return _run_bootstrap_iterations(
        pnls,
        sampler=lambda: [pnls[rng.randrange(n)] for _ in range(n)],
        initial_capital=initial_capital,
        n_iter=n_iter,
        mode="iid",
        block_count=0,
        trades=trades,
    )


def block_bootstrap_metrics(
    trades: List[Dict[str, Any]],
    initial_capital: float = 10_000.0,
    n_iter: int = 1000,
    seed: Optional[int] = 42,
    block_mode: Literal["day", "regime"] = "day",
) -> MCMetrics:
    """Block bootstrap: resample whole calendar-day or regime blocks."""
    pnls = [float(t.get("pnl_usd", 0.0)) for t in trades]
    n = len(pnls)
    if n == 0:
        return _empty_mc(n_iter, mode=f"block_{block_mode}")

    blocks = group_trades_into_blocks(trades, mode=block_mode)
    if not blocks:
        return _empty_mc(n_iter, mode=f"block_{block_mode}")

    rng = random.Random(seed)
    return _run_bootstrap_iterations(
        pnls,
        sampler=lambda: _resample_blocks(blocks, n, rng),
        initial_capital=initial_capital,
        n_iter=n_iter,
        mode=f"block_{block_mode}",
        block_count=len(blocks),
        trades=trades,
    )
