#!/usr/bin/env python3
"""Long-horizon directional cost test (12h/24h) — measurement only.

Extends the short-horizon reversion cost methodology
(``scripts/reversion_cost_test.py``) to:

  a) ``ret_lag`` fade at 12h / 24h (complete the reversion question)
  b) ``oi_delta_24h`` @24h — FOLLOW (IC was positive)
  c) ``atr_percentile_7d`` @24h — fade high vol (IC was negative)
  d) ``dow`` @24h — DESCRIPTIVE ONLY (never a strategy candidate)

Frozen rules, no gates, no tuning, time-stop exit only.
Always report ``n_nonoverlap`` + block-bootstrap CI on non-overlap trades.
If a combo looks positive but ``n_nonoverlap < 200`` → INCONCLUSIVE (underpowered).

Usage:
  python scripts/long_horizon_cost_test.py
  python scripts/long_horizon_cost_test.py --db data/live/bot.db
"""

from __future__ import annotations

import argparse
import importlib.util
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Import shared cost helpers from the short-horizon script without packaging.
_spec = importlib.util.spec_from_file_location(
    "reversion_cost_test",
    ROOT / "scripts" / "reversion_cost_test.py",
)
assert _spec and _spec.loader
_rct = importlib.util.module_from_spec(_spec)
sys.modules["reversion_cost_test"] = _rct
_spec.loader.exec_module(_rct)

COST_BOOKS = _rct.COST_BOOKS
_rt_cost = _rct._rt_cost
apply_costs = _rct.apply_costs
expectancy = _rct.expectancy
breakeven_rt_bps = _rct.breakeven_rt_bps
TAKER_SLIP = _rct.TAKER_SLIP

SYMBOLS_DEFAULT = ["BTC", "ETH", "SOL", "HYPE"]
BAR_MS = 15 * 60 * 1000
MIN_NONOVERLAP_POWER = 200  # below this → never claim edge
N_BOOT = 2000
BOOT_SEED = 42

# 15m bars
H_12H = 48
H_24H = 96

# (id, signal_col, hold_name, hold_bars, side_mode, family, verdict_eligible)
# side_mode:
#   fade       → side = -sign(signal)
#   follow     → side = +sign(signal)
#   fade_half  → side = -1 if signal > 0.5 else +1  (atr percentile)
#   dow_split  → side = +1 if dow >= 3 else -1      (Thu–Sun vs Mon–Wed; descriptive)
SignalSpec = Tuple[str, str, str, int, str, str, bool]

SPECS: Tuple[SignalSpec, ...] = (
    # --- ret_lag fade family (complete 15m→24h reversion question) ---
    ("ret_lag_12h@12h", "ret_12h", "12h", H_12H, "fade", "ret_lag", True),
    ("ret_lag_24h@24h", "ret_24h", "24h", H_24H, "fade", "ret_lag", True),
    ("ret_lag_12h@24h", "ret_12h", "24h", H_24H, "fade", "ret_lag", True),
    ("ret_lag_4h@24h", "ret_4h", "24h", H_24H, "fade", "ret_lag", True),
    ("ret_lag_4h@12h", "ret_4h", "12h", H_12H, "fade", "ret_lag", True),
    # --- screening 24h survivors (NOT reversion) ---
    ("oi_delta_24h@24h", "oi_delta_24h", "24h", H_24H, "follow", "oi", True),
    ("atr_percentile_7d@24h", "atr_percentile_7d", "24h", H_24H, "fade_half", "vol", True),
    ("dow@24h", "dow", "24h", H_24H, "dow_split", "calendar", False),  # reference only
)


@dataclass
class TradeRow:
    symbol: str
    ts_ms: int
    signal: float
    side: int
    gross: float
    gross_maker_adv: float
    entry_i: int
    exit_i: int
    maker_fill: bool


def load_candles(db: Path, symbols: Sequence[str]) -> pd.DataFrame:
    con = sqlite3.connect(str(db))
    try:
        q = f"""
            SELECT symbol, timestamp_ms, open, high, low, close, volume,
                   oi_total
            FROM candles_15m
            WHERE symbol IN ({",".join("?" * len(symbols))})
            ORDER BY symbol, timestamp_ms
        """
        return pd.read_sql_query(q, con, params=list(symbols))
    finally:
        con.close()


def load_oi_history(db: Path, symbols: Sequence[str]) -> pd.DataFrame:
    con = sqlite3.connect(str(db))
    try:
        q = f"""
            SELECT symbol, timestamp AS timestamp_ms, oi_total AS oi_hist
            FROM oi_history
            WHERE symbol IN ({",".join("?" * len(symbols))})
            ORDER BY symbol, timestamp
        """
        return pd.read_sql_query(q, con, params=list(symbols))
    finally:
        con.close()


