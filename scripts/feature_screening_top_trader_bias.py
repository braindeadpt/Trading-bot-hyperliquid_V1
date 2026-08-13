#!/usr/bin/env python3
"""Top-trader bias feature probe — level & delta vs forward returns.

Reuses the existing FDR / date-block bootstrap machinery
(``feature_screening_24m_candles.screen_cell``,
``feature_screening.benjamini_hochberg``,
``feature_screening_24m_candles.survives_strict``) to measure whether the
top-trader aggregate bias carries predictive content for forward returns.

Features (aligned to the 15m grid, asof — no lookahead):
  * tt_bias_level        — net_bias (asof, last sample <= bar close)
  * tt_bias_delta_15m    — level diff over 1 bar
  * tt_bias_delta_1h     — level diff over 4 bars
  * tt_bias_delta_4h     — level diff over 16 bars
Controls (from the candle pipeline): leaky-forward positive control + 3
random negatives. FDR is run on the candidate family only.

Data:
  * bias samples: data/research/hyperliquid.db -> top_trader_bias_samples
  * candles:      data/live/bot.db -> candles_15m (same window as samples)

Usage:
  python scripts/feature_screening_top_trader_bias.py
  python scripts/feature_screening_top_trader_bias.py --n-boot 1000
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from feature_screening import (  # noqa: E402
    CONTROL_NEGS,
    CONTROL_POS,
    FDR_ALPHA,
    HORIZONS,
    RNG_SEED,
    benjamini_hochberg,
)
from feature_screening_24m_candles import (  # noqa: E402
    N_BOOT_DEFAULT,
    SYMBOLS_DEFAULT,
    attach_btc_vol_regime,
    build_candle_features,
    load_ohlcv_15m,
    screen_cell,
    survives_strict,
)

CANDLES_DB = ROOT / "data" / "live" / "bot.db"
BIAS_DB = ROOT / "data" / "research" / "hyperliquid.db"

CANDIDATE_FEATURES = [
    "tt_bias_level",
    "tt_bias_delta_15m",
    "tt_bias_delta_1h",
    "tt_bias_delta_4h",
]


def load_bias_samples(db: Path, symbols: List[str]) -> pd.DataFrame:
    con = sqlite3.connect(str(db))
    ph = ",".join("?" * len(symbols))
    df = pd.read_sql_query(
        f"""
        SELECT timestamp_ms, symbol, net_bias, long_frac
        FROM top_trader_bias_samples
        WHERE symbol IN ({ph})
        ORDER BY symbol, timestamp_ms
        """,
        con,
        params=list(symbols),
    )
    con.close()
    return df


def attach_bias_features(
    candles: pd.DataFrame, bias: pd.DataFrame
) -> pd.DataFrame:
    """As-of merge bias level per 15m bar close + deltas over 1/4/16 bars.

    Uses ``searchsorted`` per symbol (O(log n)) instead of ``merge_asof``,
    which is brittle about key ordering across groups.
    """
    out = candles.copy()
    level = pd.Series(np.nan, index=out.index, dtype=float)
    for sym, g in out.groupby("symbol", sort=False):
        b = bias.loc[bias["symbol"] == sym, ["timestamp_ms", "net_bias"]]
        if b.empty:
            continue
        b = b.sort_values("timestamp_ms")
        ts = b["timestamp_ms"].to_numpy(dtype=np.int64)
        vals = b["net_bias"].to_numpy(dtype=float)
        bar_ts = g["timestamp_ms"].to_numpy(dtype=np.int64)
        pos = np.searchsorted(ts, bar_ts, side="right") - 1
        valid = pos >= 0
        level.loc[g.index[valid]] = vals[pos[valid]]
    out["tt_bias_level"] = level
    out["tt_bias_delta_15m"] = out.groupby("symbol")["tt_bias_level"].diff(1)
    out["tt_bias_delta_1h"] = out.groupby("symbol")["tt_bias_level"].diff(4)
    out["tt_bias_delta_4h"] = out.groupby("symbol")["tt_bias_level"].diff(16)
    return out


def write_report(
    rows: List[Dict[str, Any]],
    meta: Dict[str, Any],
    out_path: Path,
) -> None:
    lines: List[str] = []
    lines.append("# Feature screening probe — top-trader bias (level & delta)")
    lines.append("")
    lines.append(
        f"Gerado: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · "
        f"pipeline reutilizado: `screen_cell` (date-block bootstrap) + "
        f"`benjamini_hochberg` + `survives_strict`."
    )
    lines.append("")
    lines.append("## Amostra")
    lines.append("")
    lines.append(
        f"Bias samples: {meta['n_bias']} ("
        + ", ".join(f"{k}={v}" for k, v in meta.get("n_bias_sym", {}).items())
        + f") · janela {meta['window']} · grid 15m · "
        f"candles: {meta['n_bars']} barras em {meta['n_dates']} datas."
    )
    lines.append("")
    lines.append(
        "**Aviso de suficiência:** o gate estrito exige ≥20 datas (bootstrap), "
        "≥6 subperíodos, ≥3 regimes e ≥3 símbolos. Com a janela atual "
        f"({meta['n_dates']} datas) o gate é **estruturalmente inatingível** — "
        "os ICs abaixo são evidência direcional, não decisão."
    )
    lines.append("")

    lines.append("## Tabela de células (candidatas + controlos)")
    lines.append("")
    lines.append(
        "| feature | h | IC | p_NW | p_boot | n_bars | n_dates | mono | "
        "syms | per | reg | FDR | GATE |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: (r["is_control"], -abs(r["ic"]))):
        lines.append(
            f"| {r['feature']} | {r['horizon']} | "
            f"{r['ic']:.3f} | {r['p_nw']:.2e} | "
            f"{r['p_raw']:.2e} | {r['n_bars']} | {r['n_dates']} | "
            f"{r['mono']:.2f} | {r['same_sign_symbols']}/{r['n_symbols']} | "
            f"{r['same_sign_periods']}/{r['n_periods']} | "
            f"{r['same_sign_regimes']}/{r['n_regimes']} | "
            f"{'Y' if r['fdr_reject'] else 'n'} | "
            f"{'**SIM**' if r['survives'] else 'não'} |"
        )
    lines.append("")

    survived = [r for r in rows if r["survives"]]
    if survived:
        lines.append("## Veredito: sobrevive ao gate")
        lines.append("")
        lines.append(
            "As células seguintes passam FDR + monotonicidade + estabilidade "
            "temporal + consistência cross-symbol:"
        )
        for r in survived:
            lines.append(f"* `{r['feature']}` @ {r['horizon']} — IC {r['ic']:.3f}")
    else:
        lines.append("## Veredito: NÃO sobrevive ao gate")
        lines.append("")
        lines.append(
            "Nenhuma célula passou `survives_strict`. Motivo dominante: "
            "amostra insuficiente (datas < 20 ⇒ p_boot indefinido ⇒ FDR sem "
            "rejeições). Nenhuma estratégia deve ser construída sobre este "
            "sinal até a janela de bias ≥ 20 datas."
        )
        lines.append("")
        lines.append(
            "**Requisitos para o gate:** re-correr quando `top_trader_bias_samples` "
            "cobrir ≥20 datas (≈3 semanas de polling a 60s). O script é "
            "idempotente — basta relançar com mais dados."
        )

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Relatório: {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candles-db", type=Path, default=CANDLES_DB)
    ap.add_argument("--bias-db", type=Path, default=BIAS_DB)
    ap.add_argument("--symbols", default=",".join(SYMBOLS_DEFAULT))
    ap.add_argument("--n-boot", type=int, default=N_BOOT_DEFAULT)
    ap.add_argument(
        "--out", type=Path, default=ROOT / "docs" / "FEATURE_SCREENING_TOP_TRADER_BIAS.md"
    )
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    t0 = time.time()

    bias = load_bias_samples(args.bias_db, symbols)
    print(f"Bias samples: {len(bias)} ({time.time()-t0:.1f}s)")
    if bias.empty:
        raise SystemExit("Sem top_trader_bias_samples — o tracker ainda não gravou.")

    lo = int(bias["timestamp_ms"].min())
    hi = int(bias["timestamp_ms"].max())

    con = sqlite3.connect(str(args.candles_db))
    raw = load_ohlcv_15m(con, symbols)
    con.close()
    raw = raw[(raw["timestamp_ms"] >= lo - 4 * 15 * 60_000) & (raw["timestamp_ms"] <= hi)]
    print(f"Candles 15m na janela: {len(raw)}")

    df = build_candle_features(raw)
    df = attach_btc_vol_regime(df)
    df = attach_bias_features(df, bias)
    df = df.dropna(subset=CANDIDATE_FEATURES, how="all")

    controls = [CONTROL_POS, *CONTROL_NEGS]
    all_feats = CANDIDATE_FEATURES + controls

    rows: List[Dict[str, Any]] = []
    seed = RNG_SEED
    for feat in all_feats:
        for h in HORIZONS:
            seed += 1
            cell = screen_cell(df, feat, h, n_boot=args.n_boot, seed=seed)
            row = asdict(cell)
            row["is_control"] = feat in controls
            rows.append(row)

    for r in rows:
        r.setdefault("fdr_reject", False)
        r.setdefault("q_fdr", float("nan"))
    cand_idx = [i for i, r in enumerate(rows) if not r["is_control"]]
    pvals = [rows[i]["p_raw"] for i in cand_idx]
    rejected, qvals = benjamini_hochberg(pvals, alpha=FDR_ALPHA)
    for j, i in enumerate(cand_idx):
        rows[i]["fdr_reject"] = bool(rejected[j])
        rows[i]["q_fdr"] = float(qvals[j]) if np.isfinite(qvals[j]) else float("nan")
    for r in rows:
        r["survives"] = bool(survives_strict(r))

    n_dates = int(df["date"].nunique())
    meta = {
        "n_bias": int(len(bias)),
        "n_bias_sym": {
            s: int((bias["symbol"] == s).sum()) for s in symbols
        },
        "window": f"{datetime.fromtimestamp(lo/1000, tz=timezone.utc):%Y-%m-%d %H:%M} "
                  f"→ {datetime.fromtimestamp(hi/1000, tz=timezone.utc):%Y-%m-%d %H:%M} UTC",
        "n_bars": int(len(df)),
        "n_dates": n_dates,
    }
    write_report(rows, meta, args.out)

    print("\nCélulas candidatas (por |IC|):")
    print(f"{'feature':22} {'h':5} {'IC':>6} {'p_NW':>9} {'p_raw':>9} {'n':>5} {'mono':>5} "
          f"{'syms':>6} {'per':>5} {'GATE':>5}")
    for r in sorted(rows, key=lambda r: (r["is_control"], -abs(r["ic"]))):
        if r["is_control"]:
            continue
        print(f"{r['feature']:22} {r['horizon']:5} {r['ic']:>6.3f} "
              f"{r['p_nw']:>9.2e} {r['p_raw']:>9.2e} {r['n_bars']:>5} "
              f"{r['mono']:>5.2f} {r['same_sign_symbols']}/{r['n_symbols']:>2} "
              f"{r['same_sign_periods']}/{r['n_periods']:>1} "
              f"{'SIM' if r['survives'] else 'não'}")

    print(f"\nControlo positivo (leaky) IC por horizonte:")
    for r in rows:
        if r["feature"] == CONTROL_POS:
            print(f"  {r['horizon']:5} IC={r['ic']:+.3f} n={r['n_bars']}")
    print(f"\nFeito em {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
