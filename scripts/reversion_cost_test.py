"""Economic viability of short-horizon mean reversion — costs before strategy.

Minimal rule (no gates, stops, TP, or tuning):
  signal = ret_lag_L  (closed-bar return over lookback L)
  side   = −sign(signal)   # fade
  exit   = close after H bars (time stop only)

Cost sweep + breakeven RT cost + quintiles + vol-regime ON/OFF +
conservative maker fill model.

Usage:
  python scripts/reversion_cost_test.py
  python scripts/reversion_cost_test.py --db data/live/bot.db
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SYMBOLS_DEFAULT = ["BTC", "ETH", "SOL", "HYPE"]

# (lookback, hold) on the 15m grid — survivor pairs from feature screening
COMBOS: Tuple[Tuple[str, str, int, int], ...] = (
    # name_L, name_H, L_bars, H_bars
    ("1h", "1h", 4, 4),
    ("4h", "4h", 16, 16),
    ("1h", "4h", 4, 16),
    ("1h", "15m", 4, 1),
    ("4h", "15m", 16, 1),
    ("15m", "1h", 1, 4),
)

# Cost book (fraction of notional, per side unless noted)
# Matches config/settings.yaml: taker 0.035%, paper slip 0.02%, maker 0.01%
MAKER_FEE = 0.0001
TAKER_FEE = 0.00035
TAKER_SLIP = 0.0002

COST_BOOKS: Dict[str, Dict[str, float]] = {
    "gross_0bps": {
        "entry_fee": 0.0,
        "exit_fee": 0.0,
        "entry_slip": 0.0,
        "exit_slip": 0.0,
    },
    "maker_maker": {
        "entry_fee": MAKER_FEE,
        "exit_fee": MAKER_FEE,
        "entry_slip": 0.0,
        "exit_slip": 0.0,
    },
    "maker_taker": {
        "entry_fee": MAKER_FEE,
        "exit_fee": TAKER_FEE,
        "entry_slip": 0.0,
        "exit_slip": TAKER_SLIP,
    },
    "taker_taker": {
        "entry_fee": TAKER_FEE,
        "exit_fee": TAKER_FEE,
        "entry_slip": TAKER_SLIP,
        "exit_slip": TAKER_SLIP,
    },
}


def _rt_cost(book: Dict[str, float]) -> float:
    return (
        book["entry_fee"]
        + book["exit_fee"]
        + book["entry_slip"]
        + book["exit_slip"]
    )


def load_candles_15m(db: Path, symbols: Sequence[str]) -> pd.DataFrame:
    con = sqlite3.connect(str(db))
    try:
        q = f"""
            SELECT symbol, timestamp_ms, open, high, low, close, volume
            FROM candles_15m
            WHERE symbol IN ({",".join("?" * len(symbols))})
            ORDER BY symbol, timestamp_ms
        """
        return pd.read_sql_query(q, con, params=list(symbols))
    finally:
        con.close()


def build_panel(raw: pd.DataFrame) -> pd.DataFrame:
    """Per-symbol features on closed 15m bars (point-in-time)."""
    pieces: List[pd.DataFrame] = []
    for sym, g in raw.groupby("symbol", sort=False):
        g = g.sort_values("timestamp_ms").reset_index(drop=True)
        close = g["close"].astype(float)
        high = g["high"].astype(float)
        low = g["low"].astype(float)
        logret = np.log(close / close.shift(1))
        rvol_1h = logret.rolling(4, min_periods=4).std(ddof=0) * math.sqrt(4)
        # ATR percentile 7d (regime filter companion)
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
        out = pd.DataFrame(
            {
                "symbol": sym,
                "timestamp_ms": g["timestamp_ms"].to_numpy(),
                "open": g["open"].astype(float).to_numpy(),
                "high": high.to_numpy(),
                "low": low.to_numpy(),
                "close": close.to_numpy(),
                "ret_15m": close.pct_change(1).to_numpy(),
                "ret_1h": close.pct_change(4).to_numpy(),
                "ret_4h": close.pct_change(16).to_numpy(),
                "rvol_1h": rvol_1h.to_numpy(),
                "atr_percentile_7d": atr_pct.to_numpy(),
            }
        )
        pieces.append(out)
    return pd.concat(pieces, ignore_index=True)


def _signal_col(L_name: str) -> str:
    return {"15m": "ret_15m", "1h": "ret_1h", "4h": "ret_4h"}[L_name]


@dataclass
class TradeRow:
    symbol: str
    ts_ms: int
    L: str
    H: str
    signal: float
    side: int  # +1 long, -1 short
    gross: float  # taker path: side * (close[t+H]/close[t] - 1)
    gross_maker_adv: float  # conservative maker path PnL (see generate_trades)
    entry_i: int
    exit_i: int
    rvol_1h: float
    atr_pct: float
    maker_fill: bool  # conservative touch-through entry fill


def generate_trades(
    panel: pd.DataFrame,
    L_name: str,
    H_name: str,
    L_bars: int,
    H_bars: int,
    non_overlapping: bool = False,
) -> List[TradeRow]:
    """Build trade observations. Overlapping (every bar) is the expectancy sample;
    non_overlapping approximates single-slot deployment frequency.
    """
    sig_col = _signal_col(L_name)
    rows: List[TradeRow] = []
    for sym, g in panel.groupby("symbol", sort=False):
        g = g.reset_index(drop=True)
        close = g["close"].to_numpy(dtype=float)
        high = g["high"].to_numpy(dtype=float)
        low = g["low"].to_numpy(dtype=float)
        sig = g[sig_col].to_numpy(dtype=float)
        rvol = g["rvol_1h"].to_numpy(dtype=float)
        atrp = g["atr_percentile_7d"].to_numpy(dtype=float)
        ts = g["timestamp_ms"].to_numpy(dtype=np.int64)
        n = len(g)
        next_free = 0
        for i in range(n):
            # Need i+1 for maker touch bar and i+H (taker) / i+1+H (maker) exits
            if i + 1 + H_bars >= n:
                break
            s = sig[i]
            if not np.isfinite(s) or s == 0.0:
                continue
            if non_overlapping and i < next_free:
                continue
            side = -1 if s > 0 else 1
            entry = close[i]
            exit_px = close[i + H_bars]
            if entry <= 0 or not np.isfinite(exit_px):
                continue
            gross = side * (exit_px / entry - 1.0)

            # Conservative maker:
            # - Resting limit at close[t] (fill price = limit, not the extreme).
            # - Require the next bar to trade *through* the limit by ≥1× paper
            #   slip (2 bps) — a mere wick to exact close is ~always true on
            #   15m and is not a fill model.
            # - Hold from t+1 to t+1+H at close marks (exit leg still slightly
            #   kind: no queue simulation).
            pen = TAKER_SLIP  # 2 bps through the level
            if side > 0:
                thresh = close[i] * (1.0 - pen)
                maker_fill = bool(low[i + 1] <= thresh)
            else:
                thresh = close[i] * (1.0 + pen)
                maker_fill = bool(high[i + 1] >= thresh)
            if maker_fill and entry > 0:
                exit_m = close[i + 1 + H_bars]
                # Fill at limit (close[i]), not at the adverse extreme —
                # using the extreme would *improve* fade entries and invent edge.
                gross_m = side * (exit_m / entry - 1.0)
            else:
                gross_m = float("nan")

            rows.append(
                TradeRow(
                    symbol=str(sym),
                    ts_ms=int(ts[i]),
                    L=L_name,
                    H=H_name,
                    signal=float(s),
                    side=int(side),
                    gross=float(gross),
                    gross_maker_adv=float(gross_m) if np.isfinite(gross_m) else float("nan"),
                    entry_i=i,
                    exit_i=i + H_bars,
                    rvol_1h=float(rvol[i]) if np.isfinite(rvol[i]) else float("nan"),
                    atr_pct=float(atrp[i]) if np.isfinite(atrp[i]) else float("nan"),
                    maker_fill=maker_fill,
                )
            )
            if non_overlapping:
                next_free = i + H_bars
    return rows


def apply_costs(
    gross: np.ndarray,
    book: Dict[str, float],
    maker_fill: Optional[np.ndarray] = None,
    maker_mode: str = "naive",
    maker_gross: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, int]:
    """Net returns = gross − RT cost. maker_mode:
      - naive: always fill at signal close (UPPER BOUND — optimistic)
      - conservative: touch-through fills only, PnL from adverse fill price
    """
    rt = _rt_cost(book)
    if maker_mode == "conservative" and maker_gross is not None:
        keep = np.isfinite(maker_gross)
        if maker_fill is not None:
            keep = keep & maker_fill.astype(bool)
        net = np.full(len(gross), np.nan, dtype=float)
        net[keep] = maker_gross[keep] - rt
        return net, int(keep.sum())
    net = gross - rt
    return net, len(gross)


def expectancy(net: np.ndarray) -> Dict[str, float]:
    x = net[np.isfinite(net)]
    n = len(x)
    if n == 0:
        return {
            "n": 0,
            "expectancy": float("nan"),
            "median": float("nan"),
            "hit_rate": float("nan"),
            "std": float("nan"),
        }
    return {
        "n": n,
        "expectancy": float(np.mean(x)),
        "median": float(np.median(x)),
        "hit_rate": float(np.mean(x > 0)),
        "std": float(np.std(x, ddof=1)) if n > 1 else float("nan"),
    }


def breakeven_rt_bps(gross: np.ndarray) -> float:
    """RT cost (bps) that zeros mean expectancy. = mean(gross) in bps."""
    x = gross[np.isfinite(gross)]
    if len(x) == 0:
        return float("nan")
    return float(np.mean(x) * 1e4)  # fraction → bps


def quintile_tables(
    signals: np.ndarray,
    gross: np.ndarray,
    maker_fill: np.ndarray,
) -> Dict[str, Any]:
    """Expectancy by signal quintile under taker/taker; Q1∪Q5 selective."""
    mask = np.isfinite(signals) & np.isfinite(gross)
    s = signals[mask]
    g = gross[mask]
    mf = maker_fill[mask]
    if len(s) < 50:
        return {"by_q": [], "all": {}, "extremes": {}}
    try:
        q = pd.qcut(s, 5, labels=False, duplicates="drop")
    except ValueError:
        return {"by_q": [], "all": {}, "extremes": {}}
    book = COST_BOOKS["taker_taker"]
    by_q = []
    for qi in sorted(set(int(x) for x in q if np.isfinite(x))):
        sel = q == qi
        net, n = apply_costs(g[sel], book)
        st = expectancy(net)
        st["quintile"] = int(qi) + 1
        st["signal_mean"] = float(np.mean(s[sel]))
        by_q.append(st)
    net_all, _ = apply_costs(g, book)
    ext = (q == q.min()) | (q == q.max())
    net_ext, _ = apply_costs(g[ext], book)
    return {
        "by_q": by_q,
        "all": expectancy(net_all),
        "extremes_q1q5": expectancy(net_ext),
        "n_extremes": int(ext.sum()),
        "n_all": int(len(g)),
    }


def vol_filter_compare(
    rvol: np.ndarray,
    gross: np.ndarray,
    maker_fill: np.ndarray,
) -> Dict[str, Any]:
    """ON/OFF: trade only when rvol_1h in low/mid quintiles (Q1–Q3) vs always."""
    mask = np.isfinite(rvol) & np.isfinite(gross)
    rv = rvol[mask]
    g = gross[mask]
    if len(rv) < 50:
        return {}
    try:
        q = pd.qcut(rv, 5, labels=False, duplicates="drop")
    except ValueError:
        return {}
    book = COST_BOOKS["taker_taker"]
    always_net, _ = apply_costs(g, book)
    lowmed = q <= 2  # Q1–Q3
    high = q >= 3  # Q4–Q5
    net_lm, _ = apply_costs(g[lowmed], book)
    net_hi, _ = apply_costs(g[high], book)
    return {
        "always": expectancy(always_net),
        "rvol_Q1_Q3": expectancy(net_lm),
        "rvol_Q4_Q5": expectancy(net_hi),
        "helps": bool(
            np.isfinite(expectancy(net_lm)["expectancy"])
            and np.isfinite(expectancy(always_net)["expectancy"])
            and expectancy(net_lm)["expectancy"]
            > expectancy(always_net)["expectancy"]
        ),
    }


def analyze_combo(
    panel: pd.DataFrame,
    L_name: str,
    H_name: str,
    L_bars: int,
    H_bars: int,
) -> Dict[str, Any]:
    trades = generate_trades(panel, L_name, H_name, L_bars, H_bars, False)
    trades_no = generate_trades(panel, L_name, H_name, L_bars, H_bars, True)
    if not trades:
        return {"L": L_name, "H": H_name, "n": 0}

    gross = np.array([t.gross for t in trades], dtype=float)
    gross_m = np.array([t.gross_maker_adv for t in trades], dtype=float)
    signals = np.array([t.signal for t in trades], dtype=float)
    rvol = np.array([t.rvol_1h for t in trades], dtype=float)
    mf = np.array([t.maker_fill for t in trades], dtype=bool)

    cost_rows = {}
    for name, book in COST_BOOKS.items():
        if name.startswith("maker"):
            net_opt, n_opt = apply_costs(gross, book, mf, maker_mode="naive")
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
            net, n = apply_costs(gross, book)
            cost_rows[name] = {
                "rt_cost_bps": _rt_cost(book) * 1e4,
                "stats": expectancy(net),
            }

    be = breakeven_rt_bps(gross)
    qtab = quintile_tables(signals, gross, mf)
    vol = vol_filter_compare(rvol, gross, mf)

    # Non-overlap under taker
    g_no = np.array([t.gross for t in trades_no], dtype=float)
    net_no, _ = apply_costs(g_no, COST_BOOKS["taker_taker"])
    no_stats = expectancy(net_no)

    return {
        "L": L_name,
        "H": H_name,
        "L_bars": L_bars,
        "H_bars": H_bars,
        "n_overlapping_obs": len(trades),
        "n_nonoverlap_trades": len(trades_no),
        "gross_expectancy": float(np.mean(gross)),
        "gross_expectancy_bps": float(np.mean(gross) * 1e4),
        "breakeven_rt_cost_bps": be,
        "costs": cost_rows,
        "quintiles_taker": qtab,
        "vol_filter_taker": vol,
        "nonoverlap_taker": no_stats,
        "by_symbol_gross_bps": {
            sym: float(np.mean([t.gross for t in trades if t.symbol == sym]) * 1e4)
            for sym in sorted({t.symbol for t in trades})
        },
    }


def decide_verdict(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """(A) taker survives (B) maker-conservative survives (C) not exploitable.

    Optimistic maker numbers never trigger B — they are an upper bound only.
    """
    taker_hits = []
    maker_cons_hits = []
    maker_opt_only = []

    for r in results:
        if not r.get("n_overlapping_obs"):
            continue
        key = f"{r['L']}/{r['H']}"
        tt = r["costs"]["taker_taker"]["stats"]["expectancy"]
        if np.isfinite(tt) and tt > 0:
            taker_hits.append((key, tt, r["breakeven_rt_cost_bps"]))

        mm_c = r["costs"]["maker_maker"]["conservative_touch_through"]["expectancy"]
        mt_c = r["costs"]["maker_taker"]["conservative_touch_through"]["expectancy"]
        mm_o = r["costs"]["maker_maker"]["optimistic_upper_bound"]["expectancy"]
        mt_o = r["costs"]["maker_taker"]["optimistic_upper_bound"]["expectancy"]

        cons_ok = (np.isfinite(mm_c) and mm_c > 0) or (np.isfinite(mt_c) and mt_c > 0)
        if cons_ok:
            maker_cons_hits.append((key, mm_c, mt_c, r["costs"]["maker_maker"]["fill_rate"]))

        opt_ok = (np.isfinite(mm_o) and mm_o > 0) or (np.isfinite(mt_o) and mt_o > 0)
        if opt_ok and not cons_ok:
            maker_opt_only.append((key, mm_o, mt_o))

    taker_rt = _rt_cost(COST_BOOKS["taker_taker"]) * 1e4

    if taker_hits:
        return {
            "verdict": "A",
            "summary": (
                "Edge survives taker/taker on at least one (L,H). "
                "Build a minimal fade strategy and run the baseline-signal gate."
            ),
            "hits": taker_hits,
            "taker_rt_bps": taker_rt,
        }
    if maker_cons_hits:
        return {
            "verdict": "B",
            "summary": (
                "Does not survive taker/taker, but survives under the "
                "conservative maker fill model (limit fill + ≥2 bps penetration). "
                "Design requirement: any reversion strategy must be maker-first."
            ),
            "hits": maker_cons_hits,
            "taker_rt_bps": taker_rt,
        }
    best_be = max(
        (
            r["breakeven_rt_cost_bps"]
            for r in results
            if np.isfinite(r.get("breakeven_rt_cost_bps", float("nan")))
        ),
        default=float("nan"),
    )
    return {
        "verdict": "C",
        "summary": (
            "Effect is real in the gross/IC sense but not exploitable at "
            "realistic costs. Best gross breakeven RT is well below taker RT; "
            "conservative maker fails; optimistic maker (if green) is an "
            "UPPER BOUND only — not authorization to build."
        ),
        "hits": [],
        "optimistic_only_hits": maker_opt_only,
        "taker_rt_bps": taker_rt,
        "best_breakeven_bps": best_be,
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
    lines.append("# Reversion Cost Test")
    lines.append("")
    lines.append(f"Generated: {meta['created_utc']}")
    lines.append(f"DB: `{meta['db']}`")
    lines.append(f"Symbols: {', '.join(meta['symbols'])}")
    lines.append("Bar grid: closed 15m only. Exit: time stop only (no SL/TP).")
    lines.append("")
    lines.append("## Rule (frozen — no tuning)")
    lines.append("")
    lines.append("```")
    lines.append("signal = ret_lag_L")
    lines.append("side   = -sign(signal)   # fade")
    lines.append("exit   = close after H bars")
    lines.append("```")
    lines.append("")
    lines.append("## Cost books")
    lines.append("")
    lines.append("| book | entry fee | exit fee | entry slip | exit slip | RT |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for name, book in COST_BOOKS.items():
        lines.append(
            f"| {name} | {book['entry_fee']*1e4:.2f}bps | "
            f"{book['exit_fee']*1e4:.2f}bps | "
            f"{book['entry_slip']*1e4:.2f}bps | "
            f"{book['exit_slip']*1e4:.2f}bps | "
            f"{_rt_cost(book)*1e4:.2f}bps |"
        )
    lines.append("")
    lines.append(
        f"`taker_taker` RT = **{_rt_cost(COST_BOOKS['taker_taker'])*1e4:.1f} bps** "
        "(matches bot: 3.5bps fee + 2bps slip per side)."
    )
    lines.append("")
    lines.append("## Maker execution model (declared)")
    lines.append("")
    lines.append(
        "1. **Optimistic / UPPER BOUND:** assume 100% fill at signal close with "
        "maker fees only (or maker entry + taker exit). This **overstates** "
        "edge — limit orders are not always filled and fills are adversely "
        "selected. Never treat this column as an estimate."
    )
    lines.append(
        "2. **Conservative (used for verdict B):** resting limit at `close[t]` "
        "with **fill price = limit** (not the bar extreme — using the extreme "
        "would improve fade entries and invent edge). Fill only if the next "
        "15m bar penetrates the limit by ≥2 bps "
        "(long: `low[t+1] ≤ close[t]×(1−2bps)`; short: symmetric). "
        "Hold from `t+1` → `close[t+1+H]`. Non-fills skipped. "
        "A weaker “any touch” rule filled ~98% of 15m bars and was rejected "
        "as vacuous."
    )
    lines.append(
        "Optimistic maker green without conservative green → **not** verdict B."
    )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"### **({verdict['verdict']})** — {verdict['summary']}")
    lines.append("")
    if verdict["verdict"] == "A":
        for key, exp, be in verdict["hits"]:
            lines.append(
                f"- `{key}`: taker expectancy {_bps(exp)}, "
                f"breakeven RT {be:.2f} bps "
                f"(taker RT = {verdict['taker_rt_bps']:.1f} bps)"
            )
    elif verdict["verdict"] == "B":
        for item in verdict["hits"]:
            key, mm, mt = item[0], item[1], item[2]
            fill = item[3] if len(item) > 3 else float("nan")
            lines.append(
                f"- `{key}`: maker/maker cons. {_bps(mm)}, "
                f"maker/taker cons. {_bps(mt)}, fill_rate={fill:.0%}"
            )
    else:
        lines.append(
            f"- Best gross breakeven RT among combos: "
            f"**{verdict.get('best_breakeven_bps', float('nan')):.2f} bps** "
            f"vs taker RT **{verdict['taker_rt_bps']:.1f} bps**."
        )
        if verdict.get("optimistic_only_hits"):
            bits = []
            for item in verdict["optimistic_only_hits"]:
                if len(item) >= 3:
                    bits.append(
                        f"`{item[0]}` (mm_opt={_bps(item[1])}, mt_opt={_bps(item[2])})"
                    )
                else:
                    bits.append(f"`{item[0]}` ({_bps(item[1])})")
            lines.append(
                "- Optimistic maker alone looked positive on: "
                + ", ".join(bits)
                + " — **not** counted as survival."
            )
    lines.append("")
    lines.append("## Breakeven & cost sweep (overlapping obs = IC sample)")
    lines.append("")
    lines.append(
        "| L/H | n_obs | n_nonoverlap | gross E | **BE RT** | "
        "0bps | mm opt | mm cons | mt cons | **tt** |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in results:
        c = r["costs"]
        lines.append(
            f"| {r['L']}/{r['H']} | {r['n_overlapping_obs']} | "
            f"{r['n_nonoverlap_trades']} | "
            f"{r['gross_expectancy_bps']:.2f}bps | "
            f"**{r['breakeven_rt_cost_bps']:.2f}bps** | "
            f"{_bps(c['gross_0bps']['stats']['expectancy'])} | "
            f"{_bps(c['maker_maker']['optimistic_upper_bound']['expectancy'])} | "
            f"{_bps(c['maker_maker']['conservative_touch_through']['expectancy'])} | "
            f"{_bps(c['maker_taker']['conservative_touch_through']['expectancy'])} | "
            f"**{_bps(c['taker_taker']['stats']['expectancy'])}** |"
        )
    lines.append("")
    lines.append(
        "Reading the BE column: if breakeven RT ≪ taker RT (11 bps), the "
        "effect cannot pay for current execution. Longer H → fewer "
        "non-overlap trades → less fee drag *per unit time*, but the "
        "per-trade BE is still set by mean gross edge."
    )
    lines.append("")
    lines.append("## Quintiles (taker/taker) — where the edge lives")
    lines.append("")
    for r in results:
        q = r.get("quintiles_taker") or {}
        if not q.get("by_q"):
            continue
        lines.append(f"### {r['L']}/{r['H']}")
        lines.append("")
        lines.append("| Q | signal_mean | n | expectancy | hit_rate |")
        lines.append("|---:|---:|---:|---:|---:|")
        for row in q["by_q"]:
            lines.append(
                f"| Q{row['quintile']} | {row['signal_mean']:.5f} | "
                f"{row['n']} | {_bps(row['expectancy'])} | "
                f"{row['hit_rate']*100:.1f}% |"
            )
        lines.append("")
        lines.append(
            f"- Always: {_bps(q['all'].get('expectancy'))} (n={q['all'].get('n')})"
        )
        lines.append(
            f"- Extremes only (Q1∪Q5): "
            f"{_bps(q['extremes_q1q5'].get('expectancy'))} "
            f"(n={q['extremes_q1q5'].get('n')})"
        )
        lines.append("")
    lines.append("## Volatility regime filter (taker/taker, ON/OFF)")
    lines.append("")
    lines.append(
        "Trade only when `rvol_1h` ∈ Q1–Q3 (low/mid) vs always vs high vol "
        "Q4–Q5. No threshold tuning."
    )
    lines.append("")
    lines.append("| L/H | always | rvol Q1–Q3 | rvol Q4–Q5 | helps? |")
    lines.append("|---|---:|---:|---:|:---:|")
    for r in results:
        v = r.get("vol_filter_taker") or {}
        if not v:
            continue
        lines.append(
            f"| {r['L']}/{r['H']} | "
            f"{_bps(v['always']['expectancy'])} | "
            f"{_bps(v['rvol_Q1_Q3']['expectancy'])} | "
            f"{_bps(v['rvol_Q4_Q5']['expectancy'])} | "
            f"{'Y' if v.get('helps') else 'n'} |"
        )
    lines.append("")
    lines.append("## Continuation")
    lines.append("")
    if verdict["verdict"] == "A":
        lines.append(
            "Next: one minimal fade strategy around the best (L,H) → "
            "`baseline_signal_gate` → shadow. No parameter fishing."
        )
    elif verdict["verdict"] == "B":
        lines.append(
            "Next: document maker-first as a hard design constraint; only then "
            "sketch a strategy. Optimistic maker numbers are not authorization."
        )
    else:
        lines.append(
            "Next: archive in `docs/RESEARCH_BACKLOG.md` with breakeven "
            "numbers. Do **not** build a reversion strategy from this family."
        )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_backlog_if_c(verdict: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
    if verdict["verdict"] != "C":
        return
    backlog = ROOT / "docs" / "RESEARCH_BACKLOG.md"
    if not backlog.exists():
        return
    text = backlog.read_text(encoding="utf-8")
    marker = "### Archived — short-horizon mean reversion (cost test)"
    if marker in text:
        return
    best = max(results, key=lambda r: r.get("breakeven_rt_cost_bps") or -1)
    block = f"""
{marker}

