#!/usr/bin/env python3
"""IV (DVOL implied) vs ADX router (realized) — where they disagree, who wins.

Both gates claim to separate the bleeding trades from the surviving ones:

  * IV gate "high_iv only" (scripts/iv_high_only_ab_split.py): keep BOTH
    VB and VWAP only when the trailing-30d DVOL percentile > p66.
  * ADX router (scripts/regime_router_a_b_test.py, post-rework): keep VB in
    expansion only, VWAP in low_vol (range/unknown fallback).

They use different information (implied vol vs realized trend), so they can
disagree on a trade. This script tags EVERY trade in the same full-window
sample with BOTH signals and cross-tabulates the keep/block decisions, then
asks: in each disagreement cell, which gate's decision matches the realized
PnL (i.e. which one better predicts the bleeding)?

Reuses (no duplication):
  * run_strategy / precompute_adx / adx_at / classify_market_regime /
    VB_ALLOWED_REGIMES / VWAP_ALLOWED_REGIMES / summarize from
    scripts/regime_router_a_b_test.py
  * fetch_dvol / build_iv_percentile / iv_pct_at / dvol_series_for from
    scripts/iv_percentile_regime_gate_test.py
  * IV_ONLY_PCT / SPECS from scripts/iv_high_only_ab_split.py

Usage:
  python scripts/iv_vs_adx_disagreement.py --start 2026-05-18 --end 2026-08-07
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.iv_high_only_ab_split import IV_ONLY_PCT, SPECS  # noqa: E402
from scripts.iv_percentile_regime_gate_test import (  # noqa: E402
    DVOL_WINDOW_DAYS,
    build_iv_percentile,
    dvol_series_for,
    fetch_dvol,
    iv_pct_at,
)
from scripts.regime_router_a_b_test import (  # noqa: E402
    VB_ALLOWED_REGIMES,
    VWAP_ALLOWED_REGIMES,
    adx_at,
    classify_market_regime,
    ms,
    precompute_adx,
    run_strategy,
    summarize,
)
from src.data.database import Database  # noqa: E402
from src.utils.config import load_config  # noqa: E402

START_ARG, END_ARG = "2026-05-18", "2026-08-07"
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]


def iv_keep(iv_pct: Optional[float]) -> bool:
    """IV gate: keep only in high_iv (>p66)."""
    return iv_pct is not None and iv_pct > IV_ONLY_PCT


def adx_keep(strategy: str, regime: str) -> bool:
    """ADX router: VB in expansion; VWAP in range/low_vol/unknown (fallback)."""
    if strategy == "VolatilityBreakout":
        return regime in VB_ALLOWED_REGIMES
    return regime in VWAP_ALLOWED_REGIMES


def iv_tercile(iv_pct: Optional[float]) -> str:
    if iv_pct is None:
        return "no_iv"
    if iv_pct < 33.3:
        return "low_iv"
    if iv_pct < 66.7:
        return "mid_iv"
    return "high_iv"


def disagreement_verdict(iv_keep_adx_block_pnl: float, iv_block_adx_keep_pnl: float) -> str:
    """Decide which gate wins on the two disagreement cells.

    * iv_keep_adx_block > 0  -> IV was right to keep (ADX blocked a winner).
    * iv_block_adx_keep < 0  -> IV was right to block (ADX kept a bleeder).
    """
    iv_wins = 0
    adx_wins = 0
    if iv_keep_adx_block_pnl > 0:
        iv_wins += 1
    elif iv_keep_adx_block_pnl < 0:
        adx_wins += 1
    if iv_block_adx_keep_pnl < 0:
        iv_wins += 1
    elif iv_block_adx_keep_pnl > 0:
        adx_wins += 1
    if iv_wins > adx_wins:
        return "IV (DVOL implícito) vence nas células de discordância"
    if adx_wins > iv_wins:
        return "ADX (realizado) vence nas células de discordância"
    return "empate nas células de discordância (sinais complementares)"


def _cell(trades: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    s = summarize(trades, label)
    s["pnl_usd"] = s.pop("net_pnl")
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default=START_ARG)
    ap.add_argument("--end", default=END_ARG)
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "IV_VS_ADX_DISAGREEMENT.md")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    db = Database(cfg.get("database.path", "data/live/bot.db"))

    print("=" * 78)
    print("  IV (DVOL implícito) vs ADX router (realizado) — onde discordam")
    print(f"  {args.start} -> {args.end} | IV keep = DVOL pct > {IV_ONLY_PCT} | "
          f"ADX keep = VB exp / VWAP low_vol")
    print("=" * 78)

    s_ms, e_ms = ms(args.start), ms(args.end, True)
    fetch_lo = ms(args.start) - 60 * 86_400_000

    # ── Both signals, full span ──
    print("\n[0] Deribit DVOL + ADX(14) precompute...", flush=True)
    t0 = time.time()
    btc_raw = fetch_dvol("BTC", fetch_lo, e_ms)
    eth_raw = fetch_dvol("ETH", fetch_lo, e_ms)
    btc_iv = build_iv_percentile(btc_raw, DVOL_WINDOW_DAYS)
    eth_iv = build_iv_percentile(eth_raw, DVOL_WINDOW_DAYS)
    iv_by_sym = {sym: dvol_series_for(sym, btc_iv, eth_iv) for sym in symbols}
    adx_series = precompute_adx(db, symbols, s_ms, e_ms)
    print(f"    signals ready in {time.time()-t0:.0f}s")

    adx_range = float((cfg.get("strategy.phase08", {}) or {}).get("regime_router", {})
                      .get("adx_range_threshold", cfg.get("strategy.adx_range_threshold", 20.0)))
    adx_trend = float((cfg.get("strategy.phase08", {}) or {}).get("regime_router", {})
                      .get("adx_trend_threshold", cfg.get("strategy.adx_trend_threshold", 25.0)))

    # ── Raw trades (the SAME sample both gates see) ──
    print("\n[1] Raw backtests (full window)...", flush=True)
    all_trades: List[Dict[str, Any]] = []
    for name, cls, path in SPECS:
        t1 = time.time()
        trades = run_strategy(cfg, db, cls, path, s_ms, e_ms, symbols)
        for t in trades:
            t["_strategy"] = name
        all_trades.extend(trades)
        print(f"    {name}: {len(trades)} trades in {time.time()-t1:.0f}s", flush=True)

    for t in all_trades:
        series = iv_by_sym.get(t["symbol"], btc_iv)
        t["_iv_pct"] = iv_pct_at(series, int(t["entry_time"]))
        adx = adx_at(adx_series.get(t["symbol"], []), int(t["entry_time"]))
        t["_adx_regime"] = classify_market_regime(
            adx, adx_range_threshold=adx_range, adx_trend_threshold=adx_trend
        )
        t["_adx"] = adx
        t["_iv_keep"] = iv_keep(t["_iv_pct"])
        t["_adx_keep"] = adx_keep(t["_strategy"], t["_adx_regime"])

    # ── 2x2 confusion table ──
    both_keep = [t for t in all_trades if t["_iv_keep"] and t["_adx_keep"]]
    both_block = [t for t in all_trades if not t["_iv_keep"] and not t["_adx_keep"]]
    iv_keep_adx_block = [t for t in all_trades if t["_iv_keep"] and not t["_adx_keep"]]
    iv_block_adx_keep = [t for t in all_trades if not t["_iv_keep"] and t["_adx_keep"]]

    cells = {
        "both_keep": _cell(both_keep, "ambos mantêm"),
        "iv_keep_adx_block": _cell(iv_keep_adx_block, "IV mantém / ADX bloqueia"),
        "iv_block_adx_keep": _cell(iv_block_adx_keep, "IV bloqueia / ADX mantém"),
        "both_block": _cell(both_block, "ambos bloqueiam"),
    }

    iv_kept = both_keep + iv_keep_adx_block
    adx_kept = both_keep + iv_block_adx_keep

    # ── Joint ADX x IV table ──
    joint: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for t in all_trades:
        r = t["_adx_regime"]
        c = iv_tercile(t["_iv_pct"])
        joint.setdefault(r, {}).setdefault(c, []).append(t)

    print("\n" + "=" * 78)
    print("  CONCORDÂNCIA / DISCORDÂNCIA (n, net USD, WR%)")
    print("=" * 78)
    for k in ("both_keep", "iv_keep_adx_block", "iv_block_adx_keep", "both_block"):
        c = cells[k]
        print(f"  {c['label']:26} n={c['n']:>4} net={c['pnl_usd']:>9.2f} WR={c['win_rate']:>5.1f}%")

    print(f"\n  IV keep-set   : n={len(iv_kept)} net={summarize(iv_kept,'')['net_pnl']:+.2f}")
    print(f"  ADX keep-set  : n={len(adx_kept)} net={summarize(adx_kept,'')['net_pnl']:+.2f}")

    # ── Verdict on disagreement cells ──
    iv_ka = cells["iv_keep_adx_block"]["pnl_usd"]
    iv_bk = cells["iv_block_adx_keep"]["pnl_usd"]
    verdict = disagreement_verdict(iv_ka, iv_bk)

    # ── Persist evidence ──
    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    detail = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "start": args.start, "end": args.end,
        "iv_only_pct": IV_ONLY_PCT,
        "adx_range": adx_range, "adx_trend": adx_trend,
        "vb_allowed": sorted(VB_ALLOWED_REGIMES),
        "vwap_allowed": sorted(VWAP_ALLOWED_REGIMES),
        "cells": {k: v for k, v in cells.items()},
        "keep_sets": {
            "iv": summarize(iv_kept, "iv keep"),
            "adx": summarize(adx_kept, "adx keep"),
            "union": summarize(both_keep + iv_keep_adx_block + iv_block_adx_keep, "union"),
            "intersection": summarize(both_keep, "intersection"),
        },
        "joint": {
            r: {c: summarize(trades, f"{r}/{c}") for c, trades in cols.items()}
            for r, cols in joint.items()
        },
        "verdict": verdict,
    }
    jp = out_dir / f"iv_vs_adx_disagreement_{stamp}.json"
    jp.write_text(json.dumps(detail, indent=2, default=str), encoding="utf-8")

    lines = [
        "# IV (DVOL implícito) vs ADX router (realizado) — onde discordam",
        "",
        f"Gerado: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · janela "
        f"{args.start} -> {args.end} · IV keep = DVOL pct(30d) > {IV_ONLY_PCT} · "
        f"ADX keep = VB {{expansion}} / VWAP {{low_vol, unknown}} · "
        f"ADX(14) 15m fechado (range {adx_range:.0f} / trend {adx_trend:.0f}).",
        "",
        "## Concordância / discordância (mesmo conjunto de trades)",
        "",
        "| célula | n | net | WR |",
        "|---|---|---|---|",
    ]
    for k in ("both_keep", "iv_keep_adx_block", "iv_block_adx_keep", "both_block"):
        c = cells[k]
        lines.append(f"| {c['label']} | {c['n']} | {c['pnl_usd']:+.2f} | {c['win_rate']:.1f}% |")
    lines += [
        "",
        f"| keep-set | n | net |",
        "|---|---|---|",
        f"| IV (DVOL) | {len(iv_kept)} | {summarize(iv_kept,'')['net_pnl']:+.2f} |",
        f"| ADX (realizado) | {len(adx_kept)} | {summarize(adx_kept,'')['net_pnl']:+.2f} |",
        f"| união | {len(both_keep + iv_keep_adx_block + iv_block_adx_keep)} | "
        f"{summarize(both_keep + iv_keep_adx_block + iv_block_adx_keep,'')['net_pnl']:+.2f} |",
        f"| interseção (ambos) | {len(both_keep)} | {summarize(both_keep,'')['net_pnl']:+.2f} |",
        "",
        "## Regime ADX × tercil IV (net / n)",
        "",
        "| ADX \\ IV | low_iv | mid_iv | high_iv | no_iv |",
        "|---|---|---|---|---|",
    ]
    order = ["low_vol", "expansion", "trend", "unknown"]
    for r in order:
        if r not in joint:
            continue
        row = []
        for c in ("low_iv", "mid_iv", "high_iv", "no_iv"):
            trades = joint[r].get(c, [])
            s = summarize(trades, "")
            row.append(f"{s['net_pnl']:+.1f} / {s['n']}")
        lines.append(f"| {r} | " + " | ".join(row) + " |")
    lines += [
        "",
        "## Veredito",
        "",
        f"**{verdict}.** IV mantém/ADX bloqueia net {iv_ka:+.2f}; "
        f"IV bloqueia/ADX mantém net {iv_bk:+.2f}.",
        "",
        "## Contexto",
        "",
        "* Ambas as leituras são sobre os mesmos trades (raw backtests, sem gate).",
        "* DVOL é implícito (Deribit, diário, trailing 30d); ADX é realizado "
        "(candles 15m). Nenhuma mudança à janela congelada.",
        f"* JSON: `{jp.name}`.",
        "",
    ]
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nRelatório: {args.out}\nJSON: {jp}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
