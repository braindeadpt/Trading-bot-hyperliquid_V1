#!/usr/bin/env python3
"""Maker fill + adverse-selection cost test on 24m Binance spot proxy candles.

LIMITATION (declared): ``data/research/binance_spot_proxy.db`` is spot OHLC only —
no historical L2 / queue position. Fill models use price penetration of the next
15m bar only. Results are always reported as an INTERVAL [M1 .. M3], never a
single point estimate. M1 (naive touch) is an UPPER BOUND — never an estimate.

Frozen signal rules (no search / no tuning):
  * rvol_1h @ 4h  (IC > 0): side = +1 if rvol_1h > rolling 1d median else −1
  * ret_lag_15m @ 1h (IC < 0): side = −sign(ret_lag_15m)  # fade

Usage:
  python scripts/maker_fill_adverse_selection_24m.py
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_DEFAULT = ROOT / "data" / "research" / "binance_spot_proxy.db"
OUT_JSON = ROOT / "data" / "backtests" / "maker_fill_adverse_selection_24m.json"
OUT_DOC = ROOT / "docs" / "MAKER_FILL_ADVERSE_SELECTION_24M.md"

SYMBOLS_DEFAULT = ["BTC", "ETH", "SOL", "HYPE"]

MAKER_FEE = 0.0001  # 1 bps / side
TAKER_FEE = 0.00035
TAKER_SLIP = 0.0002
MAKER_RT = 2 * MAKER_FEE  # 2 bps optimistic maker/maker RT
MAKER_RT_MID = 0.0004  # 4 bps mid of "2–4 bps" HL maker RT band
TAKER_RT = 2 * (TAKER_FEE + TAKER_SLIP)  # 11 bps

# M3 penetration sweep (fraction)
M3_PENS_BPS = (2, 3, 4, 5)

# Adverse-selection horizons on 15m grid (1m/5m unavailable).
# Offset 0 = close of the fill bar vs limit (intra-bar markout after penetration).
AS_HORIZONS = (
    ("fill_bar", 0),
    ("15m", 1),
    ("1h", 4),
)


@dataclass(frozen=True)
class Spec:
    name: str
    feature: str
    hold_bars: int
    hold_name: str
    screening_be_bps: float
    rule: str


SPECS: Tuple[Spec, ...] = (
    Spec(
        name="rvol_1h@4h",
        feature="rvol_1h",
        hold_bars=16,
        hold_name="4h",
        screening_be_bps=6.81,
        rule="side=+1 if rvol_1h>rolling_median_96 else -1 (positive IC)",
    ),
    Spec(
        name="ret_lag_15m@1h",
        feature="ret_lag_15m",
        hold_bars=4,
        hold_name="1h",
        screening_be_bps=5.56,
        rule="side=-sign(ret_lag_15m) fade (negative IC)",
    ),
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
    pieces: List[pd.DataFrame] = []
    for sym, g0 in raw.groupby("symbol", sort=False):
        g = g0.sort_values("timestamp_ms").reset_index(drop=True)
        close = g["close"].astype(float)
        high = g["high"].astype(float)
        low = g["low"].astype(float)
        logret = np.log(close / close.shift(1))
        rvol_1h = logret.rolling(4, min_periods=4).std(ddof=0) * math.sqrt(4)
        ret_lag_15m = close.pct_change(1)
        rvol_med = rvol_1h.rolling(96, min_periods=24).median()
        pieces.append(
            pd.DataFrame(
                {
                    "symbol": sym,
                    "timestamp_ms": g["timestamp_ms"].to_numpy(),
                    "open": g["open"].astype(float).to_numpy(),
                    "high": high.to_numpy(),
                    "low": low.to_numpy(),
                    "close": close.to_numpy(),
                    "rvol_1h": rvol_1h.to_numpy(),
                    "rvol_median_1d": rvol_med.to_numpy(),
                    "ret_lag_15m": ret_lag_15m.to_numpy(),
                }
            )
        )
    return pd.concat(pieces, ignore_index=True)


def _safe_mean(x: np.ndarray) -> float:
    y = x[np.isfinite(x)]
    return float(np.mean(y)) if len(y) else float("nan")


def _safe_std(x: np.ndarray) -> float:
    y = x[np.isfinite(x)]
    return float(np.std(y, ddof=1)) if len(y) > 1 else float("nan")


def simulate_spec(
    panel: pd.DataFrame,
    spec: Spec,
    *,
    non_overlapping: bool,
) -> Dict[str, Any]:
    """Simulate all fill models for one frozen signal spec (numpy-fast path)."""
    models: List[Tuple[str, float]] = [
        ("M1_touch", 0.0),
        ("M2_pen_1bps", 1.0),
    ]
    for p in M3_PENS_BPS:
        models.append((f"M3_pen_{p}bps", float(p)))

    max_as = max(k for _, k in AS_HORIZONS)
    h = spec.hold_bars

    # Flatten candidates across symbols
    sides: List[int] = []
    limits: List[float] = []
    high_next: List[float] = []
    low_next: List[float] = []
    gross_taker: List[float] = []
    gross_maker: List[float] = []
    as_cols: Dict[str, List[float]] = {a: [] for a, _ in AS_HORIZONS}

    for _, g0 in panel.groupby("symbol", sort=False):
        g = g0.reset_index(drop=True)
        close = g["close"].to_numpy(dtype=float)
        high = g["high"].to_numpy(dtype=float)
        low = g["low"].to_numpy(dtype=float)
        n = len(g)
        if spec.feature == "rvol_1h":
            rv = g["rvol_1h"].to_numpy(dtype=float)
            med = g["rvol_median_1d"].to_numpy(dtype=float)
            side_arr = np.zeros(n, dtype=np.int8)
            ok = np.isfinite(rv) & np.isfinite(med) & (rv != med)
            side_arr[ok & (rv > med)] = 1
            side_arr[ok & (rv < med)] = -1
        else:
            lag = g["ret_lag_15m"].to_numpy(dtype=float)
            side_arr = np.zeros(n, dtype=np.int8)
            ok = np.isfinite(lag) & (lag != 0.0)
            side_arr[ok & (lag > 0)] = -1
            side_arr[ok & (lag < 0)] = 1

        last_i = n - 1 - (1 + h + max_as)
        if last_i < 0:
            continue
        idxs = np.arange(0, last_i + 1, dtype=np.int64)
        if non_overlapping:
            kept: List[int] = []
            next_free = 0
            for i in idxs:
                if i < next_free:
                    continue
                if side_arr[i] == 0:
                    continue
                kept.append(int(i))
                next_free = int(i + h)
            idxs = np.asarray(kept, dtype=np.int64)
        else:
            idxs = idxs[side_arr[idxs] != 0]

        if len(idxs) == 0:
            continue

        fill_i = idxs + 1
        exit_i = fill_i + h
        lim = close[idxs]
        sd = side_arr[idxs].astype(int)
        valid = (lim > 0) & np.isfinite(lim) & np.isfinite(close[exit_i]) & (close[exit_i] > 0)
        # taker exit at i+h (may equal fill path length)
        taker_exit = close[idxs + h]
        valid &= np.isfinite(taker_exit) & (taker_exit > 0)
        idxs = idxs[valid]
        fill_i = fill_i[valid]
        exit_i = exit_i[valid]
        lim = lim[valid]
        sd = sd[valid]
        taker_exit = taker_exit[valid]

        sides.extend(sd.tolist())
        limits.extend(lim.tolist())
        high_next.extend(high[fill_i].tolist())
        low_next.extend(low[fill_i].tolist())
        gross_taker.extend((sd * (taker_exit / lim - 1.0)).tolist())
        gross_maker.extend((sd * (close[exit_i] / lim - 1.0)).tolist())
        for as_name, kb in AS_HORIZONS:
            px = close[fill_i + kb]
            as_cols[as_name].extend((-sd * (px / lim - 1.0) * 1e4).tolist())

    n_signals = len(sides)
    side_a = np.asarray(sides, dtype=int)
    lim_a = np.asarray(limits, dtype=float)
    hi_a = np.asarray(high_next, dtype=float)
    lo_a = np.asarray(low_next, dtype=float)
    g_taker = np.asarray(gross_taker, dtype=float)
    g_maker = np.asarray(gross_maker, dtype=float)
    as_arr = {k: np.asarray(v, dtype=float) for k, v in as_cols.items()}

    model_rows: Dict[str, Any] = {}
    for mid, pen in models:
        if n_signals == 0:
            fill_mask = np.zeros(0, dtype=bool)
        else:
            pen_f = pen * 1e-4
            long_m = (side_a > 0) & np.isfinite(lo_a) & (lo_a <= lim_a * (1.0 - pen_f))
            short_m = (side_a < 0) & np.isfinite(hi_a) & (hi_a >= lim_a * (1.0 + pen_f))
            fill_mask = long_m | short_m
        g = g_maker[fill_mask]
        fill_rate = float(fill_mask.mean()) if n_signals else float("nan")
        mean_gross = _safe_mean(g)
        be_bps = float(mean_gross * 1e4) if np.isfinite(mean_gross) else float("nan")
        as_summary = {
            a: {
                "mean_bps": _safe_mean(as_arr[a][fill_mask]) if n_signals else float("nan"),
                "std_bps": _safe_std(as_arr[a][fill_mask]) if n_signals else float("nan"),
                "n": int(fill_mask.sum()),
            }
            for a in as_arr
        }
        as_fill = as_summary["fill_bar"]["mean_bps"]
        as_15 = as_summary["15m"]["mean_bps"]
        as_1h = as_summary["1h"]["mean_bps"]

        def _nets(as_bps: float, maker_rt: float) -> Dict[str, float]:
            if not np.isfinite(mean_gross):
                return {
                    "net_fee_only_bps": float("nan"),
                    "net_fee_minus_as_bps": float("nan"),
                    "maker_rt_bps": maker_rt * 1e4,
                    "as_subtracted_bps": as_bps,
                }
            fee_net = mean_gross * 1e4 - maker_rt * 1e4
            adj = fee_net - (as_bps if np.isfinite(as_bps) else 0.0)
            return {
                "net_fee_only_bps": float(fee_net),
                "net_fee_minus_as_bps": float(adj),
                "maker_rt_bps": float(maker_rt * 1e4),
                "as_subtracted_bps": float(as_bps) if np.isfinite(as_bps) else float("nan"),
            }

        model_rows[mid] = {
            "penetration_bps": pen,
            "n_signals": n_signals,
            "n_fills": int(fill_mask.sum()),
            "fill_rate": fill_rate,
            "mean_gross_bps": float(mean_gross * 1e4) if np.isfinite(mean_gross) else float("nan"),
            "breakeven_rt_bps": be_bps,
            "hit_rate_gross": float(np.mean(g > 0)) if len(g) else float("nan"),
            "adverse_selection": as_summary,
            "maker_rt_2bps": _nets(as_fill, MAKER_RT),
            "maker_rt_4bps": _nets(as_fill, MAKER_RT_MID),
            "maker_rt_2bps_as15m": _nets(as_15, MAKER_RT),
            "maker_rt_2bps_as1h": _nets(as_1h, MAKER_RT),
            "fee_savings_vs_taker_2bps_rt": float((TAKER_RT - MAKER_RT) * 1e4),
            "fee_savings_vs_taker_4bps_rt": float((TAKER_RT - MAKER_RT_MID) * 1e4),
            "as_exceeds_savings_fill_bar_vs_2bps": bool(
                np.isfinite(as_fill) and as_fill > (TAKER_RT - MAKER_RT) * 1e4
            ),
            "as_exceeds_savings_fill_bar_vs_4bps": bool(
                np.isfinite(as_fill) and as_fill > (TAKER_RT - MAKER_RT_MID) * 1e4
            ),
            "as_exceeds_savings_15m_vs_2bps": bool(
                np.isfinite(as_fill) and as_fill > (TAKER_RT - MAKER_RT) * 1e4
            ),
            "as_exceeds_savings_15m_vs_4bps": bool(
                np.isfinite(as_fill) and as_fill > (TAKER_RT - MAKER_RT_MID) * 1e4
            ),
        }

    m1 = model_rows["M1_touch"]
    m2 = model_rows["M2_pen_1bps"]
    m3_keys = [f"M3_pen_{p}bps" for p in M3_PENS_BPS]
    m3_worst_key = min(
        m3_keys,
        key=lambda k: (
            model_rows[k]["maker_rt_2bps"]["net_fee_minus_as_bps"]
            if np.isfinite(model_rows[k]["maker_rt_2bps"]["net_fee_minus_as_bps"])
            else -1e18
        ),
    )
    m3w = model_rows[m3_worst_key]

    def _survives(row: Dict[str, Any]) -> bool:
        adj = row["maker_rt_2bps"]["net_fee_minus_as_bps"]
        if not np.isfinite(adj) or adj <= 0:
            return False
        if row["as_exceeds_savings_fill_bar_vs_2bps"]:
            return False
        if row["n_fills"] < 30:
            return False
        return True

    m2_ok = _survives(m2)
    m3_all_ok = all(_survives(model_rows[k]) for k in m3_keys)
    m1_ok = _survives(m1)

    if m2_ok and m3_all_ok:
        verdict = "A"
        verdict_note = (
            "Survives M2 and all M3 (2–5 bps) with fill-bar AS measured and "
            "subtracted at maker RT 2 bps; AS does not exceed fee savings."
        )
    elif m1_ok or m2_ok:
        verdict = "B"
        verdict_note = (
            "Survives only at optimistic M1 and/or M2 — UPPER BOUND / soft band only. "
            "Not authorization to build."
        )
    else:
        verdict = "C"
        verdict_note = (
            "Does not survive M2∩M3 with measured AS subtracted. Maker execution "
            "does not rescue the signal; screening verdict (C) confirmed for maker too."
        )

    return {
        "spec": asdict(spec),
        "non_overlapping": non_overlapping,
        "n_signals": n_signals,
        "taker_path": {
            "mean_gross_bps": float(_safe_mean(g_taker) * 1e4),
            "breakeven_rt_bps": float(_safe_mean(g_taker) * 1e4),
            "screening_be_bps_ref": spec.screening_be_bps,
            "net_vs_11bps": float(_safe_mean(g_taker) * 1e4 - 11.0),
        },
        "models": model_rows,
        "interval_M1_to_M3": {
            "fill_rate": [m1["fill_rate"], m3w["fill_rate"]],
            "mean_gross_bps": [m1["mean_gross_bps"], m3w["mean_gross_bps"]],
            "breakeven_rt_bps": [m1["breakeven_rt_bps"], m3w["breakeven_rt_bps"]],
            "net_fee_minus_as_2bps_rt": [
                m1["maker_rt_2bps"]["net_fee_minus_as_bps"],
                m3w["maker_rt_2bps"]["net_fee_minus_as_bps"],
            ],
            "as_fill_bar_bps": [
                m1["adverse_selection"]["fill_bar"]["mean_bps"],
                m3w["adverse_selection"]["fill_bar"]["mean_bps"],
            ],
            "as_15m_bps": [
                m1["adverse_selection"]["15m"]["mean_bps"],
                m3w["adverse_selection"]["15m"]["mean_bps"],
            ],
            "m3_worst_model": m3_worst_key,
        },
        "survival": {
            "M1": m1_ok,
            "M2": m2_ok,
            "M3_all": m3_all_ok,
            "M3_by_pen": {k: _survives(model_rows[k]) for k in m3_keys},
        },
        "verdict": verdict,
        "verdict_note": verdict_note,
    }


def side_for_row(spec: Spec, row: pd.Series) -> int:
    """Kept for tests / ad-hoc checks; hot path uses vectorized sides."""
    if spec.feature == "rvol_1h":
        rv = float(row["rvol_1h"])
        med = float(row["rvol_median_1d"])
        if not np.isfinite(rv) or not np.isfinite(med) or rv == med:
            return 0
        return 1 if rv > med else -1
    s = float(row["ret_lag_15m"])
    if not np.isfinite(s) or s == 0.0:
        return 0
    return -1 if s > 0 else 1


def penetrates(
    side: int,
    limit: float,
    high: float,
    low: float,
    pen_bps: float,
) -> bool:
    """OHLC penetration of resting limit. pen_bps=0 → naive touch."""
    if limit <= 0 or not np.isfinite(limit):
        return False
    if side > 0:
        thresh = limit * (1.0 - pen_bps * 1e-4)
        return bool(np.isfinite(low) and low <= thresh)
    thresh = limit * (1.0 + pen_bps * 1e-4)
    return bool(np.isfinite(high) and high >= thresh)


def write_report(path: Path, payload: Dict[str, Any]) -> None:
    lines: List[str] = []
    lines.append("# Maker Fill + Adverse Selection — 24m candle proxy")
    lines.append("")
    lines.append(f"Generated: {payload['generated_at']}")
    lines.append(f"DB: `{payload['db']}`")
    lines.append(f"Symbols: {', '.join(payload['symbols'])}")
    lines.append(
        f"Bars: {payload['n_bars']:,} · dates ≈ {payload['n_dates']} · "
        f"{payload['date_min']} → {payload['date_max']}"
    )
    lines.append("")
    lines.append("## Limitations (declared)")
    lines.append("")
    lines.append(
        "- Data are **Binance spot 15m OHLC** — **no L2 / queue**. Fill models "
        "use next-bar price penetration only."
    )
    lines.append(
        "- **M1 (naive touch)** is an **UPPER BOUND**, never an estimate."
    )
    lines.append(
        "- Adverse selection: **fill-bar close**, **+15m**, **+1h** "
        "(1m/5m not observable on this grid)."
    )
    lines.append(
        "- Subtracting fill-bar AS from full-hold gross is **conservative** "
        "(partial double-count of the start of the path)."
    )
    lines.append(
        "- Maker RT band: **2 bps** (1+1) primary; **4 bps** sensitivity. "
        "Taker RT reference remains **11 bps**."
    )
    lines.append(
        "- Results always as **interval [M1 .. M3_worst]** — never a single point."
    )
    lines.append("")
    lines.append("## Fill models")
    lines.append("")
    lines.append("| Model | Rule |")
    lines.append("|---|---|")
    lines.append("| M1 | Next bar touches limit (high/low) |")
    lines.append("| M2 | Penetration ≥ **1 bps** beyond limit |")
    lines.append("| M3 | Penetration ≥ **2,3,4,5 bps** (sweep) |")
    lines.append("")
    lines.append(
        "Fill price = resting limit at `close[t]` (not the bar extreme). "
        "Hold starts at fill bar `t+1`, exit at `close[t+1+H]`."
    )
    lines.append("")
    lines.append("## Frozen signal rules")
    lines.append("")
    for spec in SPECS:
        lines.append(f"- **{spec.name}:** {spec.rule}; hold={spec.hold_name}")
    lines.append("")
    lines.append(
        "Note: the 24m screening taker cost test for `rvol_1h` used "
        "`sign(feature)` with always-positive rvol → effectively always-long "
        f"(BE {SPECS[0].screening_be_bps} bps). This maker test uses a "
        "**median-split** so the feature actually selects side. Taker-path BE "
        "on that rule is reported separately."
    )
    lines.append("")
    lines.append("## Verdict rules")
    lines.append("")
    lines.append(
        "- **(A)** Survives **M2 and all M3** with fill-bar AS subtracted at 2 bps "
        "maker RT, AS ≤ fee savings (11−2=9 bps), n_fills≥30."
    )
    lines.append(
        "- **(B)** Survives only M1 and/or M2 → optimistic upper bound; do not build."
    )
    lines.append(
        "- **(C)** Does not survive → screening (C) confirmed under maker too."
    )
    lines.append("")

    overall = payload["overall_verdict"]
    lines.append(f"## Overall verdict: **({overall['code']})**")
    lines.append("")
    lines.append(overall["note"])
    lines.append("")

    for block_name, key in (
        ("Overlapping (every signal bar)", "overlapping"),
        ("Non-overlapping (step by hold)", "non_overlapping"),
    ):
        lines.append(f"## Results — {block_name}")
        lines.append("")
        for res in payload[key]:
            name = res["spec"]["name"]
            iv = res["interval_M1_to_M3"]
            lines.append(f"### {name} — verdict **({res['verdict']})**")
            lines.append("")
            lines.append(res["verdict_note"])
            lines.append("")
            lines.append(f"- Signals: **{res['n_signals']:,}**")
            tp = res["taker_path"]
            lines.append(
                f"- Taker-path BE (this rule): **{tp['breakeven_rt_bps']:.2f} bps** "
                f"(screening ref {tp['screening_be_bps_ref']:.2f}; "
                f"net vs 11 bps = {tp['net_vs_11bps']:.2f})"
            )
            lines.append(
                f"- Interval fill rate M1→M3: "
                f"**[{iv['fill_rate'][0]*100:.1f}% .. {iv['fill_rate'][1]*100:.1f}%]**"
            )
            lines.append(
                f"- Interval gross BE M1→M3: "
                f"**[{iv['breakeven_rt_bps'][0]:.2f} .. {iv['breakeven_rt_bps'][1]:.2f}] bps**"
            )
            lines.append(
                f"- Interval net (2 bps RT − fill-bar AS) M1→M3: "
                f"**[{iv['net_fee_minus_as_2bps_rt'][0]:.2f} .. "
                f"{iv['net_fee_minus_as_2bps_rt'][1]:.2f}] bps**"
            )
            lines.append(
                f"- Interval AS fill-bar M1→M3: "
                f"**[{iv['as_fill_bar_bps'][0]:.2f} .. {iv['as_fill_bar_bps'][1]:.2f}] bps** "
                f"(fee savings at 2 bps RT = 9.0 bps)"
            )
            lines.append(
                f"- Interval AS +15m M1→M3: "
                f"**[{iv['as_15m_bps'][0]:.2f} .. {iv['as_15m_bps'][1]:.2f}] bps**"
            )
            lines.append("")
            lines.append(
                "| model | pen | fill% | n_fills | gross BE | "
                "AS fill | AS +15m | AS +1h | net−AS @2bps | net−AS @4bps | AS>sav? |"
            )
            lines.append(
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|"
            )
            for mid, row in res["models"].items():
                as0 = row["adverse_selection"]["fill_bar"]["mean_bps"]
                as15 = row["adverse_selection"]["15m"]["mean_bps"]
                as1h = row["adverse_selection"]["1h"]["mean_bps"]
                n2 = row["maker_rt_2bps"]["net_fee_minus_as_bps"]
                n4 = row["maker_rt_4bps"]["net_fee_minus_as_bps"]
                lines.append(
                    f"| {mid} | {row['penetration_bps']:.0f} | "
                    f"{row['fill_rate']*100:.1f}% | {row['n_fills']} | "
                    f"{row['breakeven_rt_bps']:.2f} | "
                    f"{as0:.2f} | {as15:.2f} | {as1h:.2f} | "
                    f"{n2:.2f} | {n4:.2f} | "
                    f"{'Y' if row['as_exceeds_savings_fill_bar_vs_2bps'] else 'n'} |"
                )
            lines.append("")
            surv = res["survival"]
            lines.append(
                f"Survival flags: M1={surv['M1']} M2={surv['M2']} "
                f"M3_all={surv['M3_all']} {surv['M3_by_pen']}"
            )
            lines.append("")

    lines.append("## Fee savings vs adverse selection")
    lines.append("")
    lines.append(
        "Taker RT 11 bps → maker 2 bps saves **9 bps**; maker 4 bps saves **7 bps**. "
        "If measured AS (15m markout against the fill) exceeds that saving, "
        "maker fees do not fix economics — adverse selection ate the rebate."
    )
    lines.append("")
    lines.append("## Conclusion")
    lines.append("")
    lines.append(overall["conclusion"])
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DB_DEFAULT)
    ap.add_argument("--symbols", default=",".join(SYMBOLS_DEFAULT))
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-doc", type=Path, default=OUT_DOC)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    raw = load_candles_15m(args.db, symbols)
    if raw.empty:
        print("ERROR: no candles", file=sys.stderr)
        return 2
    panel = build_panel(raw)
    ts = pd.to_datetime(panel["timestamp_ms"], unit="ms", utc=True)
    dates = ts.dt.strftime("%Y-%m-%d")

    overlapping = [
        simulate_spec(panel, spec, non_overlapping=False) for spec in SPECS
    ]
    non_overlapping = [
        simulate_spec(panel, spec, non_overlapping=True) for spec in SPECS
    ]

    # Primary verdict from overlapping sample (power); non-overlap is deployment check
    codes = [r["verdict"] for r in overlapping]
    # Worst of primary + secondary features
    rank = {"A": 0, "B": 1, "C": 2}
    worst = max(codes, key=lambda c: rank[c])
    primary = overlapping[0]
    secondary = overlapping[1]

    if worst == "A":
        note = (
            f"Primary {primary['spec']['name']} and secondary "
            f"{secondary['spec']['name']} both clear M2∩M3 with AS subtracted."
        )
        conclusion = (
            "First maker-viable signal class under this OHLC fill model. "
            "Next: minimal maker-first strategy + baseline-signal gate "
            "(not done in this measurement script)."
        )
    elif worst == "B":
        note = (
            "At least one survivor clears only the optimistic band (M1/M2). "
            "Treat as upper bound; do not build."
        )
        conclusion = (
            "Optimistic maker looks soft-positive in places, but M3 and/or "
            "measured adverse selection block a build decision."
        )
    else:
        note = (
            f"Primary `{primary['spec']['name']}` verdict ({primary['verdict']}); "
            f"secondary `{secondary['spec']['name']}` verdict ({secondary['verdict']}). "
            "Neither clears M2∩M3 with AS subtracted on the overlapping sample."
        )
        conclusion = (
            "Maker execution does **not** rescue these directional candle signals. "
            "Screening verdict **(C)** is confirmed for maker as well; the "
            "directional candle-feature family is **FINAL** under this cost model. "
            "The fill/AS machinery remains useful for future MM research."
        )

    payload: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(args.db.resolve()),
        "symbols": symbols,
        "n_bars": int(len(panel)),
        "n_dates": int(dates.nunique()),
        "date_min": str(dates.min()),
        "date_max": str(dates.max()),
        "limitations": [
            "spot_ohlc_no_l2",
            "penetration_fill_only",
            "as_horizons_15m_1h_only",
            "m1_is_upper_bound",
            "as_subtract_partially_double_counts_first_bar",
        ],
        "costs": {
            "maker_rt_bps_primary": 2.0,
            "maker_rt_bps_mid": 4.0,
            "taker_rt_bps": 11.0,
            "fee_savings_2bps": 9.0,
            "fee_savings_4bps": 7.0,
        },
        "overlapping": overlapping,
        "non_overlapping": non_overlapping,
        "overall_verdict": {
            "code": worst,
            "note": note,
            "conclusion": conclusion,
            "primary": primary["spec"]["name"],
            "primary_verdict": primary["verdict"],
            "secondary": secondary["spec"]["name"],
            "secondary_verdict": secondary["verdict"],
        },
    }

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(payload, indent=2, allow_nan=True), encoding="utf-8"
    )
    write_report(args.out_doc, payload)

    print(f"Wrote {args.out_json}")
    print(f"Wrote {args.out_doc}")
    print(
        f"OVERALL ({worst}): primary={primary['verdict']} "
        f"secondary={secondary['verdict']}"
    )
    for r in overlapping:
        iv = r["interval_M1_to_M3"]
        print(
            f"  {r['spec']['name']} ({r['verdict']}): "
            f"net−AS@2bps [{iv['net_fee_minus_as_2bps_rt'][0]:.2f} .. "
            f"{iv['net_fee_minus_as_2bps_rt'][1]:.2f}] | "
            f"AS_fill [{iv['as_fill_bar_bps'][0]:.2f} .. {iv['as_fill_bar_bps'][1]:.2f}]"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
