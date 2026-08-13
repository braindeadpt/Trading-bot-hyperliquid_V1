#!/usr/bin/env python3
"""A/B: "ambas em high_iv only" — IV gate variant on independent 30d windows.

The single-window IV gate A/B (`scripts/iv_percentile_regime_gate_test.py`)
found high_iv (>p66 of the trailing-30d DVOL percentile) is the only positive
regime for BOTH VB and VWAP, suggesting a variant "keep both strategies only
in high_iv" -> **+42.99 USD (n=13)**. But that was one window (05-18..08-07),
dominated by the June DVOL spike — the classic overlap/selection caveat.

This script re-runs that exact variant on the SAME non-overlapping split the
regime-router A/B uses (`scripts/regime_router_a_b_test.split_windows`, 30d
windows), so the +42.99 can be confirmed (or not) without the caveat.

Reuses (no code duplication):
  * `split_windows`, `ms`, `summarize`, `build_cfg`, `run_strategy` from
    scripts/regime_router_a_b_test.py
  * `fetch_dvol`, `build_iv_percentile`, `iv_pct_at`, `dvol_series_for` from
    scripts/iv_percentile_regime_gate_test.py

The IV percentile is a rolling trailing-30d value, so a trade's high/low-IV
classification is window-independent — the split only changes *which* trades
are aggregated together, exactly like the regime-router A/B.

Usage:
  python scripts/iv_high_only_ab_split.py --start 2026-05-18 --end 2026-08-07 --split-days 30
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.iv_percentile_regime_gate_test import (  # noqa: E402
    DVOL_WINDOW_DAYS,
    build_iv_percentile,
    dvol_series_for,
    fetch_dvol,
    iv_pct_at,
)
from scripts.regime_router_a_b_test import (  # noqa: E402
    ms,
    run_strategy,
    split_windows,
    summarize,
)
from src.data.database import Database  # noqa: E402
from src.strategies.volatility_breakout import VolatilityBreakout  # noqa: E402
from src.strategies.vwap_deviation import VWAPDeviation  # noqa: E402
from src.utils.config import load_config  # noqa: E402

START_ARG, END_ARG = "2026-05-18", "2026-08-07"
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]
IV_ONLY_PCT = 66.7  # high_iv tercile cut (matches the +42.99 figure exactly)

SPECS = [
    ("VolatilityBreakout", VolatilityBreakout, "strategy.volatility_breakout"),
    ("VWAPDeviation", VWAPDeviation, "strategy.vwap_deviation"),
]


def run_window(
    cfg: Any,
    db: Database,
    w_start: str,
    w_end: str,
    symbols: List[str],
    iv_by_sym: Dict[str, List[tuple]],
    btc_iv: List[tuple],
) -> Dict[str, Any]:
    """Run both raw backtests over one window and apply the high_iv-only gate."""
    s_ms, e_ms = ms(w_start), ms(w_end, True)
    all_trades: List[Dict[str, Any]] = []
    per_strategy: Dict[str, Dict[str, Any]] = {}
    for name, cls, path in SPECS:
        print(f"    [{name}] raw backtest {w_start}..{w_end}...", flush=True)
        t1 = time.time()
        trades = run_strategy(cfg, db, cls, path, s_ms, e_ms, symbols)
        print(f"      {len(trades)} trades in {time.time()-t1:.0f}s", flush=True)
        for t in trades:
            t["_strategy"] = name
        all_trades.extend(trades)
        per_strategy[name] = {"all": trades}

    for t in all_trades:
        series = iv_by_sym.get(t["symbol"], btc_iv)
        t["_iv_pct"] = iv_pct_at(series, int(t["entry_time"]))

    high_iv_only = [t for t in all_trades if (t.get("_iv_pct") or 0.0) > IV_ONLY_PCT]
    blocked = [t for t in all_trades if t not in high_iv_only]
    return {
        "window": f"{w_start}..{w_end}",
        "without": summarize(all_trades, "sem gate"),
        "high_iv_only": summarize(high_iv_only, "high_iv only"),
        "blocked": summarize(blocked, "bloqueados"),
        "n_no_iv": sum(1 for t in all_trades if t.get("_iv_pct") is None),
        "per_strategy": {
            name: {
                "all": summarize(v["all"], name),
                "high_iv_only": summarize(
                    [t for t in v["all"] if (t.get("_iv_pct") or 0.0) > IV_ONLY_PCT],
                    name + " high_iv",
                ),
            }
            for name, v in per_strategy.items()
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default=START_ARG)
    ap.add_argument("--end", default=END_ARG)
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--split-days", type=int, default=30,
                    help="non-overlapping window size (default 30d)")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "IV_HIGH_ONLY_AB_SPLIT.md")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    db = Database(cfg.get("database.path", "data/live/bot.db"))

    print("=" * 78)
    print("  IV 'ambas em high_iv only' A/B — janelas independentes (sem sobreposição)")
    print(f"  {args.start} -> {args.end} | split {args.split_days}d | "
          f"high_iv = DVOL percentil > {IV_ONLY_PCT}")
    print("=" * 78)

    # DVOL fetched once for the full span (+60d lookback for the percentile).
    e_ms = ms(args.end, True)
    fetch_lo = ms(args.start) - 60 * 86_400_000
    print("\n[0] Fetching Deribit DVOL (full span + lookback)...")
    btc_raw = fetch_dvol("BTC", fetch_lo, e_ms)
    eth_raw = fetch_dvol("ETH", fetch_lo, e_ms)
    print(f"    BTC DVOL: {len(btc_raw)} dias | ETH vol index: {len(eth_raw)} dias")
    btc_iv = build_iv_percentile(btc_raw, DVOL_WINDOW_DAYS)
    eth_iv = build_iv_percentile(eth_raw, DVOL_WINDOW_DAYS)
    iv_by_sym = {sym: dvol_series_for(sym, btc_iv, eth_iv) for sym in symbols}

    windows = split_windows(args.start, args.end, args.split_days)
    print(f"\n[1] {len(windows)} janela(s): "
          + ", ".join(f"{a}..{b}" for a, b in windows))

    per_window: List[Dict[str, Any]] = []
    for w_start, w_end in windows:
        print(f"\n  === JANELA {w_start} -> {w_end} ===", flush=True)
        per_window.append(run_window(cfg, db, w_start, w_end, symbols, iv_by_sym, btc_iv))

    # ── Summary ──
    print("\n" + "=" * 78)
    print("  SUMÁRIO — 'ambas em high_iv only' por janela independente")
    print("=" * 78)
    hdr = f"{'janela':24}{'sem':>10}{'high_iv':>10}{'bloqueado':>10}{'n_hiv':>7}"
    print(hdr)
    tot_without = tot_hi = tot_blocked = tot_n = 0.0
    for w in per_window:
        wo, hi, bl = w["without"], w["high_iv_only"], w["blocked"]
        tot_without += wo["net_pnl"]
        tot_hi += hi["net_pnl"]
        tot_blocked += bl["net_pnl"]
        tot_n += hi["n"]
        print(f"{w['window']:24}{wo['net_pnl']:>10.2f}{hi['net_pnl']:>10.2f}"
              f"{bl['net_pnl']:>10.2f}{hi['n']:>7d}")
    print(f"{'TOTAL':24}{tot_without:>10.2f}{tot_hi:>10.2f}{tot_blocked:>10.2f}{tot_n:>7.0f}")

    positive_windows = sum(1 for w in per_window if w["high_iv_only"]["net_pnl"] > 0)
    print(f"\n  high_iv-only positivo em {positive_windows}/{len(per_window)} janelas "
          f"| net total {tot_hi:+.2f} USD")

    # ── Persist evidence ──
    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    detail = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "start": args.start,
        "end": args.end,
        "split_days": args.split_days,
        "iv_only_pct": IV_ONLY_PCT,
        "dvol_window_days": DVOL_WINDOW_DAYS,
        "windows": per_window,
    }
    jp = out_dir / f"iv_high_only_ab_split_{stamp}.json"
    jp.write_text(json.dumps(detail, indent=2, default=str), encoding="utf-8")

    lines = [
        "# IV 'ambas em high_iv only' — A/B com janelas independentes de 30d",
        "",
        f"Gerado: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · janela "
        f"{args.start} -> {args.end} · split {args.split_days}d · high_iv = "
        f"DVOL percentil(30d) > {IV_ONLY_PCT} · DVOL diário Deribit (BTC + ETH "
        "vol index; SOL/HYPE = proxy BTC).",
        "",
        "## Sumário por janela independente (sem sobreposição)",
        "",
        "| janela | sem gate | high_iv only | bloqueados | n high_iv |",
        "|---|---|---|---|---|",
    ]
    for w in per_window:
        wo, hi, bl = w["without"], w["high_iv_only"], w["blocked"]
        lines.append(f"| {w['window']} | {wo['net_pnl']:+.2f} (n={wo['n']}) | "
                     f"**{hi['net_pnl']:+.2f}** (n={hi['n']}, WR {hi['win_rate']:.0f}%) | "
                     f"{bl['net_pnl']:+.2f} (n={bl['n']}) | {hi['n']} |")
    lines += [
        f"| **TOTAL** | **{tot_without:+.2f}** | **{tot_hi:+.2f}** | "
        f"**{tot_blocked:+.2f}** | {int(tot_n)} |",
        "",
        f"high_iv-only positivo em **{positive_windows}/{len(per_window)}** janelas "
        f"independentes; net total {tot_hi:+.2f} USD.",
        "",
        "## Por estratégia (high_iv only)",
        "",
        "| janela | VB high_iv | VWAP high_iv |",
        "|---|---|---|",
    ]
    for w in per_window:
        vb = w["per_strategy"]["VolatilityBreakout"]["high_iv_only"]
        vw = w["per_strategy"]["VWAPDeviation"]["high_iv_only"]
        lines.append(f"| {w['window']} | {vb['net_pnl']:+.2f} (n={vb['n']}) | "
                     f"{vw['net_pnl']:+.2f} (n={vw['n']}) |")
    lines += [
        "",
        "## Veredito",
        "",
    ]
    # Build the verdict line based on robustness.
    verdict = _verdict_text(per_window, positive_windows, tot_hi)
    lines.append(verdict)
    lines += [
        "",
        "## Contexto",
        "",
        "* A variante veio de `docs/IV_PERCENTILE_REGIME_GATE.md` (+42.99, n=13, "
        "janela única 05-18..08-07, dominada pelo pico de DVOL de junho).",
        "* A classificação high/low-IV é *rolling* (janela 30d) — independente do "
        "split; o split só muda a agregação dos trades.",
        "* DVOL é informação implícita nova, aplicada post-hoc ao nível do trade — "
        "nenhuma mudança ao settings.yaml nem à janela congelada.",
        f"* JSON: `{jp.name}`.",
        "",
    ]
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nRelatório: {args.out}\nJSON: {jp}")
    db.close()
    return 0


def _verdict_text(per_window: List[Dict[str, Any]], positive_windows: int,
                  tot_hi: float) -> str:
    n_hi = sum(w["high_iv_only"]["n"] for w in per_window)
    if n_hi < 30:
        suffix = ("**INCONCLUSIVO** — n total de high_iv ainda < 30: a direção é "
                  f"sugestiva mas sem poder estatístico para promover o gate.")
    elif positive_windows == len(per_window):
        suffix = ("**ROBUSTO** — high_iv-only positivo em todas as janelas "
                  "independentes: o +42.99 não é artefacto de sobreposição.")
    elif positive_windows == 0:
        suffix = ("**REJEITADO** — high_iv-only negativo em todas as janelas "
                  "independentes: o +42.99 era artefacto da janela única.")
    else:
        suffix = (f"**MISTO** — positivo em {positive_windows}/{len(per_window)} "
                  "janelas: o edge concentra-se num subconjunto de regimes, não é "
                  "uniforme; promover só com mais amostra.")
    return (f"Net high_iv-only {tot_hi:+.2f} USD (n={n_hi}) em janelas independentes. "
            f"{suffix}")


if __name__ == "__main__":
    raise SystemExit(main())
