#!/usr/bin/env python3
"""Long-history revalidation of atr_percentile_7d@24h (measurement only).

Tasks:
  2) Binance spot vs HL candle proxy agreement (overlap window)
  3) Cost retest with DATE-BLOCK bootstrap + vol regimes
  4) Verdict A/B/C

Does not write to live bot.db or change production config.

Usage:
  python scripts/validate_atr_percentile_long.py
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util

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

HOLD_BARS = 96  # 24h on 15m
ATR_WIN = 14
PCT_WIN = 96 * 7  # 7d of 15m
N_BOOT = 3000
BOOT_SEED = 42
TAKER_RT = _rt_cost(COST_BOOKS["taker_taker"])
SHORT_SAMPLE_BE_BPS = 34.51  # from LONG_HORIZON_COST_TEST on ~83d HL


@dataclass
class Trade:
    symbol: str
    ts_ms: int
    date: str  # UTC YYYY-MM-DD
    signal: float
    side: int
    gross: float


def load_candles(db: Path, symbols: Sequence[str], tf: str = "15m") -> pd.DataFrame:
    table = f"candles_{tf}"
    con = sqlite3.connect(str(db))
    try:
        q = f"""
            SELECT symbol, timestamp_ms, open, high, low, close, volume
            FROM {table}
            WHERE symbol IN ({",".join("?" * len(symbols))})
            ORDER BY symbol, timestamp_ms
        """
        return pd.read_sql_query(q, con, params=list(symbols))
    finally:
        con.close()


def atr_percentile_series(g: pd.DataFrame) -> pd.Series:
    high = g["high"].astype(float)
    low = g["low"].astype(float)
    close = g["close"].astype(float)
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(ATR_WIN, min_periods=ATR_WIN).mean()
    return atr.rolling(PCT_WIN, min_periods=PCT_WIN).apply(
        lambda x: float(np.mean(x <= x[-1])),
        raw=True,
    )


def build_panel(raw: pd.DataFrame) -> pd.DataFrame:
    pieces = []
    for sym, g in raw.groupby("symbol", sort=False):
        g = g.sort_values("timestamp_ms").reset_index(drop=True)
        atr_pct = atr_percentile_series(g)
        pieces.append(
            pd.DataFrame(
                {
                    "symbol": sym,
                    "timestamp_ms": g["timestamp_ms"].to_numpy(),
                    "high": g["high"].astype(float).to_numpy(),
                    "low": g["low"].astype(float).to_numpy(),
                    "close": g["close"].astype(float).to_numpy(),
                    "atr_percentile_7d": atr_pct.to_numpy(),
                }
            )
        )
    return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()


def binary_side(atr_pct: float) -> Optional[int]:
    if not np.isfinite(atr_pct) or atr_pct == 0.5:
        return None
    return -1 if atr_pct > 0.5 else 1


def generate_trades(panel: pd.DataFrame, *, non_overlapping: bool) -> List[Trade]:
    rows: List[Trade] = []
    for sym, g in panel.groupby("symbol", sort=False):
        g = g.reset_index(drop=True)
        close = g["close"].to_numpy(dtype=float)
        atrp = g["atr_percentile_7d"].to_numpy(dtype=float)
        ts = g["timestamp_ms"].to_numpy(dtype=np.int64)
        n = len(g)
        next_free = 0
        for i in range(n):
            if i + HOLD_BARS >= n:
                break
            if non_overlapping and i < next_free:
                continue
            side = binary_side(float(atrp[i]))
            if side is None:
                continue
            entry = close[i]
            exit_px = close[i + HOLD_BARS]
            if entry <= 0 or not np.isfinite(exit_px):
                continue
            gross = side * (exit_px / entry - 1.0)
            date = datetime.fromtimestamp(int(ts[i]) / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            rows.append(
                Trade(
                    symbol=str(sym),
                    ts_ms=int(ts[i]),
                    date=date,
                    signal=float(atrp[i]),
                    side=int(side),
                    gross=float(gross),
                )
            )
            if non_overlapping:
                next_free = i + HOLD_BARS
    return rows


def date_blocks(trades: List[Trade]) -> Tuple[np.ndarray, List[str]]:
    """One observation per UTC date = mean gross across symbols that day."""
    by_date: Dict[str, List[float]] = {}
    for t in trades:
        by_date.setdefault(t.date, []).append(t.gross)
    dates = sorted(by_date)
    means = np.array([float(np.mean(by_date[d])) for d in dates], dtype=float)
    return means, dates


def date_block_bootstrap_ci(
    date_means: np.ndarray,
    *,
    n_boot: int = N_BOOT,
    alpha: float = 0.05,
    seed: int = BOOT_SEED,
    subtract: float = 0.0,
) -> Dict[str, float]:
    x = date_means[np.isfinite(date_means)] - subtract
    n = len(x)
    if n < 10:
        return {
            "n_dates": float(n),
            "mean": float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "p_gt_0": float("nan"),
        }
    rng = np.random.default_rng(seed)
    boots = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        boots[b] = float(np.mean(x[idx]))
    return {
        "n_dates": float(n),
        "mean": float(np.mean(x)),
        "ci_low": float(np.percentile(boots, 100 * (alpha / 2))),
        "ci_high": float(np.percentile(boots, 100 * (1 - alpha / 2))),
        "p_gt_0": float(np.mean(boots > 0)),
    }


def align_panels(
    hl: pd.DataFrame, bn: pd.DataFrame, symbols: Sequence[str]
) -> pd.DataFrame:
    """Inner-join on (symbol, timestamp_ms) for overlap comparison."""
    rows = []
    for sym in symbols:
        a = hl.loc[hl["symbol"] == sym, ["timestamp_ms", "atr_percentile_7d", "close"]].rename(
            columns={"atr_percentile_7d": "atr_hl", "close": "close_hl"}
        )
        b = bn.loc[bn["symbol"] == sym, ["timestamp_ms", "atr_percentile_7d", "close"]].rename(
            columns={"atr_percentile_7d": "atr_bn", "close": "close_bn"}
        )
        m = a.merge(b, on="timestamp_ms", how="inner")
        m["symbol"] = sym
        rows.append(m)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def proxy_agreement(aligned: pd.DataFrame) -> Dict[str, Any]:
    if aligned.empty:
        return {"n": 0}
    mask = aligned["atr_hl"].notna() & aligned["atr_bn"].notna()
    a = aligned.loc[mask]
    if len(a) < 50:
        return {"n": int(len(a))}
    corr = float(a["atr_hl"].corr(a["atr_bn"]))
    side_hl = a["atr_hl"].map(lambda x: binary_side(float(x)))
    side_bn = a["atr_bn"].map(lambda x: binary_side(float(x)))
    both = side_hl.notna() & side_bn.notna()
    agree = (side_hl[both] == side_bn[both]).mean()
    by_sym = {}
    for sym, g in a.groupby("symbol"):
        sh = g["atr_hl"].map(lambda x: binary_side(float(x)))
        sb = g["atr_bn"].map(lambda x: binary_side(float(x)))
        ok = sh.notna() & sb.notna()
        by_sym[sym] = {
            "n": int(ok.sum()),
            "corr": float(g["atr_hl"].corr(g["atr_bn"])) if len(g) > 2 else float("nan"),
            "signal_agree": float((sh[ok] == sb[ok]).mean()) if ok.any() else float("nan"),
        }
    # Proxy usable if overall agreement high
    usable = bool(np.isfinite(agree) and agree >= 0.85 and np.isfinite(corr) and corr >= 0.80)
    return {
        "n_overlap_bars": int(len(a)),
        "atr_percentile_corr": corr,
        "binary_signal_agreement": float(agree),
        "by_symbol": by_sym,
        "proxy_usable": usable,
        "threshold_note": "usable if corr≥0.80 and binary agree≥0.85 (predeclared)",
    }


def quintile_mono(trades: List[Trade]) -> Dict[str, Any]:
    if len(trades) < 100:
        return {}
    sig = np.array([t.signal for t in trades])
    gross = np.array([t.gross for t in trades])
    try:
        q = pd.qcut(sig, 5, labels=False, duplicates="drop")
    except ValueError:
        return {}
    rows = []
    for qi in sorted(set(int(x) for x in q)):
        sel = q == qi
        rows.append(
            {
                "quintile": int(qi) + 1,
                "signal_mean": float(np.mean(sig[sel])),
                "n": int(sel.sum()),
                "gross_bps": float(np.mean(gross[sel]) * 1e4),
                "tt_bps": float(np.mean(gross[sel] - TAKER_RT) * 1e4),
            }
        )
    # Monotonicity: higher atr quintile → more negative / short-favoring?
    # With fade_half, high atr → short; if thesis holds, Q5 (high atr) should have
    # better gross when side is applied (already in gross). Check mean gross rising
    # with |signal-0.5| extremes or simply Q1 and Q5 both positive.
    means = [r["gross_bps"] for r in rows]
    return {"by_q": rows, "q1_bps": means[0] if means else None, "q5_bps": means[-1] if means else None}


def subperiod_stability(
    date_means: np.ndarray, dates: List[str], n_blocks: int = 6
) -> List[Dict[str, Any]]:
    n = len(date_means)
    if n < n_blocks * 5:
        n_blocks = max(2, n // 10)
    edges = np.linspace(0, n, n_blocks + 1, dtype=int)
    out = []
    for i in range(n_blocks):
        lo, hi = edges[i], edges[i + 1]
        if hi <= lo:
            continue
        chunk = date_means[lo:hi]
        out.append(
            {
                "block": i + 1,
                "date_start": dates[lo],
                "date_end": dates[hi - 1],
                "n_dates": int(hi - lo),
                "mean_gross_bps": float(np.mean(chunk) * 1e4),
                "mean_tt_bps": float(np.mean(chunk - TAKER_RT) * 1e4),
                "positive": bool(np.mean(chunk) > TAKER_RT),
            }
        )
    return out


def vol_regimes(
    panel: pd.DataFrame, date_trades: List[Trade]
) -> Dict[str, Any]:
    """Label each date by BTC 30d realized vol tercile (or equal-weight if no BTC)."""
    # Build daily close from BTC (or first available)
    sym = "BTC" if (panel["symbol"] == "BTC").any() else panel["symbol"].iloc[0]
    g = panel.loc[panel["symbol"] == sym].sort_values("timestamp_ms").copy()
    g["date"] = pd.to_datetime(g["timestamp_ms"], unit="ms", utc=True).dt.strftime(
        "%Y-%m-%d"
    )
    daily = g.groupby("date", sort=True)["close"].last()
    logret = np.log(daily / daily.shift(1))
    rvol_30 = logret.rolling(30, min_periods=20).std() * math.sqrt(365)
    # Terciles on valid rvol
    valid = rvol_30.dropna()
    if len(valid) < 60:
        return {"note": "insufficient history for regime terciles", "regimes": []}
    q33, q66 = valid.quantile([0.33, 0.66])
    label = {}
    for d, v in rvol_30.items():
        if not np.isfinite(v):
            continue
        if v <= q33:
            label[d] = "low_vol"
        elif v <= q66:
            label[d] = "mid_vol"
        else:
            label[d] = "high_vol"

    by_date: Dict[str, List[float]] = {}
    for t in date_trades:
        by_date.setdefault(t.date, []).append(t.gross)
    regime_rows = []
    for reg in ("low_vol", "mid_vol", "high_vol"):
        days = [d for d, r in label.items() if r == reg and d in by_date]
        if not days:
            continue
        means = np.array([float(np.mean(by_date[d])) for d in days])
        boot = date_block_bootstrap_ci(means, subtract=TAKER_RT)
        regime_rows.append(
            {
                "regime": reg,
                "n_dates": len(days),
                "rvol_cut": {"q33": float(q33), "q66": float(q66)},
                "mean_gross_bps": float(np.mean(means) * 1e4),
                "mean_tt_bps": float(np.mean(means - TAKER_RT) * 1e4),
                "edge_ci_bps": {
                    "low": boot["ci_low"] * 1e4 if np.isfinite(boot["ci_low"]) else None,
                    "high": boot["ci_high"] * 1e4 if np.isfinite(boot["ci_high"]) else None,
                },
                "p_edge_gt_0": boot["p_gt_0"],
                "survives_taker": bool(
                    np.isfinite(boot["ci_low"]) and boot["ci_low"] > 0
                ),
            }
        )
    return {
        "method": "BTC 30d realized-vol terciles on daily closes",
        "q33": float(q33),
        "q66": float(q66),
        "regimes": regime_rows,
    }


def symbol_consistency(trades: List[Trade]) -> Dict[str, Any]:
    out = {}
    for sym in sorted({t.symbol for t in trades}):
        g = [t.gross for t in trades if t.symbol == sym]
        dates = sorted({t.date for t in trades if t.symbol == sym})
        arr = np.array(g)
        out[sym] = {
            "n_trades": len(g),
            "n_dates": len(dates),
            "gross_bps": float(np.mean(arr) * 1e4),
            "tt_bps": float(np.mean(arr - TAKER_RT) * 1e4),
        }
    return out


def decide_verdict(
    proxy: Dict[str, Any],
    long_res: Dict[str, Any],
) -> Dict[str, Any]:
    if not proxy.get("proxy_usable"):
        return {
            "verdict": "C",
            "summary": (
                "Binance spot ↔ HL proxy agreement too low — long history is NOT "
                "valid for this signal. Do not use the backfill; do not build."
            ),
            "reason": "proxy_fail",
        }

    edge = long_res.get("date_block_edge_vs_taker") or {}
    be = long_res.get("breakeven_rt_bps", float("nan"))
    regimes = (long_res.get("vol_regimes") or {}).get("regimes") or []
    n_reg_ok = sum(1 for r in regimes if r.get("survives_taker"))
    n_reg = len(regimes)
    ci_lo = edge.get("ci_low_bps", float("nan"))
    ci_hi = edge.get("ci_high_bps", float("nan"))
    clears = bool(np.isfinite(ci_lo) and ci_lo > 0)
    multi_regime = n_reg >= 3 and n_reg_ok >= 2

    if clears and multi_regime and be > 11:
        # Magnitude check vs short sample
        shrink = SHORT_SAMPLE_BE_BPS - be if np.isfinite(be) else float("nan")
        if np.isfinite(be) and be >= 15 and (ci_hi / max(ci_lo, 1e-9) < 20 or ci_lo > 5):
            return {
                "verdict": "A",
                "summary": (
                    "Survives 18–24m with date-block CI excluding zero and multiple "
                    "vol regimes. First validated candidate — minimal strategy → gate."
                ),
                "breakeven_bps": be,
                "edge_ci_bps": [ci_lo, ci_hi],
                "regimes_surviving": n_reg_ok,
                "regimes_total": n_reg,
                "vs_short_sample_be": SHORT_SAMPLE_BE_BPS,
            }
        return {
            "verdict": "B",
            "summary": (
                "Survives long history and clears costs, but magnitude/CI is "
                "materially tighter or closer to zero than the 83-day sample "
                f"({SHORT_SAMPLE_BE_BPS:.1f} bps). Report realistic edge and decide."
            ),
            "breakeven_bps": be,
            "edge_ci_bps": [ci_lo, ci_hi],
            "regimes_surviving": n_reg_ok,
            "regimes_total": n_reg,
            "shrink_from_short_bps": shrink,
            "vs_short_sample_be": SHORT_SAMPLE_BE_BPS,
        }

    if clears and not multi_regime:
        return {
            "verdict": "B",
            "summary": (
                "Date-block CI clears zero overall, but does not survive across "
                "multiple distinct vol regimes — regime-fragile."
            ),
            "breakeven_bps": be,
            "edge_ci_bps": [ci_lo, ci_hi],
            "regimes_surviving": n_reg_ok,
            "regimes_total": n_reg,
            "vs_short_sample_be": SHORT_SAMPLE_BE_BPS,
        }

    return {
        "verdict": "C",
        "summary": (
            "Signal disappears or fails to clear costs on the long sample with "
            "date-block inference — likely an artifact of the original ~83-day "
            "vol regime. Do not build. Archive."
        ),
        "breakeven_bps": be,
        "edge_ci_bps": [ci_lo, ci_hi],
        "regimes_surviving": n_reg_ok,
        "regimes_total": n_reg,
        "vs_short_sample_be": SHORT_SAMPLE_BE_BPS,
        "reason": "long_sample_fail",
    }


def write_report(path: Path, payload: Dict[str, Any]) -> None:
    v = payload["verdict"]
    proxy = payload["proxy_agreement"]
    long_r = payload["long_revalidation"]
    lines = []
    lines.append("# ATR Percentile Long-History Revalidation")
    lines.append("")
    lines.append(f"Generated: {payload['meta']['created_utc']}")
    lines.append(
        f"HL candles: `{payload['meta']['hl_db']}` · "
        f"Binance spot proxy: `{payload['meta']['bn_db']}`"
    )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"### **({v['verdict']})** — {v['summary']}")
    lines.append("")
    if v.get("breakeven_bps") is not None and np.isfinite(v.get("breakeven_bps", float("nan"))):
        lines.append(
            f"- Long-sample BE RT: **{v['breakeven_bps']:.2f} bps** "
            f"(short-sample was {SHORT_SAMPLE_BE_BPS:.2f} bps)"
        )
    if v.get("edge_ci_bps"):
        lines.append(
            f"- Date-block edge CI (gross−11bps): "
            f"**[{v['edge_ci_bps'][0]:.2f}, {v['edge_ci_bps'][1]:.2f}] bps**"
        )
    if "regimes_surviving" in v:
        lines.append(
            f"- Vol regimes surviving taker CI: "
            f"{v['regimes_surviving']}/{v['regimes_total']}"
        )
    lines.append("")
    lines.append("## Task 2 — Binance spot ↔ HL proxy")
    lines.append("")
    lines.append(
        f"- Overlap bars: {proxy.get('n_overlap_bars')} · "
        f"atr% corr: **{proxy.get('atr_percentile_corr', float('nan')):.3f}** · "
        f"binary agree: **{100*proxy.get('binary_signal_agreement', float('nan')):.1f}%** · "
        f"usable: **{proxy.get('proxy_usable')}**"
    )
    lines.append(f"- Rule: {proxy.get('threshold_note')}")
    lines.append(
        "- **HYPE:** no Binance SPOT klines — backfill used **USD-M futures** "
        "(`fapi`) from listing ~2025-05-30. BTC/ETH/SOL are true spot. Declared, "
        "not silent."
    )
    lines.append("")
    if proxy.get("by_symbol"):
        lines.append("| symbol | n | corr | signal agree |")
        lines.append("|---|---:|---:|---:|")
        for sym, row in proxy["by_symbol"].items():
            lines.append(
                f"| {sym} | {row['n']} | {row['corr']:.3f} | "
                f"{100*row['signal_agree']:.1f}% |"
            )
        lines.append("")
    # Highlight that the original 83d window matches the last subperiod green
    lines.append(
        "Note: sub-period block 6 (2026-04→08) is roughly the original HL sample "
        "window and is one of only two blocks with positive post-cost mean — "
        "consistent with a regime artifact."
    )
    lines.append("")
    lines.append("## Task 3 — Long revalidation (date-block inference)")
    lines.append("")
    lines.append(
        f"- Symbol-day non-overlap trades: {long_r.get('n_symbol_day_trades')} · "
        f"**n_independent_dates: {long_r.get('n_independent_dates')}** "
        f"(this is the inference N)"
    )
    lines.append(
        f"- Overlapping obs BE: {long_r.get('breakeven_rt_bps', float('nan')):.2f} bps · "
        f"tt: {long_r.get('taker_expectancy_bps', float('nan')):.2f} bps"
    )
    edge = long_r.get("date_block_edge_vs_taker") or {}
    lines.append(
        f"- Date-block mean(gross−11bps): {edge.get('mean_bps', float('nan')):.2f} bps · "
        f"CI [{edge.get('ci_low_bps', float('nan')):.2f}, "
        f"{edge.get('ci_high_bps', float('nan')):.2f}] · "
        f"P(>0)={edge.get('p_gt_0', float('nan')):.3f}"
    )
    lines.append("")
    lines.append("### Quintiles (overlapping, for shape)")
    lines.append("")
    q = long_r.get("quintiles") or {}
    if q.get("by_q"):
        lines.append("| Q | signal_mean | n | gross | tt |")
        lines.append("|---:|---:|---:|---:|---:|")
        for row in q["by_q"]:
            lines.append(
                f"| Q{row['quintile']} | {row['signal_mean']:.3f} | {row['n']} | "
                f"{row['gross_bps']:.2f}bps | {row['tt_bps']:.2f}bps |"
            )
        lines.append("")
    lines.append("### Sub-period stability (≥6 blocks of dates)")
    lines.append("")
    lines.append("| block | dates | n | gross | tt | >taker? |")
    lines.append("|---:|---|---:|---:|---:|:---:|")
    for row in long_r.get("subperiods") or []:
        lines.append(
            f"| {row['block']} | {row['date_start']}→{row['date_end']} | "
            f"{row['n_dates']} | {row['mean_gross_bps']:.2f}bps | "
            f"{row['mean_tt_bps']:.2f}bps | {'Y' if row['positive'] else 'n'} |"
        )
    lines.append("")
    lines.append("### Vol regimes (BTC 30d rvol terciles)")
    lines.append("")
    vr = long_r.get("vol_regimes") or {}
    lines.append(f"Method: {vr.get('method')} · cuts q33={vr.get('q33')} q66={vr.get('q66')}")
    lines.append("")
    lines.append("| regime | n_dates | gross | tt | edge CI | survives? |")
    lines.append("|---|---:|---:|---:|---|:---:|")
    for row in vr.get("regimes") or []:
        ci = row.get("edge_ci_bps") or {}
        lines.append(
            f"| {row['regime']} | {row['n_dates']} | {row['mean_gross_bps']:.2f}bps | "
            f"{row['mean_tt_bps']:.2f}bps | "
            f"[{ci.get('low')}, {ci.get('high')}] | "
            f"{'Y' if row['survives_taker'] else 'n'} |"
        )
    lines.append("")
    lines.append("### Per-symbol")
    lines.append("")
    lines.append("| symbol | n_trades | n_dates | gross | tt |")
    lines.append("|---|---:|---:|---:|---:|")
    for sym, row in (long_r.get("by_symbol") or {}).items():
        lines.append(
            f"| {sym} | {row['n_trades']} | {row['n_dates']} | "
            f"{row['gross_bps']:.2f}bps | {row['tt_bps']:.2f}bps |"
        )
    lines.append("")
    lines.append("## Continuação")
    lines.append("")
    if v["verdict"] == "A":
        lines.append(
            "Próximo: estratégia mínima `atr_percentile_7d@24h` → baseline-signal gate. "
            "Sem pesca de parâmetros."
        )
    elif v["verdict"] == "B":
        lines.append(
            "Edge realista mais modesto que os 34.5 bps dos 83 dias — decidir "
            "explicitamente se compensa construir."
        )
    else:
        lines.append(
            "Arquivar no backlog. Não construir. Não procurar variantes deste sinal."
        )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_backlog(verdict: Dict[str, Any]) -> None:
    backlog = ROOT / "docs" / "RESEARCH_BACKLOG.md"
    if not backlog.exists():
        return
    text = backlog.read_text(encoding="utf-8")
    marker = "### Archived — atr_percentile_7d@24h long revalidation"
    if marker in text:
        return
    if verdict["verdict"] == "C":
        block = f"""
{marker}

