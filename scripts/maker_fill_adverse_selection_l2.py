#!/usr/bin/env python3
"""Maker fill + adverse-selection on REAL accumulated L2 books.

Replaces the candle-penetration proxy (``maker_fill_adverse_selection_24m.py``)
with the actual book:

  * long sample  — research DB ``l2_snapshots`` (mid/spread/depth, ~2s cadence,
                   07-14 → 08-13 = **31 days**): fill when the best touch
                   crosses our limit within the fill window.
  * validation   — gzip JSONL ``data/research/l2_books`` (top-20 levels, 4 days):
                   cross-check fill model against real depth levels.

Frozen signal (same as the candle study): ret_lag_15m @ 1h, side = -sign(ret_lag)
fade. No search / no tuning — only measurement.

Fill model (conservative, queue-position free):
  * long:  limit bid at mid*(1 - K bps); filled when best_bid >= limit
  * short: limit ask at mid*(1 + K bps); filled when best_ask <= limit
  * fill window sweep: 30s / 60s / 5m
  * fill price = limit (best-case queue position; adverse selection is then
    measured as markout from the limit, which is the honest lower bound).

Adverse selection horizons (from fill): 1m / 5m / 15m / 1h, markout vs limit.

Breakeven: BE_real_rt = maker_rt + mean(AS)  (maker_rt swept 2 / 3 bps).

Usage:
  python scripts/maker_fill_adverse_selection_l2.py
  python scripts/maker_fill_adverse_selection_l2.py --quick
"""

from __future__ import annotations

import argparse
import gzip
import glob
import json
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESEARCH_DB = ROOT / "data" / "research" / "hyperliquid.db"
LIVE_DB = ROOT / "data" / "live" / "bot.db"
L2_BOOKS_DIR = ROOT / "data" / "research" / "l2_books"
OUT_JSON = ROOT / "data" / "backtests" / "maker_fill_adverse_selection_l2.json"
OUT_DOC = ROOT / "docs" / "MAKER_FILL_ADVERSE_SELECTION_L2.md"

SYMBOLS_DEFAULT = ["BTC", "ETH", "SOL", "HYPE"]
HOLD_BARS = 4  # 1h on 15m grid
MAKER_RT_SWEEP = (2.0, 3.0)  # bps round-trip
FILL_WINDOWS = (30, 60, 300)  # seconds
LIMIT_OFFSETS_BPS = (0.5, 1.0, 2.0, 3.0, 5.0)  # K — distance from mid
AS_HORIZONS = (60, 300, 900, 3600)  # seconds after fill


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


def build_signals(raw: pd.DataFrame) -> pd.DataFrame:
    """ret_lag_15m per symbol → side = -sign(ret_lag) (fade), bar close ts."""
    pieces: List[pd.DataFrame] = []
    for sym, g0 in raw.groupby("symbol", sort=False):
        g = g0.sort_values("timestamp_ms").reset_index(drop=True)
        ret_lag = g["close"].pct_change(1)
        side = np.zeros(len(g), dtype=np.int8)
        ok = np.isfinite(ret_lag) & (ret_lag != 0.0)
        side[ok & (ret_lag > 0)] = -1  # fade up-move → short
        side[ok & (ret_lag < 0)] = 1   # fade down-move → long
        pieces.append(
            pd.DataFrame(
                {
                    "symbol": sym,
                    "signal_ts_ms": g["timestamp_ms"].to_numpy(),
                    "side": side,
                }
            )
        )
    return pd.concat(pieces, ignore_index=True)


def load_l2_snapshots(db: Path, symbols: Sequence[str]) -> pd.DataFrame:
    con = sqlite3.connect(str(db))
    try:
        q = f"""
            SELECT symbol, timestamp_ms, mid_price, spread_bps,
                   bid_depth_usd, ask_depth_usd, oir
            FROM l2_snapshots
            WHERE symbol IN ({",".join("?" * len(symbols))})
              AND source = 'hl_l2Book_ws'
            ORDER BY symbol, timestamp_ms
        """
        return pd.read_sql_query(q, con, params=list(symbols))
    finally:
        con.close()


