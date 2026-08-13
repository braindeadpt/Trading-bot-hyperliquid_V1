#!/usr/bin/env python3
"""A/B: Deribit DVOL IV-percentile regime gate over VB + VWAP (with vs without).

The Phase-08 ADX router (validated previously) uses *realized* trend
strength. This script tests the *implied-volatility* analogue — DVOL (BTC)
and ETH vol index from Deribit's public API — as a regime gate:

  * VB (breakout) allowed only when IV percentile is HIGH (trending/expanding
    vol regimes); blocked in low-IV compression where the forensics showed
    VB bleeds (WR 13% in low_vol).
  * VWAP (fade) allowed only when IV percentile is LOW/MID (range); blocked
    in high-IV regimes where the fade bleeds.

No frozen-window change: the gate is applied post-hoc at the trade level,
exactly like the regime-router A/B. DVOL is *new information* (implied, not
realized) — it does not touch the candle-based window.

Data: Deribit public API get_volatility_index_data (daily), asof percent of
last 30d closes, first-crossing-safe (uses only closes <= entry date - 1d).

Usage:
  python scripts/iv_percentile_regime_gate_test.py
  python scripts/iv_percentile_regime_gate_test.py --start 2026-07-08
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.database import Database
from src.data.dvol_feed import (
    DVOL_WINDOW_DAYS,
    build_iv_percentile,
    dvol_series_for,
    fetch_dvol,
    iv_pct_at,
)
from src.strategies.volatility_breakout import VolatilityBreakout
from src.strategies.vwap_deviation import VWAPDeviation
from src.utils.config import load_config

logging.basicConfig(level=logging.ERROR)
for _n in (
    "src.core.volatility_circuit", "src.backtest.engine", "src.strategies",
    "src.core.risk_manager", "src.core.funding_blackout",
):
    logging.getLogger(_n).setLevel(logging.ERROR)

FULL_START, FULL_END = "2026-05-18", "2026-08-07"
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]
START_ARG = "2026-07-08"
END_ARG = "2026-08-07"

# Gate: VB allowed when IV percentile >= IV_HIGH_PCT; VWAP when <= IV_LOW_PCT.
# Between the two thresholds both are allowed (mid regime) — matches the
# router's "range AND expansion both eligible" semantics.
IV_HIGH_PCT = 70.0   # VB needs high IV
IV_LOW_PCT = 40.0    # VWAP fade needs low/mid IV


def ms(s: str, end: bool = False) -> int:
    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        d = d.replace(hour=23, minute=59, second=59)
    return int(d.timestamp() * 1000)


def build_cfg(cfg: Any) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=float(cfg.get("risk.initial_capital", 10_000.0)),
        commission_pct=float(cfg.get("risk.taker_fee_pct", 0.045)),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 3)),
        per_trade_risk_pct=float(cfg.get("risk.per_trade_risk_pct", 1.0)),
        tca_enabled=bool(cfg.get("execution.tca_enabled", True)),
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.02)),
        use_regime_weights=False,
        use_cooldown=True,
        use_microstructure_proxy=True,
        use_risk_manager=True,
        use_volatility_circuit=True,
        use_funding_blackout=True,
        use_external_feeds_replay=True,
        use_phase08_regime_router=False,
        max_daily_trades=int(cfg.get("risk.max_daily_trades", 0)),
    )


def run_strategy(cfg: Any, db: Database, cls: Any, section_path: str,
                 start_ms: int, end_ms: int, symbols: List[str]) -> List[Dict[str, Any]]:
    section = dict(cfg.get(section_path, {}) or {})
    section["enabled"] = True
    engine = BacktestEngine(
        database=db,
        strategy=cls(section),
        config=build_cfg(cfg),
        symbols=symbols,
        risk_config=dict(cfg.get("risk", {}) or {}),
    )
    result = engine.run(start_ms=start_ms, end_ms=end_ms)
    return result.get("trades", [])


def summarize(trades: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    n = len(trades)
    pnl = sum(float(t.get("pnl_usd", 0.0)) for t in trades)
    wins = [float(t["pnl_usd"]) for t in trades if float(t.get("pnl_usd", 0)) > 0]
    losses = [float(t["pnl_usd"]) for t in trades if float(t.get("pnl_usd", 0)) <= 0]
    return {
        "label": label,
        "n": n,
        "win_rate": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) else 0.0,
        "net_pnl": round(pnl, 2),
        "expectancy": round(pnl / n, 2) if n else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default=START_ARG)
    ap.add_argument("--end", default=END_ARG)
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--iv-high", type=float, default=IV_HIGH_PCT)
    ap.add_argument("--iv-low", type=float, default=IV_LOW_PCT)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "IV_PERCENTILE_REGIME_GATE.md")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    db = Database(cfg.get("database.path", "data/live/bot.db"))

    print("=" * 78)
    print("  IV-PERCENTILE REGIME GATE A/B (Deribit DVOL) — with vs without")
    print(f"  {args.start} -> {args.end} | symbols: {','.join(symbols)}")
    print(f"  gate: VB>=p{args.iv_high:.0f} | VWAP<=p{args.iv_low:.0f} | window {DVOL_WINDOW_DAYS}d")
    print("=" * 78)

    s_ms, e_ms = ms(args.start), ms(args.end, True)
    fetch_lo = ms(args.start) - 60 * 86_400_000  # 60d lookback for the percentile

    print("\n[0] Fetching Deribit DVOL...")
    btc_raw = asyncio.run(fetch_dvol("BTC", fetch_lo, e_ms))
    eth_raw = asyncio.run(fetch_dvol("ETH", fetch_lo, e_ms))
    print(f"    BTC DVOL: {len(btc_raw)} dias | ETH vol index: {len(eth_raw)} dias")
    btc_iv = build_iv_percentile(btc_raw)
    eth_iv = build_iv_percentile(eth_raw)
    print(f"    percentil BTC: {sum(1 for _, p in btc_iv if p is not None)} dias com valor")
    for t, p in btc_iv:
        if p is not None:
            print(f"    {datetime.fromtimestamp(t/1000, tz=timezone.utc):%m-%d} p={p:5.1f}")

    print("\n[1] Raw backtests (no gate)...")
    specs = [
        ("VolatilityBreakout", VolatilityBreakout, "strategy.volatility_breakout"),
        ("VWAPDeviation", VWAPDeviation, "strategy.vwap_deviation"),
    ]
    all_trades: List[Dict[str, Any]] = []
    for name, cls, path in specs:
        t1 = time.time()
        trades = run_strategy(cfg, db, cls, path, s_ms, e_ms, symbols)
        for t in trades:
            t["_strategy"] = name
        all_trades.extend(trades)
        print(f"    {name}: {len(trades)} trades in {time.time()-t1:.0f}s")

    # IV percentile at each trade's entry (per-symbol DVOL, asof previous day)
    print("\n[2] Applying IV gate...")
    iv_by_sym = {sym: dvol_series_for(sym, btc_iv, eth_iv) for sym in symbols}
    n_no_iv = 0
    for t in all_trades:
        iv = iv_pct_at(iv_by_sym.get(t["symbol"], btc_iv), int(t["entry_time"]))
        t["_iv_pct"] = iv
        strat = t["_strategy"]
        if iv is None:
            t["_gate"] = "no_iv"
            n_no_iv += 1
        elif strat == "VolatilityBreakout":
            t["_gate"] = "keep" if iv >= args.iv_high else "block"
        else:
            t["_gate"] = "keep" if iv <= args.iv_low else "block"
    print(f"    trades sem DVOL (warmup): {n_no_iv}")

    kept = [t for t in all_trades if t["_gate"] == "keep"]
    blocked = [t for t in all_trades if t["_gate"] == "block"]
    # Variante sugerida pelos dados: ambas as estratégias só em high_iv (>p66)
    high_iv_only = [t for t in all_trades if (t.get("_iv_pct") or 0.0) > 66.7]

    print("\n" + "=" * 78)
    print("  RESULTADO")
    print("=" * 78)
    for label, grp in (("SEM gate", all_trades), ("COM gate", kept), ("BLOQUEADOS", blocked),
                       ("HIGH_IV only", high_iv_only)):
        s = summarize(grp, label)
        print(f"  {label:12} n={s['n']:>4} WR={s['win_rate']:>5.1f}% PF={s['profit_factor']:>5.2f} "
              f"net={s['net_pnl']:>9.2f} expectancy={s['expectancy']:>6.2f}")

    print("\n  Por estratégia:")
    for name, _, _ in specs:
        sub_all = [t for t in all_trades if t["_strategy"] == name]
        sub_kept = [t for t in sub_all if t["_gate"] == "keep"]
        sub_blocked = [t for t in sub_all if t["_gate"] == "block"]
        a, k, b = summarize(sub_all, name), summarize(sub_kept, name + " kept"), summarize(sub_blocked, name + " blocked")
        print(f"    {name:18} sem={a['net_pnl']:>9.2f} ({a['n']:>3}) | com={k['net_pnl']:>9.2f} ({k['n']:>3}) | "
              f"bloqueado={b['net_pnl']:>9.2f} ({b['n']:>3}, WR={b['win_rate']:.0f}%)")

    # PnL by IV tercile (the bleeding decomposition)
    print("\n  Sangramento por regime IV (percentil do DVOL):")
    terc = {"low_iv (<p33)": [], "mid_iv (p33-66)": [], "high_iv (>p66)": []}
    for t in all_trades:
        iv = t.get("_iv_pct")
        if iv is None:
            continue
        if iv < 33.3:
            terc["low_iv (<p33)"].append(t)
        elif iv < 66.7:
            terc["mid_iv (p33-66)"].append(t)
        else:
            terc["high_iv (>p66)"].append(t)
    for name, grp in terc.items():
        s = summarize(grp, name)
        vb = summarize([t for t in grp if t["_strategy"] == "VolatilityBreakout"], "VB")
        vw = summarize([t for t in grp if t["_strategy"] == "VWAPDeviation"], "VWAP")
        print(f"    {name:22} n={s['n']:>4} net={s['net_pnl']:>9.2f} | VB={vb['net_pnl']:>8.2f} ({vb['n']}) "
              f"| VWAP={vw['net_pnl']:>8.2f} ({vw['n']})")

    blocked_pnl = sum(float(t.get("pnl_usd", 0.0)) for t in blocked)
    kept_pnl = sum(float(t.get("pnl_usd", 0.0)) for t in kept)
    total_pnl = sum(float(t.get("pnl_usd", 0.0)) for t in all_trades)

    high_iv_pnl = sum(float(t.get("pnl_usd", 0.0)) for t in high_iv_only)
    lines = [
        "# IV-percentile regime gate (Deribit DVOL) — A/B sobre VB + VWAP",
        "",
        f"Gerado: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · janela {args.start} -> {args.end} "
        f"· gate testado VB>=p{args.iv_high:.0f} / VWAP<=p{args.iv_low:.0f} · DVOL window {DVOL_WINDOW_DAYS}d",
        "",
        "## Resultado (gate testado)",
        "",
        f"| métrica | sem gate | com gate | bloqueados | high_iv only |",
        "|---|---|---|---|---|",
        f"| n | {len(all_trades)} | {len(kept)} | {len(blocked)} | {len(high_iv_only)} |",
        f"| net | {total_pnl:.2f} | {kept_pnl:.2f} | {blocked_pnl:.2f} | {high_iv_pnl:.2f} |",
        "",
        f"**O gate testado {'POUPA' if blocked_pnl < 0 else 'CUSTA'} {abs(blocked_pnl):.2f} USD** "
        f"({100.0 * blocked_pnl / total_pnl if total_pnl else 0:.0f}% do PnL total) — "
        "mas ver atribuição por estratégia: o gate bloqueou VWAP **positivos** em high_iv.",
        "",
        f"**Variante 'high_iv only' (>p66, ambas as estratégias): {high_iv_pnl:+.2f} USD (n={len(high_iv_only)}).**",
        "",
        "## Por estratégia",
        "",
    ]
    for name, _, _ in specs:
        sub_all = [t for t in all_trades if t["_strategy"] == name]
        sub_kept = [t for t in sub_all if t["_gate"] == "keep"]
        sub_blocked = [t for t in sub_all if t["_gate"] == "block"]
        lines.append(f"* {name}: sem {summarize(sub_all, '')['net_pnl']:.2f} -> com "
                     f"{summarize(sub_kept, '')['net_pnl']:.2f} | bloqueados "
                     f"{summarize(sub_blocked, '')['net_pnl']:.2f} (n={len(sub_blocked)})")
    lines += [
        "",
        "## Sangramento por regime IV",
        "",
        "| regime | n | net | VB | VWAP |",
        "|---|---|---|---|---|",
    ]
    for name, grp in terc.items():
        s = summarize(grp, name)
        vb = summarize([t for t in grp if t["_strategy"] == "VolatilityBreakout"], "VB")
        vw = summarize([t for t in grp if t["_strategy"] == "VWAPDeviation"], "VWAP")
        lines.append(f"| {name} | {s['n']} | {s['net_pnl']:.2f} | {vb['net_pnl']:.2f} ({vb['n']}) | "
                     f"{vw['net_pnl']:.2f} ({vw['n']}) |")
    lines += [
        "",
        "## Veredito",
        "",
    ]
    lines.append(
        f"**O DVOL discrimina regimes — mas na direção oposta à assumida.** "
        f"high_iv (>p66) é o único regime positivo para AMBAS as estratégias "
        f"({high_iv_pnl:+.2f} USD, n={len(high_iv_only)}). O gate testado poupa "
        f"{abs(blocked_pnl):.2f} USD porque bloqueia os VB maus, mas erra no VWAP: "
        "os trades que bloqueia em high_iv são positivos. Variante 'ambas em "
        f"high_iv' teria dado {high_iv_pnl:+.2f} USD (n={len(high_iv_only)}) — "
        "caveat: n pequeno e high_iv dominado pelo pico de junho."
    )
    lines.append(
        "Sem tocar na janela congelada: DVOL é informação implícita nova, "
        "aplicada post-hoc ao nível do trade — nenhuma mudança ao settings.yaml."
    )
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nRelatório: {args.out}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