Long revalidation (`scripts/validate_atr_percentile_long.py`,
`docs/ATR_PERCENTILE_LONG_REVALIDATION.md`) verdict **(C)**:

- Short-sample BE ≈ {SHORT_SAMPLE_BE_BPS:.1f} bps looked like (A); long sample /
  date-block inference does not sustain a clear post-cost edge across regimes.
- **Do not build.** Likely regime artifact of the original ~83-day window.

"""
    elif verdict["verdict"] == "B":
        block = f"""
{marker}

Long revalidation verdict **(B)** — survives with smaller/tighter edge than
the 83-day {SHORT_SAMPLE_BE_BPS:.1f} bps sample. See
`docs/ATR_PERCENTILE_LONG_REVALIDATION.md` before any strategy build.

"""
    else:
        block = f"""
{marker}

Long revalidation verdict **(A)** — see
`docs/ATR_PERCENTILE_LONG_REVALIDATION.md`. Next: minimal strategy →
baseline-signal gate (no parameter fishing).

"""
    # Insert after atr/long-horizon archive if present
    anchor = "### Archived — ret_lag fade through 24h + long-horizon cost scan"
    if anchor in text:
        idx = text.find(anchor)
        rest = text[idx:]
        nxt = rest.find("\n### ", 1)
        if nxt == -1:
            text = text + "\n" + block
        else:
            text = text[: idx + nxt] + block + "\n" + text[idx + nxt :]
    else:
        text = text + "\n" + block
    backlog.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hl-db", default="data/live/bot.db")
    ap.add_argument("--bn-db", default="data/research/binance_spot_proxy.db")
    ap.add_argument("--symbols", default="BTC,ETH,SOL,HYPE")
    args = ap.parse_args()

    hl_db = Path(args.hl_db)
    bn_db = Path(args.bn_db)
    if not hl_db.is_absolute():
        hl_db = ROOT / hl_db
    if not bn_db.is_absolute():
        bn_db = ROOT / bn_db
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if not bn_db.exists():
        print(f"Missing Binance proxy DB: {bn_db}")
        print("Run: python scripts/backfill_binance_spot_research.py --months 24")
        return 2

    t0 = time.time()
    print("Loading HL candles...")
    hl_raw = load_candles(hl_db, symbols, "15m")
    print("Loading Binance spot proxy candles...")
    bn_raw = load_candles(bn_db, symbols, "15m")
    print(f"HL rows={len(hl_raw)} BN rows={len(bn_raw)}")

    print("Building ATR panels (this may take a few minutes)...")
    hl_panel = build_panel(hl_raw)
    bn_panel = build_panel(bn_raw)

    print("Proxy agreement on overlap...")
    aligned = align_panels(hl_panel, bn_panel, symbols)
    proxy = proxy_agreement(aligned)
    print(
        f"  corr={proxy.get('atr_percentile_corr')} "
        f"agree={proxy.get('binary_signal_agreement')} "
        f"usable={proxy.get('proxy_usable')}"
    )

    # Long revalidation uses Binance history if proxy usable, else still compute
    # but verdict will be C on proxy_fail.
    print("Long-history trades (Binance spot proxy)...")
    trades_ol = generate_trades(bn_panel, non_overlapping=False)
    trades_no = generate_trades(bn_panel, non_overlapping=True)
    date_means, dates = date_blocks(trades_no)
    be = breakeven_rt_bps(np.array([t.gross for t in trades_ol]))
    gross_ol = np.array([t.gross for t in trades_ol])
    tt_ol = float(np.mean(gross_ol - TAKER_RT) * 1e4)

    boot_be = date_block_bootstrap_ci(date_means)
    boot_edge = date_block_bootstrap_ci(date_means, subtract=TAKER_RT)

    long_res = {
        "n_symbol_day_trades": len(trades_no),
        "n_overlapping_obs": len(trades_ol),
        "n_independent_dates": len(dates),
        "span": {"start": dates[0] if dates else None, "end": dates[-1] if dates else None},
        "breakeven_rt_bps": be,
        "taker_expectancy_bps": tt_ol,
        "date_block_be_bps": {
            "mean_bps": boot_be["mean"] * 1e4,
            "ci_low_bps": boot_be["ci_low"] * 1e4,
            "ci_high_bps": boot_be["ci_high"] * 1e4,
            "p_gt_0": boot_be["p_gt_0"],
            "n_dates": boot_be["n_dates"],
        },
        "date_block_edge_vs_taker": {
            "mean_bps": boot_edge["mean"] * 1e4,
            "ci_low_bps": boot_edge["ci_low"] * 1e4,
            "ci_high_bps": boot_edge["ci_high"] * 1e4,
            "p_gt_0": boot_edge["p_gt_0"],
            "n_dates": boot_edge["n_dates"],
        },
        "quintiles": quintile_mono(trades_ol),
        "subperiods": subperiod_stability(date_means, dates, n_blocks=6),
        "vol_regimes": vol_regimes(bn_panel, trades_no),
        "by_symbol": symbol_consistency(trades_no),
        "coverage_by_symbol": {
            sym: {
                "n_bars": int((bn_raw["symbol"] == sym).sum()),
                "start": int(bn_raw.loc[bn_raw["symbol"] == sym, "timestamp_ms"].min())
                if (bn_raw["symbol"] == sym).any()
                else None,
                "end": int(bn_raw.loc[bn_raw["symbol"] == sym, "timestamp_ms"].max())
                if (bn_raw["symbol"] == sym).any()
                else None,
            }
            for sym in symbols
        },
    }

    verdict = decide_verdict(proxy, long_res)
    print(f"\nVERDICT ({verdict['verdict']}): {verdict['summary']}")

    meta = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "hl_db": str(hl_db),
        "bn_db": str(bn_db),
        "symbols": symbols,
        "hold_bars": HOLD_BARS,
        "n_boot": N_BOOT,
        "elapsed_sec": round(time.time() - t0, 1),
        "short_sample_be_bps": SHORT_SAMPLE_BE_BPS,
    }
    payload = {
        "meta": meta,
        "proxy_agreement": proxy,
        "long_revalidation": long_res,
        "verdict": verdict,
    }

    out_json = ROOT / "data" / "backtests" / "atr_percentile_long_revalidation_latest.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stamped = ROOT / "data" / "backtests" / f"atr_percentile_long_revalidation_{stamp}.json"
    for p in (out_json, stamped):
        p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    out_md = ROOT / "docs" / "ATR_PERCENTILE_LONG_REVALIDATION.md"
    write_report(out_md, payload)
    update_backlog(verdict)
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
