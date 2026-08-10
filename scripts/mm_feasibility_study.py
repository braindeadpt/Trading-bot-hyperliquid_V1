#!/usr/bin/env python3
"""Market-making economic feasibility on Hyperliquid research data.

MEASUREMENT ONLY — no quoting logic, no strategy, no production config changes.

Data:
  * ``data/research/hyperliquid.db`` (READ-ONLY): ~1 month ``l2_snapshots`` + ``trade_tape``
  * ``data/research/l2_books``: few days of top-K depth (declare short window)

Equation (per fill, two-sided MM):
  half_spread − adverse_selection − maker_fee  (+ rebate only at MM volume tiers)

Usage:
  python scripts/mm_feasibility_study.py
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_DEFAULT = ROOT / "data" / "research" / "hyperliquid.db"
L2_BOOKS_DEFAULT = Path("data/research/l2_books")
OUT_JSON = ROOT / "data" / "backtests" / "mm_feasibility.json"
OUT_DOC = ROOT / "docs" / "MARKET_MAKING_FEASIBILITY.md"

SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")
AS_HORIZONS_MS = (("1s", 1_000), ("10s", 10_000), ("60s", 60_000))
TRADE_SAMPLE_PER_SYM = 120_000
BOOT_N = 800
RNG_SEED = 42

# Official Hyperliquid perps fee schedule (base / no staking), sourced
# 2026-08-10 from https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
# Rates are percent of notional; convert to bps via *100.
HL_FEES_SOURCE = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees"
HL_PERP_MAKER_BPS = {
    "tier0_base": 1.5,  # 0.015%
    "tier1_5M": 1.2,  # 0.012%
    "tier2_25M": 0.8,  # 0.008%
    "tier3_100M": 0.4,  # 0.004%
    "tier4_500M": 0.0,  # 0.000%
}
HL_PERP_TAKER_BPS = {
    "tier0_base": 4.5,  # 0.045%
    "tier2_25M": 3.5,  # matches bot risk.taker_fee_pct 0.035%
}
# Maker rebate tiers require share of 14d weighted *maker* volume (not retail-realistic)
HL_MAKER_REBATE_BPS = {
    "mm_share_0.5pct": -0.1,  # -0.001%
    "mm_share_1.5pct": -0.2,
    "mm_share_3.0pct": -0.3,
}
# Bot config historically used 0.01% maker — document delta vs official base
BOT_CONFIG_MAKER_BPS = 1.0
DIRECTIONAL_TAKER_RT_BPS = 11.0  # bot taker 3.5+2 slip each side


def open_ro(db: Path) -> sqlite3.Connection:
    uri = f"file:{db.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def pctiles(x: np.ndarray, ps: Sequence[float] = (10, 25, 50, 75, 90)) -> Dict[str, float]:
    y = x[np.isfinite(x)]
    if len(y) == 0:
        return {f"p{int(p)}": float("nan") for p in ps}
    vals = np.percentile(y, list(ps))
    return {f"p{int(p)}": float(v) for p, v in zip(ps, vals)}


def mean_ci(x: np.ndarray, n_boot: int, rng: np.random.Generator) -> Dict[str, float]:
    y = x[np.isfinite(x)]
    if len(y) < 30:
        return {
            "mean": float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "n": int(len(y)),
        }
    boots = np.empty(n_boot, dtype=float)
    n = len(y)
    for i in range(n_boot):
        boots[i] = float(np.mean(y[rng.integers(0, n, size=n)]))
    return {
        "mean": float(np.mean(y)),
        "ci_low": float(np.percentile(boots, 2.5)),
        "ci_high": float(np.percentile(boots, 97.5)),
        "n": int(n),
    }


def load_l2_metrics(con: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    q = """
        SELECT timestamp_ms, mid_price, spread_bps, bid_depth_usd, ask_depth_usd, oir
        FROM l2_snapshots
        WHERE symbol = ?
        ORDER BY timestamp_ms
    """
    return pd.read_sql_query(q, con, params=[symbol])


def sample_trades(
    con: sqlite3.Connection,
    symbol: str,
    n_sample: int,
    rng: np.random.Generator,
) -> pd.DataFrame:
    n = con.execute(
        "SELECT COUNT(*) FROM trade_tape WHERE symbol = ?", (symbol,)
    ).fetchone()[0]
    if n <= 0:
        return pd.DataFrame()
    # Systematic sample by rowid-ish: pull evenly spaced via ORDER BY timestamp
    # Avoid loading 7M rows — use modulo on id when dense, else LIMIT OFFSET chunks.
    step = max(1, n // n_sample)
    q = """
        SELECT timestamp_ms, price, size, side
        FROM trade_tape
        WHERE symbol = ? AND (id % ?) = 0
        ORDER BY timestamp_ms
        LIMIT ?
    """
    df = pd.read_sql_query(q, con, params=[symbol, int(step), int(n_sample)])
    if len(df) < min(1000, n_sample // 2):
        # Fallback: random-ish via timestamp bucket
        q2 = """
            SELECT timestamp_ms, price, size, side
            FROM trade_tape
            WHERE symbol = ?
            ORDER BY timestamp_ms
            LIMIT ?
        """
        # Take first n_sample after shuffle of a larger window is hard in SQL;
        # load a stride via BETWEEN windows.
        df = pd.read_sql_query(q2, con, params=[symbol, n_sample * 3])
        if len(df) > n_sample:
            idx = rng.choice(len(df), size=n_sample, replace=False)
            df = df.iloc[np.sort(idx)].reset_index(drop=True)
    return df


def aggressor_sign(side: str) -> int:
    s = str(side).upper()
    if s in ("B", "BUY", "BID"):
        return 1
    if s in ("A", "SELL", "ASK", "S"):
        return -1
    return 0


def measure_spread(l2: pd.DataFrame) -> Dict[str, Any]:
    sp = l2["spread_bps"].to_numpy(dtype=float)
    ts = pd.to_datetime(l2["timestamp_ms"], unit="ms", utc=True)
    hour = ts.dt.hour.to_numpy()
    by_hour: Dict[str, Any] = {}
    for h in range(24):
        m = hour == h
        if m.sum() < 30:
            continue
        by_hour[str(h)] = pctiles(sp[m])
    # Sampling interval diagnostic
    dts = np.diff(l2["timestamp_ms"].to_numpy(dtype=np.int64))
    dts = dts[dts > 0]
    return {
        "n": int(len(sp)),
        "spread_bps": pctiles(sp),
        "mean_spread_bps": float(np.nanmean(sp)),
        "half_spread_p50_bps": float(np.nanpercentile(sp, 50) / 2.0),
        "half_spread_p25_bps": float(np.nanpercentile(sp, 25) / 2.0),
        "half_spread_p75_bps": float(np.nanpercentile(sp, 75) / 2.0),
        "by_hour_utc": by_hour,
        "l2_sample_interval_ms": pctiles(dts.astype(float), (50, 90, 99)),
        "depth_usd": {
            "bid": pctiles(l2["bid_depth_usd"].to_numpy(dtype=float)),
            "ask": pctiles(l2["ask_depth_usd"].to_numpy(dtype=float)),
        },
    }


def measure_adverse_selection(
    l2: pd.DataFrame,
    trades: pd.DataFrame,
    rng: np.random.Generator,
) -> Dict[str, Any]:
    mid_ts = l2["timestamp_ms"].to_numpy(dtype=np.int64)
    mid = l2["mid_price"].to_numpy(dtype=float)
    if len(mid_ts) < 100 or trades.empty:
        return {"error": "insufficient_data"}

    t_ts = trades["timestamp_ms"].to_numpy(dtype=np.int64)
    signs = np.array([aggressor_sign(s) for s in trades["side"]], dtype=int)
    ok = signs != 0
    t_ts, signs = t_ts[ok], signs[ok]

    # Mid at or before trade
    idx0 = np.searchsorted(mid_ts, t_ts, side="right") - 1
    valid = (idx0 >= 0) & (idx0 < len(mid_ts) - 1)
    # Require mid not older than 30s
    age = t_ts - mid_ts[np.clip(idx0, 0, len(mid_ts) - 1)]
    valid &= (age >= 0) & (age <= 30_000)

    out: Dict[str, Any] = {"n_trades_sampled": int(len(t_ts)), "n_matched": int(valid.sum())}
    for name, horizon in AS_HORIZONS_MS:
        target = t_ts + horizon
        idx1 = np.searchsorted(mid_ts, target, side="left")
        v = valid & (idx1 < len(mid_ts))
        # Forward mid should be within horizon+30s of target
        fwd_age = mid_ts[np.clip(idx1, 0, len(mid_ts) - 1)] - target
        v &= (fwd_age >= -horizon) & (fwd_age <= 30_000)
        m0 = mid[idx0[v]]
        m1 = mid[idx1[v]]
        sg = signs[v]
        good = (m0 > 0) & np.isfinite(m0) & np.isfinite(m1)
        # Positive = adverse for MM counterparty
        as_bps = sg[good] * (m1[good] / m0[good] - 1.0) * 1e4
        out[name] = mean_ci(as_bps, BOOT_N, rng)
        out[name]["pctiles"] = pctiles(as_bps)
        # Effective realized horizon (median)
        out[name]["median_realized_lag_ms"] = float(
            np.median(mid_ts[idx1[v][good]] - t_ts[v][good])
        ) if good.sum() else float("nan")
    return out


def measure_inventory(trades: pd.DataFrame, mid_ref: float) -> Dict[str, Any]:
    """Inventory risk proxies from tape (MM as counterparty to every trade)."""
    if trades.empty:
        return {"error": "no_trades"}
    df = trades.sort_values("timestamp_ms").copy()
    signs = np.array([aggressor_sign(s) for s in df["side"]], dtype=float)
    px = df["price"].to_numpy(dtype=float)
    sz = df["size"].to_numpy(dtype=float)
    # MM inventory change: opposite of aggressor
    notional = signs * px * sz  # aggressor signed notional
    mm_dinv = -notional
    cum = np.cumsum(mm_dinv)
    # Zero-crossing times of cumulative inventory
    cross_idx = np.where(np.diff(np.sign(cum)) != 0)[0]
    ts = df["timestamp_ms"].to_numpy(dtype=np.int64)
    if len(cross_idx) >= 2:
        gaps = np.diff(ts[cross_idx])
        neutralize_ms = pctiles(gaps.astype(float), (50, 75, 90))
    else:
        neutralize_ms = {"p50": float("nan"), "p75": float("nan"), "p90": float("nan")}

    # 1-minute flow imbalance
    minute = (ts // 60_000).astype(np.int64)
    buy = np.where(signs > 0, px * sz, 0.0)
    sell = np.where(signs < 0, px * sz, 0.0)
    g = pd.DataFrame({"minute": minute, "buy": buy, "sell": sell}).groupby("minute", sort=True)
    agg = g.sum()
    tot = agg["buy"] + agg["sell"]
    imb = ((agg["buy"] - agg["sell"]) / tot.replace(0, np.nan)).to_numpy(dtype=float)
    # Run length of same-sign imbalance
    s = np.sign(imb)
    s = s[np.isfinite(s) & (s != 0)]
    runs: List[int] = []
    if len(s):
        cur, ln = s[0], 1
        for x in s[1:]:
            if x == cur:
                ln += 1
            else:
                runs.append(ln)
                cur, ln = x, 1
        runs.append(ln)
    run_a = np.asarray(runs, dtype=float) if runs else np.array([])

    mid = mid_ref if mid_ref > 0 else float(np.nanmedian(px))
    max_inv_usd = float(np.nanmax(np.abs(cum))) if len(cum) else float("nan")
    max_inv_coin = max_inv_usd / mid if mid > 0 else float("nan")

    return {
        "n_trades": int(len(df)),
        "max_abs_inventory_usd_if_take_all_flow": max_inv_usd,
        "max_abs_inventory_coin_if_take_all_flow": max_inv_coin,
        "inventory_zero_crossing_gap_ms": neutralize_ms,
        "imbalance_1m": {
            "mean_abs": float(np.nanmean(np.abs(imb))) if len(imb) else float("nan"),
            "pctiles_abs": pctiles(np.abs(imb)) if len(imb) else {},
            "same_sign_run_minutes": pctiles(run_a, (50, 75, 90, 99)) if len(run_a) else {},
        },
        "note": (
            "Take-all-flow inventory is a WORST-CASE upper bound (retail MM does not "
            "fill every trade). Neutralization gap = time between cum-inventory sign flips "
            "without crossing the spread."
        ),
    }


def analyze_l2_books(root: Path, symbol: str) -> Dict[str, Any]:
    sym_dir = root / symbol
    if not sym_dir.exists():
        return {"available": False, "reason": "missing_dir"}
    files = sorted(sym_dir.glob("*.jsonl.gz"))
    if not files:
        return {"available": False, "reason": "no_files"}

    best_bid_sz: List[float] = []
    best_ask_sz: List[float] = []
    best_bid_px: List[float] = []
    best_ask_px: List[float] = []
    mids: List[float] = []
    spreads_bps: List[float] = []
    ts_list: List[int] = []
    n_levels: List[int] = []
    n_rows = 0

    for fp in files:
        with gzip.open(fp, "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                obj = json.loads(line)
                bids = obj.get("bids") or []
                asks = obj.get("asks") or []
                if not bids or not asks:
                    continue
                bb, bsz = float(bids[0][0]), float(bids[0][1])
                ba, asz = float(asks[0][0]), float(asks[0][1])
                mid = float(obj.get("mid") or (bb + ba) / 2.0)
                sp_pct = obj.get("spread_pct")
                if sp_pct is None and mid > 0:
                    sp_bps = (ba - bb) / mid * 1e4
                else:
                    sp_bps = float(sp_pct) * 1e4
                best_bid_sz.append(bsz)
                best_ask_sz.append(asz)
                best_bid_px.append(bb)
                best_ask_px.append(ba)
                mids.append(mid)
                spreads_bps.append(sp_bps)
                ts_list.append(int(obj.get("exchange_ts_ms") or obj.get("received_ts_ms") or 0))
                n_levels.append(min(len(bids), len(asks)))
                n_rows += 1

    if n_rows < 10:
        return {"available": False, "reason": "too_few_rows", "n": n_rows}

    bb = np.asarray(best_bid_px)
    ba = np.asarray(best_ask_px)
    bsz = np.asarray(best_bid_sz)
    asz = np.asarray(best_ask_sz)
    mid = np.asarray(mids)
    ts = np.asarray(ts_list, dtype=np.int64)

    # BBO stability
    bid_chg = np.concatenate([[False], bb[1:] != bb[:-1]])
    ask_chg = np.concatenate([[False], ba[1:] != ba[:-1]])
    bbo_chg = bid_chg | ask_chg
    dts = np.diff(ts)
    dts = dts[dts > 0]
    # Time between BBO changes
    chg_idx = np.where(bbo_chg)[0]
    if len(chg_idx) >= 2:
        chg_gaps = np.diff(ts[chg_idx]).astype(float)
        chg_gaps = chg_gaps[chg_gaps > 0]
    else:
        chg_gaps = np.array([])

    # Queue-ahead estimate at touch (base size * mid → USD)
    touch_usd_bid = bsz * mid
    touch_usd_ask = asz * mid

    return {
        "available": True,
        "n_snapshots": n_rows,
        "n_files": len(files),
        "files": [f.name for f in files],
        "date_min_ms": int(ts.min()),
        "date_max_ms": int(ts.max()),
        "span_hours": float((ts.max() - ts.min()) / 3_600_000.0),
        "levels_recorded_p50": float(np.median(n_levels)),
        "spread_bps": pctiles(np.asarray(spreads_bps, dtype=float)),
        "touch_size_base": {
            "bid": pctiles(bsz),
            "ask": pctiles(asz),
        },
        "touch_size_usd": {
            "bid": pctiles(touch_usd_bid),
            "ask": pctiles(touch_usd_ask),
        },
        "bbo_change_rate": float(np.mean(bbo_chg)),
        "bbo_change_gap_ms": pctiles(chg_gaps, (50, 75, 90)) if len(chg_gaps) else {},
        "sample_interval_ms": pctiles(dts.astype(float), (50, 90)) if len(dts) else {},
        "note": (
            "Retail MM joins the BACK of the queue at the touch. Expected wait "
            "≈ touch_USD / trade_arrival_USD_at_touch under FIFO — HL matching "
            "is not guaranteed FIFO; this is an order-of-magnitude proxy only."
        ),
    }


def estimate_arrival_and_queue(
    trades: pd.DataFrame,
    l2_books: Dict[str, Any],
    half_spread_bps: float,
) -> Dict[str, Any]:
    if trades.empty or not l2_books.get("available"):
        return {"available": False}
    ts = trades["timestamp_ms"].to_numpy(dtype=np.int64)
    span_s = max(1.0, (ts.max() - ts.min()) / 1000.0)
    notional = (trades["price"] * trades["size"]).to_numpy(dtype=float)
    usd_per_hour = float(np.nansum(notional) / span_s * 3600.0)
    trades_per_hour = float(len(trades) / span_s * 3600.0)
    touch_usd = float(
        np.nanmedian(
            [
                l2_books["touch_size_usd"]["bid"]["p50"],
                l2_books["touch_size_usd"]["ask"]["p50"],
            ]
        )
    )
    # Fraction of tape notionally "at touch": |trade_px - rolling mid|≲ half spread
    # Without sync mid, approximate: trades near local price mode — use consecutive
    # price equality bursts as touch prints proxy.
    px = trades["price"].to_numpy(dtype=float)
    at_touch = np.concatenate([[True], px[1:] == px[:-1]])
    touch_frac = float(np.mean(at_touch))
    touch_usd_per_hour = usd_per_hour * max(touch_frac, 0.05)
    wait_s = touch_usd / (touch_usd_per_hour / 3600.0) if touch_usd_per_hour > 0 else float("nan")
    fills_per_hour_one_side = (
        (touch_usd_per_hour / touch_usd) if touch_usd > 0 else float("nan")
    )
    return {
        "available": True,
        "tape_usd_per_hour": usd_per_hour,
        "tape_trades_per_hour": trades_per_hour,
        "touch_size_usd_p50": touch_usd,
        "proxy_touch_trade_frac": touch_frac,
        "proxy_touch_usd_per_hour": touch_usd_per_hour,
        "est_queue_wait_sec_fifo": wait_s,
        "est_fills_per_hour_one_lot_at_touch": fills_per_hour_one_side,
        "half_spread_ref_bps": half_spread_bps,
        "limitation": (
            "Arrival/queue estimates combine ~1 month tape sample with ~1–2 days "
            "of depth books; touch fraction proxy is crude."
        ),
    }


def equation_row(
    spread: Dict[str, Any],
    asel: Dict[str, Any],
    maker_fee_bps: float,
    as_horizon: str = "10s",
) -> Dict[str, Any]:
    half = spread["half_spread_p50_bps"]
    as_mean = asel.get(as_horizon, {}).get("mean", float("nan"))
    as_lo = asel.get(as_horizon, {}).get("ci_low", float("nan"))
    as_hi = asel.get(as_horizon, {}).get("ci_high", float("nan"))
    # Per fill (one side)
    edge = half - as_mean - maker_fee_bps
    edge_lo = half - as_hi - maker_fee_bps  # worse AS
    edge_hi = half - as_lo - maker_fee_bps
    # Optimistic RT if both sides fill independently with same AS
    rt = 2 * half - 2 * as_mean - 2 * maker_fee_bps
    return {
        "half_spread_p50_bps": half,
        "as_horizon": as_horizon,
        "as_mean_bps": as_mean,
        "as_ci": [as_lo, as_hi],
        "maker_fee_bps": maker_fee_bps,
        "edge_per_fill_bps": edge,
        "edge_per_fill_ci_bps": [edge_lo, edge_hi],
        "edge_rt_optimistic_bps": rt,
        "positive_point": bool(np.isfinite(edge) and edge > 0),
        "positive_ci_low": bool(np.isfinite(edge_lo) and edge_lo > 0),
    }


def by_hour_equation(
    l2: pd.DataFrame,
    trades: pd.DataFrame,
    asel_global: Dict[str, Any],
    maker_fee_bps: float,
) -> Dict[str, Any]:
    """Per-hour spread p50/2 − global AS(10s) − fee (AS not re-estimated hourly — power)."""
    as10 = asel_global.get("10s", {}).get("mean", float("nan"))
    ts = pd.to_datetime(l2["timestamp_ms"], unit="ms", utc=True)
    hour = ts.dt.hour
    out: Dict[str, Any] = {}
    for h in range(24):
        sp = l2.loc[hour == h, "spread_bps"].to_numpy(dtype=float)
        if len(sp) < 50:
            continue
        half = float(np.nanpercentile(sp, 50) / 2.0)
        edge = half - as10 - maker_fee_bps
        out[str(h)] = {
            "half_spread_p50_bps": half,
            "edge_per_fill_bps": edge,
            "positive": bool(np.isfinite(edge) and edge > 0),
        }
    pos_hours = [h for h, v in out.items() if v["positive"]]
    return {"by_hour": out, "positive_hours_utc": pos_hours, "as10_global_bps": as10}


def write_report(path: Path, payload: Dict[str, Any]) -> None:
    lines: List[str] = []
    lines.append("# Market Making Feasibility — Hyperliquid")
    lines.append("")
    lines.append(f"Generated: {payload['generated_at']}")
    lines.append(f"DB (read-only): `{payload['db']}`")
    lines.append(f"L2 books: `{payload['l2_books_root']}`")
    lines.append(
        f"Window (metrics DB): {payload['window']['date_min']} → {payload['window']['date_max']} "
        f"({payload['window']['span_days']:.1f} days)"
    )
    lines.append("")
    lines.append("## Scope")
    lines.append("")
    lines.append(
        "Economic **viability** study only. No quoting logic, no strategy, "
        "no production config changes. The study is allowed to conclude that "
        "MM is **not** viable at retail access."
    )
    lines.append("")
    lines.append("## Limitations (declared)")
    lines.append("")
    for lim in payload["limitations"]:
        lines.append(f"- {lim}")
    lines.append("")
    lines.append("## Fundamental equation")
    lines.append("")
    lines.append("```")
    lines.append("edge_per_fill ≈ half_spread − adverse_selection − maker_fee")
    lines.append("edge_RT_opt  ≈ spread − 2·AS − 2·maker_fee   # both sides fill")
    lines.append("```")
    lines.append("")
    lines.append(
        "Inventory cost is reported separately (not subtracted into the point "
        "estimate) because it depends on risk limits / skew — see § Inventory."
    )
    lines.append("")
    lines.append("## Fees / rebate (documented, not assumed)")
    lines.append("")
    lines.append(f"Source: [{HL_FEES_SOURCE}]({HL_FEES_SOURCE}) (fetched 2026-08-10).")
    lines.append("")
    lines.append("| Context | Maker (bps/side) | Notes |")
    lines.append("|---|---:|---|")
    lines.append("| **Perps tier 0 base (retail)** | **1.5** | 0.015% — primary assumption |")
    lines.append("| Tier 2 (>$25M 14d) | 0.8 | 0.008% |")
    lines.append("| Tier 4 (>$500M) | 0.0 | maker only |")
    lines.append("| Maker rebate (≥0.5% maker share) | −0.1 | **not retail-realistic** |")
    lines.append(
        f"| Bot `execution.maker_orders.maker_fee_pct` | {BOT_CONFIG_MAKER_BPS:.1f} | "
        "config 0.01% — **below** current HL base; do not use as truth |"
    )
    lines.append(
        f"| Bot directional taker RT (ref) | {DIRECTIONAL_TAKER_RT_BPS:.1f} | "
        "3.5 fee + 2 slip ×2 |"
    )
    lines.append("")
    lines.append(
        "Primary verdict uses **tier-0 maker 1.5 bps/side**. Rebate tiers require "
        "≥0.5% of exchange maker volume — out of scope for this account class."
    )
    lines.append("")

    ov = payload["overall_verdict"]
    lines.append(f"## Overall verdict: **({ov['code']})**")
    lines.append("")
    lines.append(ov["summary"])
    lines.append("")
    lines.append(ov["detail"])
    lines.append("")

    lines.append("## Per-symbol equation (AS horizon = 10s)")
    lines.append("")
    lines.append(
        "| symbol | spread p50 | half p50 | AS 10s [CI] | fee | "
        "edge/fill [CI] | edge RT opt | +ve? |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|:---:|")
    for sym, row in payload["symbols"].items():
        eq = row["equation_tier0"]
        sp = row["spread"]["spread_bps"]["p50"]
        as10 = eq["as_mean_bps"]
        ci = eq["as_ci"]
        eci = eq["edge_per_fill_ci_bps"]
        lines.append(
            f"| {sym} | {sp:.3f} | {eq['half_spread_p50_bps']:.3f} | "
            f"{as10:.3f} [{ci[0]:.3f},{ci[1]:.3f}] | {eq['maker_fee_bps']:.1f} | "
            f"{eq['edge_per_fill_bps']:.3f} [{eci[0]:.3f},{eci[1]:.3f}] | "
            f"{eq['edge_rt_optimistic_bps']:.3f} | "
            f"{'Y' if eq['positive_point'] else 'n'} |"
        )
    lines.append("")
    lines.append("### Adverse selection by horizon")
    lines.append("")
    lines.append("| symbol | AS 1s | AS 10s | AS 60s | median realized lag 10s (ms) |")
    lines.append("|---|---:|---:|---:|---:|")
    for sym, row in payload["symbols"].items():
        a = row["adverse_selection"]
        lines.append(
            f"| {sym} | {a['1s']['mean']:.3f} | {a['10s']['mean']:.3f} | "
            f"{a['60s']['mean']:.3f} | {a['10s'].get('median_realized_lag_ms', float('nan')):.0f} |"
        )
    lines.append("")
    lines.append(
        "Positive AS = mid moved against the MM who was the trade's counterparty. "
        f"l2_snapshots sample ~every {payload['typical_l2_interval_ms']:.0f} ms median — "
        "the **1s** bucket is often resolved at the next sample (declare as soft)."
    )
    lines.append("")

    lines.append("## Hours where edge > 0 (tier-0, AS=global 10s)")
    lines.append("")
    for sym, row in payload["symbols"].items():
        hours = row["by_hour"]["positive_hours_utc"]
        lines.append(
            f"- **{sym}:** {hours if hours else 'none'} "
            f"(n_pos={len(hours)}/24)"
        )
    lines.append("")

    lines.append("## Competition / fill realism (recorded L2 books)")
    lines.append("")
    lines.append(
        f"Depth window: ~{payload['l2_books_span_hours']:.1f} hours across files "
        "(started 2026-08-09). Metrics DB has **no** level sizes — queue estimates "
        "use books only."
    )
    lines.append("")
    lines.append(
        "| symbol | touch USD p50 | BBO change rate | BBO gap p50 (ms) | "
        "est queue wait (s) | est fills/h @1 lot |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|")
    for sym, row in payload["symbols"].items():
        b = row["l2_books"]
        q = row["queue"]
        if not b.get("available"):
            lines.append(f"| {sym} | n/a | n/a | n/a | n/a | n/a |")
            continue
        lines.append(
            f"| {sym} | {b['touch_size_usd']['bid']['p50']:.0f}/"
            f"{b['touch_size_usd']['ask']['p50']:.0f} | "
            f"{b['bbo_change_rate']*100:.1f}% | "
            f"{b.get('bbo_change_gap_ms', {}).get('p50', float('nan')):.0f} | "
            f"{q.get('est_queue_wait_sec_fifo', float('nan')):.1f} | "
            f"{q.get('est_fills_per_hour_one_lot_at_touch', float('nan')):.1f} |"
        )
    lines.append("")

    lines.append("## Inventory risk (tape proxies)")
    lines.append("")
    lines.append(
        "| symbol | max \\|inv\\| USD if take-all-flow | 1m \\|imb\\| p50 | "
        "same-sign run p50 (min) | zero-cross gap p50 (ms) |"
    )
    lines.append("|---|---:|---:|---:|---:|")
    for sym, row in payload["symbols"].items():
        inv = row["inventory"]
        imb = inv.get("imbalance_1m", {})
        lines.append(
            f"| {sym} | {inv.get('max_abs_inventory_usd_if_take_all_flow', float('nan')):,.0f} | "
            f"{imb.get('pctiles_abs', {}).get('p50', float('nan')):.2f} | "
            f"{imb.get('same_sign_run_minutes', {}).get('p50', float('nan')):.1f} | "
            f"{inv.get('inventory_zero_crossing_gap_ms', {}).get('p50', float('nan')):.0f} |"
        )
    lines.append("")
    lines.append(
        "Take-all-flow inventory is a **worst-case upper bound**. Real retail MM "
        "fills a tiny fraction of tape — scale inventory risk by fill share."
    )
    lines.append("")

    lines.append("## Comparison vs directional 11 bps")
    lines.append("")
    lines.append(
        "Directional candle strategies needed ~11 bps RT to break even and had "
        "best BE ~6.8 bps (verdict C). MM flips the sign of the spread term:"
    )
    lines.append("")
    for sym, row in payload["symbols"].items():
        eq = row["equation_tier0"]
        lines.append(
            f"- **{sym}:** MM edge/fill ≈ **{eq['edge_per_fill_bps']:.2f} bps** "
            f"vs paying 11 bps RT to harvest ~1–7 bps gross directional — "
            f"{'better structure' if eq['edge_per_fill_bps'] > -11 else 'still poor'}, "
            f"but absolute edge is "
            f"{'positive' if eq['positive_point'] else '≤0 at tier-0'}."
        )
    lines.append("")
    lines.append("## What would need to be true for (B)→(A)")
    lines.append("")
    for item in payload["overall_verdict"].get("would_need", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Conclusion")
    lines.append("")
    lines.append(ov["conclusion"])
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DB_DEFAULT)
    ap.add_argument("--l2-books", type=Path, default=L2_BOOKS_DEFAULT)
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-doc", type=Path, default=OUT_DOC)
    ap.add_argument("--sample-trades", type=int, default=TRADE_SAMPLE_PER_SYM)
    args = ap.parse_args()

    rng = np.random.default_rng(RNG_SEED)
    con = open_ro(args.db)
    try:
        symbols_payload: Dict[str, Any] = {}
        all_ts_min = []
        all_ts_max = []
        l2_intervals = []
        books_hours = []

        for sym in SYMBOLS:
            print(f"[{sym}] loading l2_snapshots…", flush=True)
            l2 = load_l2_metrics(con, sym)
            print(f"[{sym}] sampling trades…", flush=True)
            trades = sample_trades(con, sym, args.sample_trades, rng)
            print(f"[{sym}] spread + AS + inventory…", flush=True)
            spread = measure_spread(l2)
            asel = measure_adverse_selection(l2, trades, rng)
            inv = measure_inventory(trades, float(l2["mid_price"].median()))
            print(f"[{sym}] L2 books…", flush=True)
            books = analyze_l2_books(args.l2_books, sym)
            queue = estimate_arrival_and_queue(
                trades, books, spread["half_spread_p50_bps"]
            )
            eq0 = equation_row(spread, asel, HL_PERP_MAKER_BPS["tier0_base"], "10s")
            eq2 = equation_row(spread, asel, HL_PERP_MAKER_BPS["tier2_25M"], "10s")
            eq4 = equation_row(spread, asel, HL_PERP_MAKER_BPS["tier4_500M"], "10s")
            # Also with 60s AS (more conservative / includes more path)
            eq0_60 = equation_row(spread, asel, HL_PERP_MAKER_BPS["tier0_base"], "60s")
            by_h = by_hour_equation(l2, trades, asel, HL_PERP_MAKER_BPS["tier0_base"])

            all_ts_min.append(int(l2["timestamp_ms"].min()))
            all_ts_max.append(int(l2["timestamp_ms"].max()))
            l2_intervals.append(spread["l2_sample_interval_ms"].get("p50", float("nan")))
            if books.get("available"):
                books_hours.append(float(books["span_hours"]))

            symbols_payload[sym] = {
                "spread": spread,
                "adverse_selection": asel,
                "inventory": inv,
                "l2_books": books,
                "queue": queue,
                "equation_tier0": eq0,
                "equation_tier2": eq2,
                "equation_tier4_maker0": eq4,
                "equation_tier0_as60s": eq0_60,
                "by_hour": by_h,
                "n_trades_sampled": int(len(trades)),
            }
            print(
                f"[{sym}] half={eq0['half_spread_p50_bps']:.3f} "
                f"AS10={eq0['as_mean_bps']:.3f} edge={eq0['edge_per_fill_bps']:.3f}",
                flush=True,
            )
    finally:
        con.close()

    tmin = min(all_ts_min)
    tmax = max(all_ts_max)
    d0 = datetime.fromtimestamp(tmin / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    d1 = datetime.fromtimestamp(tmax / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    # Verdict logic
    pos_syms = [
        s
        for s, r in symbols_payload.items()
        if r["equation_tier0"]["positive_point"]
    ]
    pos_ci = [
        s
        for s, r in symbols_payload.items()
        if r["equation_tier0"]["positive_ci_low"]
    ]
    pos_tier2 = [
        s
        for s, r in symbols_payload.items()
        if r["equation_tier2"]["positive_point"]
    ]
    pos_as60 = [
        s
        for s, r in symbols_payload.items()
        if r["equation_tier0_as60s"]["positive_point"]
    ]

    would_need: List[str] = []
    if not pos_syms:
        # Check how close
        gaps = {
            s: r["equation_tier0"]["edge_per_fill_bps"]
            for s, r in symbols_payload.items()
        }
        best_s = max(gaps, key=lambda k: gaps[k] if np.isfinite(gaps[k]) else -1e9)
        best_e = gaps[best_s]
        would_need.append(
            f"Tier-0 edge best on {best_s} = {best_e:.3f} bps — need "
            f"{max(0.0, -best_e):.3f} bps more (tighter AS, wider postable spread, "
            f"or lower fee)."
        )
        would_need.append(
            "Reach fee tier ≥2 (>$25M 14d) and/or staking discounts — still "
            f"check: positive at tier2 on {pos_tier2 or 'none'}."
        )
        would_need.append(
            "Demonstrate selective quoting that avoids toxic flow so AS(10s) "
            "falls below half-spread − fee (requires live latency + cancel skill)."
        )
        would_need.append(
            "Accumulate weeks of L2 books to validate queue wait / fill rate "
            "before sizing inventory capital."
        )

    if pos_ci:
        code = "A"
        summary = (
            f"Equation positive with CI above zero on {pos_ci} at tier-0 maker "
            "fees and 10s AS — justifies investigating MM architecture "
            "(still not a license to trade)."
        )
        conclusion = (
            "Retail-access MM shows a defensible positive edge on at least one "
            "symbol after measured adverse selection. Next step would be "
            "architecture / inventory design — **not** done here."
        )
    elif pos_syms and not pos_as60:
        code = "B"
        summary = (
            f"Point estimate positive on {pos_syms} at 10s AS, but fragile: "
            f"CI not clear of zero and/or 60s AS kills edge ({pos_as60 or 'none'} "
            "survive 60s)."
        )
        conclusion = (
            "Marginal. MM is not ruled out, but only under optimistic markout "
            "horizons / fee tiers. List of required truths is in the report."
        )
    elif pos_syms:
        code = "B"
        summary = (
            f"Point positive on {pos_syms} but not CI-robust ({pos_ci or 'none'})."
        )
        conclusion = (
            "Marginal — investigate only if fee tier / latency assumptions in "
            "would_need can be met; otherwise treat as non-viable for this account."
        )
    else:
        code = "C"
        summary = (
            "Equation ≤ 0 on all symbols at retail tier-0 maker fees after "
            "measured adverse selection (10s). MM does not invert the "
            "directional cost problem at this access level."
        )
        conclusion = (
            "Market making is **not** economically viable as a retail-tier "
            "Hyperliquid strategy under the measured half-spread vs adverse-"
            "selection arithmetic. Closing this path is as valuable as a "
            "positive finding: do not build MM infrastructure expecting the "
            "spread alone to pay. Revisit only if fee tier / maker-share "
            "rebate / multi-week depth + proven toxic-flow avoidance change "
            "the inputs."
        )

    # Soften: if tier2 positive with CI, call B not C
    if code == "C" and pos_tier2:
        code = "B"
        summary = (
            "Tier-0 retail equation is ≤0 on all symbols, but tier-2 fee "
            f"schedule ({pos_tier2}) flips the point estimate — viability is "
            "conditional on volume tier, not on microstructure alone."
        )
        conclusion = (
            "Marginal / conditional. Without ≥$25M 14d volume (or equivalent "
            "staking stack), MM edge is non-positive after AS. Do not build "
            "for tier-0; only reconsider if fee tier is realistic."
        )
        would_need.insert(
            0,
            "Operate at Hyperliquid perps fee tier ≥2 (>$25M 14d weighted volume) "
            "as a hard precondition.",
        )

    payload: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(args.db.resolve()),
        "l2_books_root": str(args.l2_books),
        "window": {
            "date_min": d0,
            "date_max": d1,
            "span_days": (tmax - tmin) / 86_400_000.0,
            "t_min_ms": tmin,
            "t_max_ms": tmax,
        },
        "typical_l2_interval_ms": float(np.nanmedian(l2_intervals)),
        "l2_books_span_hours": float(np.nansum(books_hours) / max(len(SYMBOLS), 1)),
        "fees": {
            "source": HL_FEES_SOURCE,
            "perp_maker_bps": HL_PERP_MAKER_BPS,
            "perp_taker_bps": HL_PERP_TAKER_BPS,
            "maker_rebate_bps": HL_MAKER_REBATE_BPS,
            "bot_config_maker_bps": BOT_CONFIG_MAKER_BPS,
            "directional_taker_rt_bps": DIRECTIONAL_TAKER_RT_BPS,
        },
        "limitations": [
            "Metrics DB covers ~1 month of derived L2 (mid/spread/depth USD/OIR) — no queue levels.",
            "Full depth books on E: exist only since 2026-08-09 (hours–days, not months).",
            "l2_snapshots sampling interval is multi-second — 1s AS is soft / next-sample.",
            "No measured colocated latency, cancel success, or true FIFO queue position.",
            "AS assumes MM was the unique counterparty at the print — overstates toxicity exposure for a small quote size.",
            "Inventory 'take-all-flow' is a worst-case upper bound, not expected inventory.",
            "Maker rebates require material share of exchange maker volume — excluded from primary equation.",
        ],
        "symbols": symbols_payload,
        "overall_verdict": {
            "code": code,
            "summary": summary,
            "detail": (
                f"Positive tier0 point: {pos_syms or 'none'}; "
                f"CI-robust: {pos_ci or 'none'}; "
                f"tier2 point: {pos_tier2 or 'none'}; "
                f"tier0 with 60s AS: {pos_as60 or 'none'}."
            ),
            "conclusion": conclusion,
            "would_need": would_need,
            "positive_symbols_tier0": pos_syms,
            "positive_symbols_tier0_ci": pos_ci,
        },
    }

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(payload, indent=2, allow_nan=True), encoding="utf-8")
    write_report(args.out_doc, payload)
    print(f"Wrote {args.out_json}")
    print(f"Wrote {args.out_doc}")
    print(f"OVERALL ({code}): {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