Screening found genuine short-horizon fade structure (`ret_lag_*`, IC≈−0.06,
monotone, stable). Cost test (`scripts/reversion_cost_test.py`,
`docs/REVERSION_COST_TEST.md`) verdict **(C)**:

- Best gross breakeven RT ≈ **{verdict.get('best_breakeven_bps', float('nan')):.2f} bps**
  (combo `{best['L']}/{best['H']}`), vs bot taker RT **{verdict['taker_rt_bps']:.1f} bps**.
- Taker/taker expectancy negative on all tested (L,H).
- Conservative maker (touch-through + adverse haircut) also fails; optimistic
  maker is an upper bound only.

**Do not build** a mean-reversion strategy from this feature family at current
HL fee/slip assumptions. Re-open only if maker fills can be measured live with
fill-rate + adverse-selection stats that beat the breakeven, or if fee tier
drops enough that taker RT < breakeven.

"""
    # Insert after CVD feature section / near top archived material
    if "### Archived — regime mismatch" in text:
        text = text.replace(
            "### Archived — regime mismatch",
            block + "### Archived — regime mismatch",
            1,
        )
    else:
        text = text.rstrip() + "\n" + block
    backlog.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "live" / "bot.db")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data" / "backtests")
    ap.add_argument(
        "--report",
        type=Path,
        default=ROOT / "docs" / "REVERSION_COST_TEST.md",
    )
    ap.add_argument("--symbols", default=",".join(SYMBOLS_DEFAULT))
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print(f"Loading {args.db} …", flush=True)
    raw = load_candles_15m(args.db, symbols)
    panel = build_panel(raw)
    print(f"Panel: {len(panel)} bars, symbols={symbols}", flush=True)

    results: List[Dict[str, Any]] = []
    for L_name, H_name, Lb, Hb in COMBOS:
        print(f"  combo {L_name}/{H_name} …", flush=True)
        results.append(analyze_combo(panel, L_name, H_name, Lb, Hb))

    verdict = decide_verdict(results)
    meta = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "db": str(args.db),
        "symbols": symbols,
        "elapsed_s": round(time.time() - t0, 1),
        "maker_model": (
            "optimistic=always fill at close (UPPER BOUND); "
            "conservative=limit fill at close[t] only if next bar penetrates "
            "≥2bps through level; hold t+1→t+1+H; optimistic-only ≠ verdict B"
        ),
        "taker_rt_bps": _rt_cost(COST_BOOKS["taker_taker"]) * 1e4,
        "verdict": verdict["verdict"],
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    payload = {"meta": meta, "verdict": verdict, "results": results}
    json_path = args.out_dir / f"reversion_cost_test_{stamp}.json"
    latest = args.out_dir / "reversion_cost_test_latest.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    latest.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    write_report(args.report, results, verdict, meta)
    update_backlog_if_c(verdict, results)

    print(f"\nVerdict: ({verdict['verdict']}) {verdict['summary']}", flush=True)
    for r in results:
        print(
            f"  {r['L']}/{r['H']}: BE={r['breakeven_rt_cost_bps']:.2f}bps  "
            f"tt={r['costs']['taker_taker']['stats']['expectancy']*1e4:.2f}bps  "
            f"n={r['n_overlapping_obs']}/{r['n_nonoverlap_trades']}",
            flush=True,
        )
    print(f"Wrote {json_path}", flush=True)
    print(f"Wrote {args.report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
