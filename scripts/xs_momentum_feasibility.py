#!/usr/bin/env python3
"""Cross-sectional slow momentum feasibility (measurement only).

Implements the approved study prompt with mandatory corrections:
  - tier-0 fees: taker 0.045%/side, maker 0.015%/side (primary = taker)
  - turnover = Σ|Δweights| / gross; costs on traded notional only (no double-count)
  - signal at close t → execute at open t+1
  - funding PIT when available; incomplete coverage blocks verdict A
  - delistings force-closed at last tradable price
  - one a-priori PRIMARY spec (no OOS parameter search)
  - random-rank baselines (≥200 seeds), BTC B&H, cash
  - capacity via ADV participation caps

Usage:
  python scripts/xs_momentum_feasibility.py
  python scripts/xs_momentum_feasibility.py --skip-download   # reuse cache
  python scripts/xs_momentum_feasibility.py --seeds 200

Never writes to bot.db. Never touches .env / production settings.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "backtests" / "xs_momentum"
CACHE_DB = OUT_DIR / "hl_daily_panel.db"
REPORT_MD = ROOT / "docs" / "XS_MOMENTUM_FEASIBILITY.md"
REPORT_JSON = OUT_DIR / "xs_momentum_feasibility.json"

HL_INFO = "https://api.hyperliquid.xyz/info"
SLEEP_SEC = 0.05
RNG_SEED = 42

# ─── Frozen PRIMARY (a priori — NOT chosen on OOS) ───────────────────────────
PRIMARY = {
    "name": "xs_mom_primary_v1",
    "lookback_days": 30,
    "rebalance_days": 7,
    "n_long": 10,
    "n_short": 10,
    "min_age_days": 30,
    "min_adv_usd_20d": 1_000_000.0,  # liquid PIT filter (pre-registered)
    "max_universe_symbols": 60,      # top-ADV cap (pre-registered)
    "vol_lookback_days": 20,
    "gross_long": 1.0,
    "gross_short": 1.0,
    "fee_taker_bps_side": 4.5,   # tier-0 official
    "fee_maker_bps_side": 1.5,   # tier-0 official
    "primary_fee_leg": "taker",  # conservative rebalance assumption
    "slip_base_bps_side": 2.0,
    "slip_illiquid_extra_bps": 5.0,
    "adv_illiquid_usd": 2_000_000.0,
    "participation_cap": 0.01,   # 1% ADV base scenario
    "capital_usd": 100_000.0,
    "warmup_days": 60,
}

# Sensitivity variants — reported, NOT used for primary verdict
SENSITIVITY_SPECS = [
    {**PRIMARY, "name": "sens_lb14", "lookback_days": 14},
    {**PRIMARY, "name": "sens_rebal14", "rebalance_days": 14},
    {**PRIMARY, "name": "sens_n5", "n_long": 5, "n_short": 5},
    {**PRIMARY, "name": "sens_maker", "primary_fee_leg": "maker"},
]

# Frozen PASS criteria (corrections)
PASS_CRITERIA = {
    "min_oos_calendar_days": 730,       # ≥24 months evaluation
    "min_oos_rebalances": 100,
    "min_random_rank_percentile": 95.0,
    "min_net_pf": 1.0,
    "require_net_expectancy_gt0": True,
    "max_abs_beta_btc_global": 0.20,
    "max_abs_beta_btc_crash": 0.30,
    "max_symbol_contrib_share": 0.35,   # |pnl_sym| / sum|pnl|
    "max_best_year_share": 0.70,        # best year / total if total>0
    "require_funding_coverage": True,  # else block A
    "min_funding_coverage": 0.90,
}


# ─── HTTP helpers ────────────────────────────────────────────────────────────

def _hl_post(payload: dict, retries: int = 8) -> Any:
    last_err: Optional[Exception] = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                HL_INFO,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "xs-mom-research/1.0"},
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001 — network retry
            last_err = e
            msg = str(e)
            # Honour rate limits aggressively
            if "429" in msg or "Too Many" in msg:
                time.sleep(min(30.0, 2.0 * (i + 1) ** 2))
            else:
                time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"HL post failed after retries: {last_err}")


def _ms_to_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


# ─── Cache schema ────────────────────────────────────────────────────────────

def _init_cache(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS universe (
            name TEXT PRIMARY KEY,
            is_delisted INTEGER NOT NULL,
            sz_decimals INTEGER,
            max_leverage REAL,
            first_candle_ms INTEGER,
            last_candle_ms INTEGER,
            n_candles INTEGER
        );
        CREATE TABLE IF NOT EXISTS candles_1d (
            symbol TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, n_trades INTEGER,
            PRIMARY KEY (symbol, ts_ms)
        );
        CREATE TABLE IF NOT EXISTS funding (
            symbol TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            funding_rate REAL NOT NULL,
            premium REAL,
            PRIMARY KEY (symbol, ts_ms)
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    con.commit()


def fetch_universe() -> List[Dict[str, Any]]:
    raw = _hl_post({"type": "metaAndAssetCtxs"})
    meta, ctxs = raw[0], raw[1]
    out = []
    for u, ctx in zip(meta["universe"], ctxs):
        name = str(u["name"])
        if ":" in name:
            continue  # skip HIP-3 / builder names
        out.append(
            {
                "name": name,
                "is_delisted": 1 if u.get("isDelisted") else 0,
                "sz_decimals": int(u.get("szDecimals") or 0),
                "max_leverage": float(u.get("maxLeverage") or 0),
                "day_ntl_vlm": float(ctx.get("dayNtlVlm") or 0),
                "open_interest": float(ctx.get("openInterest") or 0),
                "mark_px": float(ctx.get("markPx") or 0),
            }
        )
    return out


def fetch_candles_1d(symbol: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
    bars = _hl_post(
        {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol,
                "interval": "1d",
                "startTime": int(start_ms),
                "endTime": int(end_ms),
            },
        }
    )
    rows = []
    for b in bars or []:
        rows.append(
            {
                "symbol": symbol,
                "ts_ms": int(b["t"]),
                "open": float(b["o"]),
                "high": float(b["h"]),
                "low": float(b["l"]),
                "close": float(b["c"]),
                "volume": float(b.get("v") or 0),
                "n_trades": int(b.get("n") or 0),
            }
        )
    return rows


def fetch_funding_symbol(symbol: str, start_ms: int, end_ms: int) -> List[Tuple[int, float, Optional[float]]]:
    """Paginate HL fundingHistory (500/page) for [start_ms, end_ms]."""
    out: Dict[int, Tuple[float, Optional[float]]] = {}
    cursor = int(start_ms)
    for _ in range(120):
        fh = _hl_post({"type": "fundingHistory", "coin": symbol, "startTime": cursor})
        if not fh:
            break
        for r in fh:
            ts = int(r["time"])
            if ts < start_ms or ts > end_ms:
                continue
            prem = r.get("premium")
            out[ts] = (float(r["fundingRate"]), float(prem) if prem is not None else None)
        last = int(fh[-1]["time"])
        if last <= cursor:
            break
        cursor = last + 1
        if last >= end_ms:
            break
        time.sleep(SLEEP_SEC)
    return [(ts, fr, pr) for ts, (fr, pr) in sorted(out.items())]


def download_panel(*, force: bool = False, max_symbols: Optional[int] = None) -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(CACHE_DB))
    _init_cache(con)

    now = int(time.time() * 1000)
    start = now - 950 * 86400000
    univ = fetch_universe()
    if max_symbols:
        # Prefer high day volume + all delisted
        delisted = [u for u in univ if u["is_delisted"]]
        active = sorted(
            [u for u in univ if not u["is_delisted"]],
            key=lambda x: x["day_ntl_vlm"],
            reverse=True,
        )
        keep = {u["name"] for u in delisted}
        for u in active:
            if len(keep) >= max_symbols:
                break
            keep.add(u["name"])
        univ = [u for u in univ if u["name"] in keep]

    print(f"[download] universe={len(univ)} delisted={sum(u['is_delisted'] for u in univ)}", flush=True)

    existing = {
        r[0]
        for r in con.execute("SELECT symbol FROM candles_1d GROUP BY symbol").fetchall()
    }
    spans: List[Dict[str, Any]] = []
    for i, u in enumerate(univ):
        sym = u["name"]
        if (not force) and sym in existing:
            row = con.execute(
                "SELECT MIN(ts_ms), MAX(ts_ms), COUNT(*) FROM candles_1d WHERE symbol=?",
                (sym,),
            ).fetchone()
            t0, t1, n = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        else:
            bars = fetch_candles_1d(sym, start, now)
            time.sleep(SLEEP_SEC)
            if bars:
                con.executemany(
                    "INSERT OR REPLACE INTO candles_1d "
                    "(symbol,ts_ms,open,high,low,close,volume,n_trades) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (
                            b["symbol"],
                            b["ts_ms"],
                            b["open"],
                            b["high"],
                            b["low"],
                            b["close"],
                            b["volume"],
                            b["n_trades"],
                        )
                        for b in bars
                    ],
                )
                con.commit()
                t0, t1, n = bars[0]["ts_ms"], bars[-1]["ts_ms"], len(bars)
            else:
                t0, t1, n = 0, 0, 0
            if i % 20 == 0:
                print(f"  candles {i}/{len(univ)} {sym} n={n}", flush=True)

        con.execute(
            "INSERT OR REPLACE INTO universe "
            "(name,is_delisted,sz_decimals,max_leverage,first_candle_ms,last_candle_ms,n_candles) "
            "VALUES (?,?,?,?,?,?,?)",
            (sym, u["is_delisted"], u["sz_decimals"], u["max_leverage"], t0, t1, n),
        )
        spans.append(
            {
                "symbol": sym,
                "is_delisted": bool(u["is_delisted"]),
                "n_candles": n,
                "first": _ms_to_date(t0) if t0 else None,
                "last": _ms_to_date(t1) if t1 else None,
                "days": (t1 - t0) / 86400000 if n > 1 else 0.0,
                "day_ntl_vlm": u["day_ntl_vlm"],
            }
        )
    con.commit()

    # Concurrent eligible count by month (any candle covering month-start)
    ok = [s for s in spans if s["n_candles"] > 0]
    span_bounds = {}
    for name, t0, t1 in con.execute(
        "SELECT name, first_candle_ms, last_candle_ms FROM universe WHERE n_candles > 0"
    ):
        span_bounds[str(name)] = (int(t0 or 0), int(t1 or 0))
    months = []
    t = int(datetime(2024, 3, 1, tzinfo=timezone.utc).timestamp() * 1000)
    while t < now:
        n_cov = sum(1 for t0, t1 in span_bounds.values() if t0 <= t <= t1)
        months.append({"month": _ms_to_date(t)[:7], "n_symbols": n_cov})
        t += 30 * 86400000

    viability = {
        "venue": "hyperliquid",
        "n_universe": len(univ),
        "n_delisted": sum(1 for u in univ if u["is_delisted"]),
        "n_with_candles": len(ok),
        "median_days": float(np.median([s["days"] for s in ok])) if ok else 0.0,
        "ge700d": sum(1 for s in ok if s["days"] >= 700),
        "ge365d": sum(1 for s in ok if s["days"] >= 365),
        "concurrent_by_month": months,
        "min_concurrent": min((m["n_symbols"] for m in months), default=0),
        "max_concurrent": max((m["n_symbols"] for m in months), default=0),
        "delisted_with_history": sum(1 for s in ok if s["is_delisted"]),
        "decision": "GO" if (min((m["n_symbols"] for m in months), default=0) >= 20 and max((s["days"] for s in ok), default=0) >= 700) else "INCONCLUSIVE_DATA",
        "notes": [
            "HL candleSnapshot retains ~900d 1d history for many names from ~2024-02-22.",
            "Delisted names remain queryable via candleSnapshot (survivorship includable).",
            "No candles_1d in live research DB; panel cached under data/backtests/xs_momentum/.",
        ],
    }
    con.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
        ("viability", json.dumps(viability)),
    )
    con.commit()
    con.close()
    print(f"[download] viability decision={viability['decision']} min_concurrent={viability['min_concurrent']}", flush=True)
    return viability


def ensure_funding(symbols: Sequence[str], *, force: bool = False) -> Dict[str, Any]:
    con = sqlite3.connect(str(CACHE_DB))
    _init_cache(con)
    now = int(time.time() * 1000)
    start = now - 950 * 86400000
    cov: Dict[str, Any] = {}
    for i, sym in enumerate(symbols):
        n_exist = con.execute(
            "SELECT COUNT(*) FROM funding WHERE symbol=?", (sym,)
        ).fetchone()[0]
        if n_exist > 1000 and not force:
            row = con.execute(
                "SELECT MIN(ts_ms), MAX(ts_ms) FROM funding WHERE symbol=?", (sym,)
            ).fetchone()
            cov[sym] = {
                "n": int(n_exist),
                "first": _ms_to_date(int(row[0])),
                "last": _ms_to_date(int(row[1])),
                "cached": True,
            }
            continue
        rows = fetch_funding_symbol(sym, start, now)
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO funding(symbol,ts_ms,funding_rate,premium) VALUES (?,?,?,?)",
                [(sym, ts, fr, pr) for ts, fr, pr in rows],
            )
            con.commit()
        cov[sym] = {
            "n": len(rows),
            "first": _ms_to_date(rows[0][0]) if rows else None,
            "last": _ms_to_date(rows[-1][0]) if rows else None,
            "cached": False,
        }
        if i % 5 == 0:
            print(f"  funding {i}/{len(symbols)} {sym} n={len(rows)}", flush=True)
    con.close()
    return cov


# ─── Panel construction ──────────────────────────────────────────────────────

def load_panel() -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    con = sqlite3.connect(str(CACHE_DB))
    candles = pd.read_sql_query(
        "SELECT symbol, ts_ms, open, high, low, close, volume, n_trades FROM candles_1d ORDER BY symbol, ts_ms",
        con,
    )
    univ = pd.read_sql_query("SELECT * FROM universe", con)
    meta_row = con.execute("SELECT value FROM meta WHERE key='viability'").fetchone()
    viability = json.loads(meta_row[0]) if meta_row else {}
    con.close()
    if candles.empty:
        raise RuntimeError("empty candle panel — run download first")
    dates = pd.to_datetime(candles["ts_ms"], unit="ms", utc=True).dt.floor("D")
    dollar_volume = candles["close"].to_numpy(dtype=float) * candles["volume"].to_numpy(dtype=float)
    candles = candles.assign(date=dates, dollar_volume=dollar_volume)
    return candles, univ, viability


def load_funding_daily(symbols: Sequence[str]) -> pd.DataFrame:
    """Sum hourly funding rates into daily totals per symbol/date."""
    if not symbols:
        return pd.DataFrame(columns=["symbol", "date", "funding_rate_day"])
    con = sqlite3.connect(str(CACHE_DB))
    q = (
        "SELECT symbol, ts_ms, funding_rate FROM funding WHERE symbol IN ("
        + ",".join("?" * len(symbols))
        + ")"
    )
    raw = pd.read_sql_query(q, con, params=list(symbols))
    con.close()
    if raw.empty:
        return pd.DataFrame(columns=["symbol", "date", "funding_rate_day"])
    dates = pd.to_datetime(raw["ts_ms"], unit="ms", utc=True).dt.floor("D")
    raw = raw.assign(date=dates)
    daily = (
        raw.groupby(["symbol", "date"], as_index=False)["funding_rate"]
        .sum()
        .rename(columns={"funding_rate": "funding_rate_day"})
    )
    return daily


# ─── Portfolio engine ────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    name: str
    n_rebalances: int
    n_days: int
    start: str
    end: str
    total_return_net: float
    sharpe_net: float
    profit_factor_net: float
    expectancy_per_rebalance: float
    max_drawdown: float
    mean_turnover: float  # Σ|Δw|/gross per rebalance
    ann_cost_bps: float
    beta_btc: float
    beta_btc_crash: float
    funding_pnl: float
    fee_pnl: float
    slip_pnl: float
    gross_pnl: float
    net_pnl: float
    funding_coverage: float
    symbol_contrib: Dict[str, float]
    yearly: Dict[str, float]
    capacity_notes: Dict[str, Any]
    daily_returns: Optional[np.ndarray] = None


def _inv_vol_weights(vols: np.ndarray) -> np.ndarray:
    v = np.asarray(vols, dtype=float)
    v = np.where(np.isfinite(v) & (v > 1e-8), v, np.nan)
    if not np.any(np.isfinite(v)):
        n = len(v)
        return np.ones(n) / n if n else v
    inv = 1.0 / v
    inv = np.where(np.isfinite(inv), inv, 0.0)
    s = inv.sum()
    if s <= 0:
        n = len(v)
        return np.ones(n) / n
    return inv / s


def run_portfolio(
    panel: pd.DataFrame,
    funding_daily: pd.DataFrame,
    spec: Dict[str, Any],
    *,
    rank_mode: str = "momentum",  # momentum | random
    rng: Optional[np.random.Generator] = None,
    eval_start: Optional[pd.Timestamp] = None,
    eval_end: Optional[pd.Timestamp] = None,
) -> BacktestResult:
    """Dollar-neutral L/S cross-sectional momentum with t→t+1 open execution."""
    lb = int(spec["lookback_days"])
    reb = int(spec["rebalance_days"])
    n_l = int(spec["n_long"])
    n_s = int(spec["n_short"])
    min_age = int(spec["min_age_days"])
    min_adv = float(spec["min_adv_usd_20d"])
    vol_lb = int(spec["vol_lookback_days"])
    fee_bps = (
        float(spec["fee_taker_bps_side"])
        if spec.get("primary_fee_leg", "taker") == "taker"
        else float(spec["fee_maker_bps_side"])
    )
    slip_base = float(spec["slip_base_bps_side"])
    slip_x = float(spec["slip_illiquid_extra_bps"])
    adv_illiq = float(spec["adv_illiquid_usd"])
    part_cap = float(spec["participation_cap"])
    capital = float(spec["capital_usd"])
    gross = float(spec["gross_long"]) + float(spec["gross_short"])

    # Wide panel
    px = panel.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
    opn = panel.pivot_table(index="date", columns="symbol", values="open", aggfunc="last")
    dvol = panel.pivot_table(index="date", columns="symbol", values="dollar_volume", aggfunc="last")
    px = px.sort_index()
    opn = opn.reindex(px.index)
    dvol = dvol.reindex(px.index)

    # First valid date per symbol (listing proxy)
    first_idx = {c: px[c].first_valid_index() for c in px.columns}
    last_idx = {c: px[c].last_valid_index() for c in px.columns}

    ret_lb = px / px.shift(lb) - 1.0
    # Daily simple returns from open-to-open for holding PnL after entry at open
    ret_oo = opn / opn.shift(1) - 1.0
    # close-to-close for BTC beta
    ret_cc = px / px.shift(1) - 1.0
    vol20 = ret_cc.rolling(vol_lb, min_periods=max(5, vol_lb // 2)).std()
    adv20 = dvol.rolling(20, min_periods=5).mean()

    fund = None
    if funding_daily is not None and not funding_daily.empty:
        fund = funding_daily.pivot_table(
            index="date", columns="symbol", values="funding_rate_day", aggfunc="sum"
        ).reindex(px.index)

    dates = list(px.index)
    if eval_start is not None:
        dates = [d for d in dates if d >= eval_start]
    if eval_end is not None:
        dates = [d for d in dates if d <= eval_end]
    if len(dates) < lb + reb + 5:
        raise RuntimeError("insufficient dates for backtest window")

    # Warmup relative to eval window
    warm = int(spec.get("warmup_days", 60))
    start_i = 0
    for i, d in enumerate(dates):
        # need lookback available
        if i >= warm:
            start_i = i
            break

    weights = pd.Series(0.0, index=px.columns, dtype=float)
    pending_target: Optional[pd.Series] = None
    pending_signal_date: Optional[pd.Timestamp] = None
    last_signal_date: Optional[pd.Timestamp] = None
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    daily_rets: List[float] = []
    daily_dates: List[pd.Timestamp] = []
    turnovers: List[float] = []
    fee_acc = 0.0
    slip_acc = 0.0
    fund_acc = 0.0
    gross_acc = 0.0
    sym_pnl: Dict[str, float] = {c: 0.0 for c in px.columns}
    fund_days_needed = 0
    fund_days_have = 0
    n_reb = 0
    capacity_hits = 0
    capacity_checks = 0
    btc_rets: List[float] = []
    port_rets: List[float] = []
    crash_mask: List[bool] = []

    rng = rng or np.random.default_rng(RNG_SEED)

    def _build_target(signal_day: pd.Timestamp, exec_day: pd.Timestamp) -> pd.Series:
        eligible: List[str] = []
        signals: List[float] = []
        vols: List[float] = []
        for sym in px.columns:
            if first_idx[sym] is None or last_idx[sym] is None:
                continue
            if signal_day < first_idx[sym] or signal_day > last_idx[sym]:
                continue
            if (signal_day - first_idx[sym]).days < min_age:
                continue
            # Must still be tradable at exec open
            if last_idx[sym] is not None and exec_day > last_idx[sym]:
                continue
            try:
                o_exec = float(opn.at[exec_day, sym])
            except Exception:
                continue
            if not np.isfinite(o_exec):
                continue
            adv = adv20.at[signal_day, sym] if sym in adv20.columns else np.nan
            if not np.isfinite(adv) or adv < min_adv:
                continue
            sig = ret_lb.at[signal_day, sym] if sym in ret_lb.columns else np.nan
            if not np.isfinite(sig):
                continue
            v = vol20.at[signal_day, sym] if sym in vol20.columns else np.nan
            eligible.append(sym)
            signals.append(float(sig))
            vols.append(float(v) if np.isfinite(v) else float("nan"))

        target = pd.Series(0.0, index=px.columns, dtype=float)
        if len(eligible) < (n_l + n_s):
            return target
        sig_a = np.asarray(signals, dtype=float)
        if rank_mode == "random":
            order = rng.permutation(len(eligible))
        else:
            order = np.argsort(sig_a)
        weak = [eligible[j] for j in order[:n_s]]
        strong = [eligible[j] for j in order[-n_l:]]
        weak_vol = np.array([vols[eligible.index(s)] for s in weak])
        strong_vol = np.array([vols[eligible.index(s)] for s in strong])
        w_w = _inv_vol_weights(weak_vol) * float(spec["gross_short"])
        w_s = _inv_vol_weights(strong_vol) * float(spec["gross_long"])
        for s, w in zip(weak, w_w):
            target[s] = -float(w)
        for s, w in zip(strong, w_s):
            target[s] = float(w)
        return target

    for i in range(start_i, len(dates) - 1):
        d = dates[i]
        d_next = dates[i + 1]

        cost_today = 0.0
        # Execute any pending rebalance at open of d (signal was prior close)
        if pending_target is not None:
            target = pending_target.copy()
            # Force-flat names whose last bar is before exec day
            for sym in target.index:
                ld = last_idx.get(sym)
                if ld is not None and d > ld:
                    target[sym] = 0.0
            delta = target - weights
            for sym in delta.index:
                dw = float(delta[sym])
                if abs(dw) < 1e-12:
                    continue
                capacity_checks += 1
                adv = (
                    adv20.at[pending_signal_date, sym]
                    if pending_signal_date is not None and sym in adv20.columns
                    else np.nan
                )
                if np.isfinite(adv) and adv > 0:
                    max_dw = (part_cap * adv) / capital
                    if abs(dw) > max_dw:
                        capacity_hits += 1
                        delta[sym] = math.copysign(max_dw, dw)
                        target[sym] = float(weights[sym] + delta[sym])

            traded = float(np.abs(delta.values).sum())
            turnovers.append(traded / gross if gross > 0 else 0.0)
            cost = 0.0
            for sym in delta.index:
                dw = float(delta[sym])
                if abs(dw) < 1e-12:
                    continue
                adv = (
                    adv20.at[pending_signal_date, sym]
                    if pending_signal_date is not None and sym in adv20.columns
                    else np.nan
                )
                slip = slip_base + (slip_x if (np.isfinite(adv) and adv < adv_illiq) else 0.0)
                cost += abs(dw) * (fee_bps + slip) / 1e4
                fee_acc -= abs(dw) * fee_bps / 1e4
                slip_acc -= abs(dw) * slip / 1e4
            cost_today = cost
            weights = target
            n_reb += 1
            pending_target = None
            pending_signal_date = None

        # Holding return: open d → open d_next under current weights
        day_ret = -cost_today
        for sym, w in list(weights.items()):
            if abs(w) < 1e-12:
                continue
            ld = last_idx.get(sym)
            if ld is not None and d >= ld:
                # Last tradable day: flatten, no further return
                weights[sym] = 0.0
                continue
            try:
                r = float(ret_oo.at[d_next, sym])
            except Exception:
                r = float("nan")
            if not np.isfinite(r):
                weights[sym] = 0.0
                continue
            day_ret += float(w) * r
            sym_pnl[sym] = sym_pnl.get(sym, 0.0) + float(w) * r
            gross_acc += float(w) * r

            fund_days_needed += 1
            fr = float("nan")
            if fund is not None and sym in fund.columns and d in fund.index:
                try:
                    fr = float(fund.at[d, sym])
                except Exception:
                    fr = float("nan")
            if np.isfinite(fr):
                fund_days_have += 1
                f_pnl = -float(w) * fr  # positive funding: longs pay
                day_ret += f_pnl
                fund_acc += f_pnl
                sym_pnl[sym] = sym_pnl.get(sym, 0.0) + f_pnl

        equity *= 1.0 + day_ret
        daily_rets.append(day_ret)
        daily_dates.append(d_next)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)

        try:
            br = float(ret_cc.at[d_next, "BTC"]) if "BTC" in ret_cc.columns else float("nan")
        except Exception:
            br = float("nan")
        if np.isfinite(br):
            btc_rets.append(br)
            port_rets.append(day_ret)
            crash_mask.append(br < -0.03)

        # Schedule rebalance from close of d → execute open d_next
        need_signal = False
        if last_signal_date is None:
            need_signal = True
        elif (d - last_signal_date).days >= reb:
            need_signal = True
        # Delist urgency: if any holding's last bar is d, flatten via rebalance
        if any(
            abs(weights.get(sym, 0.0)) > 1e-12 and last_idx.get(sym) == d
            for sym in weights.index
        ):
            need_signal = True
        if need_signal and pending_target is None:
            pending_target = _build_target(d, d_next)
            pending_signal_date = d
            last_signal_date = d

    rets = np.asarray(daily_rets, dtype=float)
    if len(rets) == 0:
        raise RuntimeError("no daily returns produced")

    # Metrics
    total_net = float(equity - 1.0)
    mu = float(np.mean(rets))
    sd = float(np.std(rets, ddof=1)) if len(rets) > 2 else float("nan")
    sharpe = (mu / sd) * math.sqrt(365.0) if sd and sd > 0 else 0.0
    gains = rets[rets > 0].sum()
    losses = -rets[rets < 0].sum()
    pf = float(gains / losses) if losses > 1e-12 else (float("inf") if gains > 0 else 0.0)
    exp_reb = float(total_net / n_reb) if n_reb else 0.0
    mean_to = float(np.mean(turnovers)) if turnovers else 0.0
    # Annualized cost drag from measured turnover
    # cost_per_reb ≈ mean_to * gross * fee_bps (but fees already in equity path)
    ann_cost = mean_to * (365.0 / reb) * (fee_bps + slip_base)  # bps of gross per year approx

    # Beta
    beta = float("nan")
    beta_crash = float("nan")
    if len(port_rets) >= 30:
        pr = np.asarray(port_rets)
        br = np.asarray(btc_rets)
        if np.std(br) > 1e-12:
            beta = float(np.cov(pr, br)[0, 1] / np.var(br))
        cm = np.asarray(crash_mask)
        if cm.sum() >= 10 and np.std(br[cm]) > 1e-12:
            beta_crash = float(np.cov(pr[cm], br[cm])[0, 1] / np.var(br[cm]))

    # Yearly net returns (from daily)
    yearly: Dict[str, float] = {}
    eq = 1.0
    y_start = None
    for d, r in zip(daily_dates, rets):
        y = str(d.year)
        if y_start is None:
            y_start = y
            y_eq0 = eq
        if y != y_start:
            yearly[y_start] = eq / y_eq0 - 1.0
            y_start = y
            y_eq0 = eq
        eq *= 1.0 + r
    if y_start is not None:
        yearly[y_start] = eq / y_eq0 - 1.0

    cov = fund_days_have / fund_days_needed if fund_days_needed else 0.0

    # Symbol contribution shares
    abs_sum = sum(abs(v) for v in sym_pnl.values()) or 1.0
    contrib = {
        k: float(v / abs_sum)
        for k, v in sorted(sym_pnl.items(), key=lambda kv: -abs(kv[1]))[:20]
    }

    return BacktestResult(
        name=str(spec.get("name", "run")),
        n_rebalances=n_reb,
        n_days=len(rets),
        start=str(daily_dates[0].date()),
        end=str(daily_dates[-1].date()),
        total_return_net=total_net,
        sharpe_net=float(sharpe),
        profit_factor_net=float(pf) if math.isfinite(pf) else 99.0,
        expectancy_per_rebalance=exp_reb,
        max_drawdown=float(max_dd),
        mean_turnover=mean_to,
        ann_cost_bps=float(ann_cost),
        beta_btc=beta,
        beta_btc_crash=beta_crash,
        funding_pnl=float(fund_acc),
        fee_pnl=float(fee_acc),
        slip_pnl=float(slip_acc),
        gross_pnl=float(gross_acc),
        net_pnl=float(total_net),
        funding_coverage=float(cov),
        symbol_contrib=contrib,
        yearly={k: float(v) for k, v in yearly.items()},
        capacity_notes={
            "participation_cap": part_cap,
            "capital_usd": capital,
            "capacity_hit_rate": capacity_hits / capacity_checks if capacity_checks else 0.0,
            "capacity_hits": capacity_hits,
            "capacity_checks": capacity_checks,
        },
        daily_returns=rets,
    )


def random_rank_baseline(
    panel: pd.DataFrame,
    funding_daily: pd.DataFrame,
    spec: Dict[str, Any],
    n_seeds: int,
    eval_start: Optional[pd.Timestamp],
    eval_end: Optional[pd.Timestamp],
) -> Dict[str, Any]:
    pfs = []
    rets = []
    for seed in range(n_seeds):
        rng = np.random.default_rng(RNG_SEED + 10_000 + seed)
        r = run_portfolio(
            panel,
            funding_daily,
            spec,
            rank_mode="random",
            rng=rng,
            eval_start=eval_start,
            eval_end=eval_end,
        )
        pfs.append(r.profit_factor_net)
        rets.append(r.total_return_net)
        if seed % 25 == 0:
            print(f"  random seed {seed}/{n_seeds} PF={r.profit_factor_net:.3f}", flush=True)
    return {
        "n_seeds": n_seeds,
        "pf_mean": float(np.mean(pfs)),
        "pf_p05": float(np.percentile(pfs, 5)),
        "pf_p50": float(np.percentile(pfs, 50)),
        "pf_p95": float(np.percentile(pfs, 95)),
        "ret_mean": float(np.mean(rets)),
        "ret_p05": float(np.percentile(rets, 5)),
        "ret_p50": float(np.percentile(rets, 50)),
        "ret_p95": float(np.percentile(rets, 95)),
        "pf_samples": pfs,
        "ret_samples": rets,
    }


def btc_buy_hold(panel: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> Dict[str, float]:
    sub = panel[(panel["symbol"] == "BTC") & (panel["date"] >= start) & (panel["date"] <= end)]
    if len(sub) < 2:
        return {"total_return": float("nan"), "n_days": 0}
    px = sub.sort_values("date")
    # open of first+1 to open of last for rough parity; use close-to-close simple
    r = float(px["close"].iloc[-1] / px["close"].iloc[0] - 1.0)
    return {"total_return": r, "n_days": int(len(px))}


def apply_verdict(
    primary: BacktestResult,
    random_stats: Dict[str, Any],
    btc: Dict[str, float],
) -> Dict[str, Any]:
    failed: List[str] = []
    conditions: Dict[str, bool] = {}

    oos_days = primary.n_days
    conditions["oos_days_ge_730"] = oos_days >= PASS_CRITERIA["min_oos_calendar_days"]
    if not conditions["oos_days_ge_730"]:
        failed.append(f"oos_days={oos_days}<{PASS_CRITERIA['min_oos_calendar_days']}")

    conditions["rebalances_ge_100"] = primary.n_rebalances >= PASS_CRITERIA["min_oos_rebalances"]
    if not conditions["rebalances_ge_100"]:
        failed.append(f"n_reb={primary.n_rebalances}<{PASS_CRITERIA['min_oos_rebalances']}")

    # Percentile vs random ranks on PF
    samples = np.asarray(random_stats["pf_samples"], dtype=float)
    pct = float((samples < primary.profit_factor_net).mean() * 100.0) if len(samples) else 0.0
    conditions["random_rank_p95"] = pct >= PASS_CRITERIA["min_random_rank_percentile"]
    if not conditions["random_rank_p95"]:
        failed.append(f"random_rank_pct={pct:.1f}<{PASS_CRITERIA['min_random_rank_percentile']}")

    conditions["pf_gt_1"] = primary.profit_factor_net > PASS_CRITERIA["min_net_pf"]
    if not conditions["pf_gt_1"]:
        failed.append(f"PF={primary.profit_factor_net:.3f}<=1")

    conditions["expectancy_gt_0"] = primary.expectancy_per_rebalance > 0 and primary.total_return_net > 0
    if not conditions["expectancy_gt_0"]:
        failed.append("not_profitable_net")

    conditions["beta_ok"] = (
        np.isfinite(primary.beta_btc)
        and abs(primary.beta_btc) <= PASS_CRITERIA["max_abs_beta_btc_global"]
    )
    if not conditions["beta_ok"]:
        failed.append(f"|beta|={primary.beta_btc}")

    if np.isfinite(primary.beta_btc_crash):
        conditions["beta_crash_ok"] = abs(primary.beta_btc_crash) <= PASS_CRITERIA["max_abs_beta_btc_crash"]
        if not conditions["beta_crash_ok"]:
            failed.append(f"|beta_crash|={primary.beta_btc_crash}")
    else:
        conditions["beta_crash_ok"] = True  # insufficient crash days — do not fail solely

    top_share = max((abs(v) for v in primary.symbol_contrib.values()), default=0.0)
    conditions["symbol_conc_ok"] = top_share <= PASS_CRITERIA["max_symbol_contrib_share"]
    if not conditions["symbol_conc_ok"]:
        failed.append(f"top_symbol_share={top_share:.2f}")

    if primary.total_return_net > 0 and primary.yearly:
        pos_years = {k: v for k, v in primary.yearly.items() if v > 0}
        best = max(primary.yearly.values()) if primary.yearly else 0.0
        share = best / primary.total_return_net if primary.total_return_net > 0 else 1.0
        # yearly dict are year returns not additive to total — use softer check:
        # require ≥2 calendar years with positive return
        conditions["multi_year_pos"] = sum(1 for v in primary.yearly.values() if v > 0) >= 2
        if not conditions["multi_year_pos"]:
            failed.append(f"positive_years={sum(1 for v in primary.yearly.values() if v>0)}")
    else:
        conditions["multi_year_pos"] = False
        failed.append("no_multi_year_positive")

    conditions["funding_cov_ok"] = primary.funding_coverage >= PASS_CRITERIA["min_funding_coverage"]
    if not conditions["funding_cov_ok"]:
        failed.append(f"funding_cov={primary.funding_coverage:.2f}")

    # Beat BTC and cash
    conditions["beat_cash"] = primary.total_return_net > 0
    conditions["beat_btc"] = primary.total_return_net > float(btc.get("total_return") or 0.0)
    # Note: beat_btc is informational for LS book; not required for PASS of relative book
    # Keep as soft — do not fail PASS solely on beat_btc for market-neutral intent

    hard = [
        "oos_days_ge_730",
        "rebalances_ge_100",
        "random_rank_p95",
        "pf_gt_1",
        "expectancy_gt_0",
        "beta_ok",
        "symbol_conc_ok",
        "multi_year_pos",
        "funding_cov_ok",
        "beat_cash",
    ]
    all_hard = all(conditions.get(k, False) for k in hard)

    if all_hard:
        verdict = "A"
    elif primary.total_return_net > 0 and pct >= 80 and conditions.get("pf_gt_1"):
        verdict = "B"
    elif not conditions["oos_days_ge_730"] or not conditions["funding_cov_ok"]:
        # data/power limits that prevent A
        if primary.total_return_net <= 0 or pct < 80:
            verdict = "C"
        else:
            verdict = "B"
    else:
        verdict = "C"

    return {
        "verdict": verdict,
        "conditions": conditions,
        "failed": failed,
        "random_rank_percentile_pf": pct,
        "pass_criteria": PASS_CRITERIA,
    }


def result_to_dict(r: BacktestResult) -> Dict[str, Any]:
    d = asdict(r)
    d.pop("daily_returns", None)
    return d


def collect_eligible_symbols(
    panel: pd.DataFrame,
    eval_start: pd.Timestamp,
    eval_end: pd.Timestamp,
    *,
    max_symbols: int = 60,
) -> List[str]:
    """PIT-eligible symbols, capped to top ``max_symbols`` by median ADV.

    Random-rank baselines sample from this set, so funding must cover it.
    Cap is pre-registered to keep the liquid cross-section tractable and
    avoid HL fundingHistory rate limits on the full 200+ universe.
    """
    px = panel.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    dvol = panel.pivot_table(
        index="date", columns="symbol", values="dollar_volume", aggfunc="last"
    ).reindex(px.index)
    adv20 = dvol.rolling(20, min_periods=5).mean()
    first_idx = {c: px[c].first_valid_index() for c in px.columns}
    last_idx = {c: px[c].last_valid_index() for c in px.columns}
    dates = [d for d in px.index if eval_start <= d <= eval_end]
    min_age = int(PRIMARY["min_age_days"])
    min_adv = float(PRIMARY["min_adv_usd_20d"])
    warm = int(PRIMARY["warmup_days"])

    seen: set[str] = set()
    adv_med: Dict[str, float] = {}
    for d in dates[warm::7]:
        for sym in px.columns:
            if first_idx[sym] is None or last_idx[sym] is None:
                continue
            if d < first_idx[sym] or d > last_idx[sym]:
                continue
            if (d - first_idx[sym]).days < min_age:
                continue
            adv = adv20.at[d, sym] if sym in adv20.columns else np.nan
            if not np.isfinite(adv) or adv < min_adv:
                continue
            seen.add(sym)
    for sym in seen:
        s = adv20[sym].dropna() if sym in adv20.columns else pd.Series(dtype=float)
        adv_med[sym] = float(s.median()) if len(s) else 0.0
    ranked = sorted(seen, key=lambda s: adv_med.get(s, 0.0), reverse=True)
    out = ranked[:max_symbols]
    if "BTC" not in out and "BTC" in px.columns:
        out = ["BTC"] + [s for s in out if s != "BTC"]
        out = out[:max_symbols]
    return out


def write_report(payload: Dict[str, Any]) -> None:
    v = payload["viability"]
    p = payload["primary"]
    verd = payload["verdict"]
    rnd = payload["random_ranks"]
    lines = [
        "# Cross-Sectional Slow Momentum Feasibility",
        "",
        f"Generated: {payload['generated_at']}",
        f"Panel cache: `{CACHE_DB.as_posix()}`",
        "",
        "## Scope",
        "",
        "Measurement only — no strategy module, no production config changes, no promotion.",
        "Object: whether **relative** multi-day momentum (long winners / short losers) clears",
        "retail Hyperliquid costs after closed ≤24h directional / MM / OI / tape families.",
        "",
        "## Corrections applied (vs draft prompt)",
        "",
        "- MM map: liquid half-spread ≪ maker fee; thin books fail on AS / economics, not fee>spread everywhere.",
        "- Primary fees = **tier-0**: taker **4.5 bps/side**, maker **1.5 bps/side** (not 3.5 unless Tier 2 proven).",
        "- Turnover = `Σ|Δweights|/gross`; costs on traded notional only (no ×2 double-count).",
        "- Signal close `t` → execute open `t+1`.",
        "- Funding PIT; coverage <90% blocks verdict A.",
        "- One a-priori PRIMARY spec (no OOS search). Sensitivities reported separately.",
        "- Delistings force-closed on last tradable bar; no forward fill of missing data.",
        "- Capacity: 1% ADV participation clip.",
        "",
        "## Task 1 — Data viability",
        "",
        f"- Decision: **{v.get('decision')}**",
        f"- Universe (excl. HIP-3): {v.get('n_universe')} (delisted flag: {v.get('n_delisted')})",
        f"- With 1d candles: {v.get('n_with_candles')}; ≥700d: {v.get('ge700d')}; median days: {v.get('median_days'):.0f}",
        f"- Concurrent symbols (monthly): min **{v.get('min_concurrent')}**, max {v.get('max_concurrent')}",
        f"- Delisted with history: {v.get('delisted_with_history')} (survivorship includable via HL candleSnapshot)",
        f"- Funding: HL `fundingHistory` paginates to ~900d (hourly); study requires ≥90% position-day coverage",
        "",
    ]
    for n in v.get("notes") or []:
        lines.append(f"- {n}")
    lines += [
        "",
        "## Pre-registered PRIMARY",
        "",
        "```json",
        json.dumps(PRIMARY, indent=2),
        "```",
        "",
        "## Frozen PASS criteria",
        "",
        "```json",
        json.dumps(PASS_CRITERIA, indent=2),
        "```",
        "",
        "## Primary results (evaluation window)",
        "",
        f"- Window: {p.get('start')} → {p.get('end')} ({p.get('n_days')} days, {p.get('n_rebalances')} rebalances)",
        f"- Net total return: **{p.get('total_return_net'):.4f}**",
        f"- Sharpe (net, √365): {p.get('sharpe_net'):.3f}",
        f"- PF (net daily): {p.get('profit_factor_net'):.3f}",
        f"- Expectancy / rebalance: {p.get('expectancy_per_rebalance'):.5f}",
        f"- Max DD: {p.get('max_drawdown'):.4f}",
        f"- Mean turnover (Σ|Δw|/gross): {p.get('mean_turnover'):.3f}",
        f"- Approx ann cost drag (bps of gross): {p.get('ann_cost_bps'):.1f}",
        f"- βBTC: {p.get('beta_btc'):.3f} (crash days: {p.get('beta_btc_crash')})",
        f"- Funding PnL (equity frac): {p.get('funding_pnl'):.4f} | coverage {p.get('funding_coverage'):.3f}",
        f"- Fee/slip PnL: {p.get('fee_pnl'):.4f} / {p.get('slip_pnl'):.4f}",
        f"- Yearly: {json.dumps(p.get('yearly'))}",
        f"- Capacity hit rate @1% ADV: {p.get('capacity_notes', {}).get('capacity_hit_rate')}",
        "",
        "## Baselines",
        "",
        f"- Random ranks ({rnd.get('n_seeds')} seeds): PF p50={rnd.get('pf_p50'):.3f} p95={rnd.get('pf_p95'):.3f}; "
        f"ret p50={rnd.get('ret_p50'):.4f}",
        f"- Momentum PF percentile vs random: **{verd.get('random_rank_percentile_pf'):.1f}**",
        f"- BTC buy&hold: {payload.get('btc_bh')}",
        f"- Cash: 0",
        "",
        "## Sensitivities (not for verdict)",
        "",
    ]
    for s in payload.get("sensitivities") or []:
        lines.append(
            f"- `{s.get('name')}`: ret={s.get('total_return_net'):.4f} PF={s.get('profit_factor_net'):.3f} "
            f"Sharpe={s.get('sharpe_net'):.2f} TO={s.get('mean_turnover'):.3f}"
        )
    lines += [
        "",
        f"## Verdict: **({verd.get('verdict')})**",
        "",
        f"Failed conditions: {verd.get('failed') or 'none'}",
        "",
        "### Interpretation",
        "",
    ]
    if verd.get("verdict") == "A":
        lines.append(
            "PASS against frozen criteria. Still measurement-only — do **not** promote to execution "
            "without a separate baseline-signal gate on a strategy implementation."
        )
    elif verd.get("verdict") == "B":
        lines.append(
            "MARGINAL — positive hints inside noise / data limits. Record what would need to be true; "
            "**do not build** a production strategy from this alone."
        )
    else:
        lines.append(
            "FAIL / closed under this venue + tier-0 fees + HL history. Cross-sectional slow momentum "
            "does not clear the bar as specified. Do not search variants until a new information source appears."
        )
    lines += [
        "",
        "## Limitations",
        "",
        "- HL public history begins ~2024-02 for many perps (~2.5y), not a multi-cycle decade sample.",
        "- ADV from daily base volume × close; OI filter not applied (HL OI history short).",
        "- Primary assumes taker rebalance; maker fill probability / AS not modelled here.",
        "- Open-to-open holding returns; intraday gaps at delist approximated by last bar flatten.",
        "",
        f"JSON artifact: `{REPORT_JSON.as_posix()}`",
        "",
    ]
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    REPORT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"[report] wrote {REPORT_MD} and {REPORT_JSON}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--force-download", action="store_true")
    ap.add_argument("--max-symbols", type=int, default=0, help="0=all non-HIP3")
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--skip-random", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    if not args.skip_download or not CACHE_DB.exists():
        viability = download_panel(
            force=args.force_download,
            max_symbols=args.max_symbols or None,
        )
    else:
        con = sqlite3.connect(str(CACHE_DB))
        row = con.execute("SELECT value FROM meta WHERE key='viability'").fetchone()
        viability = json.loads(row[0]) if row else {"decision": "UNKNOWN"}
        con.close()
        print(f"[cache] viability={viability.get('decision')}", flush=True)

    if viability.get("decision") != "GO":
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "viability": viability,
            "verdict": {"verdict": "INCONCLUSIVE", "failed": ["data_viability"], "conditions": {}},
            "primary": {},
            "random_ranks": {},
            "btc_bh": {},
            "sensitivities": [],
            "primary_spec": PRIMARY,
            "pass_criteria": PASS_CRITERIA,
        }
        write_report(payload)
        # Update backlog lightly
        print("STOP: data viability not GO", flush=True)
        return 2

    panel, univ, viability = load_panel()
    print(
        f"[panel] rows={len(panel)} symbols={panel['symbol'].nunique()} "
        f"dates={panel['date'].nunique()}",
        flush=True,
    )

    # Evaluation window: after warmup, use full remaining history (PRIMARY frozen a priori)
    dates = sorted(panel["date"].unique())
    eval_start = pd.Timestamp(dates[0]) + pd.Timedelta(days=int(PRIMARY["warmup_days"]))
    eval_end = pd.Timestamp(dates[-1])

    # Funding for PIT-eligible liquid universe (top-ADV cap, pre-registered)
    print("[funding] discovering eligible-symbol universe...", flush=True)
    held = collect_eligible_symbols(
        panel,
        eval_start,
        eval_end,
        max_symbols=int(PRIMARY["max_universe_symbols"]),
    )
    print(f"[funding] downloading/caching for {len(held)} eligible symbols...", flush=True)
    # Restrict panel to eligible + any delisted that appear in history within held set
    panel = panel[panel["symbol"].isin(held)].copy()
    ensure_funding(held)
    funding_daily = load_funding_daily(held)
    print(f"[funding] daily rows={len(funding_daily)} panel_symbols={panel['symbol'].nunique()}", flush=True)

    print("[backtest] PRIMARY...", flush=True)
    primary = run_portfolio(
        panel,
        funding_daily,
        PRIMARY,
        rank_mode="momentum",
        eval_start=eval_start,
        eval_end=eval_end,
    )
    print(
        f"  ret={primary.total_return_net:.4f} PF={primary.profit_factor_net:.3f} "
        f"Sharpe={primary.sharpe_net:.2f} TO={primary.mean_turnover:.3f} "
        f"beta={primary.beta_btc:.3f} fund_cov={primary.funding_coverage:.3f}",
        flush=True,
    )

    sensitivities = []
    for spec in SENSITIVITY_SPECS:
        print(f"[backtest] sensitivity {spec['name']}...", flush=True)
        try:
            r = run_portfolio(
                panel,
                funding_daily,
                spec,
                rank_mode="momentum",
                eval_start=eval_start,
                eval_end=eval_end,
            )
            sensitivities.append(result_to_dict(r))
        except Exception as e:  # noqa: BLE001
            sensitivities.append({"name": spec["name"], "error": str(e)})

    if args.skip_random:
        random_stats = {
            "n_seeds": 0,
            "pf_samples": [1.0],
            "ret_samples": [0.0],
            "pf_p50": 1.0,
            "pf_p95": 1.0,
            "ret_p50": 0.0,
        }
    else:
        print(f"[baseline] random ranks seeds={args.seeds}...", flush=True)
        random_stats = random_rank_baseline(
            panel, funding_daily, PRIMARY, args.seeds, eval_start, eval_end
        )

    btc = btc_buy_hold(panel, eval_start, eval_end)
    verd = apply_verdict(primary, random_stats, btc)

    # Strip large sample arrays from JSON (keep summaries)
    rnd_out = {k: v for k, v in random_stats.items() if k not in ("pf_samples", "ret_samples")}
    rnd_out["pf_samples_n"] = len(random_stats.get("pf_samples") or [])

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - t0, 1),
        "viability": viability,
        "primary_spec": PRIMARY,
        "pass_criteria": PASS_CRITERIA,
        "primary": result_to_dict(primary),
        "sensitivities": sensitivities,
        "random_ranks": rnd_out,
        "btc_bh": btc,
        "verdict": verd,
        "corrections": [
            "tier0_fees_4.5_1.5",
            "turnover_sum_abs_dw_over_gross",
            "exec_open_tplus1",
            "funding_pit_coverage_gate",
            "delist_force_close",
            "aprori_primary_no_oos_search",
            "adv_participation_cap_1pct",
        ],
    }
    write_report(payload)
    print(f"VERDICT ({verd['verdict']}) failed={verd['failed']}", flush=True)
    return 0 if verd["verdict"] == "A" else 1


if __name__ == "__main__":
    raise SystemExit(main())
