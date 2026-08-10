#!/usr/bin/env python3
"""OI / positioning feature screen after Bybit OI backfill (research only).

Uses Bybit OI (CEX proxy, declared) + Binance spot 15m prices for forward
returns (long history). Date-cluster bootstrap, FDR, mono, cost test @ 11 bps.

Usage:
  python scripts/feature_screening_oi_positioning.py
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Reuse date-cluster IC from 24m screening module
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "fs24", ROOT / "scripts" / "feature_screening_24m_candles.py"
)
_fs24 = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["fs24"] = _fs24
_spec.loader.exec_module(_fs24)
FDR_ALPHA = _fs24.FDR_ALPHA
spearman_ic_date_block = _fs24.spearman_ic_date_block
spearman_ic_hac = _fs24.spearman_ic_hac

OI_DB = ROOT / "data" / "research" / "hyperliquid.db"
PX_DB = ROOT / "data" / "research" / "binance_spot_proxy.db"
OUT_JSON = ROOT / "data" / "backtests" / "feature_screening_oi_positioning.json"
OUT_DOC = ROOT / "docs" / "FEATURE_SCREENING_OI_POSITIONING.md"

SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]
HORIZONS = {"15m": 1, "1h": 4, "4h": 16, "24h": 96}
TAKER_RT = 0.0011
MAKER_RT = 0.0003  # 1.5 bps × 2 (corrected HL tier-0)
RNG_SEED = 42
CONTROL_POS = "CONTROL_POS_leaky_forward"
CONTROL_NEGS = ("CONTROL_NEG_rand_a", "CONTROL_NEG_rand_b", "CONTROL_NEG_rand_c")
N_BOOT = 400


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


def load_oi(db: Path, symbols: Sequence[str]) -> pd.DataFrame:
    uri = f"file:{db.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        q = f"""
            SELECT symbol, timestamp AS timestamp_ms, oi_total, oi_delta, source, venue
            FROM oi_history
            WHERE symbol IN ({",".join("?" * len(symbols))})
            ORDER BY symbol, timestamp
        """
        return pd.read_sql_query(q, con, params=list(symbols))
    finally:
        con.close()


def load_px(db: Path, symbols: Sequence[str]) -> pd.DataFrame:
    uri = f"file:{db.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
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


def build_panel(oi: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    pieces: List[pd.DataFrame] = []
    rng = np.random.default_rng(RNG_SEED)
    for sym in SYMBOLS:
        p = px[px["symbol"] == sym].sort_values("timestamp_ms").reset_index(drop=True)
        o = oi[oi["symbol"] == sym].sort_values("timestamp_ms").reset_index(drop=True)
        if p.empty or o.empty:
            continue
        # Normalize OI timestamps to ms
        ots = o["timestamp_ms"].astype(np.int64).to_numpy()
        if ots.max() < 10_000_000_000:
            ots = ots * 1000
            o = o.copy()
            o["timestamp_ms"] = ots
        merged = pd.merge_asof(
            p,
            o[["timestamp_ms", "oi_total"]].rename(columns={"timestamp_ms": "oi_ts"}),
            left_on="timestamp_ms",
            right_on="oi_ts",
            direction="backward",
            tolerance=3 * 3600 * 1000,
        )
        close = merged["close"].astype(float)
        vol = merged["volume"].astype(float)
        oi_v = merged["oi_total"].astype(float)
        # Hourly OI → resampled onto 15m grid via asof; deltas in bar units
        oi_d1 = oi_v.pct_change(4)  # ~1h
        oi_d4 = oi_v.pct_change(16)  # ~4h
        oi_d24 = oi_v.pct_change(96)  # ~24h
        oi_accel = oi_d1 - oi_d1.shift(4)
        # z-score of OI level over 7d
        mu = oi_v.rolling(96 * 7, min_periods=96).mean()
        sd = oi_v.rolling(96 * 7, min_periods=96).std(ddof=0)
        oi_z = (oi_v - mu) / sd.replace(0, np.nan)
        ret_1 = close.pct_change(1)
        # divergence: OI rising while price falling (and vice versa)
        oi_price_div = np.sign(oi_d1) * (-np.sign(ret_1.rolling(4).sum()))
        oi_price_div = oi_price_div.where(oi_d1.abs() > 0)
        # OI change relative to volume (activity-normalized)
        oi_rel_vol = oi_d1 / (vol.replace(0, np.nan) / vol.rolling(96, min_periods=12).mean())

        ts = pd.to_datetime(merged["timestamp_ms"], unit="ms", utc=True)
        feat = pd.DataFrame(
            {
                "symbol": sym,
                "timestamp_ms": merged["timestamp_ms"].to_numpy(),
                "date": ts.dt.strftime("%Y-%m-%d").to_numpy(),
                "close": close.to_numpy(),
                "oi_delta_1h": oi_d1.to_numpy(),
                "oi_delta_4h": oi_d4.to_numpy(),
                "oi_delta_24h": oi_d24.to_numpy(),
                "oi_accel_1h": oi_accel.to_numpy(),
                "oi_z_7d": oi_z.to_numpy(),
                "oi_price_div_1h": oi_price_div.to_numpy(dtype=float),
                "oi_rel_volume_1h": oi_rel_vol.to_numpy(),
            }
        )
        for name, hb in HORIZONS.items():
            r = np.full(len(feat), np.nan)
            c = close.to_numpy(dtype=float)
            if len(c) > hb:
                r[: len(c) - hb] = c[hb:] / c[: len(c) - hb] - 1.0
            feat[f"fwd_{name}"] = r
        n = len(feat)
        fwd = feat["fwd_1h"].to_numpy(dtype=float)
        noise = rng.normal(0.0, np.nanstd(fwd) * 0.5 if np.isfinite(np.nanstd(fwd)) else 0.001, n)
        feat[CONTROL_POS] = fwd + noise
        for cn in CONTROL_NEGS:
            feat[cn] = rng.normal(size=n)
        pieces.append(feat)
    return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()


FEATURES = [
    "oi_delta_1h",
    "oi_delta_4h",
    "oi_delta_24h",
    "oi_accel_1h",
    "oi_z_7d",
    "oi_price_div_1h",
    "oi_rel_volume_1h",
]


def mono_spearman(feature: np.ndarray, forward: np.ndarray) -> float:
    mask = np.isfinite(feature) & np.isfinite(forward)
    if mask.sum() < 100:
        return float("nan")
    try:
        q = pd.qcut(feature[mask], 5, labels=False, duplicates="drop")
    except ValueError:
        return float("nan")
    means = []
    for qi in sorted(set(q)):
        means.append(float(np.mean(forward[mask][q == qi])))
    if len(means) < 3:
        return float("nan")
    rho, _ = stats.spearmanr(np.arange(len(means)), means)
    return float(rho) if np.isfinite(rho) else float("nan")


def block_stability(df: pd.DataFrame, feature: str, horizon: str) -> Dict[str, Any]:
    dates = sorted(df["date"].dropna().unique())
    if len(dates) < 30:
        return {"n_blocks": 0, "agree": 0}
    cuts = np.array_split(dates, 6)
    ics = []
    for block in cuts:
        if len(block) < 5:
            continue
        sub = df[df["date"].isin(block)]
        f = sub[feature].to_numpy(dtype=float)
        r = sub[f"fwd_{horizon}"].to_numpy(dtype=float)
        m = np.isfinite(f) & np.isfinite(r)
        if m.sum() < 50:
            continue
        rho, _ = stats.spearmanr(f[m], r[m])
        if np.isfinite(rho):
            ics.append(float(rho))
    if not ics:
        return {"n_blocks": 0, "agree": 0}
    sign = np.sign(np.median(ics))
    agree = int(np.sum(np.sign(ics) == sign))
    return {"n_blocks": len(ics), "agree": agree, "ics": ics}


def cost_test(df: pd.DataFrame, feature: str, horizon: str, ic: float) -> Dict[str, Any]:
    hb = HORIZONS[horizon]
    sub = df.dropna(subset=[feature, f"fwd_{horizon}", "close"]).copy()
    first = sub.groupby(["symbol", "date"], as_index=False).first()
    gross, dates = [], []
    for _, row in first.iterrows():
        sig = float(row[feature])
        if not np.isfinite(sig) or sig == 0:
            continue
        if ic > 0:
            side = 1 if sig > 0 else -1
        else:
            side = -1 if sig > 0 else 1
        g = float(row[f"fwd_{horizon}"]) * side
        if not np.isfinite(g):
            continue
        gross.append(g)
        dates.append(row["date"])
    if len(gross) < 30:
        return {"n": len(gross), "be_bps": float("nan"), "clears_11": False, "inconclusive": True}
    ga = np.asarray(gross, dtype=float)
    be = float(np.mean(ga) * 1e4)
    by: Dict[str, List[float]] = {}
    for d, g in zip(dates, ga):
        by.setdefault(str(d), []).append(float(g))
    dm = np.array([float(np.mean(v)) for v in by.values()])
    rng = np.random.default_rng(RNG_SEED)
    edge = dm - TAKER_RT
    boots = np.empty(N_BOOT)
    for i in range(N_BOOT):
        boots[i] = float(np.mean(edge[rng.integers(0, len(dm), size=len(dm))]))
    ci = (float(np.percentile(boots, 2.5) * 1e4), float(np.percentile(boots, 97.5) * 1e4))
    clears = bool(be > 11.0 and ci[0] > 0 and len(dm) >= 90)
    inconclusive = bool(be > 11.0 and ci[0] <= 0)
    return {
        "n": int(len(ga)),
        "n_dates": int(len(dm)),
        "be_bps": be,
        "edge_mean_bps": float(np.mean(edge) * 1e4),
        "ci_bps": list(ci),
        "clears_11": clears,
        "inconclusive_power": inconclusive,
        "maker_net_if_be_ok_bps": float(be - MAKER_RT * 1e4) if be >= 4 else None,
    }


def write_doc(path: Path, payload: Dict[str, Any]) -> None:
    lines = [
        "# Feature Screening — OI / positioning (Bybit proxy)",
        "",
        f"Generated: {payload['generated_at']}",
        f"OI DB: `{payload['oi_db']}` (source={payload['oi_source']})",
        f"Price DB: `{payload['px_db']}`",
        f"Span: {payload['date_min']} → {payload['date_max']} ({payload['n_dates']} dates)",
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
    lines.append("## Controls")
    lines.append("")
    for c in payload["controls"]:
        lines.append(f"- {c['name']}: IC={c['ic']:.4f} FDR_pass={c.get('fdr_pass')}")
    lines.append("")
    lines.append("## TOP / survivors")
    lines.append("")
    lines.append("| feature | h | IC | q_FDR | mono | blocks | BE bps | clears 11? |")
    lines.append("|---|---|---:|---:|---:|---:|---:|:---:|")
    for r in payload["survivors"]:
        lines.append(
            f"| {r['feature']} | {r['horizon']} | {r['ic']:.4f} | {r['q_fdr']:.3f} | "
            f"{r['mono']:.2f} | {r['blocks_agree']}/{r['n_blocks']} | "
            f"{r['cost']['be_bps']:.2f} | {'Y' if r['cost']['clears_11'] else 'n'} |"
        )
    if not payload["survivors"]:
        lines.append("_(none)_")
    lines.append("")
    lines.append("## Full ranking (candidates)")
    lines.append("")
    lines.append("| feature | h | IC | p_date | q_FDR | mono | n_dates | survives? |")
    lines.append("|---|---|---:|---:|---:|---:|---:|:---:|")
    for r in payload["rows"]:
        if r.get("is_control"):
            continue
        lines.append(
            f"| {r['feature']} | {r['horizon']} | {r['ic']:.4f} | {r['p_date']:.3g} | "
            f"{r['q_fdr']:.3f} | {r['mono']:.2f} | {r['n_dates']} | "
            f"{'Y' if r.get('survives') else 'n'} |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--oi-db", type=Path, default=OI_DB)
    ap.add_argument("--px-db", type=Path, default=PX_DB)
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-doc", type=Path, default=OUT_DOC)
    ap.add_argument("--n-boot", type=int, default=N_BOOT)
    args = ap.parse_args()

    oi = load_oi(args.oi_db, SYMBOLS)
    px = load_px(args.px_db, SYMBOLS)
    if oi.empty:
        print("ERROR: oi_history empty — run backfill_oi_bybit_research.py first", file=sys.stderr)
        return 2
    print(f"OI rows={len(oi)} sources={oi['source'].value_counts().to_dict() if 'source' in oi else '?'}")
    print(f"PX rows={len(px)}")
    panel = build_panel(oi, px)
    print(f"Panel bars={len(panel)} dates={panel['date'].nunique()}")

    rows: List[Dict[str, Any]] = []
    tests: List[Tuple[str, str, bool]] = []
    for feat in FEATURES:
        for hor in HORIZONS:
            tests.append((feat, hor, False))
    for hor in HORIZONS:
        tests.append((CONTROL_POS, hor, True))
        for cn in CONTROL_NEGS:
            tests.append((cn, hor, True))

    for feat, hor, is_ctrl in tests:
        hb = HORIZONS[hor]
        st = spearman_ic_date_block(
            panel[feat].to_numpy(dtype=float),
            panel[f"fwd_{hor}"].to_numpy(dtype=float),
            panel["date"].to_numpy(),
            hb,
            n_boot=args.n_boot,
            seed=RNG_SEED,
        )
        mono = mono_spearman(panel[feat].to_numpy(dtype=float), panel[f"fwd_{hor}"].to_numpy(dtype=float))
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
    pvals = np.array([rows[i]["p_date"] for i in cand_idx], dtype=float)
    qvals = _bh_fdr(pvals)
    for j, i in enumerate(cand_idx):
        rows[i]["q_fdr"] = float(qvals[j]) if np.isfinite(qvals[j]) else float("nan")
        r = rows[i]
        survives = bool(
            np.isfinite(r["q_fdr"])
            and r["q_fdr"] <= FDR_ALPHA
            and np.isfinite(r["mono"])
            and abs(r["mono"]) >= 0.6
            and r["n_blocks"] >= 5
            and r["blocks_agree"] >= 5
            and r["n_dates"] >= 90
        )
        r["survives"] = survives
        if survives:
            r["cost"] = cost_test(panel, r["feature"], r["horizon"], float(r["ic"]))
        else:
            r["cost"] = None

    for r in rows:
        if r["is_control"]:
            r["q_fdr"] = float("nan")
            r["fdr_pass"] = abs(r["ic"] or 0) > 0.05 if "POS" in r["feature"] else abs(r["ic"] or 0) < 0.02

    survivors = [r for r in rows if r.get("survives")]
    survivors.sort(key=lambda x: abs(x["ic"] or 0), reverse=True)
    cost_pass = [s for s in survivors if s.get("cost") and s["cost"].get("clears_11")]

    # Controls check
    ctrl_pos_ok = any(
        r["feature"] == CONTROL_POS and abs(r["ic"] or 0) > 0.05 for r in rows if r["is_control"]
    )
    ctrl_neg_ok = all(
        abs(r["ic"] or 0) < 0.05
        for r in rows
        if r["is_control"] and r["feature"] in CONTROL_NEGS and r["horizon"] == "1h"
    )

    if cost_pass:
        code, summary = "A", f"OI survivors clear 11 bps: {[s['feature']+'@'+s['horizon'] for s in cost_pass]}"
        detail = "Proceed to minimal strategy + baseline gate (not in this script)."
    elif survivors:
        code = "C"
        summary = (
            f"{len(survivors)} statistical survivor(s) but none clear powered 11 bps cost "
            f"(or CI straddles zero)."
        )
        detail = "; ".join(
            f"{s['feature']}@{s['horizon']} BE={s['cost']['be_bps']:.1f} "
            f"CI={s['cost']['ci_bps']} n_dates={s['cost']['n_dates']}"
            for s in survivors
            if s.get("cost")
        )
    else:
        code = "C"
        summary = "No OI/positioning feature cleared FDR+mono+block gates on the extended sample."
        detail = "Family does not earn a strategy attempt."

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "oi_db": str(args.oi_db),
        "px_db": str(args.px_db),
        "oi_source": "bybit_open_interest (proxy)",
        "n_bars": int(len(panel)),
        "n_dates": int(panel["date"].nunique()),
        "date_min": str(panel["date"].min()),
        "date_max": str(panel["date"].max()),
        "limitations": [
            "OI is Bybit linear perpetual open interest — not Hyperliquid-native.",
            "Forward returns from Binance spot 15m proxy (cross-venue).",
            "Maker fee assumption 1.5 bps/side (HL tier-0); maker RT 3 bps only evaluated if BE≥4.",
            "Prior 66d HL-native OI sample left oi_delta_24h INCONCLUSIVE (BE 19.6, CI straddled 0).",
        ],
        "controls_note": f"pos_ok={ctrl_pos_ok} neg_ok={ctrl_neg_ok}",
        "controls": [
            {"name": r["feature"] + "@" + r["horizon"], "ic": r["ic"], "fdr_pass": r.get("fdr_pass")}
            for r in rows
            if r["is_control"] and r["horizon"] == "1h"
        ],
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