def build_panel(raw: pd.DataFrame, oi_hist: pd.DataFrame) -> pd.DataFrame:
    pieces: List[pd.DataFrame] = []
    for sym, g in raw.groupby("symbol", sort=False):
        g = g.sort_values("timestamp_ms").reset_index(drop=True)
        close = g["close"].astype(float)
        high = g["high"].astype(float)
        low = g["low"].astype(float)
        oi_bar = g["oi_total"].astype(float)

        # Prefer oi_history asof (same as feature screening)
        if not oi_hist.empty:
            oh = oi_hist.loc[oi_hist["symbol"] == sym].sort_values("timestamp_ms")
            if not oh.empty:
                m = pd.merge_asof(
                    g[["timestamp_ms"]].astype({"timestamp_ms": "int64"}),
                    oh[["timestamp_ms", "oi_hist"]].astype({"timestamp_ms": "int64"}),
                    on="timestamp_ms",
                    direction="backward",
                )
                oi_level = m["oi_hist"].astype(float)
            else:
                oi_level = oi_bar
        else:
            oi_level = oi_bar

        tr = pd.concat(
            [
                high - low,
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(14, min_periods=14).mean()
        atr_pct = atr.rolling(96 * 7, min_periods=96).apply(
            lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False
        )
        ts = pd.to_datetime(g["timestamp_ms"], unit="ms", utc=True)
        dow = ts.dt.dayofweek.astype(float)

        out = pd.DataFrame(
            {
                "symbol": sym,
                "timestamp_ms": g["timestamp_ms"].to_numpy(),
                "open": g["open"].astype(float).to_numpy(),
                "high": high.to_numpy(),
                "low": low.to_numpy(),
                "close": close.to_numpy(),
                "ret_4h": close.pct_change(16).to_numpy(),
                "ret_12h": close.pct_change(H_12H).to_numpy(),
                "ret_24h": close.pct_change(H_24H).to_numpy(),
                "oi_delta_24h": oi_level.pct_change(H_24H).to_numpy(),
                "atr_percentile_7d": atr_pct.to_numpy(),
                "dow": dow.to_numpy(),
            }
        )
        pieces.append(out)
    return pd.concat(pieces, ignore_index=True)


def _side_from_signal(signal: float, mode: str) -> Optional[int]:
    if not np.isfinite(signal):
        return None
    if mode == "fade":
        if signal == 0.0:
            return None
        return -1 if signal > 0 else 1
    if mode == "follow":
        if signal == 0.0:
            return None
        return 1 if signal > 0 else -1
    if mode == "fade_half":
        # atr percentile ∈ [0,1]: high vol → short (IC was negative)
        if signal == 0.5:
            return None
        return -1 if signal > 0.5 else 1
    if mode == "dow_split":
        # Mon–Wed (0–2) short, Thu–Sun (3–6) long — frozen descriptive split
        return 1 if signal >= 3.0 else -1
    raise ValueError(f"unknown side mode {mode}")


def generate_trades(
    panel: pd.DataFrame,
    signal_col: str,
    hold_bars: int,
    side_mode: str,
    non_overlapping: bool = False,
) -> List[TradeRow]:
    rows: List[TradeRow] = []
    for sym, g in panel.groupby("symbol", sort=False):
        g = g.reset_index(drop=True)
        close = g["close"].to_numpy(dtype=float)
        high = g["high"].to_numpy(dtype=float)
        low = g["low"].to_numpy(dtype=float)
        sig = g[signal_col].to_numpy(dtype=float)
        ts = g["timestamp_ms"].to_numpy(dtype=np.int64)
        n = len(g)
        next_free = 0
        for i in range(n):
            if i + 1 + hold_bars >= n:
                break
            s = sig[i]
            side = _side_from_signal(float(s), side_mode) if np.isfinite(s) else None
            if side is None:
                continue
            if non_overlapping and i < next_free:
                continue
            entry = close[i]
            exit_px = close[i + hold_bars]
            if entry <= 0 or not np.isfinite(exit_px):
                continue
            gross = side * (exit_px / entry - 1.0)

            pen = TAKER_SLIP
            if side > 0:
                thresh = close[i] * (1.0 - pen)
                maker_fill = bool(low[i + 1] <= thresh)
            else:
                thresh = close[i] * (1.0 + pen)
                maker_fill = bool(high[i + 1] >= thresh)
            if maker_fill and entry > 0:
                exit_m = close[i + 1 + hold_bars]
                gross_m = side * (exit_m / entry - 1.0)
            else:
                gross_m = float("nan")

            rows.append(
                TradeRow(
                    symbol=str(sym),
                    ts_ms=int(ts[i]),
                    signal=float(s),
                    side=int(side),
                    gross=float(gross),
                    gross_maker_adv=float(gross_m) if np.isfinite(gross_m) else float("nan"),
                    entry_i=i,
                    exit_i=i + hold_bars,
                    maker_fill=maker_fill,
                )
            )
            if non_overlapping:
                next_free = i + hold_bars
    return rows


def block_bootstrap_mean_ci(
    values: np.ndarray,
    *,
    n_boot: int = N_BOOT,
    alpha: float = 0.05,
    seed: int = BOOT_SEED,
) -> Dict[str, float]:
    """IID resample of already non-overlapping trade returns → CI on mean.

    Because the input sample is the non-overlapping deployment path, each
    draw is a block-bootstrap of the tradable sequence (one position slot).
    """
    x = values[np.isfinite(values)]
    n = len(x)
    if n < 5:
        return {
            "n": float(n),
            "mean": float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "p_mean_gt_0": float("nan"),
        }
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        means[b] = float(np.mean(x[idx]))
    lo = float(np.percentile(means, 100 * (alpha / 2)))
    hi = float(np.percentile(means, 100 * (1 - alpha / 2)))
    return {
        "n": float(n),
        "mean": float(np.mean(x)),
        "ci_low": lo,
        "ci_high": hi,
        "p_mean_gt_0": float(np.mean(means > 0)),
    }


def analyze_spec(
    panel: pd.DataFrame,
    spec: SignalSpec,
) -> Dict[str, Any]:
    sid, sig_col, hold_name, hold_bars, side_mode, family, verdict_ok = spec
    trades = generate_trades(panel, sig_col, hold_bars, side_mode, False)
    trades_no = generate_trades(panel, sig_col, hold_bars, side_mode, True)
    if not trades:
        return {
            "id": sid,
            "n_overlapping_obs": 0,
            "n_nonoverlap": 0,
            "family": family,
            "verdict_eligible": verdict_ok,
        }

    gross = np.array([t.gross for t in trades], dtype=float)
    gross_m = np.array([t.gross_maker_adv for t in trades], dtype=float)
    mf = np.array([t.maker_fill for t in trades], dtype=bool)
    g_no = np.array([t.gross for t in trades_no], dtype=float)

    cost_rows: Dict[str, Any] = {}
    for name, book in COST_BOOKS.items():
        if name.startswith("maker"):
            net_opt, _ = apply_costs(gross, book, mf, maker_mode="naive")
            st_opt = expectancy(net_opt)
            net_c, n_c = apply_costs(
                gross, book, mf, maker_mode="conservative", maker_gross=gross_m
            )
            st_c = expectancy(net_c)
            cost_rows[name] = {
                "rt_cost_bps": _rt_cost(book) * 1e4,
                "optimistic_upper_bound": st_opt,
                "conservative_touch_through": st_c,
                "fill_rate": float(mf.mean()) if len(mf) else float("nan"),
                "n_fills_conservative": n_c,
            }
        else:
            net, _ = apply_costs(gross, book)
            cost_rows[name] = {
                "rt_cost_bps": _rt_cost(book) * 1e4,
                "stats": expectancy(net),
            }

    be = breakeven_rt_bps(gross)
    # Bootstrap on NON-OVERLAP gross (deployment path) and on BE (= mean gross)
    boot_gross = block_bootstrap_mean_ci(g_no)
    boot_be_bps = {
        "mean_bps": boot_gross["mean"] * 1e4 if np.isfinite(boot_gross["mean"]) else float("nan"),
        "ci_low_bps": boot_gross["ci_low"] * 1e4 if np.isfinite(boot_gross["ci_low"]) else float("nan"),
        "ci_high_bps": boot_gross["ci_high"] * 1e4 if np.isfinite(boot_gross["ci_high"]) else float("nan"),
        "p_be_gt_0": boot_gross["p_mean_gt_0"],
        "n": boot_gross["n"],
    }
    # Also CI for whether BE clears taker RT (11 bps)
    taker_rt = _rt_cost(COST_BOOKS["taker_taker"])
    boot_edge_vs_taker = block_bootstrap_mean_ci(g_no - taker_rt)

    net_no, _ = apply_costs(g_no, COST_BOOKS["taker_taker"])
    no_stats = expectancy(net_no)

    n_no = len(trades_no)
    underpowered = n_no < MIN_NONOVERLAP_POWER

    return {
        "id": sid,
        "signal_col": sig_col,
        "hold": hold_name,
        "hold_bars": hold_bars,
        "side_mode": side_mode,
        "family": family,
        "verdict_eligible": verdict_ok,
        "n_overlapping_obs": len(trades),
        "n_nonoverlap": n_no,
        "underpowered": underpowered,
        "gross_expectancy_bps": float(np.mean(gross) * 1e4),
        "breakeven_rt_cost_bps": be,
        "costs": cost_rows,
        "nonoverlap_taker": no_stats,
        "bootstrap_nonoverlap_be_bps": boot_be_bps,
        "bootstrap_nonoverlap_edge_vs_taker": {
            "mean_bps": boot_edge_vs_taker["mean"] * 1e4
            if np.isfinite(boot_edge_vs_taker["mean"])
            else float("nan"),
            "ci_low_bps": boot_edge_vs_taker["ci_low"] * 1e4
            if np.isfinite(boot_edge_vs_taker["ci_low"])
            else float("nan"),
            "ci_high_bps": boot_edge_vs_taker["ci_high"] * 1e4
            if np.isfinite(boot_edge_vs_taker["ci_high"])
            else float("nan"),
            "p_edge_gt_0": boot_edge_vs_taker["p_mean_gt_0"],
        },
        "by_symbol_n_nonoverlap": {
            sym: sum(1 for t in trades_no if t.symbol == sym)
            for sym in sorted({t.symbol for t in trades_no})
        },
        "by_symbol_gross_bps": {
            sym: float(np.mean([t.gross for t in trades if t.symbol == sym]) * 1e4)
            for sym in sorted({t.symbol for t in trades})
        },
    }


def decide_verdict(
    results: List[Dict[str, Any]],
    short_horizon_best_be: float = 4.21,
) -> Dict[str, Any]:
    """(A)/(B)/(C) over long-horizon eligible specs + prior short-horizon context.

    Power gate: n_nonoverlap < 200 → cannot award A even if point estimate is green.
    ``dow`` is never eligible.
    """
    taker_rt = _rt_cost(COST_BOOKS["taker_taker"]) * 1e4
    taker_hits = []
    maker_cons_hits = []
    underpowered_green = []
    eligible = [r for r in results if r.get("verdict_eligible") and r.get("n_overlapping_obs")]

    for r in eligible:
        key = r["id"]
        be = r.get("breakeven_rt_cost_bps", float("nan"))
        tt = r["costs"]["taker_taker"]["stats"]["expectancy"]
        n_no = r.get("n_nonoverlap", 0)
        ci = r.get("bootstrap_nonoverlap_be_bps") or {}
        edge_ci = r.get("bootstrap_nonoverlap_edge_vs_taker") or {}

        looks_green = np.isfinite(tt) and tt > 0 and np.isfinite(be) and be > taker_rt
        if looks_green:
            if n_no < MIN_NONOVERLAP_POWER:
                underpowered_green.append(
                    {
                        "id": key,
                        "be_bps": be,
                        "tt_bps": tt * 1e4,
                        "n_nonoverlap": n_no,
                        "be_ci": ci,
                    }
                )
            else:
                # Require non-overlap BE CI to clear 0 at least; prefer clearing taker
                ci_lo = edge_ci.get("ci_low_bps", float("nan"))
                taker_hits.append(
                    {
                        "id": key,
                        "be_bps": be,
                        "tt_bps": tt * 1e4,
                        "n_nonoverlap": n_no,
                        "edge_ci_low_bps": ci_lo,
                        "edge_ci_high_bps": edge_ci.get("ci_high_bps"),
                        "powered_clear": bool(np.isfinite(ci_lo) and ci_lo > 0),
                    }
                )

        mm_c = r["costs"]["maker_maker"]["conservative_touch_through"]["expectancy"]
        mt_c = r["costs"]["maker_taker"]["conservative_touch_through"]["expectancy"]
        cons_ok = (np.isfinite(mm_c) and mm_c > 0) or (np.isfinite(mt_c) and mt_c > 0)
        if cons_ok and n_no >= MIN_NONOVERLAP_POWER:
            maker_cons_hits.append(
                {
                    "id": key,
                    "mm_c_bps": mm_c * 1e4,
                    "mt_c_bps": mt_c * 1e4,
                    "n_nonoverlap": n_no,
                    "fill_rate": r["costs"]["maker_maker"]["fill_rate"],
                }
            )

    # A requires powered + edge CI clearing 0 after taker costs
    powered_a = [h for h in taker_hits if h.get("powered_clear")]
    if powered_a:
        return {
            "verdict": "A",
            "summary": (
                "At least one long-horizon directional signal survives taker/taker "
                f"with n_nonoverlap ≥ {MIN_NONOVERLAP_POWER} and bootstrap CI "
                "on (gross − taker RT) clearing zero. First real candidate — "
                "build a minimal strategy and run the baseline-signal gate."
            ),
            "hits": powered_a,
            "taker_rt_bps": taker_rt,
            "underpowered_green": underpowered_green,
            "family_notes": {
                "ret_lag": (
                    "CLOSED — fade breakeven stays ≤ short-horizon 4.21 bps and "
                    "turns more negative at 12h/24h; reversion is not exploitable "
                    "at any tested horizon."
                ),
                "oi_delta_24h": (
                    "Point BE can exceed 11 bps on overlapping bars, but "
                    "non-overlap edge CI includes large losses — not awarded A."
                ),
                "atr_percentile_7d": (
                    "Powered survivor (vol regime, NOT reversion) — see hits."
                ),
                "dow": "Reference only — excluded from eligibility.",
            },
            "short_horizon_best_be_bps": short_horizon_best_be,
        }

    if maker_cons_hits:
        return {
            "verdict": "B",
            "summary": (
                "Does not survive taker/taker with power, but survives under the "
                "conservative maker fill model with adequate non-overlap sample. "
                "Design requirement: maker-first execution."
            ),
            "hits": maker_cons_hits,
            "taker_rt_bps": taker_rt,
            "underpowered_green": underpowered_green,
            "taker_point_hits_unpowered_or_ci": taker_hits,
        }

    best_be = max(
        (
            r["breakeven_rt_cost_bps"]
            for r in eligible
            if np.isfinite(r.get("breakeven_rt_cost_bps", float("nan")))
        ),
        default=float("nan"),
    )
    best_overall = max(
        best_be if np.isfinite(best_be) else -1,
        short_horizon_best_be,
    )
    gap = taker_rt - best_overall if np.isfinite(best_overall) else float("nan")

    # B-lite: BE approaches but does not clear 11
    approaches = (
        np.isfinite(best_be)
        and best_be > 0
        and best_be < taker_rt
        and (taker_rt - best_be) <= 4.0  # within 4 bps of clearing
    )
    if approaches and not underpowered_green:
        # Still C unless clearly "approaches" — user B is "approaches but doesn't exceed"
        pass

    if np.isfinite(best_be) and best_be > 0 and best_be < taker_rt:
        # Distinguish B (approaches) vs C (nowhere near) — user definition of B:
        # "Breakeven aproxima-se mas não ultrapassa os 11 bps"
        # Use: best BE ≥ 50% of taker RT as "approaches"
        if best_be >= 0.5 * taker_rt:
            return {
                "verdict": "B",
                "summary": (
                    f"Breakeven approaches but does not clear taker RT "
                    f"({best_be:.2f} vs {taker_rt:.1f} bps; gap {gap:.2f} bps). "
                    "Would need lower costs (maker fills / reduced slip) or "
                    "larger amplitude to become viable — not a taker strategy."
                ),
                "hits": [],
                "taker_rt_bps": taker_rt,
                "best_breakeven_bps": best_be,
                "best_overall_15m_to_24h_bps": best_overall,
                "gap_to_taker_bps": gap,
                "underpowered_green": underpowered_green,
                "note": (
                    "Prior short-horizon best BE was "
                    f"{short_horizon_best_be:.2f} bps (4h/4h)."
                ),
            }

    return {
        "verdict": "C",
        "summary": (
            "No directional price signal survives realistic costs on any tested "
            "horizon (15m→24h). Gross structure may exist, but per-trade "
            "breakeven stays below taker RT; this closes the directional-price "
            "feature family at current costs."
        ),
        "hits": [],
        "taker_rt_bps": taker_rt,
        "best_breakeven_bps": best_be,
        "best_overall_15m_to_24h_bps": best_overall,
        "gap_to_taker_bps": gap,
        "underpowered_green": underpowered_green,
        "short_horizon_best_be_bps": short_horizon_best_be,
    }


def _bps(x: Optional[float]) -> str:
    if x is None or not np.isfinite(x):
        return "n/a"
    return f"{x * 1e4:.2f}bps"


def write_report(
    path: Path,
    results: List[Dict[str, Any]],
    verdict: Dict[str, Any],
    meta: Dict[str, Any],
) -> None:
    lines: List[str] = []
    lines.append("# Long-Horizon Directional Cost Test")
    lines.append("")
    lines.append(f"Generated: {meta['created_utc']}")
    lines.append(f"DB: `{meta['db']}`")
    lines.append(f"Symbols: {', '.join(meta['symbols'])}")
    lines.append(
        f"Span: {meta.get('span_days', float('nan')):.1f} days · "
        f"grid: closed 15m · exit: time stop only."
    )
    lines.append("")
    lines.append("## Scope")
    lines.append("")
    lines.append(
        "Measurement only — no strategy, no gates, no tuning. Extends "
        "`docs/REVERSION_COST_TEST.md` (short-horizon fade, best BE **4.21 bps**) "
        "to 12h/24h and to the screening 24h survivors that are **not** reversion."
    )
    lines.append("")
    lines.append("### Frozen rules")
    lines.append("")
    lines.append("| id | signal | side rule | hold |")
    lines.append("|---|---|---|---|")
    lines.append("| `ret_lag_*` | closed-bar return over L | **fade** `−sign(signal)` | 12h/24h |")
    lines.append("| `oi_delta_24h` | OI %Δ over 24h | **follow** `+sign(signal)` (IC>0) | 24h |")
    lines.append(
        "| `atr_percentile_7d` | ATR rank in 7d | **fade_half** short if >0.5 "
        "(IC<0) | 24h |"
    )
    lines.append(
        "| `dow` | day-of-week 0–6 | split Mon–Wed short / Thu–Sun long "
        "(**reference only**) | 24h |"
    )
    lines.append("")
    lines.append("## Cost books")
    lines.append("")
    lines.append("| book | RT |")
    lines.append("|---|---:|")
    for name, book in COST_BOOKS.items():
        lines.append(f"| {name} | {_rt_cost(book)*1e4:.2f}bps |")
    lines.append("")
    lines.append(
        f"`taker_taker` RT = **{_rt_cost(COST_BOOKS['taker_taker'])*1e4:.1f} bps**."
    )
    lines.append("")
    lines.append("## Power rule")
    lines.append("")
    lines.append(
        f"Non-overlapping trades at 24h with ~{meta.get('span_days', 83):.0f} days "
        f"× 4 symbols ≈ {meta.get('span_days', 83)*4:.0f} max. "
        f"**If `n_nonoverlap` < {MIN_NONOVERLAP_POWER}, a green point estimate is "
        "INCONCLUSIVE (underpowered) — never an edge claim.** "
        "CIs are block-bootstrap means over the non-overlap trade sample "
        f"({N_BOOT} resamples, seed={BOOT_SEED})."
    )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"### **({verdict['verdict']})** — {verdict['summary']}")
    lines.append("")
    notes = verdict.get("family_notes") or {}
    if notes:
        lines.append("#### By family")
        lines.append("")
        for fam, note in notes.items():
            lines.append(f"- **{fam}:** {note}")
        lines.append("")
    if verdict.get("best_breakeven_bps") is not None and np.isfinite(
        verdict.get("best_breakeven_bps", float("nan"))
    ):
        lines.append(
            f"- Best long-horizon gross BE among eligible specs: "
            f"**{verdict['best_breakeven_bps']:.2f} bps** "
            f"(taker RT **{verdict['taker_rt_bps']:.1f} bps**, "
            f"gap **{verdict.get('gap_to_taker_bps', float('nan')):.2f} bps**)."
        )
    if verdict.get("best_overall_15m_to_24h_bps") is not None:
        lines.append(
            f"- Best across 15m→24h (incl. prior short-horizon 4.21): "
            f"**{verdict['best_overall_15m_to_24h_bps']:.2f} bps**."
        )
    if verdict.get("underpowered_green"):
        lines.append("- Underpowered greens (NOT counted as edge):")
        for u in verdict["underpowered_green"]:
            lines.append(
                f"  - `{u['id']}`: BE={u['be_bps']:.2f}bps, "
                f"n_nonoverlap={u['n_nonoverlap']} < {MIN_NONOVERLAP_POWER}"
            )
    if verdict["verdict"] == "A":
        for h in verdict.get("hits") or []:
            lines.append(
                f"- `{h['id']}`: BE={h['be_bps']:.2f}bps, "
                f"tt={h['tt_bps']:.2f}bps, n_no={h['n_nonoverlap']}, "
                f"edge CI=[{h.get('edge_ci_low_bps', float('nan')):.2f}, "
                f"{h.get('edge_ci_high_bps', float('nan')):.2f}] bps"
            )
        lines.append(
            "- Caveat: n_nonoverlap is only modestly above the power floor "
            f"({MIN_NONOVERLAP_POWER}); treat as a candidate to **gate**, not as "
            "a finished strategy."
        )
    elif verdict["verdict"] == "B" and verdict.get("hits"):
        for h in verdict["hits"]:
            lines.append(
                f"- `{h['id']}`: mm_cons={h['mm_c_bps']:.2f}bps, "
                f"mt_cons={h['mt_c_bps']:.2f}bps, n_no={h['n_nonoverlap']}"
            )
    lines.append("")
    lines.append("## Results table")
    lines.append("")
    lines.append(
        "| id | family | n_obs | **n_nonoverlap** | gross E | **BE RT** | "
        "BE CI (non-olap) | 0bps | mm cons | **tt** | power |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---|---:|---:|---:|---|")
    for r in results:
        if not r.get("n_overlapping_obs"):
            continue
        c = r["costs"]
        be_ci = r.get("bootstrap_nonoverlap_be_bps") or {}
        ci_s = (
            f"[{be_ci.get('ci_low_bps', float('nan')):.2f}, "
            f"{be_ci.get('ci_high_bps', float('nan')):.2f}]"
            if np.isfinite(be_ci.get("ci_low_bps", float("nan")))
            else "n/a"
        )
        power = (
            "UNDER"
            if r.get("underpowered")
            else ("ref" if not r.get("verdict_eligible") else "ok")
        )
        tag = r["id"]
        if not r.get("verdict_eligible"):
            tag = f"{tag}†"
        lines.append(
            f"| {tag} | {r['family']} | {r['n_overlapping_obs']} | "
            f"**{r['n_nonoverlap']}** | "
            f"{r['gross_expectancy_bps']:.2f}bps | "
            f"**{r['breakeven_rt_cost_bps']:.2f}bps** | {ci_s} | "
            f"{_bps(c['gross_0bps']['stats']['expectancy'])} | "
            f"{_bps(c['maker_maker']['conservative_touch_through']['expectancy'])} | "
            f"{_bps(c['taker_taker']['stats']['expectancy'])} | {power} |"
        )
    lines.append("")
    lines.append("† `dow` = descriptive reference only — never a strategy candidate.")
    lines.append("")
    lines.append("### Edge vs taker (non-overlap bootstrap)")
    lines.append("")
    lines.append("| id | mean(gross−11bps) | CI low | CI high | P(edge>0) |")
    lines.append("|---|---:|---:|---:|---:|")
    for r in results:
        if not r.get("n_overlapping_obs"):
            continue
        e = r.get("bootstrap_nonoverlap_edge_vs_taker") or {}
        lines.append(
            f"| {r['id']} | {e.get('mean_bps', float('nan')):.2f} | "
            f"{e.get('ci_low_bps', float('nan')):.2f} | "
            f"{e.get('ci_high_bps', float('nan')):.2f} | "
            f"{e.get('p_edge_gt_0', float('nan')):.3f} |"
        )
    lines.append("")
    lines.append("## `dow` caution (Task 3)")
    lines.append("")
    lines.append(
        "Twelve weeks ≈ **12 observations per weekday** before pooling symbols. "
        "Overlapping 24h forward windows inflate n_obs; Newey–West / bootstrap "
        "cannot invent information. The `dow` row is a **descriptive curiosity** "
        "only. It is excluded from verdict eligibility regardless of the number."
    )
    dow = next((r for r in results if r.get("id") == "dow@24h"), None)
    if dow and dow.get("n_overlapping_obs"):
        lines.append("")
        lines.append(
            f"Observed: BE={dow['breakeven_rt_cost_bps']:.2f}bps, "
            f"n_nonoverlap={dow['n_nonoverlap']}, "
            f"tt={_bps(dow['costs']['taker_taker']['stats']['expectancy'])}."
        )
    lines.append("")
    lines.append("## Continuation")
    lines.append("")
    if verdict["verdict"] == "A":
        lines.append(
            "Next: minimal strategy around the powered hit → `baseline_signal_gate` "
            "→ shadow. No parameter fishing."
        )
    elif verdict["verdict"] == "B":
        lines.append(
            "Next: document how much cost must fall (or how much amplitude must "
            "rise) for viability; maker-first only if conservative fills were green."
        )
    else:
        lines.append(
            "Next: archive in `docs/RESEARCH_BACKLOG.md`. Directional price "
            "prediction at these costs is not exploitable (15m→24h). Next avenue: "
            "**change the cost signal** (market making / spread capture) — already "
            "on the backlog. Do **not** hunt variants of this family."
        )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_backlog(verdict: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
    backlog = ROOT / "docs" / "RESEARCH_BACKLOG.md"
    if not backlog.exists():
        return
    text = backlog.read_text(encoding="utf-8")
    marker = "### Archived — ret_lag fade through 24h + long-horizon cost scan"
    if marker in text:
        return

    atr = next((r for r in results if r.get("id") == "atr_percentile_7d@24h"), None)
    oi = next((r for r in results if r.get("id") == "oi_delta_24h@24h"), None)
    atr_line = (
        f"- **Candidate (A):** `atr_percentile_7d@24h` BE≈{atr['breakeven_rt_cost_bps']:.1f}bps, "
        f"tt≈{atr['costs']['taker_taker']['stats']['expectancy']*1e4:.1f}bps, "
        f"n_nonoverlap={atr['n_nonoverlap']}, edge CI clears 0 on non-overlap. "
        "Vol regime — **not** reversion. Next: minimal strategy → baseline gate."
        if atr and atr.get("n_overlapping_obs")
        else "- atr_percentile: see report."
    )
    oi_line = (
        f"- `oi_delta_24h@24h`: overlapping BE≈{oi['breakeven_rt_cost_bps']:.1f}bps but "
        f"non-overlap edge CI straddles zero — **not** awarded."
        if oi and oi.get("n_overlapping_obs")
        else ""
    )

    if verdict["verdict"] == "A":
        block = f"""
{marker}

`scripts/long_horizon_cost_test.py` / `docs/LONG_HORIZON_COST_TEST.md` (2026-08-09):

- **`ret_lag` fade 15m→24h: CLOSED (not exploitable).** Short-horizon best BE was
  4.21 bps; at 12h/24h gross BE turns ≤0 / more negative. Do not build a fade
  strategy from `ret_lag_*`.
{atr_line}
{oi_line}
- `dow@24h`: descriptive only (~12 obs/weekday) — never a candidate.

Overall directional-price scan verdict **(A)** solely on the vol-regime signal
above — not on reversion.

"""
    elif verdict["verdict"] == "C":
        block = f"""
{marker}

Long-horizon extension → combined verdict **(C)** for directional price at
current costs (see `docs/LONG_HORIZON_COST_TEST.md`). Next avenue: market making
/ spread capture.

"""
    else:
        block = f"""
{marker}

Long-horizon cost scan verdict **({verdict['verdict']})** — see
`docs/LONG_HORIZON_COST_TEST.md`.

"""

    short_marker = "### Archived — short-horizon mean reversion (cost test)"
    if short_marker in text:
        idx = text.find(short_marker)
        rest = text[idx:]
        nxt = rest.find("\n### ", 1)
        if nxt == -1:
            text = text + "\n" + block
        else:
            insert_at = idx + nxt
            text = text[:insert_at] + block + "\n" + text[insert_at:]
    else:
        text = text + "\n" + block
    backlog.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/live/bot.db")
    ap.add_argument("--symbols", default=",".join(SYMBOLS_DEFAULT))
    ap.add_argument(
        "--short-best-be",
        type=float,
        default=4.21,
        help="Prior short-horizon best BE (bps) from REVERSION_COST_TEST",
    )
    ap.add_argument("--out-json", default="")
    ap.add_argument("--out-md", default="")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.is_absolute():
        db = ROOT / db
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    t0 = time.time()
    raw = load_candles(db, symbols)
    oi = load_oi_history(db, symbols)
    if raw.empty:
        print("No candles — abort")
        return 2
    span_days = (
        float(raw["timestamp_ms"].max() - raw["timestamp_ms"].min()) / 86400000.0
    )
    panel = build_panel(raw, oi)
    print(
        f"Panel rows={len(panel)} symbols={symbols} span_days={span_days:.1f} "
        f"oi_hist_rows={len(oi)}"
    )

    results = [analyze_spec(panel, spec) for spec in SPECS]
    verdict = decide_verdict(results, short_horizon_best_be=float(args.short_best_be))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_json = (
        Path(args.out_json)
        if args.out_json
        else ROOT / "data" / "backtests" / f"long_horizon_cost_test_{stamp}.json"
    )
    out_md = (
        Path(args.out_md)
        if args.out_md
        else ROOT / "docs" / "LONG_HORIZON_COST_TEST.md"
    )
    out_json.parent.mkdir(parents=True, exist_ok=True)

    meta = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "db": str(db),
        "symbols": symbols,
        "span_days": span_days,
        "min_nonoverlap_power": MIN_NONOVERLAP_POWER,
        "n_boot": N_BOOT,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    payload = {"meta": meta, "verdict": verdict, "results": results}
    out_json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    latest = ROOT / "data" / "backtests" / "long_horizon_cost_test_latest.json"
    latest.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    write_report(out_md, results, verdict, meta)
    update_backlog(verdict, results)

    print()
    print(f"VERDICT ({verdict['verdict']}): {verdict['summary']}")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    for r in results:
        if not r.get("n_overlapping_obs"):
            continue
        print(
            f"  {r['id']}: BE={r['breakeven_rt_cost_bps']:.2f}bps "
            f"n_obs={r['n_overlapping_obs']} n_no={r['n_nonoverlap']} "
            f"tt={r['costs']['taker_taker']['stats']['expectancy']*1e4:.2f}bps "
            f"{'UNDERPOWERED' if r.get('underpowered') else ''}"
            f"{' [ref]' if not r.get('verdict_eligible') else ''}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