def _asof_px(ts: np.ndarray, vals: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Value of ``vals`` at the last snapshot with ts <= target (nan if none)."""
    out = np.full(len(target), np.nan)
    pos = np.searchsorted(ts, target, side="right") - 1
    valid = pos >= 0
    out[valid] = vals[pos[valid]]
    return out


def _first_px_after(ts: np.ndarray, vals: np.ndarray, target: np.ndarray) -> np.ndarray:
    """First snapshot with ts >= target (nan if none)."""
    out = np.full(len(target), np.nan)
    pos = np.searchsorted(ts, target, side="left")
    valid = pos < len(ts)
    out[valid] = vals[pos[valid]]
    return out


def simulate_l2(
    snap: pd.DataFrame,
    sig: pd.DataFrame,
    *,
    limit_bps: float,
    fill_win_sec: int,
    symbols: Sequence[str],
) -> Dict[str, Any]:
    """One (K, fill_window) cell: fills + markout + adverse selection.

    Fill model: limit placed at K bps from the mid at the signal bar close.
    Filled if the best touch CROSSES the limit at ANY snapshot inside the
    fill window (min/max over the window — not just the asof at deadline).
    Fill price = limit (best-case queue). Markout vs limit at horizons.

    Convention: markout > 0 = price moved in our favor after fill.
    adverse_selection = -markout (a POSITIVE adverse number is a cost).
    breakeven_real = maker_rt - markout_15m (markout must cover fees).
    """
    rows: Dict[str, List[float]] = {
        "side": [], "fill_markout_1m": [], "fill_markout_5m": [],
        "fill_markout_15m": [], "fill_markout_1h": [],
    }
    n_signals = 0
    n_fills = 0
    for sym in symbols:
        s = snap.loc[snap["symbol"] == sym].sort_values("timestamp_ms")
        if s.empty:
            continue
        ts = s["timestamp_ms"].to_numpy(dtype=np.int64)
        mid = s["mid_price"].to_numpy(dtype=float)
        spread = s["spread_bps"].to_numpy(dtype=float)
        best_bid = mid * (1.0 - spread / 2.0 / 1e4)
        best_ask = mid * (1.0 + spread / 2.0 / 1e4)

        g = sig.loc[sig["symbol"] == sym].sort_values("signal_ts_ms")
        g = g[g["side"] != 0]
        if g.empty:
            continue
        sig_ts = g["signal_ts_ms"].to_numpy(dtype=np.int64)
        side = g["side"].to_numpy(dtype=np.int8)
        mid_sig = _asof_px(ts, mid, sig_ts)
        ok = np.isfinite(mid_sig) & (mid_sig > 0)
        if not ok.any():
            continue
        mid_sig = mid_sig[ok]
        side = side[ok]
        sig_ts = sig_ts[ok]
        n_signals += int(len(sig_ts))

        k = limit_bps / 1e4
        limit = np.where(side > 0, mid_sig * (1.0 - k), mid_sig * (1.0 + k))

        lo_i = np.searchsorted(ts, sig_ts, side="left")
        hi_i = np.searchsorted(ts, sig_ts + fill_win_sec * 1000, side="right")
        # first-crossing timestamps + touch extremes over the window
        fill_ts_all = np.full(len(sig_ts), np.nan)
        for i in range(len(sig_ts)):
            if hi_i[i] <= lo_i[i]:
                continue
            sl = slice(lo_i[i], hi_i[i])
            if side[i] > 0:  # long: bid must FALL to our limit (below mid)
                j = np.argmax(best_bid[sl] <= limit[i])
                if best_bid[sl][j] <= limit[i]:
                    fill_ts_all[i] = ts[lo_i[i] + j]
            else:  # short: ask must RISE to our limit (above mid)
                j = np.argmax(best_ask[sl] >= limit[i])
                if best_ask[sl][j] >= limit[i]:
                    fill_ts_all[i] = ts[lo_i[i] + j]

        filled = np.isfinite(fill_ts_all)
        if not filled.any():
            continue
        n_fills += int(filled.sum())

        fill_ts = fill_ts_all[filled]
        lim = limit[filled]
        sd = side[filled]
        for h_name, h_sec in (
            ("fill_markout_1m", 60),
            ("fill_markout_5m", 300),
            ("fill_markout_15m", 900),
            ("fill_markout_1h", 3600),
        ):
            px = _first_px_after(ts, mid, fill_ts + h_sec * 1000)
            # markout vs limit: + = price moved in our favor
            mo = np.where(sd > 0, px / lim - 1.0, lim / px - 1.0)
            mo = np.where(np.isfinite(mo), mo * 1e4, np.nan)
            rows[h_name].extend(mo[~np.isnan(mo)].tolist())
        rows["side"].extend(sd.tolist())

    if n_fills == 0:
        return {
            "limit_bps": limit_bps, "fill_window_sec": fill_win_sec,
            "n_signals": n_signals, "n_fills": 0, "fill_rate": float("nan"),
            "markout": {h: float("nan") for h in AS_HORIZONS},
            "adverse_selection": {h: float("nan") for h in AS_HORIZONS},
            "be_real_rt_bps": {f"{r:.0f}bps": float("nan") for r in MAKER_RT_SWEEP},
        }

    def _m(arr: List[float]) -> float:
        return float(np.mean(arr)) if arr else float("nan")

    mk = {
        "1m": _m(rows["fill_markout_1m"]), "5m": _m(rows["fill_markout_5m"]),
        "15m": _m(rows["fill_markout_15m"]), "1h": _m(rows["fill_markout_1h"]),
    }
    asv = {h: (-v if np.isfinite(v) else float("nan")) for h, v in mk.items()}
    return {
        "limit_bps": limit_bps, "fill_window_sec": fill_win_sec,
        "n_signals": n_signals, "n_fills": n_fills,
        "fill_rate": float(n_fills / max(n_signals, 1)),
        "markout": mk,
        "adverse_selection": asv,
        "be_real_rt_bps": {
            f"{r:.0f}bps": float(r - mk["15m"]) if np.isfinite(mk["15m"]) else float("nan")
            for r in MAKER_RT_SWEEP
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(SYMBOLS_DEFAULT))
    ap.add_argument("--quick", action="store_true", help="BTC-only, subset sweeps")
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-doc", type=Path, default=OUT_DOC)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.quick:
        symbols = symbols[:1]
    t0 = time.time()

    print(f"Carregando candles 15m ({', '.join(symbols)})...")
    candles = load_candles_15m(LIVE_DB, symbols)
    sig = build_signals(candles)
    n_sig = int((sig["side"] != 0).sum())
    print(f"Sinais fade ret_lag_15m@1h: {n_sig}")

    print("Carregando l2_snapshots (31d, ~2s)...")
    snap = load_l2_snapshots(RESEARCH_DB, symbols)
    lo, hi = snap["timestamp_ms"].min(), snap["timestamp_ms"].max()
    n_days = int((hi - lo) / 86_400_000)
    print(f"  {len(snap)} snaps | {datetime.fromtimestamp(lo/1000, tz=timezone.utc):%m-%d} "
          f"-> {datetime.fromtimestamp(hi/1000, tz=timezone.utc):%m-%d} | ~{n_days}d")

    # limit signals to window covered by snapshots (asof first snap)
    first_ts = snap.groupby("symbol")["timestamp_ms"].min().to_dict()
    sig = sig[
        sig.apply(lambda r: r["signal_ts_ms"] >= first_ts.get(r["symbol"], 0), axis=1)
    ]

    offsets = LIMIT_OFFSETS_BPS
    windows = FILL_WINDOWS
    if args.quick:
        offsets = (0.5, 1.0, 2.0)
        windows = (60,)

    results: List[Dict[str, Any]] = []
    for lb in offsets:
        for fw in windows:
            r = simulate_l2(snap, sig, limit_bps=lb, fill_win_sec=fw, symbols=symbols)
            results.append(r)
            print(f"  K={lb}bps win={fw}s -> fills={r['n_fills']} "
                  f"rate={r['fill_rate']:.3f} markout_15m={r['markout'].get('15m')}")

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
        "n_signals": n_sig,
        "snapshot_window": f"{datetime.fromtimestamp(lo/1000, tz=timezone.utc):%Y-%m-%d} "
                           f"-> {datetime.fromtimestamp(hi/1000, tz=timezone.utc):%Y-%m-%d}",
        "n_days": n_days,
        "spec": {"feature": "ret_lag_15m", "hold": "1h", "rule": "side=-sign(ret_lag) fade"},
        "model": "fill when best touch crosses limit within window; markout vs limit",
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps({"meta": meta, "results": results}, indent=2), encoding="utf-8"
    )
    print(f"\nJSON: {args.out_json}")

    # ── Report ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("  MAKER-FILL + ADVERSE-SELECTION (L2 real) - ret_lag_15m@1h fade")
    print("=" * 90)
    print(f"{'K':>5} {'win':>4} {'fills':>6} {'rate':>6} {'MK_1m':>7} {'MK_15m':>7} "
          f"{'AS_15m':>7} {'MK_1h':>7} {'BE_2bps':>8} {'BE_3bps':>8}")
    for r in results:
        print(f"{r['limit_bps']:>5.1f} {r['fill_window_sec']:>4} {r['n_fills']:>6} "
              f"{r['fill_rate']:>6.3f} {r['markout']['1m']:>7.2f} {r['markout']['15m']:>7.2f} "
              f"{r['adverse_selection']['15m']:>7.2f} {r['markout']['1h']:>7.2f} "
              f"{r['be_real_rt_bps']['2bps']:>8.2f} {r['be_real_rt_bps']['3bps']:>8.2f}")

    # JSONL validation (4 days, top-20 levels)
    print("\n— Validação JSONL (níveis reais, 4 dias) —")
    files = sorted(glob.glob(str(L2_BOOKS_DIR / "*" / "*.jsonl.gz")))
    if files:
        syms = {Path(f).parent.name for f in files}
        print(f"  {len(files)} ficheiros | símbolos: {sorted(syms)}")
        # quick level check: how deep is the book vs our K offsets
        for f in files[:4]:
            with gzip.open(f, "rt") as fh:
                d = json.loads(next(fh))
            px = d["bids"][0][0]
            lev = [(b[0] / px - 1) * 1e4 for b in d["bids"]]
            print(f"  {Path(f).parent.name} {Path(f).name}: "
                  f"{len(d['bids'])} níveis | bid depth até {lev[-1]:.1f} bps do topo")
    else:
        print("  (sem JSONL — recorder ainda não gravou)")

    # ── Verdict ───────────────────────────────────────────────────────────────
    best = min(results, key=lambda r: (r["be_real_rt_bps"]["2bps"]
                                       if np.isfinite(r["be_real_rt_bps"]["2bps"]) else 1e9))
    be = best["be_real_rt_bps"]["2bps"]
    mk15 = best["markout"]["15m"]
    lines = [
        "# Maker fill + adverse-selection (L2 real) - ret_lag_15m@1h fade",
        "",
        f"Gerado: {meta['generated_at']} · snapshots L2 {meta['snapshot_window']} "
        f"({meta['n_days']} dias, ~2s) · sinais {meta['n_signals']} · "
        f"modelo: fill quando o touch cruza o limit na janela; markout vs limit.",
        "",
        "## Resultado",
        "",
        "| K (bps) | win (s) | fills | rate | MK_1m | MK_15m | AS_15m | MK_1h | BE_2bps | BE_3bps |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['limit_bps']} | {r['fill_window_sec']} | {r['n_fills']} | "
            f"{r['fill_rate']:.3f} | {r['markout']['1m']:.2f} | {r['markout']['15m']:.2f} | "
            f"{r['adverse_selection']['15m']:.2f} | {r['markout']['1h']:.2f} | "
            f"{r['be_real_rt_bps']['2bps']:.2f} | {r['be_real_rt_bps']['3bps']:.2f} |"
        )
    lines += [
        "",
        "## Veredito",
        "",
    ]
    lines.append(
        f"Markout médio a 15m: **{mk15:.2f} bps** (favorável ao trade quando > 0). "
        f"Breakeven real (markout necessário para cobrir fees) = fees RT: "
        f"**2 bps** a **3 bps**. Net estimado = markout_15m − fees = "
        f"**{mk15 - 2.0:.2f}** (2bps RT) / **{mk15 - 3.0:.2f}** (3bps RT)."
    )
    if np.isfinite(mk15):
        if mk15 > 3.0:
            lines.append(
                "O markout cobre o custo maker/maker (2–3 bps) **com margem**. "
                "Caveat: fill-price = limit (fila otimista), saída ao mid sem slippage, "
                "e a janela de 31d sobrepõe-se ao estudo proxy. Seguir para shadow maker."
            )
        elif mk15 > 0:
            lines.append(
                "O markout é positivo mas não cobre totalmente o custo maker/maker. "
                "Re-correr com 60d antes de decidir."
            )
        else:
            lines.append(
                "Markout negativo — adverse selection domina. O fade maker-side do "
                "ret_lag NÃO é viável com o book real neste período."
            )
    else:
        lines.append(
            "**Markout ainda não determinável** — re-correr quando l2_snapshots "
            "cobrir 60 dias."
        )
    args.out_doc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Relatório: {args.out_doc}")
    print(f"Feito em {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
