#!/usr/bin/env python3
"""24m price-STRUCTURE feature screen (measurement only).

Closes the gap left by ``feature_screening_24m_candles.py``: continuous
S/R, Donchian, Bollinger %B, breakout state, confirmed pivots, channel
slope, range compression — with **documented confirmation lag** and a
deliberate look-ahead control that must rank at the top.

FDR is run on the **enlarged** family (prior candle candidates + structure),
not a separate family. Survivors → side-distribution check → 11 bps cost test.

Usage:
  python scripts/feature_screening_24m_structure.py
  python scripts/feature_screening_24m_structure.py --n-boot 200
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from dataclasses import asdict
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
)
from feature_screening_24m_candles import (  # noqa: E402
    CANDIDATE_FEATURES as BASE_CANDLES,
    PROXY_DB,
    SYMBOLS_DEFAULT,
    TAKER_RT,
    attach_btc_vol_regime,
    build_candle_features,
    load_ohlcv_15m,
    screen_cell,
    survives_strict,
)

# ── Confirmation lags (bars on the 15m grid) ───────────────────────────────
# Documented per-feature in FEATURE_LAGS below. Pivot swings need k bars of
# confirmation on EACH side → known only at pivot_index + k.
PIVOT_CONFIRM_K = 3
STRUCTURE_NS = (20, 50, 100)
MAKER_FEE_BPS = 1.5  # corrected HL tier-0 0.015% per side
MAKER_RT_BPS = 2 * MAKER_FEE_BPS  # 3.0 if both sides maker
BE_MAKER_GATE_BPS = 4.0  # only evaluate maker if gross BE ≥ this

CONTROL_LOOKAHEAD = "CONTROL_LOOKAHEAD_dist_future_high_96"

# name → confirmation lag in bars (0 = trailing window only, no future)
FEATURE_LAGS: Dict[str, int] = {}


def _register_lag(name: str, lag: int) -> str:
    FEATURE_LAGS[name] = int(lag)
    return name


def _atr14(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev = np.roll(close, 1)
    prev[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    return pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy(dtype=float)


def _confirmed_pivots(
    high: np.ndarray,
    low: np.ndarray,
    *,
    k: int = PIVOT_CONFIRM_K,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Most-recent confirmed swing high/low + touch counts, causal at t.

    A swing high at index ``i`` requires ``high[i]`` strictly greater than
    ``high[i-k:i]`` and ``high[i+1:i+k+1]``. It becomes known at ``i+k``.
    """
    n = len(high)
    last_hi = np.full(n, np.nan)
    last_lo = np.full(n, np.nan)
    touches_hi = np.full(n, np.nan)
    touches_lo = np.full(n, np.nan)

    events_hi: List[Tuple[int, float]] = []  # (confirm_idx, price)
    events_lo: List[Tuple[int, float]] = []

    for i in range(k, n - k):
        left_h = high[i - k : i]
        right_h = high[i + 1 : i + k + 1]
        if high[i] > np.max(left_h) and high[i] >= np.max(right_h):
            events_hi.append((i + k, float(high[i])))
        left_l = low[i - k : i]
        right_l = low[i + 1 : i + k + 1]
        if low[i] < np.min(left_l) and low[i] <= np.min(right_l):
            events_lo.append((i + k, float(low[i])))

    # Replay events onto the timeline (O(n + events))
    hi_lvls: List[float] = []
    lo_lvls: List[float] = []
    hi_touch = 0
    lo_touch = 0
    eh = el = 0
    for t in range(n):
        while eh < len(events_hi) and events_hi[eh][0] <= t:
            px = events_hi[eh][1]
            if hi_lvls and abs(px - hi_lvls[-1]) / hi_lvls[-1] < 0.0015:
                hi_touch += 1
            else:
                hi_touch = 1
            hi_lvls.append(px)
            eh += 1
        while el < len(events_lo) and events_lo[el][0] <= t:
            px = events_lo[el][1]
            if lo_lvls and abs(px - lo_lvls[-1]) / lo_lvls[-1] < 0.0015:
                lo_touch += 1
            else:
                lo_touch = 1
            lo_lvls.append(px)
            el += 1
        if hi_lvls:
            last_hi[t] = hi_lvls[-1]
            touches_hi[t] = float(hi_touch)
        if lo_lvls:
            last_lo[t] = lo_lvls[-1]
            touches_lo[t] = float(lo_touch)
    return last_hi, last_lo, touches_hi, touches_lo


