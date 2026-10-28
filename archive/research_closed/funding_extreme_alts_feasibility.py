#!/usr/bin/env python3
"""A2 feasibility — extreme-funding mean reversion in non-major HL perps.

Implements docs/PREREGISTER_FUNDING_EXTREME_ALTS_2026-10-06.md EXACTLY:
  * universe: every HL perp except BTC/ETH/SOL/HYPE (HIP-3 ':' names out);
    PIT tradable if trailing-30d median daily notional >= $250k
  * f24(t) = mean of last 24 hourly funding rates; per-symbol percentile vs
    its own trailing 180d f24 distribution (min 30d history)
  * SHORT when pct >= 0.90, LONG when pct <= 0.10; hold H = 48h fixed;
    re-arm only after f24 re-enters [p20, p80]; one open episode/symbol
  * funding paid/received during the hold is credited inside the episode
  * cost/symbol: taker 9 bps RT + l2Book half-spread x1.5 + 2 bps impact
    ($5k clip); symbols with no live book get the p90 measured cost
    (conservative, flagged — keeps delisted history in the sample)
  * split: discovery <= 2026-02-28 descriptives only; verdict on
    2026-03-01..pull-date
  * random control: same durations, uniform entries in the symbol's
    in-universe window, seed 42, >=200 runs

Research-only: public API + cache. Never touches bot.db/.env/YAML/engine.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.research.hl_public_history import (  # noqa: E402
    cache_get_candles, cache_get_funding, cache_put_candles,
    cache_put_funding, cache_symbols, fetch_candles, fetch_funding,
    fetch_l2_half_spread_bps, fetch_perp_universe, init_cache,
)

OUT_DIR = ROOT / "data" / "backtests" / "funding_mr_a2"
CACHE_DB = OUT_DIR / "funding_mr_a2_cache.db"
REPORT_JSON = OUT_DIR / "funding_mr_a2_results.json"

# ─── frozen prereg constants ─────────────────────────────────────────────────
WINDOW_START = pd.Timestamp("2025-06-01", tz="UTC")
DISCOVERY_END = pd.Timestamp("2026-02-28T23:59:59", tz="UTC")
MAJORS = {"BTC", "ETH", "SOL", "HYPE"}
MIN_DAILY_USD = 250_000
MIN_HIST_BARS = 360           # 30d of 2h bars before a percentile exists
PCT_WINDOW_D = 180
P_HI, P_LO = 0.90, 0.10
REARM_LO, REARM_HI = 0.20, 0.80
HOLD_H = 48                   # primary horizon; 8/24/72 are diagnostics
DIAG_HORIZONS_H = (8, 24, 72)
TAKER_RT_BPS = 9.0            # 4.5 x 2
IMPACT_BPS = 2.0              # buffer at ~$5k clip
SPREAD_HAIRCUT = 1.5
RNG_SEED = 42
RANDOM_RUNS = 200


class _BIT:
    """Fenwick tree of counts over sorted coords — exact rolling percentile."""
    def __init__(self, n: int):
        self.n = n
        self.t = [0] * (n + 1)
        self.size = 0
    def add(self, i: int, d: int) -> None:
        i += 1
        while i <= self.n:
            self.t[i] += d
            i += i & -i
        self.size += d
    def rank(self, i: int) -> int:
        """count of values with compressed index <= i"""
        i += 1
        s = 0
        while i > 0:
            s += self.t[i]
            i -= i & -i
        return s


def _rolling_pctile(vals: np.ndarray, ts: np.ndarray) -> np.ndarray:
    """pct[i] = fraction of vals[j] (t_j in (t_i-180d, t_i]) <= vals[i].
    Two-pointer + BIT → O(n log n). NaN vals propagate as NaN."""
    n = len(vals)
    out = np.full(n, np.nan)
    idxs = np.where(~np.isnan(vals))[0]
    if len(idxs) < MIN_HIST_BARS:
        return out
    coords = np.sort(np.unique(vals[idxs]))
    comp = {v: i for i, v in enumerate(coords)}
    bit = _BIT(len(coords))
    left = 0
    win_ms = np.int64(PCT_WINDOW_D) * 86400000
    for i in range(n):
        while left <= i and ts[i] - ts[left] > win_ms:
            if not np.isnan(vals[left]):
                bit.add(comp[vals[left]], -1)
            left += 1
        if np.isnan(vals[i]):
            continue
        bit.add(comp[vals[i]], 1)
        if bit.size >= MIN_HIST_BARS:
            out[i] = bit.rank(comp[vals[i]]) / bit.size
    return out


def download(symbols: List[str], start_ms: int, end_ms: int) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(CACHE_DB))
    init_cache(con)
    have_c = cache_symbols(con, "candles")
    have_f = cache_symbols(con, "funding")
    for i, sym in enumerate(symbols):
        if sym not in have_c:
            rows = fetch_candles(sym, "2h", start_ms, end_ms, page_ms=60 * 86400000)
            cache_put_candles(con, sym, rows)
            print(f"[{i+1}/{len(symbols)}] candles {sym}: {len(rows)} bars", flush=True)
        if sym not in have_f:
            rows = fetch_funding(sym, start_ms, end_ms)
            cache_put_funding(con, sym, rows)
            print(f"[{i+1}/{len(symbols)}] funding {sym}: {len(rows)} rows", flush=True)
    con.close()


def _pit_liquid(daily: pd.Series) -> pd.Series:
    return daily.rolling(30, min_periods=30).median() >= MIN_DAILY_USD


def simulate_symbol(sym: str, df: pd.DataFrame, fund: pd.Series,
                    cost_bps: float, val_start: pd.Timestamp) -> Dict[str, Any]:
    df = df[df.index >= WINDOW_START].copy()
    if df.empty:
        return {"episodes": [], "reason": "no_data"}
    daily = (df["c"] * df["v"]).resample("1d").sum()
    liq = _pit_liquid(daily)
    df["liq_ok"] = liq.reindex(df.index.normalize()).fillna(False).to_numpy(dtype=bool)

    hourly = fund.resample("1h").mean()
    f24 = hourly.rolling(24, min_periods=20).mean()
    df["f24"] = f24.reindex(df.index, method="ffill")

    ts_ms = df.index.asi8 // 10**6
    df["pct"] = _rolling_pctile(df["f24"].to_numpy(), ts_ms)

    episodes: List[Dict[str, Any]] = []
    open_ep: Optional[Dict[str, Any]] = None
    armed = True
    for ts, row in df.iterrows():
        p = row["pct"]
        if pd.isna(p):
            continue
        if open_ep is None:
            if not armed:
                if REARM_LO <= p <= REARM_HI:
                    armed = True
                continue
            if not row["liq_ok"]:
                continue
            if p >= P_HI:
                side = -1
            elif p <= P_LO:
                side = 1
            else:
                continue
            open_ep = {"entry_ts": ts, "px0": row["c"], "side": side,
                       "pct": p, "last_ts": ts, "cum_f": 0.0}
        else:
            seg = fund[(fund.index > open_ep["last_ts"]) & (fund.index <= ts)]
            open_ep["cum_f"] += float(seg.sum())
            open_ep["last_ts"] = ts
            if (ts - open_ep["entry_ts"]) >= pd.Timedelta(hours=HOLD_H):
                ret = row["c"] / open_ep["px0"] - 1
                s = open_ep["side"]
                gross = s * ret - s * open_ep["cum_f"]
                ep = {
                    "symbol": sym, "entry_ts": open_ep["entry_ts"], "exit_ts": ts,
                    "side": "SHORT" if s < 0 else "LONG",
                    "entry_pct": float(open_ep["pct"]),
                    "gross_bps": gross * 1e4,
                    "funding_bps": (-s * open_ep["cum_f"]) * 1e4,
                    "cost_bps": cost_bps,
                    "net_bps": gross * 1e4 - cost_bps,
                }
                episodes.append(ep)
                open_ep = None
                armed = False
    return {"episodes": episodes, "df": df}


def _fwd_net(df: pd.DataFrame, fund: pd.Series, entry_ts, side: int,
             hold_h: float, cost_bps: float) -> float:
    idx = df.index
    k = idx.searchsorted(entry_ts)
    if k >= len(idx):
        return float("nan")
    entry_ts = idx[k]
    k = idx.searchsorted(entry_ts + pd.Timedelta(hours=hold_h))
    if k >= len(idx):
        return float("nan")
    exit_ts = idx[k]
    ret = df["c"].iloc[k] / df.loc[entry_ts, "c"] - 1
    seg = fund[(fund.index > entry_ts) & (fund.index <= exit_ts)]
    gross = side * ret - side * float(seg.sum())
    return gross * 1e4 - cost_bps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--max-symbols", type=int, default=None)
    args = ap.parse_args()

    pull_end = int(time.time() * 1000)
    start_ms = int(WINDOW_START.timestamp() * 1000)

    univ = fetch_perp_universe()
    symbols = [u["name"] for u in univ if u["name"] not in MAJORS]
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    print(f"universe: {len(symbols)} non-major perps "
          f"({sum(u['is_delisted'] for u in univ if u['name'] in symbols)} delisted)",
          flush=True)

    if not args.skip_download:
        download(symbols, start_ms, pull_end)

    # measured cost per symbol (live book); delisted/no-book → p90 fallback
    half_spreads: Dict[str, Optional[float]] = {}
    for sym in symbols:
        half_spreads[sym] = fetch_l2_half_spread_bps(sym)
        time.sleep(0.08)
    meas = [v for v in half_spreads.values() if v is not None]
    p90_spread = float(np.percentile(meas, 90)) if meas else 10.0
    costs: Dict[str, float] = {}
    for sym, hs in half_spreads.items():
        eff = hs if hs is not None else p90_spread
        costs[sym] = TAKER_RT_BPS + SPREAD_HAIRCUT * eff + IMPACT_BPS

    con = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    all_eps: List[Dict[str, Any]] = []
    per_symbol: Dict[str, Any] = {}
    frames: Dict[str, Dict[str, Any]] = {}
    funds: Dict[str, pd.Series] = {}

    for sym in symbols:
        rows = cache_get_candles(con, sym)
        if not rows:
            per_symbol[sym] = {"episodes": 0, "reason": "no_candles"}
            continue
        df = pd.DataFrame(rows, columns=["ts_ms", "o", "h", "l", "c", "v", "n"])
        df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
        df = df.set_index("ts").sort_index()
        fund_rows = cache_get_funding(con, sym)
        if not fund_rows:
            per_symbol[sym] = {"episodes": 0, "reason": "no_funding"}
            continue
        fund = pd.Series({pd.to_datetime(t, unit="ms", utc=True): v
                          for t, v in fund_rows}).sort_index()
        funds[sym] = fund
        sim = simulate_symbol(sym, df, fund, costs[sym], DISCOVERY_END)
        frames[sym] = sim
        eps = sim["episodes"]
        val_n = sum(1 for e in eps if e["entry_ts"] > DISCOVERY_END)
        per_symbol[sym] = {
            "episodes_total": len(eps), "episodes_validation": val_n,
            "cost_bps": round(costs[sym], 2),
            "half_spread_bps_now": half_spreads[sym],
            "cost_estimated": half_spreads[sym] is None,
        }
        all_eps.extend(eps)

    val_eps = [e for e in all_eps if e["entry_ts"] > DISCOVERY_END]
    n_indep = len(val_eps)
    mean_net = float(np.mean([e["net_bps"] for e in val_eps])) if val_eps else float("nan")
    mean_gross = float(np.mean([e["gross_bps"] for e in val_eps])) if val_eps else float("nan")
    med_cost = float(np.median([e["cost_bps"] for e in val_eps])) if val_eps else float("nan")
    long_net = [e["net_bps"] for e in val_eps if e["side"] == "LONG"]
    short_net = [e["net_bps"] for e in val_eps if e["side"] == "SHORT"]
    both_pos = (long_net and short_net
                and float(np.mean(long_net)) > 0 and float(np.mean(short_net)) > 0)

    # random control — same n, durations fixed 48h, uniform entry in window
    rng = np.random.default_rng(RNG_SEED)
    real_total = sum(e["net_bps"] for e in val_eps)
    rand_totals: List[float] = []
    for _run in range(RANDOM_RUNS):
        tot = 0.0
        for e in val_eps:
            df = frames[e["symbol"]]["df"]
            w = df[(df.index > DISCOVERY_END) & df["liq_ok"]]
            if len(w) < 2:
                continue
            hi = df.index[-1] - pd.Timedelta(hours=HOLD_H)
            lo = w.index[0]
            if hi <= lo:
                continue
            t0 = lo + (hi - lo) * rng.random()
            side = -1 if e["side"] == "SHORT" else 1
            v = _fwd_net(df, funds[e["symbol"]], t0, side, HOLD_H, e["cost_bps"])
            if not np.isnan(v):
                tot += v
        rand_totals.append(tot)
    pct = (float(np.mean([r < real_total for r in rand_totals]) * 100)
           if rand_totals else float("nan"))

    # Verdict per prereg: A needs the 3x gate + n>=30 + pct>=90 + both dirs;
    # B = net>0 with weaker evidence (n in [15,30) or pct in [75,90) or
    # one-sided); C = net<=0 / pct<75 / n<15 / cost beats edge for majority.
    edge3x = (not np.isnan(mean_net)) and (not np.isnan(med_cost)) and mean_net >= 3 * med_cost
    verdict = "C"
    if not np.isnan(mean_net) and mean_net > 0 and n_indep >= 15 and pct >= 75:
        if edge3x and n_indep >= 30 and pct >= 90 and both_pos:
            verdict = "A"
        else:
            verdict = "B"

    # horizon diagnostics on discovery window only (never tuned)
    diag = {}
    for h in DIAG_HORIZONS_H + (HOLD_H,):
        vals = []
        for e in all_eps:
            if e["entry_ts"] > DISCOVERY_END:
                continue
            df = frames[e["symbol"]]["df"]
            side = -1 if e["side"] == "SHORT" else 1
            v = _fwd_net(df, funds[e["symbol"]], e["entry_ts"], side, h,
                         e["cost_bps"])
            if not np.isnan(v):
                vals.append(v)
        diag[f"net_bps_h{h}"] = {"n": len(vals),
                                 "mean": float(np.mean(vals)) if vals else None,
                                 "median": float(np.median(vals)) if vals else None}

    out = {
        "pull_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "verdict": verdict,
        "validation": {
            "n_indep_episodes": n_indep,
            "pooled_mean_net_bps": mean_net,
            "pooled_mean_gross_bps": mean_gross,
            "median_cost_bps": med_cost,
            "edge_over_3x_cost": (mean_net >= 3 * med_cost
                                 if not np.isnan(mean_net) else None),
            "random_percentile": pct,
            "long_n": len(long_net), "long_mean_net_bps":
                float(np.mean(long_net)) if long_net else None,
            "short_n": len(short_net), "short_mean_net_bps":
                float(np.mean(short_net)) if short_net else None,
            "both_directions_positive": bool(both_pos),
        },
        "cost_model": {"measured_symbols": len(meas),
                       "p90_spread_fallback_bps": p90_spread,
                       "median_cost_bps_all": float(np.median(list(costs.values()))),
                       "cost_estimated_n": sum(1 for s in symbols if half_spreads[s] is None)},
        "horizon_diagnostics_discovery": diag,
        "episodes_validation": [
            {k: (v.isoformat() if isinstance(v, pd.Timestamp) else
                 round(v, 2) if isinstance(v, float) else v)
             for k, v in e.items()} for e in val_eps],
        "per_symbol": per_symbol,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps({"verdict": verdict, "validation": out["validation"],
                      "cost_model": out["cost_model"]}, indent=2))
    print(f"report -> {REPORT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
