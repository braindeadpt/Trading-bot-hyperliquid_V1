"""Feature screening: raw predictive power of f(t) vs forward returns r(t→t+h).

No strategy, stops, sizing, gates, or costs. Point-in-time discipline:
features at bar t use only data available at the close of bar t; forward
returns use close[t+h] / close[t] - 1 on closed bars only.

Overlapping-return inference: Newey–West HAC on the rank-product series
with lag = horizon_bars − 1. Multiple-testing: Benjamini–Hochberg FDR
across candidate feature × horizon tests (controls excluded from the
family, reported separately for pipeline validation).

Usage:
  python scripts/feature_screening.py
  python scripts/feature_screening.py --db data/live/bot.db --out-dir data/backtests
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SYMBOLS_DEFAULT = ["BTC", "ETH", "SOL", "HYPE"]
BAR_MS = 15 * 60 * 1000
HORIZONS: Dict[str, int] = {
    "15m": 1,
    "1h": 4,
    "4h": 16,
    "24h": 96,
}
N_SUBPERIODS = 3
N_QUINTILES = 5
FDR_ALPHA = 0.05
CONTROL_POS = "CONTROL_POS_leaky_forward"
CONTROL_NEGS = ("CONTROL_NEG_rand_a", "CONTROL_NEG_rand_b", "CONTROL_NEG_rand_c")
RNG_SEED = 42


# ── stats helpers ──────────────────────────────────────────────────────────


def _newey_west_se(x: np.ndarray, lag: int) -> float:
    """HAC SE of the sample mean of x (Newey–West, Bartlett weights)."""
    n = len(x)
    if n < 3:
        return float("nan")
    x = np.asarray(x, dtype=float)
    x = x - np.nanmean(x)
    gamma0 = float(np.dot(x, x) / n)
    var = gamma0
    max_lag = min(max(lag, 0), n - 1)
    for L in range(1, max_lag + 1):
        w = 1.0 - L / (max_lag + 1)
        gamma = float(np.dot(x[L:], x[:-L]) / n)
        var += 2.0 * w * gamma
    return math.sqrt(max(var, 0.0) / n)


def spearman_ic_hac(
    feature: np.ndarray,
    forward: np.ndarray,
    horizon_bars: int,
) -> Tuple[float, float, int]:
    """Spearman IC + two-sided p from Newey–West SE on demeaned rank products.

    After ranking, corr = cov(rf, rr) / (s_rf * s_rr). Inference uses the
    mean of z_t = (rf_t - mean_rf) * (rr_t - mean_rr); SE(mean(z)) via NW
    with lag = horizon_bars − 1 (overlapping forward windows).
    """
    mask = np.isfinite(feature) & np.isfinite(forward)
    n = int(mask.sum())
    if n < 30:
        return float("nan"), float("nan"), n
    f = feature[mask]
    r = forward[mask]
    rf = stats.rankdata(f).astype(float)
    rr = stats.rankdata(r).astype(float)
    # scipy spearman for the point estimate (ties handled)
    rho, _ = stats.spearmanr(f, r)
    if not np.isfinite(rho):
        return float("nan"), float("nan"), n
    rf_c = rf - rf.mean()
    rr_c = rr - rr.mean()
    z = rf_c * rr_c
    se_z = _newey_west_se(z, lag=max(horizon_bars - 1, 0))
    s_rf = float(rf_c.std(ddof=0))
    s_rr = float(rr_c.std(ddof=0))
    if se_z <= 0 or s_rf <= 0 or s_rr <= 0:
        return float(rho), float("nan"), n
    # SE(rho) ≈ SE(mean z) / (s_rf * s_rr)  (mean z = cov)
    se_rho = se_z / (s_rf * s_rr)
    if se_rho <= 0 or not np.isfinite(se_rho):
        return float(rho), float("nan"), n
    zstat = float(rho) / se_rho
    p = float(2.0 * stats.norm.sf(abs(zstat)))
    return float(rho), p, n


def benjamini_hochberg(
    pvals: Sequence[float], alpha: float = FDR_ALPHA
) -> Tuple[np.ndarray, np.ndarray]:
    """Return (rejected_mask, q_values) aligned to input order."""
    m = len(pvals)
    q = np.full(m, np.nan, dtype=float)
    rejected = np.zeros(m, dtype=bool)
    valid = [(i, float(p)) for i, p in enumerate(pvals) if np.isfinite(p)]
    if not valid:
        return rejected, q
    valid.sort(key=lambda t: t[1])
    m_eff = len(valid)
    # q-values (storey-style running min from the right)
    raw_q = np.empty(m_eff, dtype=float)
    for rank, (i, p) in enumerate(valid, start=1):
        raw_q[rank - 1] = p * m_eff / rank
    running = 1.0
    adj = np.empty(m_eff, dtype=float)
    for k in range(m_eff - 1, -1, -1):
        running = min(running, raw_q[k])
        adj[k] = min(running, 1.0)
    for k, (i, _) in enumerate(valid):
        q[i] = adj[k]
        rejected[i] = adj[k] <= alpha
    return rejected, q


def quintile_means(feature: np.ndarray, forward: np.ndarray) -> Tuple[List[float], float]:
    """Mean forward return per feature quintile + monotonicity score in [-1,1].

    Monotonicity = Spearman(quintile_index, quintile_mean). |score|≈1 ⇒
    monotone; near 0 ⇒ U-shape / noise.
    """
    mask = np.isfinite(feature) & np.isfinite(forward)
    f = feature[mask]
    r = forward[mask]
    if len(f) < N_QUINTILES * 10:
        return [float("nan")] * N_QUINTILES, float("nan")
    try:
        q = pd.qcut(f, N_QUINTILES, labels=False, duplicates="drop")
    except ValueError:
        return [float("nan")] * N_QUINTILES, float("nan")
    means: List[float] = []
    idxs: List[int] = []
    for qi in range(int(np.nanmax(q)) + 1):
        sel = q == qi
        if sel.sum() == 0:
            continue
        means.append(float(np.mean(r[sel])))
        idxs.append(qi)
    if len(means) < 3:
        return means + [float("nan")] * (N_QUINTILES - len(means)), float("nan")
    mono, _ = stats.spearmanr(idxs, means)
    return means, float(mono) if np.isfinite(mono) else float("nan")


# ── data loading ───────────────────────────────────────────────────────────


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def load_candles_15m(con: sqlite3.Connection, symbols: Sequence[str]) -> pd.DataFrame:
    q = f"""
        SELECT symbol, timestamp_ms, open, high, low, close, volume,
               funding_rate, oi_total, oi_delta,
               COALESCE(buy_volume, 0) AS buy_volume,
               COALESCE(sell_volume, 0) AS sell_volume,
               COALESCE(trade_count, 0) AS trade_count
        FROM candles_15m
        WHERE symbol IN ({",".join("?" * len(symbols))})
        ORDER BY symbol, timestamp_ms
    """
    df = pd.read_sql_query(q, con, params=list(symbols))
    return df


def load_funding(con: sqlite3.Connection, symbols: Sequence[str]) -> pd.DataFrame:
    q = f"""
        SELECT symbol, timestamp AS timestamp_ms, current AS funding, predicted
        FROM funding_history
        WHERE symbol IN ({",".join("?" * len(symbols))})
        ORDER BY symbol, timestamp
    """
    return pd.read_sql_query(q, con, params=list(symbols))


def load_oi(con: sqlite3.Connection, symbols: Sequence[str]) -> pd.DataFrame:
    q = f"""
        SELECT symbol, timestamp AS timestamp_ms, oi_total AS oi_hist, oi_delta AS oi_d_hist
        FROM oi_history
        WHERE symbol IN ({",".join("?" * len(symbols))})
        ORDER BY symbol, timestamp
    """
    return pd.read_sql_query(q, con, params=list(symbols))


def load_binance_perp(con: sqlite3.Connection, symbols: Sequence[str]) -> pd.DataFrame:
    q = f"""
        SELECT symbol, timestamp_ms, price AS bn_perp
        FROM binance_perp_prices
        WHERE symbol IN ({",".join("?" * len(symbols))})
        ORDER BY symbol, timestamp_ms
    """
    return pd.read_sql_query(q, con, params=list(symbols))


def load_liquidations(con: sqlite3.Connection, symbols: Sequence[str]) -> pd.DataFrame:
    q = f"""
        SELECT symbol, timestamp_ms, notional_usd, side
        FROM liquidation_events
        WHERE symbol IN ({",".join("?" * len(symbols))})
        ORDER BY symbol, timestamp_ms
    """
    return pd.read_sql_query(q, con, params=list(symbols))


def _asof_merge(
    left: pd.DataFrame,
    right: pd.DataFrame,
    cols: Sequence[str],
) -> pd.DataFrame:
    """Point-in-time merge_asof per symbol (right timestamp ≤ left timestamp)."""
    pieces = []
    for sym, g in left.groupby("symbol", sort=False):
        r = right.loc[right["symbol"] == sym, ["timestamp_ms", *cols]].sort_values(
            "timestamp_ms"
        )
        g2 = g.sort_values("timestamp_ms")
        if r.empty:
            out = g2.copy()
            for c in cols:
                out[c] = np.nan
        else:
            out = pd.merge_asof(
                g2, r, on="timestamp_ms", direction="backward"
            )
        pieces.append(out)
    return pd.concat(pieces, ignore_index=True)


def _liq_bar_agg(liq: pd.DataFrame, bar_ts: pd.Series, symbol: str) -> pd.DataFrame:
    """Aggregate liquidations onto 15m bar closes (events with ts ≤ bar close)."""
    # Map each liq to the 15m bucket end that contains it.
    if liq.empty:
        return pd.DataFrame(
            {
                "timestamp_ms": bar_ts.values,
                "liq_notional": 0.0,
                "liq_signed": 0.0,
                "liq_count": 0.0,
            }
        )
    s = liq.loc[liq["symbol"] == symbol].copy()
    if s.empty:
        return pd.DataFrame(
            {
                "timestamp_ms": bar_ts.values,
                "liq_notional": 0.0,
                "liq_signed": 0.0,
                "liq_count": 0.0,
            }
        )
    # Bar close timestamps in this project are typically period-end (…999) or
    # aligned opens; bucket by floor to BAR_MS then attach to nearest bar ≤ event
    # via asof onto cumulative sums.
    s["sign"] = np.where(s["side"].str.lower().isin(["sell", "short"]), -1.0, 1.0)
    s["signed_notional"] = s["notional_usd"] * s["sign"]
    s = s.sort_values("timestamp_ms")
    # Cumulative then asof to each bar
    s["cum_notional"] = s["notional_usd"].cumsum()
    s["cum_signed"] = s["signed_notional"].cumsum()
    s["cum_count"] = np.arange(1, len(s) + 1, dtype=float)
    bars = pd.DataFrame({"timestamp_ms": bar_ts.sort_values().values})
    m = pd.merge_asof(
        bars,
        s[["timestamp_ms", "cum_notional", "cum_signed", "cum_count"]],
        on="timestamp_ms",
        direction="backward",
    )
    m = m.fillna(0.0)
    m["liq_notional"] = m["cum_notional"].diff().fillna(m["cum_notional"])
    m["liq_signed"] = m["cum_signed"].diff().fillna(m["cum_signed"])
    m["liq_count"] = m["cum_count"].diff().fillna(m["cum_count"])
    return m[["timestamp_ms", "liq_notional", "liq_signed", "liq_count"]]


# ── feature engineering (point-in-time) ────────────────────────────────────


def build_feature_frame(con: sqlite3.Connection, symbols: Sequence[str]) -> pd.DataFrame:
    """Build one row per (symbol, 15m closed bar) with all candidate features."""
    candles = load_candles_15m(con, symbols)
    if candles.empty:
        raise SystemExit("No candles_15m rows found")

    funding = load_funding(con, symbols)
    oi = load_oi(con, symbols)
    bn = load_binance_perp(con, symbols)
    liq = load_liquidations(con, symbols)

    # PIT joins: last observation at or before bar timestamp_ms
    df = candles.copy()
    if not funding.empty:
        df = _asof_merge(df, funding, ["funding", "predicted"])
    else:
        df["funding"] = np.nan
        df["predicted"] = np.nan
    if not oi.empty:
        df = _asof_merge(df, oi, ["oi_hist", "oi_d_hist"])
    else:
        df["oi_hist"] = np.nan
        df["oi_d_hist"] = np.nan
    if not bn.empty:
        df = _asof_merge(df, bn, ["bn_perp"])
        # Invalidate stale asof matches — never silently carry a dead feed's
        # last print across days (fstream outage lesson, 2026-06-29).
        max_age_ms = 3_600_000  # 1h
        pieces_bn = []
        for sym, g in df.groupby("symbol", sort=False):
            g = g.sort_values("timestamp_ms").copy()
            r = bn.loc[bn["symbol"] == sym, ["timestamp_ms"]].sort_values(
                "timestamp_ms"
            )
            if r.empty:
                g["bn_perp"] = np.nan
            else:
                # age vs last available bn timestamp at or before bar
                last_bn = pd.merge_asof(
                    g[["timestamp_ms"]],
                    r.rename(columns={"timestamp_ms": "bn_ts"}),
                    left_on="timestamp_ms",
                    right_on="bn_ts",
                    direction="backward",
                )
                bn_ts = last_bn["bn_ts"].to_numpy()
                age = g["timestamp_ms"].to_numpy() - bn_ts
                stale = np.isnan(bn_ts.astype(float)) | (age > max_age_ms)
                g.loc[stale, "bn_perp"] = np.nan
            pieces_bn.append(g)
        df = pd.concat(pieces_bn, ignore_index=True)
    else:
        df["bn_perp"] = np.nan

    pieces = []
    rng = np.random.default_rng(RNG_SEED)

    for sym, g0 in df.groupby("symbol", sort=False):
        g = g0.sort_values("timestamp_ms").reset_index(drop=True)
        liq_bars = _liq_bar_agg(liq, g["timestamp_ms"], str(sym))
        g = g.merge(liq_bars, on="timestamp_ms", how="left")

        close = g["close"].astype(float)
        high = g["high"].astype(float)
        low = g["low"].astype(float)
        buy = g["buy_volume"].astype(float)
        sell = g["sell_volume"].astype(float)
        vol = g["volume"].astype(float)
        oi_c = g["oi_total"].astype(float)
        fund = g["funding"].astype(float)
        if fund.isna().all() and "funding_rate" in g.columns:
            fund = g["funding_rate"].astype(float)
        oi_level = g["oi_hist"].astype(float)
        if oi_level.isna().all():
            oi_level = oi_c

        ret_1 = close.pct_change(1)
        ret_4 = close.pct_change(4)
        ret_16 = close.pct_change(16)
        ret_96 = close.pct_change(96)

        delta = buy - sell
        cvd_1 = delta
        cvd_1h = delta.rolling(4, min_periods=4).sum()
        cvd_4h = delta.rolling(16, min_periods=16).sum()
        taker_buy_ratio = buy / (buy + sell).replace(0, np.nan)

        missing = ret_4.isna() | cvd_1h.isna()
        divergent = (ret_4 > 0) != (cvd_1h > 0)
        cvd_px_div = pd.Series(0.0, index=g.index, dtype=float)
        cvd_px_div = cvd_px_div.mask(missing, np.nan)
        cvd_px_div = cvd_px_div.mask(
            (~missing) & divergent, np.sign(cvd_1h) * -np.sign(ret_4)
        )
        mag = cvd_1h.abs() * ret_4.abs()
        cvd_div_strength = mag.where((~missing) & divergent, 0.0)
        cvd_div_strength = cvd_div_strength.mask(missing, np.nan)
        cvd_div_signed = cvd_px_div * mag

        oi_d_1h = oi_level.pct_change(4)
        oi_d_24h = oi_level.pct_change(96)
        oi_px_div = np.sign(ret_4) * (-np.sign(oi_d_1h))
        oi_px_div = np.where(ret_4.isna() | oi_d_1h.isna(), np.nan, oi_px_div)

        bn_px = g["bn_perp"].astype(float)
        basis = (close - bn_px) / bn_px.replace(0, np.nan)
        basis_mu = basis.rolling(96 * 7, min_periods=96).mean()
        basis_sd = basis.rolling(96 * 7, min_periods=96).std(ddof=0)
        basis_z = (basis - basis_mu) / basis_sd.replace(0, np.nan)
        basis_vel = basis.diff(4)

        fund_chg_8h = fund.diff(32)
        fund_chg_24h = fund.diff(96)
        fund_mu = fund.rolling(96 * 30, min_periods=96).mean()
        fund_sd = fund.rolling(96 * 30, min_periods=96).std(ddof=0)
        fund_z = (fund - fund_mu) / fund_sd.replace(0, np.nan)
        fund_pred_spread = g["predicted"].astype(float) - fund

        logret = np.log(close / close.shift(1))
        rvol_1h = logret.rolling(4, min_periods=4).std(ddof=0) * math.sqrt(4)
        rvol_24h = logret.rolling(96, min_periods=24).std(ddof=0) * math.sqrt(96)
        tr = pd.concat(
            [
                (high - low),
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr_14 = tr.rolling(14, min_periods=14).mean()
        # Fast ATR percentile via rank within trailing window
        atr_pctile = atr_14.rolling(96 * 7, min_periods=96).apply(
            lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False
        )
        ma20 = close.rolling(20, min_periods=20).mean()
        sd20 = close.rolling(20, min_periods=20).std(ddof=0)
        bb_width = (2 * sd20) / ma20.replace(0, np.nan)

        up = high.diff()
        down = -low.diff()
        plus_dm = pd.Series(
            np.where((up > down) & (up > 0), up, 0.0), index=g.index
        )
        minus_dm = pd.Series(
            np.where((down > up) & (down > 0), down, 0.0), index=g.index
        )
        atr_adx = tr.rolling(14, min_periods=14).mean()
        plus_di = 100 * plus_dm.rolling(14, min_periods=14).mean() / atr_adx
        minus_di = 100 * minus_dm.rolling(14, min_periods=14).mean() / atr_adx
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.rolling(14, min_periods=14).mean()

        ts = pd.to_datetime(g["timestamp_ms"], unit="ms", utc=True)
        hour = ts.dt.hour + ts.dt.minute / 60.0
        hour_sin = np.sin(2 * math.pi * hour / 24.0)
        hour_cos = np.cos(2 * math.pi * hour / 24.0)
        dow = ts.dt.dayofweek.astype(float)
        minutes = (ts.dt.hour * 60 + ts.dt.minute).to_numpy(dtype=float)
        resets = np.array([0.0, 8 * 60, 16 * 60, 24 * 60])
        mins_to_funding = np.array(
            [float(np.min(resets[resets >= m] - m)) for m in minutes]
        )

        typical = (high + low + close) / 3.0
        pv = typical * vol
        vwap_1d = pv.rolling(96, min_periods=12).sum() / vol.rolling(
            96, min_periods=12
        ).sum().replace(0, np.nan)
        dist_vwap = (close - vwap_1d) / vwap_1d.replace(0, np.nan)
        autocorr_1 = ret_1.rolling(96, min_periods=24).corr(ret_1.shift(1))

        liq_n_1h = g["liq_notional"].rolling(4, min_periods=1).sum()
        liq_side_1h = g["liq_signed"].rolling(4, min_periods=1).sum()
        busy = g["liq_notional"] > g["liq_notional"].rolling(
            96, min_periods=4
        ).median().fillna(0)
        last_busy = pd.Series(
            np.where(busy.to_numpy(), np.arange(len(g)), np.nan)
        ).ffill()
        bars_since_liq = np.arange(len(g)) - last_busy.to_numpy()

        feat = pd.DataFrame(
            {
                "symbol": sym,
                "timestamp_ms": g["timestamp_ms"].to_numpy(),
                "close": close.to_numpy(),
                "funding_level": fund.to_numpy(),
                "funding_chg_8h": fund_chg_8h.to_numpy(),
                "funding_chg_24h": fund_chg_24h.to_numpy(),
                "funding_z_30d": fund_z.to_numpy(),
                "funding_pred_spread": fund_pred_spread.to_numpy(),
                "oi_delta_1h": oi_d_1h.to_numpy(),
                "oi_delta_24h": oi_d_24h.to_numpy(),
                "oi_price_divergence": oi_px_div,
                "basis": basis.to_numpy(),
                "basis_z_7d": basis_z.to_numpy(),
                "basis_velocity_1h": basis_vel.to_numpy(),
                "cvd_15m": cvd_1.to_numpy(),
                "cvd_1h": cvd_1h.to_numpy(),
                "cvd_4h": cvd_4h.to_numpy(),
                "taker_buy_ratio": taker_buy_ratio.to_numpy(),
                "cvd_price_div_signed": cvd_div_signed.to_numpy(),
                "cvd_div_strength": cvd_div_strength.to_numpy(),
                "liq_notional_15m": g["liq_notional"].to_numpy(),
                "liq_notional_1h": liq_n_1h.to_numpy(),
                "liq_side_imbalance_1h": liq_side_1h.to_numpy(),
                "bars_since_liq_cluster": bars_since_liq,
                "rvol_1h": rvol_1h.to_numpy(),
                "rvol_24h": rvol_24h.to_numpy(),
                "atr_percentile_7d": atr_pctile.to_numpy(),
                "bb_width": bb_width.to_numpy(),
                "adx_14": adx.to_numpy(),
                "hour_sin": np.asarray(hour_sin),
                "hour_cos": np.asarray(hour_cos),
                "dow": dow.to_numpy(),
                "mins_to_funding_reset": mins_to_funding,
                "ret_lag_15m": ret_1.to_numpy(),
                "ret_lag_1h": ret_4.to_numpy(),
                "ret_lag_4h": ret_16.to_numpy(),
                "ret_lag_24h": ret_96.to_numpy(),
                "autocorr_ret_1d": autocorr_1.to_numpy(),
                "dist_to_vwap_1d": dist_vwap.to_numpy(),
            }
        )
        pieces.append(feat)

    out = pd.concat(pieces, ignore_index=True)

    # Forward returns (known only after t+h — used as target, NOT as feature
    # except in the intentional positive control)
    for name, hb in HORIZONS.items():
        fwd = []
        for sym, g in out.groupby("symbol", sort=False):
            c = g["close"].astype(float).values
            r = np.full(len(c), np.nan)
            if len(c) > hb:
                r[: len(c) - hb] = c[hb:] / c[: len(c) - hb] - 1.0
            fwd.append(pd.Series(r, index=g.index))
        out[f"fwd_{name}"] = pd.concat(fwd).sort_index()

    # Controls (per symbol, seeded)
    ctrl_pieces = []
    for sym, g in out.groupby("symbol", sort=False):
        n = len(g)
        # Positive: leaky — uses 1h forward return + noise (pipeline must rank this top)
        fwd = g["fwd_1h"].values.astype(float)
        noise = rng.normal(0.0, np.nanstd(fwd) * 0.5 if np.isfinite(np.nanstd(fwd)) else 0.001, size=n)
        pos = fwd + noise
        neg_a = rng.normal(size=n)
        neg_b = rng.normal(size=n)
        neg_c = rng.normal(size=n)
        ctrl_pieces.append(
            pd.DataFrame(
                {
                    CONTROL_POS: pos,
                    CONTROL_NEGS[0]: neg_a,
                    CONTROL_NEGS[1]: neg_b,
                    CONTROL_NEGS[2]: neg_c,
                },
                index=g.index,
            )
        )
    ctrl = pd.concat(ctrl_pieces).sort_index()
    out = pd.concat([out, ctrl], axis=1)
    return out


CANDIDATE_FEATURES = [
    "funding_level",
    "funding_chg_8h",
    "funding_chg_24h",
    "funding_z_30d",
    "funding_pred_spread",
    "oi_delta_1h",
    "oi_delta_24h",
    "oi_price_divergence",
    "basis",
    "basis_z_7d",
    "basis_velocity_1h",
    "cvd_15m",
    "cvd_1h",
    "cvd_4h",
    "taker_buy_ratio",
    "cvd_price_div_signed",
    "cvd_div_strength",
    "liq_notional_15m",
    "liq_notional_1h",
    "liq_side_imbalance_1h",
    "bars_since_liq_cluster",
    "rvol_1h",
    "rvol_24h",
    "atr_percentile_7d",
    "bb_width",
    "adx_14",
    "hour_sin",
    "hour_cos",
    "dow",
    "mins_to_funding_reset",
    "ret_lag_15m",
    "ret_lag_1h",
    "ret_lag_4h",
    "ret_lag_24h",
    "autocorr_ret_1d",
    "dist_to_vwap_1d",
]


# ── screening core ─────────────────────────────────────────────────────────


@dataclass
class ScreenCell:
    feature: str
    horizon: str
    ic_agg: float
    p_raw: float
    n_eff: int
    mono: float
    quintile_means: List[float]
    ic_by_symbol: Dict[str, float]
    n_by_symbol: Dict[str, int]
    ic_by_period: List[float]
    n_by_period: List[int]
    same_sign_symbols: int
    same_sign_periods: int
    is_control: bool


def _pool_arrays(
    df: pd.DataFrame, feature: str, horizon: str
) -> Tuple[np.ndarray, np.ndarray]:
    return df[feature].to_numpy(dtype=float), df[f"fwd_{horizon}"].to_numpy(dtype=float)


def screen_one(
    df: pd.DataFrame,
    feature: str,
    horizon: str,
    horizon_bars: int,
) -> ScreenCell:
    f_all, r_all = _pool_arrays(df, feature, horizon)
    ic, p, n = spearman_ic_hac(f_all, r_all, horizon_bars)
    qmeans, mono = quintile_means(f_all, r_all)

    ic_sym: Dict[str, float] = {}
    n_sym: Dict[str, int] = {}
    for sym, g in df.groupby("symbol"):
        ics, _, ns = spearman_ic_hac(
            g[feature].to_numpy(dtype=float),
            g[f"fwd_{horizon}"].to_numpy(dtype=float),
            horizon_bars,
        )
        ic_sym[str(sym)] = ics
        n_sym[str(sym)] = ns

    # Temporal stability: 3 equal time thirds on pooled timeline
    d = df[["timestamp_ms", feature, f"fwd_{horizon}"]].dropna()
    ic_per: List[float] = []
    n_per: List[int] = []
    if len(d) >= 90:
        edges = np.quantile(d["timestamp_ms"], [0, 1 / 3, 2 / 3, 1])
        for i in range(N_SUBPERIODS):
            lo, hi = edges[i], edges[i + 1]
            if i < N_SUBPERIODS - 1:
                m = (d["timestamp_ms"] >= lo) & (d["timestamp_ms"] < hi)
            else:
                m = (d["timestamp_ms"] >= lo) & (d["timestamp_ms"] <= hi)
            sub = d.loc[m]
            ics, _, ns = spearman_ic_hac(
                sub[feature].to_numpy(dtype=float),
                sub[f"fwd_{horizon}"].to_numpy(dtype=float),
                horizon_bars,
            )
            ic_per.append(ics)
            n_per.append(ns)
    else:
        ic_per = [float("nan")] * N_SUBPERIODS
        n_per = [0] * N_SUBPERIODS

    finite_sym = [v for v in ic_sym.values() if np.isfinite(v)]
    if finite_sym:
        sign = np.sign(ic) if np.isfinite(ic) and ic != 0 else (
            1 if sum(1 for v in finite_sym if v > 0) >= len(finite_sym) / 2 else -1
        )
        same_sym = sum(1 for v in finite_sym if np.sign(v) == sign or v == 0)
    else:
        same_sym = 0

    finite_per = [v for v in ic_per if np.isfinite(v)]
    if finite_per and np.isfinite(ic) and ic != 0:
        sign_p = np.sign(ic)
        same_per = sum(1 for v in finite_per if np.sign(v) == sign_p)
    else:
        same_per = 0

    is_ctrl = feature == CONTROL_POS or feature in CONTROL_NEGS
    return ScreenCell(
        feature=feature,
        horizon=horizon,
        ic_agg=ic,
        p_raw=p,
        n_eff=n,
        mono=mono,
        quintile_means=qmeans,
        ic_by_symbol=ic_sym,
        n_by_symbol=n_sym,
        ic_by_period=ic_per,
        n_by_period=n_per,
        same_sign_symbols=same_sym,
        same_sign_periods=same_per,
        is_control=is_ctrl,
    )


def survives_top_gate(row: Dict[str, Any]) -> bool:
    """FDR + monotonicity + temporal stability + cross-symbol consistency."""
    if row.get("is_control"):
        return False
    if not row.get("fdr_reject", False):
        return False
    mono = row.get("mono")
    if mono is None or not np.isfinite(mono) or abs(mono) < 0.8:
        return False
    if int(row.get("same_sign_periods", 0)) < 2:
        return False
    if int(row.get("same_sign_symbols", 0)) < 3:
        return False
    return True


# ── report ─────────────────────────────────────────────────────────────────


def write_report(
    path: Path,
    rows: List[Dict[str, Any]],
    meta: Dict[str, Any],
) -> None:
    cand = [r for r in rows if not r["is_control"]]
    ctrls = [r for r in rows if r["is_control"]]
    top = [r for r in cand if r.get("survives")]
    top.sort(key=lambda r: abs(r.get("ic_agg") or 0), reverse=True)

    # Pipeline validation summary
    pos_rows = [r for r in ctrls if r["feature"] == CONTROL_POS]
    neg_rows = [r for r in ctrls if r["feature"] in CONTROL_NEGS]
    # Rank all by |IC| within each horizon for control placement
    control_notes = []
    for h in HORIZONS:
        ranked = sorted(
            [r for r in rows if r["horizon"] == h],
            key=lambda r: abs(r.get("ic_agg") or 0),
            reverse=True,
        )
        for i, r in enumerate(ranked, start=1):
            if r["feature"] == CONTROL_POS:
                control_notes.append(
                    f"- Horizon {h}: positive control rank **#{i}/{len(ranked)}** "
                    f"(IC={r['ic_agg']:.4f}, n={r['n_eff']})"
                )
        neg_ics = [
            r["ic_agg"]
            for r in ranked
            if r["feature"] in CONTROL_NEGS and np.isfinite(r.get("ic_agg", float("nan")))
        ]
        if neg_ics:
            control_notes.append(
                f"- Horizon {h}: negative controls |IC| max={max(abs(x) for x in neg_ics):.4f} "
                f"(mean={np.mean([abs(x) for x in neg_ics]):.4f})"
            )

    lines: List[str] = []
    lines.append("# Feature Screening Report")
    lines.append("")
    lines.append(f"Generated: {meta['created_utc']}")
    lines.append(f"DB: `{meta['db']}`")
    lines.append(f"Symbols: {', '.join(meta['symbols'])}")
    lines.append(f"Bar grid: 15m closed candles only")
    lines.append(f"Horizons: {', '.join(HORIZONS)}")
    lines.append(f"Inference: Newey–West HAC on Spearman rank-products (lag = h−1 bars)")
    lines.append(f"Multiple testing: Benjamini–Hochberg FDR α={FDR_ALPHA} on candidate×horizon")
    lines.append("")
    lines.append("## Point-in-time guarantee")
    lines.append("")
    lines.append(
        "- Features at bar `t` use only that bar’s OHLCV / tape fields and "
        "`merge_asof(..., direction='backward')` for funding, OI, and Binance "
        "perp (timestamp ≤ bar timestamp)."
    )
    lines.append(
        "- Forward return `r(t,t+h) = close[t+h]/close[t] − 1` on the same "
        "closed-bar grid; never used as a candidate feature."
    )
    lines.append(
        "- The positive control **intentionally** leaks `fwd_1h + noise` to "
        "validate ranking; it is excluded from the FDR family."
    )
    lines.append(
        "- No L2/orderbook history exists in `bot.db` — depth features are out of scope."
    )
    lines.append("")
    lines.append("## Pipeline controls")
    lines.append("")
    lines.extend(control_notes or ["- (no control rows)"])
    lines.append("")
    pos_ok = all(
        next(
            (
                r.get("rank_in_horizon", 999)
                for r in rows
                if r["feature"] == CONTROL_POS and r["horizon"] == h
            ),
            999,
        )
        <= 2
        for h in HORIZONS
    )
    neg_max = max(
        (abs(r.get("ic_agg") or 0) for r in neg_rows if np.isfinite(r.get("ic_agg", float("nan")))),
        default=0.0,
    )
    lines.append(
        f"**Validation:** positive control near top on every horizon: "
        f"{'PASS' if pos_ok else 'FAIL'}; "
        f"negative controls |IC|≈0 (max |IC|={neg_max:.4f}): "
        f"{'PASS' if neg_max < 0.05 else 'WARN'}."
    )
    lines.append("")
    lines.append("## Continuation rule")
    lines.append("")
    lines.append(
        "Only features that survive FDR + monotonicity + temporal stability + "
        "cross-symbol consistency justify building a strategy. Flow:"
    )
    lines.append("")
    lines.append(
        "`feature with predictive power → simple strategy around it → "
        "baseline-signal gate → shadow → execution (PASS required)`"
    )
    lines.append("")
    lines.append("Never the inverse (strategy first, feature rationalization later).")
    lines.append("")
    lines.append("## TOP survivors")
    lines.append("")
    if not top:
        lines.append(
            "**None.** No candidate feature × horizon survived FDR "
            f"(α={FDR_ALPHA}) **and** |monotonicity|≥0.8 **and** same-sign IC "
            "in ≥2/3 sub-periods **and** same-sign IC in ≥3/4 symbols."
        )
        lines.append("")
        lines.append(
            "This is a legitimate and valuable result: under this catalog and "
            "window, raw predictive power is not strong enough to justify "
            "building strategy #15. Do not promote marginal pre-FDR hits."
        )
    else:
        lines.append("| feature | horizon | IC | p_raw | q_FDR | mono | stab | sym | n |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in top[:5]:
            lines.append(
                f"| {r['feature']} | {r['horizon']} | {r['ic_agg']:.4f} | "
                f"{r['p_raw']:.2e} | {r.get('q_fdr', float('nan')):.3f} | "
                f"{r['mono']:.2f} | {r['same_sign_periods']}/3 | "
                f"{r['same_sign_symbols']}/4 | {r['n_eff']} |"
            )
        if len(top) > 5:
            extra = ", ".join(f"`{r['feature']}@{r['horizon']}`" for r in top[5:])
            lines.append("")
            lines.append(f"Additional survivors ({len(top) - 5}): {extra}.")
        lines.append("")
        lines.append("### How to read the TOP (not automatic strategy mandates)")
        lines.append("")
        lines.append(
            "- Calendar features (`dow`, hour) can dominate long horizons because "
            "the feature is piecewise-constant while forward windows overlap; "
            "Newey–West reduces but does not eliminate that. Treat as market "
            "structure, not an automatic executable edge."
        )
        lines.append(
            "- Short-horizon `ret_lag_*` survivors are mean reversion: real and "
            "monotone here, but HL fees/slippage often erase sub-1h MR — costs "
            "must enter at the strategy/baseline-gate step."
        )
        lines.append(
            "- Vol / OI survivors are often **regime** signals (filter material), "
            "not standalone directional alphas."
        )
        lines.append(
            "- Features that clear FDR but fail monotonicity or stability "
            "(including sparse CVD divergence quintiles) are **not** TOP and "
            "do not authorize strategy work."
        )
        lines.append(
            "- Next step for any chosen TOP feature: one minimal strategy → "
            "baseline-signal gate → shadow. Never jump to `execution_strategies`."
        )
    lines.append("")
    lines.append("## Full ranking (candidates)")
    lines.append("")
    lines.append(
        "Sorted by \|IC\| descending. `fdr_reject` = survives BH at "
        f"α={FDR_ALPHA}. Significance **before** correction = `p_raw < 0.05`; "
        "**after** = `fdr_reject`."
    )
    lines.append("")
    lines.append(
        "| feature | h | IC | p_raw | q_FDR | p<0.05 | FDR | mono | "
        "Q means | stab | sym | n |"
    )
    lines.append("|---|---|---:|---:|---:|:---:|:---:|---:|---|---:|---:|---:|")

    ranked_c = sorted(cand, key=lambda r: abs(r.get("ic_agg") or 0), reverse=True)
    for r in ranked_c:
        qm = r.get("quintile_means") or []
        qm_s = ",".join(
            f"{x*1e4:.1f}bp" if np.isfinite(x) else "nan" for x in qm[:5]
        )
        lines.append(
            f"| {r['feature']} | {r['horizon']} | {r['ic_agg']:.4f} | "
            f"{r['p_raw']:.2e} | {r.get('q_fdr', float('nan')):.3f} | "
            f"{'Y' if (r.get('p_raw') is not None and r['p_raw'] < 0.05) else 'n'} | "
            f"{'Y' if r.get('fdr_reject') else 'n'} | "
            f"{r['mono']:.2f} | {qm_s} | "
            f"{r['same_sign_periods']}/3 | {r['same_sign_symbols']}/4 | {r['n_eff']} |"
        )

    lines.append("")
    lines.append("## Control rows (excluded from FDR family)")
    lines.append("")
    lines.append("| feature | h | IC | p_raw | mono | n | rank_in_h |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for r in sorted(ctrls, key=lambda x: (x["horizon"], -abs(x.get("ic_agg") or 0))):
        lines.append(
            f"| {r['feature']} | {r['horizon']} | {r['ic_agg']:.4f} | "
            f"{r['p_raw']:.2e} | {r['mono']:.2f} | {r['n_eff']} | "
            f"{r.get('rank_in_horizon', '')} |"
        )

    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        f"- Candidate tests in FDR family: {meta['n_fdr_tests']} "
        f"(features×horizons with finite p)."
    )
    lines.append(
        f"- Pre-FDR hits (p_raw<0.05): {meta['n_pre_fdr_hits']}; "
        f"post-FDR rejects: {meta['n_fdr_rejects']}."
    )
    lines.append(
        "- CVD divergence is included as `cvd_price_div_signed` / "
        "`cvd_div_strength` (feature re-evaluation per RESEARCH_BACKLOG — "
        "not a CVDOrderFlow reopen)."
    )
    lines.append(
        "- Basis coverage is limited to the Binance-perp overlap window; "
        "n_eff drops where `bn_perp` is missing."
    )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db",
        type=Path,
        default=ROOT / "data" / "live" / "bot.db",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "data" / "backtests",
    )
    ap.add_argument(
        "--report",
        type=Path,
        default=ROOT / "docs" / "FEATURE_SCREENING_REPORT.md",
    )
    ap.add_argument(
        "--symbols",
        default=",".join(SYMBOLS_DEFAULT),
    )
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print(f"Loading {args.db} …", flush=True)
    con = _connect(args.db)
    try:
        df = build_feature_frame(con, symbols)
    finally:
        con.close()
    print(
        f"Feature frame: {len(df)} rows, {df['symbol'].nunique()} symbols, "
        f"{len(CANDIDATE_FEATURES)} candidates (+4 controls)",
        flush=True,
    )

    features = list(CANDIDATE_FEATURES) + [CONTROL_POS, *CONTROL_NEGS]
    cells: List[ScreenCell] = []
    for feat in features:
        for h_name, h_bars in HORIZONS.items():
            cell = screen_one(df, feat, h_name, h_bars)
            cells.append(cell)
            print(
                f"  {feat:28s} {h_name:4s}  IC={cell.ic_agg:+.4f}  "
                f"p={cell.p_raw:.2e}  n={cell.n_eff}  mono={cell.mono:+.2f}",
                flush=True,
            )

    rows: List[Dict[str, Any]] = []
    for c in cells:
        d = asdict(c)
        rows.append(d)

    # Rank within horizon (all features including controls)
    for h in HORIZONS:
        subset = [r for r in rows if r["horizon"] == h]
        subset.sort(key=lambda r: abs(r["ic_agg"]) if np.isfinite(r["ic_agg"]) else -1, reverse=True)
        for i, r in enumerate(subset, start=1):
            r["rank_in_horizon"] = i

    # FDR on candidates only
    cand_idx = [i for i, r in enumerate(rows) if not r["is_control"]]
    pvals = [rows[i]["p_raw"] for i in cand_idx]
    rejected, qvals = benjamini_hochberg(pvals, alpha=FDR_ALPHA)
    for j, i in enumerate(cand_idx):
        rows[i]["q_fdr"] = float(qvals[j]) if np.isfinite(qvals[j]) else float("nan")
        rows[i]["fdr_reject"] = bool(rejected[j])
    for r in rows:
        if r["is_control"]:
            r["q_fdr"] = float("nan")
            r["fdr_reject"] = False

    for r in rows:
        r["survives"] = survives_top_gate(r)

    n_pre = sum(
        1
        for r in rows
        if not r["is_control"] and np.isfinite(r["p_raw"]) and r["p_raw"] < 0.05
    )
    n_fdr = sum(1 for r in rows if r.get("fdr_reject"))
    n_tests = sum(
        1 for r in rows if not r["is_control"] and np.isfinite(r["p_raw"])
    )

    meta = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "db": str(args.db),
        "symbols": symbols,
        "n_rows": len(df),
        "n_fdr_tests": n_tests,
        "n_pre_fdr_hits": n_pre,
        "n_fdr_rejects": n_fdr,
        "elapsed_s": round(time.time() - t0, 1),
        "pit_note": (
            "Closed 15m bars; merge_asof backward for funding/OI/bn_perp; "
            "forward returns from future closes only as targets"
        ),
        "inference": "Newey-West HAC on Spearman rank products; BH FDR α=0.05",
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = args.out_dir / f"feature_screening_{stamp}.json"
    latest = args.out_dir / "feature_screening_latest.json"
    payload = {"meta": meta, "rows": rows}
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    latest.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    write_report(args.report, rows, meta)
    print(f"\nWrote {json_path}", flush=True)
    print(f"Wrote {latest}", flush=True)
    print(f"Wrote {args.report}", flush=True)
    print(
        f"Done in {meta['elapsed_s']}s — pre-FDR hits={n_pre}, "
        f"FDR rejects={n_fdr}, TOP survivors="
        f"{sum(1 for r in rows if r.get('survives'))}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
