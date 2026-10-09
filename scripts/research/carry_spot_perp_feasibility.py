#!/usr/bin/env python3
"""A1 feasibility — delta-neutral spot–perp funding carry, maker entry.

Implements docs/PREREGISTER_SPOTPERP_CARRY_2026-10-06.md EXACTLY:
  * universe: HL spot base/USDC pairs mapping to a perp (name match or
    'U'+perp unit wrapper), PIT liquidity gates (spot median 30d daily
    notional >= $100k, perp >= $1M, >=30d dual history)
  * enter when ann. mean-24h funding >= +15%; exit when mean-24h funding
    < 0 sustained for 48h; both legs maker-at-touch
  * costs: 6 bps RT maker + non-exec model (30% unfill prob x median
    |30m move| proxied median|2h ret|/4)
  * split: discovery <= 2026-02-28 (descriptives only), validation
    2026-03-01..pull-date (verdict computed there)
  * random-entry control: same episode count/durations, random entry in
    the pair's in-universe window, seed 42, >=200 runs

Research-only: reads public API + cache. Never touches bot.db/.env/YAML.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.research.hl_public_history import (  # noqa: E402
    cache_get_candles, cache_get_funding, cache_put_candles,
    cache_put_funding, cache_symbols, fetch_candles, fetch_funding,
    fetch_l2_half_spread_bps, fetch_perp_universe, fetch_spot_pairs,
    init_cache,
)

OUT_DIR = ROOT / "data" / "backtests" / "carry_a1"
CACHE_DB = OUT_DIR / "carry_a1_cache.db"
REPORT_JSON = OUT_DIR / "carry_a1_results.json"

# ─── frozen prereg constants ─────────────────────────────────────────────────
WINDOW_START = pd.Timestamp("2025-06-01", tz="UTC")
DISCOVERY_END = pd.Timestamp("2026-02-28T23:59:59", tz="UTC")
ENTRY_F_ANN = 0.15            # annualized 24h-mean funding entry threshold
EXIT_NEG_H = 48               # exit after 48h of consecutive negative 24h-mean
RT_FEE_BPS = 6.0              # 1.5 bps/side x 2 legs x (entry+exit)
UNFILL_PROB = 0.30
SPOT_MIN_DAILY_USD = 100_000
PERP_MIN_DAILY_USD = 1_000_000
MIN_HISTORY_D = 30
RNG_SEED = 42
RANDOM_RUNS = 200


def _underlying_map() -> List[Tuple[str, str, str]]:
    """(spot_pair_name, base_token, perp_name) candidates per prereg rule."""
    pairs, _ = fetch_spot_pairs()
    perp_names = {u["name"] for u in fetch_perp_universe()}
    out = []
    for p in pairs:
        base = p["base_name"]
        if base in perp_names:
            out.append((p["pair_name"], base, base))
        elif base.startswith("U") and base[1:] in perp_names:
            out.append((p["pair_name"], base, base[1:]))
    return out


def download(cands: List[Tuple[str, str, str]], start_ms: int, end_ms: int) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(CACHE_DB))
    init_cache(con)
    have_c = cache_symbols(con, "candles")
    have_f = cache_symbols(con, "funding")
    for i, (pair_name, base, perp) in enumerate(cands):
        for coin, tag in ((pair_name, f"spot:{pair_name}"), (perp, f"perp:{perp}")):
            if coin in have_c:
                continue
            rows = fetch_candles(coin, "2h", start_ms, end_ms, page_ms=60 * 86400000)
            cache_put_candles(con, coin, rows)
            print(f"[{i+1}/{len(cands)}] candles {tag}: {len(rows)} bars", flush=True)
        if perp not in have_f:
            rows = fetch_funding(perp, start_ms, end_ms)
            cache_put_funding(con, perp, rows)
            print(f"[{i+1}/{len(cands)}] funding {perp}: {len(rows)} rows", flush=True)
    con.close()


def _daily_notional(df: pd.DataFrame) -> pd.Series:
    return (df["c"] * df["v"]).resample("1d").sum()


def _pit_liquid_mask(daily: pd.Series, min_usd: float) -> pd.Series:
    return daily.rolling(30, min_periods=30).median() >= min_usd


def _load_symbol(con, coin: str) -> pd.DataFrame:
    rows = cache_get_candles(con, coin)
    df = pd.DataFrame(rows, columns=["ts_ms", "o", "h", "l", "c", "v", "n"])
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    return df.set_index("ts").sort_index()


def _funding_series(con, perp: str) -> pd.Series:
    rows = cache_get_funding(con, perp)
    s = pd.Series({pd.to_datetime(t, unit="ms", utc=True): v for t, v in rows})
    return s.sort_index()


def _f_ann(fund: pd.Series, idx: pd.DatetimeIndex) -> pd.Series:
    """Ann. mean-24h funding evaluated on the 2h bar grid.

    Funding rows are hourly but NOT hour-aligned (ms offset), so resample to
    the hourly grid first, then rolling-24h, then ffill onto the bar grid.
    """
    hourly = fund.resample("1h").mean()
    f24 = hourly.rolling(24, min_periods=20).mean() * 24 * 365
    return f24.reindex(idx, method="ffill")


def simulate_pair(spot: pd.DataFrame, pp: pd.DataFrame, fund: pd.Series) -> Dict[str, Any]:
    idx = pp.index.union(spot.index)
    if len(idx) < MIN_HISTORY_D * 12:
        return {"episodes": [], "reason": "insufficient_history"}
    joined = pd.DataFrame(index=idx)
    joined["p"] = pp["c"].reindex(idx).ffill(limit=1)
    joined["s"] = spot["c"].reindex(idx).ffill(limit=1)
    joined = joined.dropna(subset=["p", "s"])
    joined = joined[joined.index >= WINDOW_START]
    if joined.empty:
        return {"episodes": [], "reason": "no_overlap"}

    spot_liq = _pit_liquid_mask(_daily_notional(spot), SPOT_MIN_DAILY_USD)
    perp_liq = _pit_liquid_mask(_daily_notional(pp), PERP_MIN_DAILY_USD)
    day = joined.index.normalize()
    joined["liq_ok"] = (spot_liq.reindex(day).fillna(False).to_numpy(dtype=bool)
                        & perp_liq.reindex(day).fillna(False).to_numpy(dtype=bool))
    joined["f_ann"] = _f_ann(fund, joined.index).to_numpy()

    med2h = float(joined["p"].pct_change().abs().median())
    cost_bps = RT_FEE_BPS + UNFILL_PROB * (med2h * 1e4 / 4.0)

    episodes: List[Dict[str, Any]] = []
    open_ep: Optional[Dict[str, Any]] = None
    neg_run = 0
    for ts, row in joined.iterrows():
        f_ann = row["f_ann"]
        neg_run = neg_run + 1 if (pd.notna(f_ann) and f_ann < 0) else 0
        if open_ep is None:
            if pd.notna(f_ann) and f_ann >= ENTRY_F_ANN and row["liq_ok"]:
                open_ep = {"entry_ts": ts, "s0": row["s"], "p0": row["p"],
                           "cum_f": 0.0, "last_ts": ts, "mtm": [(ts, 0.0)]}
        else:
            seg = fund[(fund.index > open_ep["last_ts"]) & (fund.index <= ts)]
            open_ep["cum_f"] += float(seg.sum())   # short perp receives +f
            open_ep["last_ts"] = ts
            open_ep["mtm"].append((ts, open_ep["cum_f"]
                                   + (row["s"] / open_ep["s0"] - 1)
                                   - (row["p"] / open_ep["p0"] - 1)))
            if neg_run * 2 >= EXIT_NEG_H:
                gross = ((row["s"] / open_ep["s0"] - 1)
                         - (row["p"] / open_ep["p0"] - 1) + open_ep["cum_f"])
                episodes.append({
                    "entry_ts": open_ep["entry_ts"], "exit_ts": ts,
                    "days": (ts - open_ep["entry_ts"]).total_seconds() / 86400,
                    "gross_bps": gross * 1e4, "funding_bps": open_ep["cum_f"] * 1e4,
                    "basis_bps": (gross - open_ep["cum_f"]) * 1e4,
                    "cost_bps": cost_bps, "net_bps": gross * 1e4 - cost_bps,
                    "forced": False, "mtm": open_ep["mtm"],
                })
                open_ep = None
    if open_ep is not None:
        row = joined.iloc[-1]
        gross = ((row["s"] / open_ep["s0"] - 1)
                 - (row["p"] / open_ep["p0"] - 1) + open_ep["cum_f"])
        episodes.append({
            "entry_ts": open_ep["entry_ts"], "exit_ts": joined.index[-1],
            "days": (joined.index[-1] - open_ep["entry_ts"]).total_seconds() / 86400,
            "gross_bps": gross * 1e4, "funding_bps": open_ep["cum_f"] * 1e4,
            "basis_bps": (gross - open_ep["cum_f"]) * 1e4,
            "cost_bps": cost_bps, "net_bps": gross * 1e4 - cost_bps,
            "forced": True, "mtm": open_ep["mtm"],
        })
    return {"episodes": episodes, "cost_bps": cost_bps, "joined": joined}


def _episode_pnl(joined: pd.DataFrame, fund: pd.Series, entry_ts,
                 days: float, cost_bps: float) -> float:
    """Same economics as the real spec — used by the random control so the
    control inherits the identical cost model."""
    idx = joined.index
    k = idx.searchsorted(entry_ts)
    if k >= len(idx):
        return float("nan")
    entry_ts = idx[k]
    k = idx.searchsorted(entry_ts + pd.Timedelta(days=days))
    if k >= len(idx):
        return float("nan")
    exit_ts = idx[k]
    e, x = joined.loc[entry_ts], joined.loc[exit_ts]
    seg = fund[(fund.index > entry_ts) & (fund.index <= exit_ts)]
    gross = (x["s"] / e["s"] - 1) - (x["p"] / e["p"] - 1) + float(seg.sum())
    return gross * 1e4 - cost_bps


def _autocorr(s: pd.Series, lag: int) -> float:
    return float(s.autocorr(lag)) if len(s) > lag + 10 else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-download", action="store_true")
    args = ap.parse_args()

    pull_end = int(time.time() * 1000)
    start_ms = int(WINDOW_START.timestamp() * 1000)

    cands = _underlying_map()
    print(f"universe candidates (name-match): {[c[2] for c in cands]}", flush=True)

    if not args.skip_download:
        download(cands, start_ms, pull_end)

    con = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)

    spreads: Dict[str, Optional[float]] = {}
    for pair_name, base, perp in cands:
        spreads[f"spot:{pair_name}"] = fetch_l2_half_spread_bps(pair_name)
        spreads[f"perp:{perp}"] = fetch_l2_half_spread_bps(perp)
        time.sleep(0.1)

    results: Dict[str, Dict[str, Any]] = {}
    funds: Dict[str, pd.Series] = {}
    descriptives: Dict[str, Any] = {}
    for pair_name, base, perp in cands:
        spot, pp = _load_symbol(con, pair_name), _load_symbol(con, perp)
        if spot.empty or pp.empty:
            results[perp] = {"episodes": [], "reason": "no_data"}
            continue
        fund = _funding_series(con, perp)
        if fund.empty:
            results[perp] = {"episodes": [], "reason": "no_funding"}
            continue
        funds[perp] = fund
        sim = simulate_pair(spot, pp, fund)
        sim["pair_name"] = pair_name
        results[perp] = sim

        f_d = fund[fund.index <= DISCOVERY_END]
        desc = {
            "funding_ann_mean": float(f_d.mean() * 24 * 365) if len(f_d) else None,
            "funding_ann_p90": float(f_d.quantile(0.9) * 24 * 365) if len(f_d) else None,
            "funding_ann_p99": float(f_d.quantile(0.99) * 24 * 365) if len(f_d) else None,
            "autocorr_1h": _autocorr(f_d, 1), "autocorr_8h": _autocorr(f_d, 8),
            "autocorr_24h": _autocorr(f_d, 24), "autocorr_7d": _autocorr(f_d, 168),
        }
        j = sim.get("joined")
        if j is not None and not j.empty:
            basis = (j["p"] / j["s"] - 1) * 1e4
            desc.update({"basis_bps_mean": float(basis.mean()),
                         "basis_bps_std": float(basis.std()),
                         "basis_bps_absmax": float(basis.abs().max())})
        desc["spot_half_spread_bps_now"] = spreads.get(f"spot:{pair_name}")
        desc["perp_half_spread_bps_now"] = spreads.get(f"perp:{perp}")
        descriptives[perp] = desc

    # ── validation-window verdict ──────────────────────────────────────────
    val_eps = [{"symbol": perp, **e} for perp, sim in results.items()
               for e in sim.get("episodes", []) if e["entry_ts"] > DISCOVERY_END]
    n_indep = len(val_eps)
    dep_days = sum(e["days"] for e in val_eps)
    net_total_frac = sum(e["net_bps"] * 1e-4 for e in val_eps)
    ann_net = (net_total_frac / dep_days * 365) if dep_days else None

    # random control: same n + durations, uniform entry inside the pair's
    # in-universe validation window; aggregate = total net bps (matched
    # durations ⇒ comparison is apples-to-apples).
    rng = np.random.default_rng(RNG_SEED)
    real_total = sum(e["net_bps"] for e in val_eps)
    rand_totals: List[float] = []
    for _run in range(RANDOM_RUNS):
        tot = 0.0
        for e in val_eps:
            sim = results[e["symbol"]]
            j = sim["joined"]
            w = j[(j.index > DISCOVERY_END) & j["liq_ok"]]
            if len(w) < 2:
                continue
            lo = w.index[0]
            hi = j.index[-1] - pd.Timedelta(days=e["days"])
            if hi <= lo:
                continue
            t0 = lo + (hi - lo) * rng.random()
            v = _episode_pnl(j, funds[e["symbol"]], t0, e["days"], e["cost_bps"])
            if not np.isnan(v):
                tot += v
        rand_totals.append(tot)
    pct = (float(np.mean([r < real_total for r in rand_totals]) * 100)
           if rand_totals else float("nan"))

    # equity curve + max DD on validation — true daily mark-to-market:
    # per open episode, daily MtM delta (funding accrued + basis move) minus
    # its cost charged on the exit day. 1 unit notional per episode.
    eq_rows: List[Tuple[pd.Timestamp, float]] = []
    for e in val_eps:
        mtm = pd.Series({t: v for t, v in e["mtm"]}).resample("1d").last().ffill()
        deltas = mtm.diff().dropna()
        for ts, d in deltas.items():
            eq_rows.append((ts.normalize(), float(d)))
        eq_rows.append((e["exit_ts"].normalize(), -e["cost_bps"] * 1e-4))
    if eq_rows:
        daily = pd.DataFrame(eq_rows, columns=["d", "r"]).groupby("d")["r"].sum()
        cum = daily.sort_index().cumsum()
        max_dd = float((cum.cummax() - cum).max())
    else:
        max_dd = 0.0

    med_cost = float(np.median([e["cost_bps"] for e in val_eps])) if val_eps else float("nan")
    mean_ep = float(np.mean([e["net_bps"] for e in val_eps])) if val_eps else 0.0
    # breadth = pairs that were ever in-universe inside the study window
    n_qual_pairs = sum(
        1 for sim in results.values()
        if sim.get("joined") is not None and sim["joined"]["liq_ok"].any())
    verdict = "C"
    if ann_net is not None and ann_net > 0:
        if n_indep >= 10 and pct >= 90 and mean_ep >= 3 * med_cost:
            verdict = "A"
        elif pct >= 75:
            verdict = "B"
    if n_qual_pairs < 2:
        verdict = "C"   # <2 qualifying pairs → insufficient breadth → C

    out = {
        "pull_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "verdict": verdict,
        "universe_candidates": [c[2] for c in cands],
        "validation": {
            "n_indep_episodes": n_indep,
            "n_qualifying_pairs": n_qual_pairs,
            "deployed_pair_days": round(dep_days, 1),
            "net_ann_return_on_deployed": ann_net,
            "max_dd_frac": max_dd,
            "random_percentile": pct,
            "mean_episode_net_bps": mean_ep,
            "median_episode_net_bps": float(np.median([e["net_bps"] for e in val_eps])) if val_eps else None,
            "median_cost_bps": med_cost,
            "episodes_net_total_bps": real_total,
        },
        "episodes": [{"symbol": e["symbol"], "entry": e["entry_ts"].isoformat(),
                      "exit": e["exit_ts"].isoformat(), "days": round(e["days"], 2),
                      "gross_bps": round(e["gross_bps"], 1),
                      "funding_bps": round(e["funding_bps"], 1),
                      "basis_bps": round(e["basis_bps"], 1),
                      "cost_bps": round(e["cost_bps"], 1),
                      "net_bps": round(e["net_bps"], 1),
                      "forced": e["forced"]} for e in val_eps],  # mtm stripped
        "descriptives_discovery": descriptives,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps({k: out[k] for k in ("verdict", "validation")}, indent=2))
    print(f"report -> {REPORT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
