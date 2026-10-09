#!/usr/bin/env python3
"""A1 robustness addendum — same frozen spec/data as
carry_spot_perp_feasibility.py (PREREGISTER unchanged; X/Y untouched).

Checks ordered by docs/CARRY_SPOTPERP_FEASIBILITY.md addendum:
 1. Censoring   — closed vs still-open episodes; open marked at last price
                  with maker exit AND worst-case taker exit (+6 bps delta);
                  days of funding at the observed pace to cover taker exit.
 2. Independence — episodes entered within 24h chained into ONE cluster;
                  cluster-level random control (common time-shift per
                  cluster, durations preserved), 1000 runs, seed 42.
 3. Concentration — leave-one-symbol-out for PURR / PUMP / XPL.
 4. Construction biases — PIT liquidity audit; long-spot/short-perp only;
                  return on TOTAL capital (spot 100% + perp margin sized to
                  survive a +50% spike) and over the TOTAL window; min
                  distance to liquidation observed on each short leg.

Read-only on the A1 cache; writes results JSON + stdout only.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.research.carry_spot_perp_feasibility import (  # noqa: E402
    CACHE_DB, DISCOVERY_END, ENTRY_F_ANN, RNG_SEED, _episode_pnl,
    _funding_series, _load_symbol, _underlying_map, simulate_pair,
)

OUT_JSON = ROOT / "data" / "backtests" / "carry_a1" / "carry_a1_robustness.json"

TAKER_EXTRA_BPS = 6.0        # exit taker (9bps) instead of maker (3bps)
SPIKE_BUFFER = 0.50          # margin sized so short survives +50% w/o liq
CAPITAL_MULT = 1.0 + SPIKE_BUFFER   # 1.5x notional committed per episode
CLUSTER_GAP_H = 24
RANDOM_RUNS = 1000


def load_episodes():
    """Re-run the frozen sim on the cache — identical params, no download."""
    con = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cands = _underlying_map()
    results, funds, joined_by_perp = {}, {}, {}
    for pair_name, base, perp in cands:
        spot, pp = _load_symbol(con, pair_name), _load_symbol(con, perp)
        if spot.empty or pp.empty:
            continue
        fund = _funding_series(con, perp)
        if fund.empty:
            continue
        sim = simulate_pair(spot, pp, fund)
        if not sim.get("episodes") and sim.get("joined") is None:
            continue
        results[perp] = sim
        funds[perp] = fund
        joined_by_perp[perp] = sim.get("joined")
    val_eps = [{"symbol": p, **e} for p, s in results.items()
               for e in s.get("episodes", []) if e["entry_ts"] > DISCOVERY_END]
    return val_eps, results, funds


def cluster_episodes(eps: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Chain episodes whose entries fall within 24h of each other."""
    eps_s = sorted(eps, key=lambda e: e["entry_ts"])
    clusters: List[List[Dict[str, Any]]] = []
    for e in eps_s:
        if clusters and (e["entry_ts"] - clusters[-1][-1]["entry_ts"]
                         <= pd.Timedelta(hours=CLUSTER_GAP_H)):
            clusters[-1].append(e)
        else:
            clusters.append([e])
    return clusters


def _episode_pnl_liq(joined, fund, entry_ts, days, cost_bps,
                     liq_adverse=SPIKE_BUFFER):
    """Liquidation-aware variant: with margin = 0.5x notional the isolated
    short is liquidated at the first bar where perp/entry >= 1+0.50.
    Realized PnL = spot_ret − 0.50 (margin lost) + funding to truncation."""
    idx = joined.index
    k = idx.searchsorted(entry_ts)
    if k >= len(idx):
        return float("nan"), None
    entry_ts = idx[k]
    k = idx.searchsorted(entry_ts + pd.Timedelta(days=days))
    if k >= len(idx):
        return float("nan"), None
    exit_ts = idx[k]
    hold = joined.loc[entry_ts:exit_ts]
    p0, s0 = float(hold["p"].iloc[0]), float(hold["s"].iloc[0])
    hit = hold[hold["p"] / p0 - 1 >= liq_adverse]
    if len(hit):
        t_liq = hit.index[0]
        s_t = float(hit["s"].iloc[0])
        seg = fund[(fund.index > entry_ts) & (fund.index <= t_liq)]
        gross = (s_t / s0 - 1) - liq_adverse + float(seg.sum())
        days_held = (t_liq - entry_ts).total_seconds() / 86400
        return gross * 1e4 - cost_bps, {"liq_ts": t_liq, "days_held": days_held}
    e, x = joined.loc[entry_ts], joined.loc[exit_ts]
    seg = fund[(fund.index > entry_ts) & (fund.index <= exit_ts)]
    gross = (x["s"] / e["s"] - 1) - (x["p"] / e["p"] - 1) + float(seg.sum())
    return gross * 1e4 - cost_bps, None


