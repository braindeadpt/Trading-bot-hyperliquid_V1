"""Trading performance metrics calculated with pandas.

All functions accept standard backtest output (equity curve + trade list)
and return scalar metrics.  Edge cases (empty data, zero std-dev) are
handled gracefully — no RuntimeWarnings or NaN propagation.

Canonical keys: ``n_trades``, ``total_return``, ``max_drawdown``.
Legacy aliases ``total_trades``, ``return_pct``, ``max_drawdown_pct`` are
populated by :func:`normalize_metrics` for walk-forward / reporting parity.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

# Crypto markets trade 24/7/365 — default annualisation for sparse curves.
CRYPTO_HOURS_PER_YEAR = 365.25 * 24
CRYPTO_MS_PER_YEAR = int(CRYPTO_HOURS_PER_YEAR * 3_600_000)


def calculate_metrics(
    equity_curve: List[Tuple[int, float]],
    trades: List[Dict[str, Any]],
) -> Dict[str, float]:
    """Compute the full metrics suite from backtest output.

    Parameters
    ----------
    equity_curve :
        List of (timestamp_ms, capital) tuples ordered chronologically.
    trades :
        List of closed trade dicts as produced by BacktestEngine.

    Returns
    -------
    Dict mapping metric name → float value.  Missing data yields 0.0.
    """
    if not equity_curve or not trades:
        return _empty_metrics()

    # --- Equity curve → returns ---
    df_eq = pd.DataFrame(equity_curve, columns=["timestamp_ms", "capital"])
    df_eq["returns"] = df_eq["capital"].pct_change().fillna(0.0)
    returns = df_eq["returns"].replace([np.inf, -np.inf], 0.0).dropna()

    # --- Trade-level PnL series ---
    pnls = pd.Series([t["pnl_usd"] for t in trades], dtype=float)
    win_mask = pnls > 0
    loss_mask = pnls < 0
    wins = pnls[win_mask]
    losses = pnls[loss_mask]

    # --- Basic counts ---
    n_trades = len(pnls)
    n_wins = len(wins)
    n_losses = len(losses)
    win_rate = n_wins / n_trades if n_trades else 0.0

    # --- PnL aggregates ---
    total_pnl = pnls.sum()
    gross_profit = wins.sum() if len(wins) else 0.0
    gross_loss = abs(losses.sum()) if len(losses) else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss != 0 else np.inf
    avg_trade = pnls.mean() if n_trades else 0.0
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = losses.mean() if len(losses) else 0.0

    # --- Equity-curve derived (time-based sampling) ---
    total_return = (df_eq["capital"].iloc[-1] - df_eq["capital"].iloc[0]) / df_eq["capital"].iloc[0]
    max_dd = max_drawdown(df_eq["capital"])
    periods_per_year = infer_periods_per_year(df_eq["timestamp_ms"])
    duration_years = sample_duration_years(df_eq["timestamp_ms"])
    sharpe = sharpe_ratio(returns, periods_per_year=periods_per_year)
    sortino = sortino_ratio(returns, periods_per_year=periods_per_year)
    calmar = calmar_ratio(total_return, max_dd, duration_years)
    expectancy_r = expectancy_in_r(trades)

    # --- Consecutive streaks ---
    consec_wins, consec_losses = consecutive_streaks(pnls)

    return normalize_metrics({
        "total_return": round(total_return, 6),
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4),
        "max_drawdown": round(max_dd, 6),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if np.isfinite(profit_factor) else 0.0,
        "avg_trade": round(avg_trade, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "calmar_ratio": round(calmar, 4),
        "expectancy_r": round(expectancy_r, 4),
        "consecutive_wins": consec_wins,
        "consecutive_losses": consec_losses,
        "n_trades": n_trades,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "sample_duration_years": round(duration_years, 6),
        "periods_per_year": round(periods_per_year, 2),
    })


def _empty_metrics() -> Dict[str, float]:
    return normalize_metrics({
        "total_return": 0.0,
        "sharpe_ratio": 0.0,
        "sortino_ratio": 0.0,
        "max_drawdown": 0.0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "avg_trade": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "calmar_ratio": 0.0,
        "expectancy_r": 0.0,
        "consecutive_wins": 0,
        "consecutive_losses": 0,
        "n_trades": 0,
        "n_wins": 0,
        "n_losses": 0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "sample_duration_years": 0.0,
        "periods_per_year": 0.0,
    })


def normalize_metrics(metrics: Mapping[str, Any]) -> Dict[str, float]:
    """Unify canonical and legacy metric keys for downstream consumers."""
    out: Dict[str, float] = {}
    for k, v in metrics.items():
        if k.startswith("__"):
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            out[k] = float(v) if isinstance(v, (int, float)) else 0.0

    n_trades = int(out.get("n_trades", out.get("total_trades", 0)))
    out["n_trades"] = float(n_trades)
    out["total_trades"] = float(n_trades)

    total_return = out.get("total_return", out.get("return_pct", 0.0))
    out["total_return"] = float(total_return)
    out["return_pct"] = float(total_return)

    max_dd = out.get("max_drawdown", out.get("max_drawdown_pct", 0.0))
    out["max_drawdown"] = float(max_dd)
    out["max_drawdown_pct"] = float(max_dd)
    return out


def infer_periods_per_year(timestamps_ms: pd.Series) -> float:
    """Infer annualisation factor from median equity-curve spacing (crypto 24/7)."""
    if timestamps_ms.empty or len(timestamps_ms) < 2:
        return float(CRYPTO_HOURS_PER_YEAR)
    deltas = timestamps_ms.astype("int64").diff().dropna()
    median_delta_ms = float(deltas.median())
    if median_delta_ms <= 0:
        return float(CRYPTO_HOURS_PER_YEAR)
    return CRYPTO_MS_PER_YEAR / median_delta_ms


def sample_duration_years(timestamps_ms: pd.Series) -> float:
    """Backtest sample length in years (wall-clock span, crypto calendar)."""
    if timestamps_ms.empty or len(timestamps_ms) < 2:
        return 0.0
    span_ms = int(timestamps_ms.iloc[-1]) - int(timestamps_ms.iloc[0])
    if span_ms <= 0:
        return 0.0
    return span_ms / CRYPTO_MS_PER_YEAR


def cagr(total_return: float, duration_years: float) -> float:
    """Compound annual growth rate over the real sample duration."""
    if duration_years <= 0:
        return 0.0
    base = 1.0 + total_return
    if base <= 0:
        return 0.0
    return float(base ** (1.0 / duration_years) - 1.0)


def expectancy_in_r(trades: List[Dict[str, Any]]) -> float:
    """Mean R-multiple per trade (net PnL / initial risk at entry)."""
    r_values: List[float] = []
    for t in trades:
        if "r_multiple" in t:
            r_values.append(float(t["r_multiple"]))
            continue
        risk = float(t.get("risk_usd", 0.0))
        if risk > 0:
            r_values.append(float(t.get("pnl_usd", 0.0)) / risk)
    if not r_values:
        return 0.0
    return float(np.mean(r_values))


def max_drawdown(equity: pd.Series) -> float:
    """Maximum peak-to-trough drawdown as a fraction of the peak.

    Returns a positive number, e.g. 0.15 means 15% drawdown.
    """
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    dd = (running_max - equity) / running_max
    return float(dd.max())


def sharpe_ratio(
    returns: pd.Series,
    risk_free: float = 0.0,
    periods_per_year: Optional[float] = None,
) -> float:
    """Annualised Sharpe from time-based equity-curve returns (crypto calendar)."""
    if periods_per_year is None:
        periods_per_year = float(CRYPTO_HOURS_PER_YEAR)
    excess = returns - risk_free
    std = excess.std(ddof=1)
    if std == 0 or np.isnan(std):
        return 0.0
    sharpe = excess.mean() / std * np.sqrt(periods_per_year)
    return float(sharpe)


def sortino_ratio(
    returns: pd.Series,
    risk_free: float = 0.0,
    periods_per_year: Optional[float] = None,
) -> float:
    """Annualised Sortino ratio (downside-deviation denominator)."""
    if periods_per_year is None:
        periods_per_year = float(CRYPTO_HOURS_PER_YEAR)
    excess = returns - risk_free
    downside = excess[excess < 0]
    if downside.empty or len(downside) < 2:
        return 0.0
    downside_std = downside.std(ddof=1)
    if downside_std == 0 or np.isnan(downside_std):
        return 0.0
    sortino = excess.mean() / downside_std * np.sqrt(periods_per_year)
    return float(sortino)


def calmar_ratio(
    total_return: float,
    max_dd: float,
    duration_years: float,
) -> float:
    """Calmar = CAGR / max drawdown using the real wall-clock sample span."""
    if max_dd <= 0 or not np.isfinite(max_dd) or duration_years <= 0:
        return 0.0
    annualised = cagr(total_return, duration_years)
    return float(annualised / max_dd)


def consecutive_streaks(pnls: pd.Series) -> Tuple[int, int]:
    """Longest consecutive winning streak and losing streak."""
    if pnls.empty:
        return 0, 0

    signs = np.sign(pnls.to_numpy())
    max_wins = 0
    max_losses = 0
    current = 0
    current_sign = 0

    for s in signs:
        if s == 0:
            continue
        if s == current_sign:
            current += 1
        else:
            current = 1
            current_sign = s
        if current_sign > 0:
            max_wins = max(max_wins, current)
        else:
            max_losses = max(max_losses, current)

    return int(max_wins), int(max_losses)
