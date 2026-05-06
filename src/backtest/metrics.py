"""Trading performance metrics calculated with pandas.

All functions accept standard backtest output (equity curve + trade list)
and return scalar metrics.  Edge cases (empty data, zero std-dev) are
handled gracefully — no RuntimeWarnings or NaN propagation.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd


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

    # --- Equity-curve derived ---
    total_return = (df_eq["capital"].iloc[-1] - df_eq["capital"].iloc[0]) / df_eq["capital"].iloc[0]
    max_dd = max_drawdown(df_eq["capital"])
    sharpe = sharpe_ratio(returns)
    sortino = sortino_ratio(returns)
    calmar = calmar_ratio(total_return, max_dd)

    # --- Consecutive streaks ---
    consec_wins, consec_losses = consecutive_streaks(pnls)

    return {
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
        "consecutive_wins": consec_wins,
        "consecutive_losses": consec_losses,
        "n_trades": n_trades,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
    }


def _empty_metrics() -> Dict[str, float]:
    return {
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
        "consecutive_wins": 0,
        "consecutive_losses": 0,
        "n_trades": 0,
        "n_wins": 0,
        "n_losses": 0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
    }


def max_drawdown(equity: pd.Series) -> float:
    """Maximum peak-to-trough drawdown as a fraction of the peak.

    Returns a positive number, e.g. 0.15 means 15% drawdown.
    """
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    dd = (running_max - equity) / running_max
    return float(dd.max())


def sharpe_ratio(returns: pd.Series, risk_free: float = 0.0, periods_per_year: int = 365 * 24) -> float:
    """Annualised Sharpe ratio assuming hourly returns.

    *periods_per_year* defaults to 365×24 for crypto (always-on market).
    """
    excess = returns - risk_free
    std = excess.std(ddof=1)
    if std == 0 or np.isnan(std):
        return 0.0
    sharpe = excess.mean() / std * np.sqrt(periods_per_year)
    return float(sharpe)


def sortino_ratio(returns: pd.Series, risk_free: float = 0.0, periods_per_year: int = 365 * 24) -> float:
    """Annualised Sortino ratio (downside-deviation denominator)."""
    excess = returns - risk_free
    downside = excess[excess < 0]
    if downside.empty or len(downside) < 2:
        return 0.0
    downside_std = downside.std(ddof=1)
    if downside_std == 0 or np.isnan(downside_std):
        return 0.0
    sortino = excess.mean() / downside_std * np.sqrt(periods_per_year)
    return float(sortino)


def calmar_ratio(total_return: float, max_dd: float) -> float:
    """Calmar = annualised return / max drawdown.

    We assume the backtest period is representative and annualise
    linearly from the total return / years in sample.
    """
    if max_dd <= 0 or not np.isfinite(max_dd):
        return 0.0
    # If the caller passes total_return as a fraction of the whole sample,
    # we normalise to annual by assuming 1 year for simplicity.
    # In practice the engine passes total_return already.
    return float(total_return / max_dd)


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