def cluster_random_percentile(clusters, results, funds, runs=RANDOM_RUNS,
                              liq_aware=False):
    """Per run: each cluster draws ONE common time-shift δ; members keep
    their own durations/pairs. Preserves intra-cluster correlation."""
    rng = np.random.default_rng(RNG_SEED)
    real = sum(e["net_bps"] for c in clusters for e in c)
    totals = []
    for _ in range(runs):
        tot = 0.0
        for c in clusters:
            # feasible δ range: each member must fit its duration in-window
            lo_s, hi_s = [], []
            for e in c:
                j = results[e["symbol"]]["joined"]
                w = j[(j.index > DISCOVERY_END) & j["liq_ok"]]
                if len(w) < 2:
                    break
                lo_s.append(w.index[0] - e["entry_ts"])
                hi_s.append(j.index[-1] - pd.Timedelta(days=e["days"])
                            - e["entry_ts"])
            else:
                lo = max(lo_s); hi = min(hi_s)
                delta = (lo + (hi - lo) * rng.random()) if hi > lo else pd.Timedelta(0)
                for e in c:
                    j = results[e["symbol"]]["joined"]
                    if liq_aware:
                        v, _liq = _episode_pnl_liq(
                            j, funds[e["symbol"]], e["entry_ts"] + delta,
                            e["days"], e["cost_bps"])
                    else:
                        v = _episode_pnl(j, funds[e["symbol"]],
                                         e["entry_ts"] + delta,
                                         e["days"], e["cost_bps"])
                    if not np.isnan(v):
                        tot += v
                continue
            continue
        totals.append(tot)
    return (float(np.mean([t < real for t in totals]) * 100)
            if totals else float("nan"))