def _bars_since_break(
    close: np.ndarray,
    extreme: np.ndarray,
    *,
    direction: str,
) -> Tuple[np.ndarray, np.ndarray]:
    """Bars since close crossed prior Donchian extreme; magnitude at break.

    ``extreme`` must already be lagged (prior window max/min), so a cross at
    ``t`` uses only data ≤ ``t``.
    """
    n = len(close)
    bars = np.full(n, np.nan)
    mag = np.full(n, np.nan)
    last = -10_000
    last_mag = float("nan")
    for t in range(n):
        ex = extreme[t]
        c = close[t]
        if not np.isfinite(ex) or not np.isfinite(c) or ex == 0:
            continue
        broke = (c > ex) if direction == "high" else (c < ex)
        if broke:
            last = t
            last_mag = (c - ex) / ex if direction == "high" else (ex - c) / ex
        if last >= 0:
            bars[t] = float(t - last)
            mag[t] = last_mag
    return bars, mag


def build_structure_on_ohlcv(raw: pd.DataFrame) -> pd.DataFrame:
    """Build structure feature frame from OHLCV 15m (per symbol)."""
    FEATURE_LAGS.clear()
    pieces: List[pd.DataFrame] = []

    for sym, g0 in raw.groupby("symbol", sort=False):
        g = g0.sort_values("timestamp_ms").reset_index(drop=True)
        close = g["close"].astype(float).to_numpy()
        high = g["high"].astype(float).to_numpy()
        low = g["low"].astype(float).to_numpy()
        n = len(g)
        atr = _atr14(high, low, close)
        atr_safe = np.where(np.isfinite(atr) & (atr > 0), atr, np.nan)

        last_hi, last_lo, touch_hi, touch_lo = _confirmed_pivots(high, low, k=PIVOT_CONFIRM_K)

        dist_sup_pct = (close - last_lo) / close
        dist_res_pct = (last_hi - close) / close
        dist_near_pct = np.where(
            np.isfinite(dist_sup_pct) & np.isfinite(dist_res_pct),
            np.where(dist_sup_pct <= dist_res_pct, dist_sup_pct, -dist_res_pct),
            np.where(np.isfinite(dist_sup_pct), dist_sup_pct, -dist_res_pct),
        )
        dist_near_atr = dist_near_pct * close / atr_safe
        level_touches = np.where(
            np.isfinite(dist_sup_pct) & np.isfinite(dist_res_pct),
            np.where(dist_sup_pct <= dist_res_pct, touch_lo, touch_hi),
            np.where(np.isfinite(touch_lo), touch_lo, touch_hi),
        )

        cols: Dict[str, np.ndarray] = {
            "symbol": np.full(n, sym, dtype=object),
            "timestamp_ms": g["timestamp_ms"].to_numpy(),
        }

        cols[_register_lag("dist_nearest_sr_pct", PIVOT_CONFIRM_K)] = dist_near_pct
        cols[_register_lag("dist_nearest_sr_atr", PIVOT_CONFIRM_K)] = dist_near_atr
        cols[_register_lag("level_strength_touches", PIVOT_CONFIRM_K)] = level_touches.astype(
            float
        )
        cols[_register_lag("dist_pivot_hi_pct", PIVOT_CONFIRM_K)] = (last_hi - close) / close
        cols[_register_lag("dist_pivot_lo_pct", PIVOT_CONFIRM_K)] = (close - last_lo) / close
        cols[_register_lag("dist_pivot_hi_atr", PIVOT_CONFIRM_K)] = (last_hi - close) / atr_safe
        cols[_register_lag("dist_pivot_lo_atr", PIVOT_CONFIRM_K)] = (close - last_lo) / atr_safe

        ma20 = pd.Series(close).rolling(20, min_periods=20).mean().to_numpy()
        sd20 = pd.Series(close).rolling(20, min_periods=20).std(ddof=0).to_numpy()
        upper = ma20 + 2 * sd20
        lower = ma20 - 2 * sd20
        bb_pctb = (close - lower) / np.where((upper - lower) != 0, upper - lower, np.nan)
        cols[_register_lag("bb_pctb_20", 0)] = bb_pctb

        for N in STRUCTURE_NS:
            hh = pd.Series(high).rolling(N, min_periods=N).max()
            ll = pd.Series(low).rolling(N, min_periods=N).min()
            width = (hh - ll).replace(0, np.nan)
            cols[_register_lag(f"donchian_pos_{N}", 0)] = (
                (pd.Series(close) - ll) / width
            ).to_numpy()

            prev_hh = hh.shift(1).to_numpy()
            prev_ll = ll.shift(1).to_numpy()
            bsh, mag_h = _bars_since_break(close, prev_hh, direction="high")
            bsl, mag_l = _bars_since_break(close, prev_ll, direction="low")
            cols[_register_lag(f"bars_since_break_hi_{N}", 0)] = bsh
            cols[_register_lag(f"bars_since_break_lo_{N}", 0)] = bsl
            cols[_register_lag(f"breakout_mag_hi_atr_{N}", 0)] = mag_h * close / atr_safe
            cols[_register_lag(f"breakout_mag_lo_atr_{N}", 0)] = mag_l * close / atr_safe

            mid = ((hh + ll) / 2.0).to_numpy()
            mid_s = pd.Series(mid)
            slope = (mid_s - mid_s.shift(N)) / mid_s.shift(N).replace(0, np.nan)
            cols[_register_lag(f"channel_slope_{N}", 0)] = slope.to_numpy()

            amp = pd.Series(high - low).rolling(N, min_periods=max(5, N // 4)).mean()
            cols[_register_lag(f"range_compress_{N}", 0)] = (
                amp / pd.Series(atr_safe)
            ).to_numpy()

        # Deliberate look-ahead control — INTENTIONAL LEAK (excluded from FDR).
        # Window = 96 bars (24h) so the leak remains informative on every
        # screened horizon (15m…24h). A 20-bar future max is too short vs fwd_24h
        # and can fall out of the top ranks without implying a pipeline bug.
        future_high = (
            pd.Series(high).shift(-1).rolling(96, min_periods=96).max().to_numpy()
        )
        cols[CONTROL_LOOKAHEAD] = (future_high - close) / close
        FEATURE_LAGS[CONTROL_LOOKAHEAD] = -96

        pieces.append(pd.DataFrame(cols))

    out = pd.concat(pieces, ignore_index=True)
    names = sorted(
        c
        for c in out.columns
        if c not in ("symbol", "timestamp_ms", CONTROL_LOOKAHEAD)
    )
    out.attrs["structure_features"] = names
    out.attrs["feature_lags"] = dict(FEATURE_LAGS)
    return out


def merge_candle_and_structure(
    candle_df: pd.DataFrame, struct_df: pd.DataFrame
) -> pd.DataFrame:
    keys = ["symbol", "timestamp_ms"]
    merged = candle_df.merge(struct_df, on=keys, how="inner", suffixes=("", "_s"))
    return merged


def side_distribution(
    feature: np.ndarray,
    *,
    ic: float,
    rule: str,
) -> Dict[str, Any]:
    """% long vs short under a derived trading rule."""
    f = feature[np.isfinite(feature)]
    if len(f) < 30 or not np.isfinite(ic) or ic == 0:
        return {"rule": rule, "pct_long": float("nan"), "pct_short": float("nan"), "n": 0}
    if rule == "sign":
        # broken for always-positive features — kept for diagnosis
        sides = np.sign(f)
        if ic < 0:
            sides = -sides
    elif rule == "median_split":
        med = float(np.median(f))
        if ic > 0:
            sides = np.where(f > med, 1.0, -1.0)
        else:
            sides = np.where(f > med, -1.0, 1.0)
    elif rule == "extremes_q1q5":
        q20, q80 = np.quantile(f, [0.2, 0.8])
        mask = (f <= q20) | (f >= q80)
        f2 = f[mask]
        if ic > 0:
            sides = np.where(f2 >= q80, 1.0, -1.0)
        else:
            sides = np.where(f2 >= q80, -1.0, 1.0)
    else:
        raise ValueError(rule)
    sides = sides[sides != 0]
    n = len(sides)
    if n == 0:
        return {"rule": rule, "pct_long": float("nan"), "pct_short": float("nan"), "n": 0}
    pct_long = float(np.mean(sides > 0) * 100)
    return {
        "rule": rule,
        "pct_long": pct_long,
        "pct_short": 100.0 - pct_long,
        "n": n,
        "unidirectional": bool(pct_long > 80 or pct_long < 20),
    }


def cost_test_rule(
    df: pd.DataFrame,
    feature: str,
    horizon: str,
    ic: float,
    *,
    rule: str = "median_split",
    n_boot: int = 1500,
    seed: int = 42,
) -> Dict[str, Any]:
    """Non-overlap (1 trade / symbol / day) cost test under ``rule``."""
    hb = HORIZONS[horizon]
    del hb  # hold encoded in fwd column
    sub = df.dropna(subset=[feature, f"fwd_{horizon}"]).copy()
    sub = sub.sort_values(["symbol", "timestamp_ms"])
    first = sub.groupby(["symbol", "date"], as_index=False).first()
    vals = first[feature].to_numpy(dtype=float)
    fwds = first[f"fwd_{horizon}"].to_numpy(dtype=float)
    dates = first["date"].to_numpy()
    finite = np.isfinite(vals) & np.isfinite(fwds)
    vals, fwds, dates = vals[finite], fwds[finite], dates[finite]
    if len(vals) < 30:
        return {
            "feature": feature,
            "horizon": horizon,
            "rule": rule,
            "n_trades": int(len(vals)),
            "breakeven_rt_bps": float("nan"),
            "clears_11bps": False,
        }

    if rule == "median_split":
        med = float(np.median(vals))
        sides = np.where(vals > med, 1.0, -1.0) if ic > 0 else np.where(vals > med, -1.0, 1.0)
    elif rule == "extremes_q1q5":
        q20, q80 = np.quantile(vals, [0.2, 0.8])
        keep = (vals <= q20) | (vals >= q80)
        vals, fwds, dates = vals[keep], fwds[keep], dates[keep]
        sides = (
            np.where(vals >= q80, 1.0, -1.0)
            if ic > 0
            else np.where(vals >= q80, -1.0, 1.0)
        )
    elif rule == "sign":
        sides = np.sign(vals)
        if ic < 0:
            sides = -sides
        keep = sides != 0
        vals, fwds, dates, sides = vals[keep], fwds[keep], dates[keep], sides[keep]
    else:
        raise ValueError(rule)

    gross = fwds * sides
    pct_long = float(np.mean(sides > 0) * 100)
    mu = float(np.mean(gross))
    be_bps = float(mu * 1e4)

    by_date: Dict[str, List[float]] = {}
    for d, g in zip(dates, gross):
        by_date.setdefault(str(d), []).append(float(g))
    date_means = np.array([float(np.mean(v)) for v in by_date.values()])
    n_dates = len(date_means)
    rng = np.random.default_rng(seed)
    edge = date_means - TAKER_RT
    boots = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n_dates, size=n_dates)
        boots[b] = float(np.mean(edge[idx]))
    ci_lo, ci_hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
    clears = bool(be_bps > 11.0 and ci_lo > 0)

    out: Dict[str, Any] = {
        "feature": feature,
        "horizon": horizon,
        "rule": rule,
        "n_trades": int(len(gross)),
        "n_dates": int(n_dates),
        "pct_long": pct_long,
        "pct_short": 100.0 - pct_long,
        "unidirectional": bool(pct_long > 80 or pct_long < 20),
        "breakeven_rt_bps": be_bps,
        "mean_gross_bps": float(mu * 1e4),
        "edge_mean_bps": float(np.mean(edge) * 1e4),
        "ci_low_bps": ci_lo * 1e4,
        "ci_high_bps": ci_hi * 1e4,
        "clears_11bps": clears,
        "taker_rt_bps": 11.0,
        "maker_evaluated": False,
    }
    if be_bps >= BE_MAKER_GATE_BPS:
        # Maker RT with corrected 1.5 bps/side — upper-bound only (no fill/AS here)
        maker_edge = date_means - (MAKER_RT_BPS / 1e4)
        mboots = np.empty(n_boot)
        for b in range(n_boot):
            idx = rng.integers(0, n_dates, size=n_dates)
            mboots[b] = float(np.mean(maker_edge[idx]))
        out["maker_evaluated"] = True
        out["maker_rt_bps"] = MAKER_RT_BPS
        out["maker_edge_mean_bps"] = float(np.mean(maker_edge) * 1e4)
        out["maker_ci_low_bps"] = float(np.percentile(mboots, 2.5) * 1e4)
        out["maker_ci_high_bps"] = float(np.percentile(mboots, 97.5) * 1e4)
        out["clears_maker_3bps_optimistic"] = bool(
            be_bps > MAKER_RT_BPS and out["maker_ci_low_bps"] > 0
        )
    return out


STRATEGY_COMPARE = [
    ("SFPReversion", "S/R pivots", "FAIL n=93/173"),
    ("VARejection", "value-area / range position", "FAIL n=44/99"),
    ("DonchianBreakout", "Donchian channel breakout", "FAIL B1=0.5/22"),
    ("VolatilityBreakout", "BB breakout", "FAIL B1=39"),
    ("VWAPTrend", "anchored VWAP trend", "FAIL PF 0.67 / 1498 trades"),
]


def write_report(
    path: Path,
    rows: List[Dict[str, Any]],
    meta: Dict[str, Any],
    costs: Dict[str, Any],
    side_reports: Dict[str, Any],
) -> None:
    cand = [r for r in rows if not r["is_control"]]
    top = [r for r in cand if r.get("survives")]
    top.sort(key=lambda r: abs(r.get("ic") or 0), reverse=True)
    struct_set = set(meta["structure_features"])

    lines: List[str] = []
    lines.append("# Feature Screening — 24m price STRUCTURE (enlarged FDR family)")
    lines.append("")
    lines.append(f"Generated: {meta['generated_at']}")
    lines.append(f"DB: `{meta['db']}`")
    lines.append(f"Symbols: {', '.join(meta['symbols'])}")
    lines.append(
        f"Bars: {meta['n_bars']:,} · dates: **{meta['n_dates']}** · "
        f"{meta['date_min']} → {meta['date_max']}"
    )
    lines.append(
        f"FDR family size: **{meta['n_fdr_tests']}** cells "
        f"(base candles {len(BASE_CANDLES)} + structure {len(meta['structure_features'])} "
        f"features × {len(HORIZONS)} horizons). BH α={FDR_ALPHA}."
    )
    lines.append(
        "Expectation stated a priori: **low** survival odds (same candle "
        "information; derived strategies already FAIL with power)."
    )
    lines.append("")
    lines.append("## Confirmation lags (look-ahead contract)")
    lines.append("")
    lines.append(
        f"Pivot confirmation **k={PIVOT_CONFIRM_K}** bars each side → a swing "
        f"at index `i` is first usable at `i+k`. Trailing Donchian / Bollinger / "
        f"breakout-vs-prior-window features have lag **0** (past window only)."
    )
    lines.append("")
    lines.append("| feature | confirmation lag (15m bars) |")
    lines.append("|---|---:|")
    for name in sorted(meta["feature_lags"], key=lambda x: (meta["feature_lags"][x], x)):
        lag = meta["feature_lags"][name]
        note = " **DELIBERATE FUTURE LEAK**" if lag < 0 else ""
        lines.append(f"| `{name}` | {lag}{note} |")
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
            if r["feature"] == CONTROL_LOOKAHEAD:
                lines.append(
                    f"- Horizon {h}: **look-ahead control** rank **#{i}/{len(ranked)}** "
                    f"(IC={r['ic']:.4f}) — must be near top"
                )
            if r["feature"] == CONTROL_POS:
                lines.append(
                    f"- Horizon {h}: positive leak control rank **#{i}/{len(ranked)}** "
                    f"(IC={r['ic']:.4f})"
                )
        neg = [
            r["ic"]
            for r in ranked
            if r["feature"] in CONTROL_NEGS and np.isfinite(r.get("ic", float("nan")))
        ]
        if neg:
            lines.append(
                f"- Horizon {h}: negative |IC| max={max(abs(x) for x in neg):.4f}"
            )
    lines.append("")
    lines.append(f"**Validation:** {meta['controls_note']}")
    lines.append("")
    lines.append("## TOP survivors (strict gate on enlarged FDR)")
    lines.append("")
    if not top:
        lines.append("**None.**")
    else:
        lines.append(
            "| feature | family | h | IC | p_date | q_FDR | mono | blocks | regimes | sym |"
        )
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in top:
            fam = "structure" if r["feature"] in struct_set else "candle"
            lines.append(
                f"| {r['feature']} | {fam} | {r['horizon']} | {r['ic']:.4f} | "
                f"{r['p_raw']:.2e} | {r.get('q_fdr', float('nan')):.3f} | "
                f"{r['mono']:.2f} | {r['same_sign_periods']}/{r['n_periods']} | "
                f"{r['same_sign_regimes']}/{r['n_regimes']} | "
                f"{r['same_sign_symbols']}/{r['n_symbols']} |"
            )
    lines.append("")
    lines.append("## Side distribution (survivors)")
    lines.append("")
    if not side_reports:
        lines.append("No survivors to check.")
    else:
        lines.append(
            "| feature | h | rule | % long | % short | uni? | action |"
        )
        lines.append("|---|---|---|---:|---:|:---:|---|")
        for key, s in side_reports.items():
            lines.append(
                f"| {s.get('feature', key)} | {s.get('horizon','')} | {s.get('rule')} | "
                f"{s.get('pct_long', float('nan')):.1f} | {s.get('pct_short', float('nan')):.1f} | "
                f"{'Y' if s.get('unidirectional') else 'n'} | {s.get('action','')} |"
            )
    lines.append("")
    lines.append("## Cost test")
    lines.append("")
    lines.append(
        f"Taker RT **11 bps**. Maker (fee **{MAKER_FEE_BPS} bps/side**, RT "
        f"{MAKER_RT_BPS} bps) only if gross BE ≥ {BE_MAKER_GATE_BPS} bps "
        "(optimistic — no fill/AS haircut in this pass)."
    )
    lines.append("")
    if not costs:
        lines.append("No cells cost-tested.")
    else:
        lines.append(
            "| feature | h | rule | BE bps | edge | CI | %long | clears 11? | maker? |"
        )
        lines.append("|---|---|---|---:|---:|---|---:|:---:|---|")
        for key, c in costs.items():
            ci = f"[{c.get('ci_low_bps', float('nan')):.1f}, {c.get('ci_high_bps', float('nan')):.1f}]"
            mk = "n/a"
            if c.get("maker_evaluated"):
                mk = (
                    f"edge={c.get('maker_edge_mean_bps', float('nan')):.1f} "
                    f"clear_opt={'Y' if c.get('clears_maker_3bps_optimistic') else 'n'}"
                )
            lines.append(
                f"| {c.get('feature')} | {c.get('horizon')} | {c.get('rule')} | "
                f"{c.get('breakeven_rt_bps', float('nan')):.2f} | "
                f"{c.get('edge_mean_bps', float('nan')):.2f} | {ci} | "
                f"{c.get('pct_long', float('nan')):.0f} | "
                f"{'Y' if c.get('clears_11bps') else 'n'} | {mk} |"
            )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"### **({meta['verdict']})** — {meta['verdict_text']}")
    lines.append("")
    lines.append("## Comparison vs strategies already gated FAIL")
    lines.append("")
    lines.append("| strategy | structure concept | gate result | feature-screen read |")
    lines.append("|---|---|---|---|")
    for name, concept, gate in STRATEGY_COMPARE:
        lines.append(
            f"| {name} | {concept} | {gate} | {meta.get('strategy_read', 'see verdict')} |"
        )
    lines.append("")
    lines.append("## Structure-only ranking (candidates in FDR)")
    lines.append("")
    lines.append(
        "| feature | h | IC | p_date | q_FDR | FDR | mono | blocks | regimes | sym |"
    )
    lines.append("|---|---|---:|---:|---:|:---:|---:|---:|---:|---:|")
    struct_rows = [r for r in cand if r["feature"] in struct_set]
    struct_rows.sort(key=lambda r: abs(r.get("ic") or 0), reverse=True)
    for r in struct_rows[:80]:
        lines.append(
            f"| {r['feature']} | {r['horizon']} | {r['ic']:.4f} | "
            f"{r['p_raw']:.2e} | {r.get('q_fdr', float('nan')):.3f} | "
            f"{'Y' if r.get('fdr_reject') else 'n'} | {r['mono']:.2f} | "
            f"{r['same_sign_periods']}/{r['n_periods']} | "
            f"{r['same_sign_regimes']}/{r['n_regimes']} | "
            f"{r['same_sign_symbols']}/{r['n_symbols']} |"
        )
    if len(struct_rows) > 80:
        lines.append(f"| … | | | | | | | | | ({len(struct_rows) - 80} more in JSON) |")
    lines.append("")
    lines.append("## Look-ahead audit note")
    lines.append("")
    lines.append(
        f"`{CONTROL_LOOKAHEAD}` is built with `Series.shift(-1).rolling(96)` — "
        "an intentional HIGH look-ahead (next-day high). Causal structure features use only "
        f"lag≥0 windows; pivots wait `k={PIVOT_CONFIRM_K}`. "
        "Run: `python scripts/lookahead_audit.py --paths scripts/feature_screening_24m_structure.py` "
        "and expect the deliberate control line to match LOOKAHEAD-001."
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=PROXY_DB)
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data" / "backtests")
    ap.add_argument(
        "--doc",
        type=Path,
        default=ROOT / "docs" / "FEATURE_SCREENING_24M_STRUCTURE.md",
    )
    ap.add_argument("--symbols", default=",".join(SYMBOLS_DEFAULT))
    ap.add_argument("--n-boot", type=int, default=200)
    ap.add_argument("--quick", action="store_true", help="BTC-only + n_boot=40 smoke")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    n_boot = 40 if args.quick else args.n_boot
    if args.quick:
        symbols = symbols[:1]

    t0 = time.time()
    print(f"Loading {args.db} symbols={symbols} n_boot={n_boot}")
    con = sqlite3.connect(str(args.db))
    raw = load_ohlcv_15m(con, symbols)
    con.close()
    if raw.empty:
        raise SystemExit("No candles_15m")

    print("Building candle features...")
    candle_df = build_candle_features(raw)
    print("Building structure features (causal + lookahead control)...")
    struct_df = build_structure_on_ohlcv(raw)
    feature_lags = dict(struct_df.attrs["feature_lags"])
    structure_features: List[str] = list(struct_df.attrs["structure_features"])

    df = merge_candle_and_structure(candle_df, struct_df)
    # Need high for nothing further; attach vol regime from candle frame path
    df = attach_btc_vol_regime(df)

    # Enlarged candidate list
    candidates = list(dict.fromkeys(list(BASE_CANDLES) + structure_features))
    controls = [CONTROL_POS, CONTROL_LOOKAHEAD, *CONTROL_NEGS]
    all_feats = candidates + controls

    print(
        f"Screening {len(candidates)} candidates (+{len(controls)} controls) "
        f"× {len(HORIZONS)} horizons ..."
    )
    rows: List[Dict[str, Any]] = []
    seed = RNG_SEED
    for feat in all_feats:
        for h in HORIZONS:
            seed += 1
            cell = screen_cell(df, feat, h, n_boot=n_boot, seed=seed)
            row = asdict(cell)
            row["is_control"] = feat in controls or feat == CONTROL_LOOKAHEAD
            rows.append(row)
            if len(rows) % 20 == 0:
                print(f"  … {len(rows)} cells")

    # FDR on candidates only (enlarged family)
    cand_idx = [i for i, r in enumerate(rows) if not r["is_control"]]
    pvals = [rows[i]["p_raw"] for i in cand_idx]
    rejected, qvals = benjamini_hochberg(pvals, alpha=FDR_ALPHA)
    for j, i in enumerate(cand_idx):
        rows[i]["fdr_reject"] = bool(rejected[j])
        rows[i]["q_fdr"] = float(qvals[j]) if np.isfinite(qvals[j]) else float("nan")
        rows[i]["survives"] = survives_strict(rows[i])

    # Control validation
    ctrl_notes = []
    la_ok = True
    pos_ok = True
    for h in HORIZONS:
        ranked = sorted(
            [r for r in rows if r["horizon"] == h],
            key=lambda r: abs(r.get("ic") or 0),
            reverse=True,
        )
        for i, r in enumerate(ranked, start=1):
            if r["feature"] == CONTROL_LOOKAHEAD:
                if i > 3:
                    la_ok = False
                ctrl_notes.append(f"{h}: lookahead_ctrl=#{i}")
            if r["feature"] == CONTROL_POS:
                if i > 3:
                    pos_ok = False
                ctrl_notes.append(f"{h}: pos_ctrl=#{i}")
    controls_note = (
        f"look-ahead control near-top: {'PASS' if la_ok else 'FAIL'}; "
        f"positive leak near-top: {'PASS' if pos_ok else 'FAIL'}; "
        + "; ".join(ctrl_notes)
    )
    if not la_ok:
        print("WARNING: look-ahead control not near top — pipeline suspect")

    # Survivors → side check → cost
    survivors = [r for r in rows if r.get("survives")]
    # Also cost-test structure cells that clear FDR∩mono even if stability misses
    fdr_mono_struct = [
        r
        for r in rows
        if (not r["is_control"])
        and r["feature"] in structure_features
        and r.get("fdr_reject")
        and np.isfinite(r.get("mono", float("nan")))
        and abs(r["mono"]) >= 0.8
    ]
    to_cost = { (r["feature"], r["horizon"]): r for r in survivors + fdr_mono_struct }

    side_reports: Dict[str, Any] = {}
    costs: Dict[str, Any] = {}
    for (feat, h), r in to_cost.items():
        key = f"{feat}@{h}"
        series = df[feat].to_numpy(dtype=float)
        side_med = side_distribution(series, ic=r["ic"], rule="median_split")
        side_med["feature"] = feat
        side_med["horizon"] = h
        rule = "median_split"
        action = "median_split (default)"
        if side_med.get("unidirectional"):
            action = "unidirectional under median_split → reprocess extremes Q1/Q5"
            rule = "extremes_q1q5"
        side_med["action"] = action
        side_reports[key] = side_med
        # Also diagnose raw sign rule
        side_sign = side_distribution(series, ic=r["ic"], rule="sign")
        if side_sign.get("unidirectional"):
            side_med["sign_rule_uni"] = True
            side_med["sign_pct_long"] = side_sign.get("pct_long")

        c = cost_test_rule(df, feat, h, r["ic"], rule=rule, n_boot=max(400, n_boot))
        c["reason"] = "survivor" if r.get("survives") else "fdr_mono_structure"
        costs[key] = c
        if rule == "extremes_q1q5":
            # also report median for transparency
            c_med = cost_test_rule(
                df, feat, h, r["ic"], rule="median_split", n_boot=max(400, n_boot)
            )
            costs[key + "|median_ref"] = {**c_med, "reason": "median_ref"}

    clears = [c for c in costs.values() if c.get("clears_11bps")]
    struct_survivors = [r for r in survivors if r["feature"] in structure_features]
    if clears:
        verdict, vtext = (
            "A",
            "At least one feature (structure and/or candle in enlarged family) "
            "clears FDR/stability gates AND taker BE>11 bps with CI>0. "
            "Next: minimal rule → baseline-signal gate (not built here).",
        )
    elif struct_survivors or any(
        r.get("survives") for r in rows if r["feature"] in structure_features
    ):
        verdict, vtext = (
            "C",
            "Structure features that cleared statistical gates failed the 11 bps "
            "cost test (or none cleared). Classic TA structure joins the closed "
            "candle-feature space.",
        )
    else:
        verdict, vtext = (
            "C",
            "No price-structure feature survived FDR + monotonicity + ≥5/6 blocks "
            "+ ≥2/3 regimes + cross-symbol on the enlarged family. Together with "
            "prior candle screening (C) and strategy-gate FAILs (SFP, VA, Donchian, "
            "VB, VWAPTrend), classical TA structure is closed as features and as "
            "strategies.",
        )

    strategy_read = (
        "Individual continuous structure features do not contradict strategy "
        "FAIL results: either no FDR/stability survival, or survival without "
        "economically meaningful BE — same information, same death."
    )

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(args.db.resolve()),
        "symbols": symbols,
        "n_bars": int(len(df)),
        "n_dates": int(df["date"].nunique()),
        "date_min": str(df["date"].min()),
        "date_max": str(df["date"].max()),
        "n_boot": n_boot,
        "n_fdr_tests": len(cand_idx),
        "structure_features": structure_features,
        "feature_lags": feature_lags,
        "controls_note": controls_note,
        "verdict": verdict,
        "verdict_text": vtext,
        "strategy_read": strategy_read,
        "elapsed_s": round(time.time() - t0, 1),
        "lookahead_control_pass": la_ok,
        "pivot_confirm_k": PIVOT_CONFIRM_K,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "meta": meta,
        "rows": rows,
        "costs": costs,
        "side_reports": side_reports,
    }
    json_path = args.out_dir / f"feature_screening_24m_structure_{stamp}.json"
    latest = args.out_dir / "feature_screening_24m_structure_latest.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    latest.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    write_report(args.doc, rows, meta, costs, side_reports)

    print(
        f"Done in {meta['elapsed_s']}s — verdict ({verdict}) — "
        f"survivors={len(survivors)} struct_survivors={len(struct_survivors)} "
        f"cost_clears={len(clears)}"
    )
    print(f"Report: {args.doc}")
    print(f"JSON: {latest}")
    return 0 if la_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
