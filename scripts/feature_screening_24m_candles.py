#!/usr/bin/env python3
"""24-month candle-only feature re-screen (measurement).

Uses ``data/research/binance_spot_proxy.db`` (validated Binance↔HL proxy).
Excludes funding / OI / basis / liquidations / CVD / taker-split (short or
partitioned feeds on this DB; not candle-derived).

Inference n = independent UTC **dates** via date-block bootstrap of Spearman IC
(not symbol-day × overlapping bars). Sub-periods ≥6; vol regimes ≥3.
Survivors go straight to taker RT cost test (11 bps).

Usage:
  python scripts/feature_screening_24m_candles.py
  python scripts/feature_screening_24m_candles.py --n-boot 1000
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from feature_screening import (  # noqa: E402
    CONTROL_NEGS,
    CONTROL_POS,
    FDR_ALPHA,
    HORIZONS,
    RNG_SEED,
    benjamini_hochberg,
    quintile_means,
    spearman_ic_hac,
)

SYMBOLS_DEFAULT = ["BTC", "ETH", "SOL", "HYPE"]
N_SUBPERIODS = 6
N_VOL_REGIMES = 3
N_BOOT_DEFAULT = 400
TAKER_RT = 0.0011  # 11 bps round-trip (2×(3.5+2))
PROXY_DB = ROOT / "data" / "research" / "binance_spot_proxy.db"

# Candle-only candidates (price / vol / calendar). Explicit exclusions below.
CANDIDATE_FEATURES = [
    "rvol_1h",
    "rvol_24h",
    "atr_percentile_7d",
    "bb_width",
    "adx_14",
    "hour_sin",
    "hour_cos",
    "dow",
    "mins_to_funding_reset",  # pure clock (HL reset schedule), no funding feed
    "ret_lag_15m",
    "ret_lag_1h",
    "ret_lag_4h",
    "ret_lag_24h",
    "autocorr_ret_1d",
    "dist_to_vwap_1d",
]

EXCLUDED_FAMILIES = {
    "funding": "funding_history empty in proxy DB; rate series not candle-OHLCV",
    "oi": "oi_history empty; OI not in spot klines",
    "basis": "binance_perp_prices empty; needs live perp mark",
    "liquidations": "liquidation_events empty; short real-feed history",
    "cvd_taker": "buy/sell volume not reliable on spot kline proxy; tape feed short",
}

# 82-day TOP cells for comparison (from docs/FEATURE_SCREENING_REPORT.md)
PRIOR_82D_TOP = [
    {"feature": "dow", "horizon": "24h", "ic": 0.2190, "note": "calendar"},
    {"feature": "atr_percentile_7d", "horizon": "24h", "ic": -0.1593, "note": "revalidated (C)"},
    {"feature": "oi_delta_24h", "horizon": "24h", "ic": 0.1349, "note": "EXCLUDED here (OI feed)"},
    {"feature": "ret_lag_4h", "horizon": "4h", "ic": -0.0649, "note": "cost-closed"},
    {"feature": "ret_lag_1h", "horizon": "1h", "ic": -0.0635, "note": "cost-closed"},
]


def _atr_percentile_fast(atr: np.ndarray, window: int, min_periods: int) -> np.ndarray:
    """Trailing percentile rank of last ATR in window via sliding windows."""
    n = len(atr)
    out = np.full(n, np.nan)
    finite = np.isfinite(atr)
    # Prefix counts of values ≤ atr[i] is expensive; use rolling apply on pandas Series
    s = pd.Series(atr)
    def _pct(x: np.ndarray) -> float:
        v = x[-1]
        if not np.isfinite(v):
            return float("nan")
        return float(np.mean(x[np.isfinite(x)] <= v))

    rolled = s.rolling(window, min_periods=min_periods).apply(_pct, raw=True)
    return rolled.to_numpy(dtype=float)


def load_ohlcv_15m(con: sqlite3.Connection, symbols: Sequence[str]) -> pd.DataFrame:
    q = f"""
        SELECT symbol, timestamp_ms, open, high, low, close, volume
        FROM candles_15m
        WHERE symbol IN ({",".join("?" * len(symbols))})
        ORDER BY symbol, timestamp_ms
    """
    return pd.read_sql_query(q, con, params=list(symbols))


def build_candle_features(df: pd.DataFrame) -> pd.DataFrame:
    pieces: List[pd.DataFrame] = []
    rng = np.random.default_rng(RNG_SEED)
    for sym, g0 in df.groupby("symbol", sort=False):
        g = g0.sort_values("timestamp_ms").reset_index(drop=True)
        close = g["close"].astype(float)
        high = g["high"].astype(float)
        low = g["low"].astype(float)
        vol = g["volume"].astype(float)

        ret_1 = close.pct_change(1)
        ret_4 = close.pct_change(4)
        ret_16 = close.pct_change(16)
        ret_96 = close.pct_change(96)

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
        atr_pctile = _atr_percentile_fast(
            atr_14.to_numpy(dtype=float), window=96 * 7, min_periods=96
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

        feat = pd.DataFrame(
            {
                "symbol": sym,
                "timestamp_ms": g["timestamp_ms"].to_numpy(),
                "close": close.to_numpy(),
                "date": ts.dt.strftime("%Y-%m-%d").to_numpy(),
                "rvol_1h": rvol_1h.to_numpy(),
                "rvol_24h": rvol_24h.to_numpy(),
                "atr_percentile_7d": atr_pctile,
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

    for name, hb in HORIZONS.items():
        fwd_all = pd.Series(np.nan, index=out.index, dtype=float)
        for _, g in out.groupby("symbol", sort=False):
            c = g["close"].astype(float).values
            r = np.full(len(c), np.nan)
            if len(c) > hb:
                r[: len(c) - hb] = c[hb:] / c[: len(c) - hb] - 1.0
            fwd_all.loc[g.index] = r
        out[f"fwd_{name}"] = fwd_all

    # Controls
    ctrl_pieces = []
    for _, g in out.groupby("symbol", sort=False):
        n = len(g)
        fwd = g["fwd_1h"].values.astype(float)
        noise = rng.normal(
            0.0,
            np.nanstd(fwd) * 0.5 if np.isfinite(np.nanstd(fwd)) else 0.001,
            size=n,
        )
        ctrl_pieces.append(
            pd.DataFrame(
                {
                    CONTROL_POS: fwd + noise,
                    CONTROL_NEGS[0]: rng.normal(size=n),
                    CONTROL_NEGS[1]: rng.normal(size=n),
                    CONTROL_NEGS[2]: rng.normal(size=n),
                },
                index=g.index,
            )
        )
    out = pd.concat([out, pd.concat(ctrl_pieces).sort_index()], axis=1)
    return out


def spearman_ic_date_block(
    feature: np.ndarray,
    forward: np.ndarray,
    dates: np.ndarray,
    horizon_bars: int,
    *,
    n_boot: int,
    seed: int,
    max_bars_per_date: int = 24,
) -> Dict[str, Any]:
    """Bar-level Spearman IC; p-value via **date-cluster bootstrap**.

    Point estimate uses all bars. Bootstrap resamples dates; within each
    selected date at most ``max_bars_per_date`` bars are kept (speed).
    Dates are factorized to int codes so cluster indexing stays O(n).
    """
    mask = np.isfinite(feature) & np.isfinite(forward) & pd.notna(dates)
    f = feature[mask].astype(float)
    r = forward[mask].astype(float)
    d = np.asarray(dates[mask])
    n_bars = int(len(f))
    if n_bars < 30:
        return {
            "ic": float("nan"),
            "p_date_boot": float("nan"),
            "p_nw": float("nan"),
            "n_bars": n_bars,
            "n_dates": 0,
        }
    rho, p_nw, _ = spearman_ic_hac(f, r, horizon_bars)
    codes, uniq = pd.factorize(d, sort=True)
    n_dates = int(len(uniq))
    if n_dates < 20 or not np.isfinite(rho):
        return {
            "ic": float(rho) if np.isfinite(rho) else float("nan"),
            "p_date_boot": float("nan"),
            "p_nw": float(p_nw) if np.isfinite(p_nw) else float("nan"),
            "n_bars": n_bars,
            "n_dates": n_dates,
        }

    rng0 = np.random.default_rng(seed)
    mapping: List[np.ndarray] = []
    for c in range(n_dates):
        idx = np.flatnonzero(codes == c)
        if len(idx) > max_bars_per_date:
            idx = rng0.choice(idx, size=max_bars_per_date, replace=False)
        mapping.append(idx)

    rng = np.random.default_rng(seed + 17)
    boots = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        sample = rng.integers(0, n_dates, size=n_dates)
        idx = np.concatenate([mapping[c] for c in sample])
        if len(idx) < 30:
            boots[b] = float("nan")
            continue
        fb, rb = f[idx], r[idx]
        rf = stats.rankdata(fb)
        rr = stats.rankdata(rb)
        boots[b] = float(np.corrcoef(rf, rr)[0, 1])
    boots = boots[np.isfinite(boots)]
    if len(boots) < 50:
        p_boot = float("nan")
    else:
        p_boot = float(2.0 * min(np.mean(boots <= 0), np.mean(boots >= 0)))
        p_boot = min(max(p_boot, 1.0 / max(len(boots), 1)), 1.0)
    return {
        "ic": float(rho) if np.isfinite(rho) else float("nan"),
        "p_date_boot": p_boot,
        "p_nw": float(p_nw) if np.isfinite(p_nw) else float("nan"),
        "n_bars": n_bars,
        "n_dates": n_dates,
    }


def attach_btc_vol_regime(df: pd.DataFrame) -> pd.DataFrame:
    """BTC 30d realized-vol terciles on daily closes → regime label per date."""
    btc = df.loc[df["symbol"] == "BTC", ["date", "timestamp_ms", "close"]].copy()
    if btc.empty:
        df["vol_regime"] = "unknown"
        return df
    daily = (
        btc.sort_values("timestamp_ms")
        .groupby("date", as_index=False)
        .agg(close=("close", "last"))
    )
    daily = daily.copy()
    daily["r"] = daily["close"].pct_change()
    daily["rvol_30d"] = daily["r"].rolling(30, min_periods=10).std(ddof=0) * math.sqrt(
        365
    )
    valid = daily["rvol_30d"].dropna()
    if len(valid) < 30:
        df["vol_regime"] = "unknown"
        return df
    q33, q66 = np.quantile(valid, [1 / 3, 2 / 3])
    def _lab(x: float) -> str:
        if not np.isfinite(x):
            return "unknown"
        if x <= q33:
            return "low_vol"
        if x <= q66:
            return "mid_vol"
        return "high_vol"

    daily["vol_regime"] = daily["rvol_30d"].map(_lab)
    regime_map = dict(zip(daily["date"], daily["vol_regime"]))
    df = df.copy()
    df["vol_regime"] = df["date"].map(regime_map).fillna("unknown")
    df.attrs["vol_cuts"] = {"q33": float(q33), "q66": float(q66)}
    return df


@dataclass
class CellResult:
    feature: str
    horizon: str
    ic: float
    p_raw: float  # date-block bootstrap (primary for FDR)
    p_nw: float
    n_bars: int
    n_dates: int
    mono: float
    quintile_means: List[float]
    same_sign_symbols: int
    n_symbols: int
    same_sign_periods: int
    n_periods: int
    same_sign_regimes: int
    n_regimes: int
    ic_by_period: List[float]
    ic_by_regime: Dict[str, float]
    is_control: bool


def screen_cell(
    df: pd.DataFrame,
    feature: str,
    horizon: str,
    *,
    n_boot: int,
    seed: int,
) -> CellResult:
    hb = HORIZONS[horizon]
    fcol, rcol = feature, f"fwd_{horizon}"
    sub = df[["symbol", "date", "vol_regime", "timestamp_ms", fcol, rcol]].dropna(
        subset=[fcol, rcol]
    )
    stats_all = spearman_ic_date_block(
        sub[fcol].to_numpy(),
        sub[rcol].to_numpy(),
        sub["date"].to_numpy(),
        hb,
        n_boot=n_boot,
        seed=seed,
    )
    qmeans, mono = quintile_means(sub[fcol].to_numpy(), sub[rcol].to_numpy())

    ic = stats_all["ic"]
    sign = np.sign(ic) if np.isfinite(ic) and ic != 0 else 0.0

    # Cross-symbol
    same_sym = 0
    n_sym = 0
    for _, g in sub.groupby("symbol"):
        ics, _ = stats.spearmanr(g[fcol], g[rcol]) if len(g) >= 30 else (float("nan"), None)
        if np.isfinite(ics):
            n_sym += 1
            if sign == 0 or np.sign(ics) == sign:
                same_sym += 1

    # ≥6 equal date blocks
    dates = np.array(sorted(sub["date"].unique()))
    ic_per: List[float] = []
    if len(dates) >= N_SUBPERIODS * 10:
        edges = np.linspace(0, len(dates), N_SUBPERIODS + 1).astype(int)
        for i in range(N_SUBPERIODS):
            block_dates = set(dates[edges[i] : edges[i + 1]].tolist())
            g = sub.loc[sub["date"].isin(block_dates)]
            if len(g) < 50:
                ic_per.append(float("nan"))
                continue
            # Bar-level IC inside the date block (same object as primary IC)
            if len(g) < 50:
                ic_per.append(float("nan"))
                continue
            ics, _ = stats.spearmanr(g[fcol], g[rcol])
            ic_per.append(float(ics) if np.isfinite(ics) else float("nan"))
    else:
        ic_per = [float("nan")] * N_SUBPERIODS
    finite_per = [v for v in ic_per if np.isfinite(v)]
    same_per = sum(1 for v in finite_per if sign != 0 and np.sign(v) == sign)

    # Vol regimes (bar-level within regime)
    ic_reg: Dict[str, float] = {}
    for reg, g in sub.groupby("vol_regime"):
        if reg == "unknown" or len(g) < 50:
            continue
        ics, _ = stats.spearmanr(g[fcol], g[rcol])
        if np.isfinite(ics):
            ic_reg[str(reg)] = float(ics)
    same_reg = sum(1 for v in ic_reg.values() if sign != 0 and np.sign(v) == sign)

    is_ctrl = feature == CONTROL_POS or feature in CONTROL_NEGS
    return CellResult(
        feature=feature,
        horizon=horizon,
        ic=float(ic) if np.isfinite(ic) else float("nan"),
        p_raw=float(stats_all["p_date_boot"])
        if np.isfinite(stats_all["p_date_boot"])
        else float("nan"),
        p_nw=float(stats_all["p_nw"]) if np.isfinite(stats_all["p_nw"]) else float("nan"),
        n_bars=int(stats_all["n_bars"]),
        n_dates=int(stats_all["n_dates"]),
        mono=float(mono) if np.isfinite(mono) else float("nan"),
        quintile_means=[float(x) if np.isfinite(x) else float("nan") for x in qmeans],
        same_sign_symbols=same_sym,
        n_symbols=n_sym,
        same_sign_periods=same_per,
        n_periods=len(finite_per),
        same_sign_regimes=same_reg,
        n_regimes=len(ic_reg),
        ic_by_period=ic_per,
        ic_by_regime=ic_reg,
        is_control=is_ctrl,
    )


def survives_strict(row: Dict[str, Any]) -> bool:
    """FDR + mono + ≥5/6 blocks + ≥2/3 regimes + ≥3 symbols."""
    if row.get("is_control"):
        return False
    if not row.get("fdr_reject", False):
        return False
    mono = row.get("mono")
    if mono is None or not np.isfinite(mono) or abs(mono) < 0.8:
        return False
    if int(row.get("same_sign_periods", 0)) < 5:
        return False
    if int(row.get("n_periods", 0)) < 6:
        return False
    if int(row.get("same_sign_regimes", 0)) < 2:
        return False
    if int(row.get("n_regimes", 0)) < 3:
        return False
    if int(row.get("same_sign_symbols", 0)) < 3:
        return False
    return True


def _side_from_signal(sig: float, ic: float, feature: str) -> int:
    """Map feature → trade side using screening IC sign."""
    if not np.isfinite(sig) or not np.isfinite(ic) or ic == 0:
        return 0
    if feature == "atr_percentile_7d":
        # fade high vol when IC negative (historical)
        return -1 if sig > 0.5 else +1 if ic < 0 else (+1 if sig > 0.5 else -1)
    if feature == "dow":
        # descriptive only — still evaluate for cost completeness
        return +1 if sig >= 3 else -1
    # follow IC sign: positive IC → long when feature high
    if ic > 0:
        return +1 if sig > 0 else -1 if sig < 0 else 0
    return -1 if sig > 0 else +1 if sig < 0 else 0


def cost_test_survivor(
    df: pd.DataFrame,
    feature: str,
    horizon: str,
    ic: float,
    *,
    n_boot: int = 1500,
    seed: int = 42,
) -> Dict[str, Any]:
    """Non-overlap by date (one trade / symbol / day at first bar), date-block CI."""
    hb = HORIZONS[horizon]
    sub = df.dropna(subset=[feature, f"fwd_{horizon}", "close"]).copy()
    sub = sub.sort_values(["symbol", "timestamp_ms"])
    # Non-overlap: keep first bar of each (symbol, date)
    first = sub.groupby(["symbol", "date"], as_index=False).first()
    sides = []
    gross = []
    dates = []
    for _, row in first.iterrows():
        side = _side_from_signal(float(row[feature]), ic, feature)
        if side == 0:
            continue
        g = float(row[f"fwd_{horizon}"]) * side
        if not np.isfinite(g):
            continue
        sides.append(side)
        gross.append(g)
        dates.append(row["date"])
    if len(gross) < 30:
        return {
            "n_trades": len(gross),
            "n_dates": 0,
            "breakeven_rt_bps": float("nan"),
            "edge_mean_bps": float("nan"),
            "ci": None,
            "clears_11bps": False,
        }
    gross_a = np.asarray(gross, dtype=float)
    # BE: 2 * mean(gross) in bps if mean>0 else nan/negative
    mu = float(np.mean(gross_a))
    be_bps = float(mu * 1e4)  # gross expectancy in bps ≈ max RT that zeros expectancy
    # Date-block: mean gross per UTC date then bootstrap edge vs taker
    by_date: Dict[str, List[float]] = {}
    for d, g in zip(dates, gross_a):
        by_date.setdefault(str(d), []).append(float(g))
    date_means = np.array([float(np.mean(v)) for v in by_date.values()], dtype=float)
    n_dates = len(date_means)
    rng = np.random.default_rng(seed)
    edge = date_means - TAKER_RT
    boots = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        idx = rng.integers(0, n_dates, size=n_dates)
        boots[b] = float(np.mean(edge[idx]))
    ci_lo, ci_hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
    p_gt = float(np.mean(boots > 0))
    clears = bool(be_bps > 11.0 and ci_lo > 0)
    return {
        "n_trades": int(len(gross_a)),
        "n_dates": int(n_dates),
        "breakeven_rt_bps": be_bps,
        "mean_gross_bps": float(mu * 1e4),
        "edge_mean_bps": float(np.mean(edge) * 1e4),
        "ci_low_bps": ci_lo * 1e4,
        "ci_high_bps": ci_hi * 1e4,
        "p_edge_gt_0": p_gt,
        "clears_11bps": clears,
        "taker_rt_bps": 11.0,
    }


def write_report(
    path: Path,
    rows: List[Dict[str, Any]],
    meta: Dict[str, Any],
    costs: Dict[str, Any],
) -> None:
    cand = [r for r in rows if not r["is_control"]]
    top = [r for r in cand if r.get("survives")]
    top.sort(key=lambda r: abs(r.get("ic") or 0), reverse=True)

    lines: List[str] = []
    lines.append("# Feature Screening — 24m candle-only re-screen")
    lines.append("")
    lines.append(f"Generated: {meta['generated_at']}")
    lines.append(f"DB: `{meta['db']}`")
    lines.append(f"Symbols: {', '.join(meta['symbols'])}")
    lines.append(
        f"Bars: {meta['n_bars']:,} · unique dates: **{meta['n_dates']}** · "
        f"span {meta['date_min']} → {meta['date_max']}"
    )
    lines.append(
        "Inference: bar-level Spearman IC; **date-cluster bootstrap** p-values "
        f"(n_boot={meta['n_boot']}, independent unit = UTC date); FDR BH α={FDR_ALPHA}. "
        "Newey–West HAC p is diagnostic only. "
        "Do **not** aggregate feature/return to daily means before IC — that "
        "turns short-horizon fade into spurious day-momentum."
    )
    lines.append("")
    lines.append("## Exclusions (declared)")
    lines.append("")
    for fam, why in EXCLUDED_FAMILIES.items():
        lines.append(f"- **{fam}:** {why}")
    lines.append(
        "- `mins_to_funding_reset` retained as **calendar/clock** only "
        "(no funding-rate feed)."
    )
    lines.append("")
    lines.append("## Pipeline controls")
    lines.append("")
    for h in HORIZONS:
        ranked = sorted(
            [r for r in rows if r["horizon"] == h],
            key=lambda r: abs(r.get("ic") or 0),
            reverse=True,
        )
        for i, r in enumerate(ranked, start=1):
            if r["feature"] == CONTROL_POS:
                lines.append(
                    f"- Horizon {h}: positive control rank **#{i}/{len(ranked)}** "
                    f"(IC={r['ic']:.4f}, n_dates={r['n_dates']})"
                )
        neg = [
            r["ic"]
            for r in ranked
            if r["feature"] in CONTROL_NEGS and np.isfinite(r.get("ic", float("nan")))
        ]
        if neg:
            lines.append(
                f"- Horizon {h}: negative |IC| max={max(abs(x) for x in neg):.4f} "
                f"(mean={float(np.mean([abs(x) for x in neg])):.4f})"
            )
    lines.append("")
    lines.append(
        f"**Validation:** {meta['controls_note']}"
    )
    lines.append("")
    lines.append("## TOP survivors (strict gate)")
    lines.append("")
    lines.append(
        "Gate: FDR + |mono|≥0.8 + same-sign IC in **≥5/6** date blocks + "
        "**≥2/3** vol regimes + ≥3/4 symbols."
    )
    lines.append("")
    if not top:
        lines.append("**None.**")
    else:
        lines.append(
            "| feature | h | IC | p_date | q_FDR | mono | blocks | regimes | sym | n_dates |"
        )
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in top:
            lines.append(
                f"| {r['feature']} | {r['horizon']} | {r['ic']:.4f} | "
                f"{r['p_raw']:.2e} | {r.get('q_fdr', float('nan')):.3f} | "
                f"{r['mono']:.2f} | {r['same_sign_periods']}/{r['n_periods']} | "
                f"{r['same_sign_regimes']}/{r['n_regimes']} | "
                f"{r['same_sign_symbols']}/{r['n_symbols']} | {r['n_dates']} |"
            )
    lines.append("")
    lines.append("## Cost test (survivors + FDR∩mono + prior-82d candle cells)")
    lines.append("")
    if not costs:
        lines.append("No cells to cost-test.")
    else:
        lines.append(
            "| feature | h | why | BE RT bps | edge mean | CI vs 11bps | n_dates | clears? |"
        )
        lines.append("|---|---|---|---:|---:|---|---:|:---:|")
        for key, c in costs.items():
            ci = (
                f"[{c.get('ci_low_bps', float('nan')):.1f}, "
                f"{c.get('ci_high_bps', float('nan')):.1f}]"
            )
            lines.append(
                f"| {c.get('feature', key)} | {c.get('horizon','')} | "
                f"{c.get('reason','')} | {c.get('breakeven_rt_bps', float('nan')):.2f} | "
                f"{c.get('edge_mean_bps', float('nan')):.2f} | {ci} | "
                f"{c.get('n_dates', 0)} | {'Y' if c.get('clears_11bps') else 'n'} |"
            )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"### **({meta['verdict']})** — {meta['verdict_text']}")
    lines.append("")
    lines.append("## Comparison vs 82-day screening")
    lines.append("")
    lines.append("| 82d TOP | 24m candle status |")
    lines.append("|---|---|")
    for p in PRIOR_82D_TOP:
        feat, h = p["feature"], p["horizon"]
        match = next(
            (r for r in cand if r["feature"] == feat and r["horizon"] == h),
            None,
        )
        if feat in ("oi_delta_24h",):
            status = "excluded (OI feed)"
        elif match is None:
            status = "not in candle set"
        else:
            status = (
                f"IC={match['ic']:.4f}, FDR={'Y' if match.get('fdr_reject') else 'n'}, "
                f"blocks={match['same_sign_periods']}/{match['n_periods']}, "
                f"regimes={match['same_sign_regimes']}/{match['n_regimes']}, "
                f"survives={match.get('survives')}"
            )
        lines.append(f"| {feat}@{h} (IC={p['ic']:+.3f}, {p['note']}) | {status} |")
    lines.append("")
    lines.append(
        "Lesson from atr_percentile: bar-level n inflated power on ~70 dates; "
        "date-block inference on ~700 dates kills regime artifacts."
    )
    lines.append("")
    lines.append("## Full ranking (candidates)")
    lines.append("")
    lines.append(
        "| feature | h | IC | p_date | q_FDR | FDR | mono | blocks | regimes | sym | n_dates | n_bars |"
    )
    lines.append("|---|---|---:|---:|---:|:---:|---:|---:|---:|---:|---:|---:|")
    cand_sorted = sorted(cand, key=lambda r: abs(r.get("ic") or 0), reverse=True)
    for r in cand_sorted:
        lines.append(
            f"| {r['feature']} | {r['horizon']} | {r['ic']:.4f} | "
            f"{r['p_raw']:.2e} | {r.get('q_fdr', float('nan')):.3f} | "
            f"{'Y' if r.get('fdr_reject') else 'n'} | {r['mono']:.2f} | "
            f"{r['same_sign_periods']}/{r['n_periods']} | "
            f"{r['same_sign_regimes']}/{r['n_regimes']} | "
            f"{r['same_sign_symbols']}/{r['n_symbols']} | "
            f"{r['n_dates']} | {r['n_bars']} |"
        )
    lines.append("")
    lines.append("## Continuação")
    lines.append("")
    if meta["verdict"] == "A":
        lines.append(
            "First serious candle-feature candidate(s) cleared costs — "
            "build minimal strategy → baseline-signal gate (not done in this run)."
        )
    else:
        lines.append(
            "No candle-derived directional edge clears FDR + stability + 11 bps taker "
            "on 24 months. Prefer families with inverted cost structure "
            "(market making / spread capture) — already parked in research backlog; "
            "L2 recording on E: is the enabling data path."
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=PROXY_DB)
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data" / "backtests")
    ap.add_argument("--doc", type=Path, default=ROOT / "docs" / "FEATURE_SCREENING_24M_CANDLES.md")
    ap.add_argument("--symbols", default=",".join(SYMBOLS_DEFAULT))
    ap.add_argument("--n-boot", type=int, default=N_BOOT_DEFAULT)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    t0 = time.time()
    print(f"Loading {args.db} ...")
    con = sqlite3.connect(str(args.db))
    raw = load_ohlcv_15m(con, symbols)
    con.close()
    if raw.empty:
        raise SystemExit("No candles_15m in proxy DB")

    print(f"Building candle features on {len(raw):,} bars ...")
    df = build_candle_features(raw)
    df = attach_btc_vol_regime(df)
    n_dates = int(df["date"].nunique())
    date_min, date_max = str(df["date"].min()), str(df["date"].max())
    print(f"Dates={n_dates} span={date_min}->{date_max}")

    features = list(CANDIDATE_FEATURES) + [CONTROL_POS, *CONTROL_NEGS]
    cells: List[CellResult] = []
    seed = RNG_SEED
    for feat in features:
        for h in HORIZONS:
            seed += 1
            print(f"  screen {feat}@{h} ...", flush=True)
            cells.append(
                screen_cell(df, feat, h, n_boot=args.n_boot, seed=seed)
            )

    rows: List[Dict[str, Any]] = []
    for c in cells:
        rows.append(
            {
                "feature": c.feature,
                "horizon": c.horizon,
                "ic": c.ic,
                "p_raw": c.p_raw,
                "p_nw": c.p_nw,
                "n_bars": c.n_bars,
                "n_dates": c.n_dates,
                "mono": c.mono,
                "quintile_means": c.quintile_means,
                "same_sign_symbols": c.same_sign_symbols,
                "n_symbols": c.n_symbols,
                "same_sign_periods": c.same_sign_periods,
                "n_periods": c.n_periods,
                "same_sign_regimes": c.same_sign_regimes,
                "n_regimes": c.n_regimes,
                "ic_by_period": c.ic_by_period,
                "ic_by_regime": c.ic_by_regime,
                "is_control": c.is_control,
            }
        )

    cand_idx = [i for i, r in enumerate(rows) if not r["is_control"]]
    pvals = [rows[i]["p_raw"] for i in cand_idx]
    rejected, qvals = benjamini_hochberg(pvals, alpha=FDR_ALPHA)
    for j, i in enumerate(cand_idx):
        rows[i]["fdr_reject"] = bool(rejected[j])
        rows[i]["q_fdr"] = float(qvals[j]) if np.isfinite(qvals[j]) else float("nan")
    for r in rows:
        if r["is_control"]:
            r["fdr_reject"] = False
            r["q_fdr"] = float("nan")
            r["survives"] = False
        else:
            r["survives"] = survives_strict(r)

    # Controls validation
    ctrl_ok_pos = True
    ctrl_ok_neg = True
    for h in HORIZONS:
        ranked = sorted(
            [r for r in rows if r["horizon"] == h],
            key=lambda r: abs(r.get("ic") or 0),
            reverse=True,
        )
        pos_rank = next(
            (i for i, r in enumerate(ranked, 1) if r["feature"] == CONTROL_POS),
            999,
        )
        if pos_rank > 3:
            ctrl_ok_pos = False
        neg_ics = [
            abs(r["ic"])
            for r in ranked
            if r["feature"] in CONTROL_NEGS and np.isfinite(r.get("ic", float("nan")))
        ]
        if neg_ics and max(neg_ics) > 0.05:
            ctrl_ok_neg = False
    controls_note = (
        f"positive near-top: {'PASS' if ctrl_ok_pos else 'FAIL'}; "
        f"negatives |IC|≈0: {'PASS' if ctrl_ok_neg else 'FAIL'}"
    )

    # Cost tests: strict survivors + prior-82d candle cells + FDR∩|mono|≥0.8
    cost_keys: Dict[Tuple[str, str], float] = {}
    for r in rows:
        if r.get("is_control"):
            continue
        key = (r["feature"], r["horizon"])
        if r.get("survives"):
            cost_keys[key] = r["ic"]
        elif r.get("fdr_reject") and np.isfinite(r.get("mono", float("nan"))) and abs(r["mono"]) >= 0.8:
            cost_keys[key] = r["ic"]
    for p in PRIOR_82D_TOP:
        if p["feature"] == "oi_delta_24h":
            continue
        match = next(
            (
                r
                for r in rows
                if r["feature"] == p["feature"] and r["horizon"] == p["horizon"]
            ),
            None,
        )
        if match is not None and np.isfinite(match.get("ic", float("nan"))):
            cost_keys[(p["feature"], p["horizon"])] = match["ic"]

    costs: Dict[str, Any] = {}
    for (feat, hor), ic in sorted(cost_keys.items()):
        key = f"{feat}@{hor}"
        print(f"  cost-test {key} ...", flush=True)
        c = cost_test_survivor(df, feat, hor, ic, n_boot=max(args.n_boot, 400))
        c["horizon"] = hor
        c["feature"] = feat
        c["reason"] = (
            "survivor"
            if any(
                r.get("survives") and r["feature"] == feat and r["horizon"] == hor
                for r in rows
            )
            else "fdr_mono_or_prior82d"
        )
        costs[key] = c

    any_clear = any(c.get("clears_11bps") for c in costs.values())
    any_surv = any(r.get("survives") for r in rows)
    if any_surv and any_clear:
        verdict, vtext = (
            "A",
            "At least one candle feature survives FDR+stability and clears 11 bps "
            "taker with date-block CI — first serious candidate.",
        )
    elif any_surv and not any_clear:
        verdict, vtext = (
            "C",
            "Feature(s) survived statistical gates but failed the 11 bps cost test "
            "(or CI straddles zero) — not exploitable at bot taker costs.",
        )
    else:
        verdict, vtext = (
            "C",
            "No candle-derived feature survives FDR + ≥5/6 blocks + ≥2/3 regimes "
            "on 24 months with date-cluster inference. No directional edge at these costs.",
        )

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(args.db.resolve()),
        "symbols": symbols,
        "n_bars": int(len(df)),
        "n_dates": n_dates,
        "date_min": date_min,
        "date_max": date_max,
        "n_boot": args.n_boot,
        "elapsed_s": round(time.time() - t0, 1),
        "controls_note": controls_note,
        "verdict": verdict,
        "verdict_text": vtext,
        "excluded": EXCLUDED_FAMILIES,
        "vol_cuts": df.attrs.get("vol_cuts"),
        "candidates": CANDIDATE_FEATURES,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "feature_screening_24m_candles.json"
    payload = {"meta": meta, "rows": rows, "costs": costs, "prior_82d": PRIOR_82D_TOP}
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    write_report(args.doc, rows, meta, costs)
    print(
        f"Done in {meta['elapsed_s']}s — verdict ({verdict}) — "
        f"TOP={sum(1 for r in rows if r.get('survives'))} — "
        f"wrote {args.doc} and {json_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