def main() -> int:
    eps, results, funds = load_episodes()
    print(f"validation episodes: {len(eps)}")

    closed = [e for e in eps if not e["forced"]]
    opened = [e for e in eps if e["forced"]]

    # ── 1. censoring ────────────────────────────────────────────────────
    for e in opened:
        e["net_taker_exit_bps"] = e["net_bps"] - TAKER_EXTRA_BPS
        pace = e["funding_bps"] / max(e["days"], 1e-9)   # bps/day collected
        e["days_funding_for_taker_exit"] = (
            TAKER_EXTRA_BPS / pace if pace > 0 else float("inf"))
    cens = {
        "closed": [{"symbol": e["symbol"], "days": round(e["days"], 1),
                    "net_bps": round(e["net_bps"], 1)} for e in closed],
        "closed_total_net_bps": sum(e["net_bps"] for e in closed),
        "open": [{"symbol": e["symbol"], "days": round(e["days"], 1),
                  "net_maker_bps": round(e["net_bps"], 1),
                  "net_taker_bps": round(e["net_taker_exit_bps"], 1),
                  "fund_bps_day": round(e["funding_bps"] / e["days"], 2),
                  "days_to_cover_taker":
                      (round(e["days_funding_for_taker_exit"], 1)
                       if np.isfinite(e["days_funding_for_taker_exit"]) else "inf")}
                 for e in opened],
    }
    cens["open_total_net_maker_bps"] = sum(e["net_bps"] for e in opened)
    cens["open_total_net_taker_bps"] = sum(e["net_taker_exit_bps"] for e in opened)

    # ── 2. independence ─────────────────────────────────────────────────
    clusters = cluster_episodes(eps)
    pct_cluster = cluster_random_percentile(clusters, results, funds)
    indep = {
        "n_episodes": len(eps), "n_clustered": len(clusters),
        "clusters": [[f"{e['symbol']}@{e['entry_ts'].date()}" for e in c]
                     for c in clusters],
        "random_percentile_clustered_1000": pct_cluster,
        "real_total_net_bps": sum(e["net_bps"] for e in eps),
    }

    # ── 3. concentration (LOSO) ─────────────────────────────────────────
    def _agg(sub):
        if not sub:
            return None
        dep = sum(e["days"] for e in sub)
        ann = sum(e["net_bps"] * 1e-4 for e in sub) / dep * 365 if dep else None
        return {"n": len(sub), "net_ann_on_deployed": round(ann, 4),
                "mean_net_bps": round(float(np.mean([e["net_bps"] for e in sub])), 1),
                "edge_over_median_cost": round(
                    float(np.mean([e["net_bps"] for e in sub])) /
                    float(np.median([e["cost_bps"] for e in sub])), 2)}
    loso = {"full": _agg(eps)}
    for sym in ("PURR", "PUMP", "XPL"):
        loso[f"without_{sym}"] = _agg([e for e in eps if e["symbol"] != sym])

    # ── 4. construction biases ──────────────────────────────────────────
    # PIT audit: liq_ok derives from rolling-30d medians evaluated at the
    # bar's own day — verify no episode entered on a non-liq day, and that
    # every episode is long-spot/short-perp (entry f_ann>0 by construction).
    pit_ok = True
    pos_funding_only = True
    for e in eps:
        j = results[e["symbol"]]["joined"]
        if not bool(j.loc[e["entry_ts"], "liq_ok"]):
            pit_ok = False
        if float(j.loc[e["entry_ts"], "f_ann"]) < ENTRY_F_ANN:
            pos_funding_only = False
    # distance to liquidation on the short leg: margin = 0.5x notional →
    # liq at ~+50% adverse move; report worst excursion observed.
    liq_rows = []
    for e in eps:
        j = results[e["symbol"]]["joined"]
        hold = j.loc[e["entry_ts"]:e["exit_ts"], "p"]
        p0 = float(j.loc[e["entry_ts"], "p"])
        worst = float((hold / p0 - 1).max()) if len(hold) and p0 else 0.0
        liq_rows.append({"symbol": e["symbol"],
                         "max_adverse_pct": round(worst * 100, 2),
                         "dist_to_50pct_liq": round((0.50 - worst) * 100, 2)})
    min_dist = min(r["dist_to_50pct_liq"] for r in liq_rows) if liq_rows else None

    dep_days = sum(e["days"] for e in eps)
    net_frac = sum(e["net_bps"] * 1e-4 for e in eps)
    ann_deployed = net_frac / dep_days * 365 if dep_days else None
    # on committed capital while deployed (1.5x notional)
    ann_committed = ann_deployed / CAPITAL_MULT if ann_deployed else None
    # on total reserved capital over the whole window: peak concurrent
    # episodes x 1.5 notional, reserved for the full validation span
    events = []
    for e in eps:
        events.append((e["entry_ts"], 1)); events.append((e["exit_ts"], -1))
    events.sort()
    peak = cur = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    t_win = (max(e["exit_ts"] for e in eps)
             - min(e["entry_ts"] for e in eps)).total_seconds() / 86400
    ann_total_capital = (net_frac / (peak * CAPITAL_MULT) / t_win * 365
                         if peak and t_win else None)
    construction = {
        "pit_liquidity_gate_holds": pit_ok,
        "all_long_spot_short_perp": pos_funding_only,
        "capital_mult_per_episode": CAPITAL_MULT,
        "peak_concurrent_episodes": peak,
        "net_ann_on_deployed": ann_deployed,
        "net_ann_on_committed_capital_1.5x": ann_committed,
        "net_ann_on_total_reserved_capital": ann_total_capital,
        "min_dist_to_liq_pct": min_dist,
        "per_episode_liq": liq_rows,
    }

    # ── 5. liquidation-aware scenario (margin = 0.5x → liq at +50%) ──────
    liq_eps = []
    for e in eps:
        j = results[e["symbol"]]["joined"]
        v, liq = _episode_pnl_liq(j, funds[e["symbol"]], e["entry_ts"],
                                  e["days"], e["cost_bps"])
        liq_eps.append({"symbol": e["symbol"], "entry": str(e["entry_ts"].date()),
                        "net_bps_liq_aware": round(v, 1),
                        "liquidated": bool(liq),
                        "days_held": round(liq["days_held"], 1) if liq else round(e["days"], 1)})
    n_liq = sum(1 for e in liq_eps if e["liquidated"])
    liq_total = sum(e["net_bps_liq_aware"] for e in liq_eps)
    dep_days_liq = sum(e["days_held"] for e in liq_eps)
    ann_liq_deployed = (sum(e["net_bps_liq_aware"] * 1e-4 for e in liq_eps)
                        / dep_days_liq * 365) if dep_days_liq else None
    liq_scenario = {
        "liquidated_n": n_liq, "of": len(liq_eps),
        "total_net_bps": round(liq_total, 1),
        "net_ann_on_committed_capital": round(ann_liq_deployed / CAPITAL_MULT, 4)
        if ann_liq_deployed else None,
        "episodes": liq_eps,
    }
    # clustered random control under the same liquidation semantics
    pct_liq = cluster_random_percentile(clusters, results, funds,
                                        liq_aware=True)
    liq_scenario["random_percentile_clustered_1000"] = pct_liq

    # final verdict per the addendum's literal rules
    verdict = "A"
    reason = []
    if indep["n_clustered"] < 10:
        verdict = "B"
        reason.append(f"n_clustered={indep['n_clustered']} < 10")
    w_purr = loso.get("without_PURR")
    purr_dependent = bool(w_purr and w_purr["net_ann_on_deployed"] <= 0)
    if purr_dependent:
        verdict = "C"
        reason.append("edge disappears without PURR")
    if liq_total <= 0:
        verdict = "C"
        reason.append(f"{n_liq}/{len(eps)} episodes liquidate at the +50% "
                      "buffer — aggregate net turns negative")

    out = {"verdict": verdict, "verdict_reasons": reason,
           "purr_dependent": purr_dependent,
           "censoring": cens, "independence": indep,
           "leave_one_out": loso, "construction": construction,
           "liquidation_scenario": liq_scenario}
    OUT_JSON.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
