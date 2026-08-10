#!/usr/bin/env python3
"""Tape-native + real OIR feature screen (hyperliquid.db READ-ONLY).

Expectation (declared up front): flow features are short-horizon — high power
to find statistical IC, low chance to clear 11 bps. Run to close the space.

Usage:
  python scripts/feature_screening_tape_native.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "fs24", ROOT / "scripts" / "feature_screening_24m_candles.py"
)
_fs24 = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["fs24"] = _fs24
_spec.loader.exec_module(_fs24)
FDR_ALPHA = _fs24.FDR_ALPHA
spearman_ic_date_block = _fs24.spearman_ic_date_block

DB = ROOT / "data" / "research" / "hyperliquid.db"
OUT_JSON = ROOT / "data" / "backtests" / "feature_screening_tape_native.json"
OUT_DOC = ROOT / "docs" / "FEATURE_SCREENING_TAPE_NATIVE.md"
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]
HORIZONS = {"15m": 1, "1h": 4, "4h": 16}
TAKER_RT = 0.0011
MAKER_RT = 0.0003  # corrected 1.5 bps * 2
RNG_SEED = 42
BAR_MS = 15 * 60 * 1000
CONTROL_POS = "CONTROL_POS_leaky_forward"
CONTROL_NEGS = ("CONTROL_NEG_rand_a", "CONTROL_NEG_rand_b", "CONTROL_NEG_rand_c")
N_BOOT = 400

FEATURES = [
    "cvd_delta_15m",
    "cvd_delta_1h",
    "aggr_imbalance_15m",
    "mean_trade_size_usd",
    "trade_intensity",
    "oir",
    "oir_chg_15m",
    "oir_chg_1h",
]


def _bh_fdr(pvals: np.ndarray) -> np.ndarray:
    p = np.asarray(pvals, dtype=float)
    out = np.full_like(p, np.nan)
    mask = np.isfinite(p)
    if not mask.any():
        return out
    idx = np.where(mask)[0]
    order = idx[np.argsort(p[idx])]
    m = len(order)
    ranked = p[order]
    q = ranked * m / (np.arange(1, m + 1))
    q = np.minimum.accumulate(q[::-1])[::-1]
    out[order] = np.clip(q, 0, 1)
    return out


def open_ro(db: Path) -> sqlite3.Connection:
    uri = f"file:{db.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def load_tape_bars(con: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    """Aggregate trade_tape to 15m bars with true side."""
    # Stream in chunks by timestamp to avoid loading 7M rows at once
    q = """
        SELECT timestamp_ms, price, size, side
        FROM trade_tape
        WHERE symbol = ?
        ORDER BY timestamp_ms
    """
    # Use pandas read with chunksize
    chunks = []
    for chunk in pd.read_sql_query(q, con, params=[symbol], chunksize=500_000):
        chunks.append(chunk)
    if not chunks:
        return pd.DataFrame()
    df = pd.concat(chunks, ignore_index=True)
    side = df["side"].astype(str).str.upper()
    buy = side.isin(["B", "BUY"])
    sell = side.isin(["A", "SELL", "S"])
    notional = df["price"].astype(float) * df["size"].astype(float)
    signed = np.where(buy, notional, np.where(sell, -notional, 0.0))
    bar = (df["timestamp_ms"].astype(np.int64) // BAR_MS) * BAR_MS
    g = pd.DataFrame(
        {
            "bar_ms": bar,
            "signed": signed,
            "notional": notional,
            "n": 1,
            "buy_n": buy.astype(int),
            "sell_n": sell.astype(int),
            "close": df["price"].astype(float),
        }
    )
    agg = g.groupby("bar_ms", sort=True).agg(
        cvd_delta=("signed", "sum"),
        volume_usd=("notional", "sum"),
        n_trades=("n", "sum"),
        buy_n=("buy_n", "sum"),
        sell_n=("sell_n", "sum"),
        close=("close", "last"),
    )
    agg["mean_trade_size_usd"] = agg["volume_usd"] / agg["n_trades"].replace(0, np.nan)
    tot = agg["buy_n"] + agg["sell_n"]
    agg["aggr_imbalance_15m"] = (agg["buy_n"] - agg["sell_n"]) / tot.replace(0, np.nan)
    agg["trade_intensity"] = agg["n_trades"]  # per 15m bar
    agg["cvd_delta_15m"] = agg["cvd_delta"]
    agg["cvd_delta_1h"] = agg["cvd_delta"].rolling(4, min_periods=4).sum()
    agg = agg.reset_index().rename(columns={"bar_ms": "timestamp_ms"})
    agg["symbol"] = symbol
    return agg


def load_oir_bars(con: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    q = """
        SELECT timestamp_ms, mid_price, oir
        FROM l2_snapshots
        WHERE symbol = ?
        ORDER BY timestamp_ms
    """
    df = pd.read_sql_query(q, con, params=[symbol])
    if df.empty:
        return df
    bar = (df["timestamp_ms"].astype(np.int64) // BAR_MS) * BAR_MS
    g = df.assign(bar_ms=bar).groupby("bar_ms", sort=True).agg(
        oir=("oir", "last"),
        mid=("mid_price", "last"),
    )
    g["oir_chg_15m"] = g["oir"].diff(1)
    g["oir_chg_1h"] = g["oir"].diff(4)
    return g.reset_index().rename(columns={"bar_ms": "timestamp_ms"})


def build_panel(con: sqlite3.Connection) -> pd.DataFrame:
    rng = np.random.default_rng(RNG_SEED)
    pieces = []
    for sym in SYMBOLS:
        print(f"[{sym}] aggregating tape…", flush=True)
        tape = load_tape_bars(con, sym)
        print(f"[{sym}] aggregating OIR…", flush=True)
        oir = load_oir_bars(con, sym)
        if tape.empty:
            continue
        m = tape.merge(oir, on="timestamp_ms", how="left")
        # Prefer L2 mid for forwards when present
        close = m["mid"].fillna(m["close"]).astype(float)
        m["close"] = close
        ts = pd.to_datetime(m["timestamp_ms"], unit="ms", utc=True)
        m["date"] = ts.dt.strftime("%Y-%m-%d")
        for name, hb in HORIZONS.items():
            r = np.full(len(m), np.nan)
            c = close.to_numpy(dtype=float)
            if len(c) > hb:
                r[: len(c) - hb] = c[hb:] / c[: len(c) - hb] - 1.0
            m[f"fwd_{name}"] = r
        n = len(m)
        fwd = m["fwd_1h"].to_numpy(dtype=float)
        noise = rng.normal(
            0.0, np.nanstd(fwd) * 0.5 if np.isfinite(np.nanstd(fwd)) else 0.001, n
        )
        m[CONTROL_POS] = fwd + noise
        for cn in CONTROL_NEGS:
            m[cn] = rng.normal(size=n)
        pieces.append(m)
    return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()


def mono_spearman(feature: np.ndarray, forward: np.ndarray) -> float:
    mask = np.isfinite(feature) & np.isfinite(forward)
    if mask.sum() < 100:
        return float("nan")
    try:
        q = pd.qcut(feature[mask], 5, labels=False, duplicates="drop")
    except ValueError:
        return float("nan")
    means = [float(np.mean(forward[mask][q == qi])) for qi in sorted(set(q))]
    if len(means) < 3:
        return float("nan")
    rho, _ = stats.spearmanr(np.arange(len(means)), means)
    return float(rho) if np.isfinite(rho) else float("nan")


def block_stability(df: pd.DataFrame, feature: str, horizon: str) -> Dict[str, Any]:
    dates = sorted(df["date"].dropna().unique())
    if len(dates) < 12:
        return {"n_blocks": 0, "agree": 0}
    # Fewer blocks when only ~30 dates
    n_split = 3 if len(dates) < 60 else 6
    cuts = np.array_split(dates, n_split)
    ics = []
    for block in cuts:
        sub = df[df["date"].isin(block)]
        f = sub[feature].to_numpy(dtype=float)
        r = sub[f"fwd_{horizon}"].to_numpy(dtype=float)
        m = np.isfinite(f) & np.isfinite(r)
        if m.sum() < 40:
            continue
        rho, _ = stats.spearmanr(f[m], r[m])
        if np.isfinite(rho):
            ics.append(float(rho))
    if not ics:
        return {"n_blocks": 0, "agree": 0}
    sign = np.sign(np.median(ics))
    return {"n_blocks": len(ics), "agree": int(np.sum(np.sign(ics) == sign)), "ics": ics}


def cost_test(df: pd.DataFrame, feature: str, horizon: str, ic: float) -> Dict[str, Any]:
    sub = df.dropna(subset=[feature, f"fwd_{horizon}"]).copy()
    first = sub.groupby(["symbol", "date"], as_index=False).first()
    gross, dates = [], []
    for _, row in first.iterrows():
        sig = float(row[feature])
        if not np.isfinite(sig) or sig == 0:
            continue
        side = (1 if sig > 0 else -1) if ic > 0 else (-1 if sig > 0 else 1)
        g = float(row[f"fwd_{horizon}"]) * side
        if np.isfinite(g):
            gross.append(g)
            dates.append(row["date"])
    if len(gross) < 20:
        return {"n": len(gross), "be_bps": float("nan"), "clears_11": False}
    ga = np.asarray(gross)
    be = float(np.mean(ga) * 1e4)
    by: Dict[str, List[float]] = {}
    for d, g in zip(dates, ga):
        by.setdefault(str(d), []).append(float(g))
    dm = np.array([float(np.mean(v)) for v in by.values()])
    rng = np.random.default_rng(RNG_SEED)
    edge = dm - TAKER_RT
    boots = np.empty(min(N_BOOT, 400))
    for i in range(len(boots)):
        boots[i] = float(np.mean(edge[rng.integers(0, len(dm), size=len(dm))]))
    ci = (float(np.percentile(boots, 2.5) * 1e4), float(np.percentile(boots, 97.5) * 1e4))
    return {
        "n": int(len(ga)),
        "n_dates": int(len(dm)),
        "be_bps": be,
        "ci_bps": list(ci),
        "clears_11": bool(be > 11 and ci[0] > 0),
        "maker_net_bps": float(be - MAKER_RT * 1e4) if be >= 4 else None,
    }


def write_doc(path: Path, payload: Dict[str, Any]) -> None:
    lines = [
        "# Feature Screening — Tape-native CVD / aggression / real OIR",
        "",
        f"Generated: {payload['generated_at']}",
        f"DB (read-only): `{payload['db']}`",
        f"Span: {payload['date_min']} → {payload['date_max']} ({payload['n_dates']} dates)",
        "",
        "## Expectation (declared before results)",
        "",
        "Flow features live on **short horizons** where statistical power is high "
        "but retail taker costs (11 bps) are implacable. Prior candle `ret_lag@15m` "
        "BE was ≤0.95 bps. This screen expects statistical signal and unlikely "
        "tradable edge — run to **close the space**, not to find a winner.",
        "",
        "## Limitations",
        "",
    ]
    for lim in payload["limitations"]:
        lines.append(f"- {lim}")
    lines.append("")
    lines.append(f"## Verdict: **({payload['verdict']['code']})**")
    lines.append("")
    lines.append(payload["verdict"]["summary"])
    lines.append("")
    lines.append(payload["verdict"]["detail"])
    lines.append("")
    lines.append("## Survivors + cost")
    lines.append("")
    lines.append("| feature | h | IC | q_FDR | mono | BE bps | clears? |")
    lines.append("|---|---|---:|---:|---:|---:|:---:|")
    for r in payload["survivors"]:
        c = r.get("cost") or {}
        lines.append(
            f"| {r['feature']} | {r['horizon']} | {r['ic']:.4f} | {r['q_fdr']:.3f} | "
            f"{r['mono']:.2f} | {c.get('be_bps', float('nan')):.2f} | "
            f"{'Y' if c.get('clears_11') else 'n'} |"
        )
    if not payload["survivors"]:
        lines.append("_(none)_")
    lines.append("")
    lines.append("## Full ranking")
    lines.append("")
    lines.append("| feature | h | IC | p_date | q_FDR | mono | survives |")
    lines.append("|---|---|---:|---:|---:|---:|:---:|")
    for r in payload["rows"]:
        if r.get("is_control"):
            continue
        lines.append(
            f"| {r['feature']} | {r['horizon']} | {r['ic']:.4f} | {r['p_date']:.3g} | "
            f"{r.get('q_fdr', float('nan')):.3f} | {r['mono']:.2f} | "
            f"{'Y' if r.get('survives') else 'n'} |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-doc", type=Path, default=OUT_DOC)
    ap.add_argument("--n-boot", type=int, default=N_BOOT)
    args = ap.parse_args()

    con = open_ro(args.db)
    try:
        panel = build_panel(con)
    finally:
        con.close()
    if panel.empty:
        print("ERROR: empty panel", file=sys.stderr)
        return 2
    print(f"Panel bars={len(panel)} dates={panel['date'].nunique()}")

    # With ~30 dates, require agree on all available blocks (≥2/3) not 5/6
    min_blocks = 2
    min_agree = 2
    min_dates = 20

    rows: List[Dict[str, Any]] = []
    tests = [(f, h, False) for f in FEATURES for h in HORIZONS]
    for h in HORIZONS:
        tests.append((CONTROL_POS, h, True))
        for cn in CONTROL_NEGS:
            tests.append((cn, h, True))

    for feat, hor, is_ctrl in tests:
        if feat not in panel.columns:
            continue
        st = spearman_ic_date_block(
            panel[feat].to_numpy(dtype=float),
            panel[f"fwd_{hor}"].to_numpy(dtype=float),
            panel["date"].to_numpy(),
            HORIZONS[hor],
            n_boot=args.n_boot,
            seed=RNG_SEED,
        )
        mono = mono_spearman(
            panel[feat].to_numpy(dtype=float),
            panel[f"fwd_{hor}"].to_numpy(dtype=float),
        )
        blk = block_stability(panel, feat, hor)
        rows.append(
            {
                "feature": feat,
                "horizon": hor,
                "is_control": is_ctrl,
                "ic": st["ic"],
                "p_date": st["p_date_boot"],
                "n_bars": st["n_bars"],
                "n_dates": st["n_dates"],
                "mono": mono,
                "n_blocks": blk["n_blocks"],
                "blocks_agree": blk["agree"],
            }
        )

    cand_idx = [i for i, r in enumerate(rows) if not r["is_control"]]
    qvals = _bh_fdr(np.array([rows[i]["p_date"] for i in cand_idx], dtype=float))
    for j, i in enumerate(cand_idx):
        rows[i]["q_fdr"] = float(qvals[j]) if np.isfinite(qvals[j]) else float("nan")
        r = rows[i]
        r["survives"] = bool(
            np.isfinite(r["q_fdr"])
            and r["q_fdr"] <= FDR_ALPHA
            and np.isfinite(r["mono"])
            and abs(r["mono"]) >= 0.6
            and r["n_blocks"] >= min_blocks
            and r["blocks_agree"] >= min_agree
            and r["n_dates"] >= min_dates
        )
        r["cost"] = (
            cost_test(panel, r["feature"], r["horizon"], float(r["ic"]))
            if r["survives"]
            else None
        )

    survivors = [r for r in rows if r.get("survives")]
    survivors.sort(key=lambda x: abs(x["ic"] or 0), reverse=True)
    cost_pass = [s for s in survivors if s.get("cost") and s["cost"].get("clears_11")]

    if cost_pass:
        code = "A"
        summary = f"Tape/OIR survivors clear 11 bps: {[s['feature']+'@'+s['horizon'] for s in cost_pass]}"
        detail = "Unexpected — proceed carefully to minimal strategy + baseline gate."
    elif survivors:
        code = "C"
        summary = (
            f"{len(survivors)} statistical survivor(s); none clear 11 bps "
            "(as expected for short-horizon flow)."
        )
        detail = "; ".join(
            f"{s['feature']}@{s['horizon']} BE={s['cost']['be_bps']:.2f} "
            f"CI={s['cost']['ci_bps']}"
            for s in survivors
            if s.get("cost")
        )
    else:
        code = "C"
        summary = "No tape/OIR feature cleared statistical gates on this ~1m window."
        detail = "Space closed for candle-derived CVD reopen; real tape also no tradable survivor."

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(args.db),
        "n_bars": int(len(panel)),
        "n_dates": int(panel["date"].nunique()),
        "date_min": str(panel["date"].min()),
        "date_max": str(panel["date"].max()),
        "limitations": [
            "Only ~1 month of real tape/OIR — date-cluster n is modest; block gate relaxed to 2/3.",
            "CVD/aggression aggregated to 15m from trade_tape side B/A.",
            "OIR from l2_snapshots metrics (real), not candle proxy.",
            "Prior CVDOrderFlow used candle-derived volume — this closes the real-tape variant.",
            "Maker fee 1.5 bps/side (corrected); only referenced if BE≥4.",
        ],
        "expectation": "statistical possible; tradable unlikely",
        "rows": rows,
        "survivors": survivors,
        "verdict": {"code": code, "summary": summary, "detail": detail},
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(payload, indent=2, allow_nan=True), encoding="utf-8")
    write_doc(args.out_doc, payload)
    print(f"Wrote {args.out_json}")
    print(f"Wrote {args.out_doc}")
    print(f"VERDICT ({code}): {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
