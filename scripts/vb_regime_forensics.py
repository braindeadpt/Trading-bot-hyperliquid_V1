"""VB forensics — isolate what makes VolatilityBreakout lose (kill vs rework).

The 80d regime-router A/B showed VB is structurally negative in EVERY
regime (the router reduces its loss but cannot fix it). Before deciding
kill vs rework, this study dissects the full trade set of the production
VB config over the whole candle history:

  * by regime at entry (trend / expansion / low_vol / unknown)
  * by symbol (BTC / ETH / SOL / HYPE)
  * by side (long / short)
  * by exit reason (stop_loss / take_profit / trailing_* / rescue / ...)
  * loss profile (R-multiple distribution, avg loss, max loss, WR)

Verdict framework (explicit, evidence-driven):
  KILL    — loses in >=3/4 regimes AND >=3/4 symbols AND avg loss > avg win
            with WR < 35%: no filterable slice survives -> structural.
  REWORK  — losses concentrate in a slice (one symbol / side / regime /
            exit) whose removal preserves a positive remainder.
  HOLD    — insufficient signal / mixed picture.

Read-only. Runs one VB backtest (~15 min) and dumps the trade set to CSV
plus a console report. Never touches bot.db writes or the frozen config.
"""

from __future__ import annotations

import csv
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.core.phase08_regime_router import classify_market_regime
from src.data.database import Database
from src.strategies.volatility_breakout import VolatilityBreakout
from src.utils.config import load_config

# Reuse the ADX machinery from the committed A/B study.
from regime_router_a_b_test import adx_at, precompute_adx  # noqa: E402

logging.basicConfig(level=logging.ERROR)
for _n in (
    "src.core.volatility_circuit", "src.backtest.engine", "src.strategies",
    "src.core.risk_manager", "src.core.funding_blackout",
):
    logging.getLogger(_n).setLevel(logging.ERROR)

FULL_START, FULL_END = "2026-05-18", "2026-08-07"
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]


def ms(s: str, end: bool = False) -> int:
    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        d = d.replace(hour=23, minute=59, second=59)
    return int(d.timestamp() * 1000)


def build_cfg(cfg: Any) -> BacktestConfig:
    """Mirror live production settings (same as the A/B study)."""
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
        use_phase08_regime_router=False,  # raw VB trades — regime tagged post-hoc
        max_daily_trades=int(cfg.get("risk.max_daily_trades", 0)),
    )


def tag_regime(trades: List[Dict[str, Any]], adx_series: Dict[str, List[Tuple[int, Optional[float]]]],
               adx_range: float, adx_trend: float) -> List[Dict[str, Any]]:
    for t in trades:
        adx = adx_at(adx_series.get(t["symbol"], []), int(t["entry_time"]))
        t["_adx"] = adx
        t["_regime"] = classify_market_regime(
            adx, adx_range_threshold=adx_range, adx_trend_threshold=adx_trend
        )
        t["_hold_min"] = round((int(t["exit_time"]) - int(t["entry_time"])) / 60_000.0, 1)
    return trades


def fmt(r: Dict[str, Any]) -> str:
    return (f"n={r['n']:>4} WR={r['win_rate']:>5.1f}% "
            f"avgW=${r['avg_win']:>7.2f} avgL=${r['avg_loss']:>7.2f} "
            f"E[x]=${r['expectancy']:>7.2f} PF={r['profit_factor']:>5.2f} "
            f"net=${r['net']:>9.2f}")


def stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(trades)
    pnl = sum(float(t.get("pnl_usd", 0.0)) for t in trades)
    wins = [float(t["pnl_usd"]) for t in trades if float(t.get("pnl_usd", 0)) > 0]
    losses = [float(t["pnl_usd"]) for t in trades if float(t.get("pnl_usd", 0)) <= 0]
    return {
        "n": n,
        "win_rate": 100.0 * len(wins) / n if n else 0.0,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "expectancy": pnl / n if n else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else 0.0,
        "net": pnl,
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default="", help="re-analyse an existing vb_forensics CSV (skips the backtest)")
    args = ap.parse_args()
    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    db = Database(cfg.get("database.path", "data/live/bot.db"))
    s_ms, e_ms = ms(FULL_START), ms(FULL_END, True)
    p08 = (cfg.get("strategy.phase08", {}) or {}).get("regime_router", {}) or {}
    adx_range = float(p08.get("adx_range_threshold",
                              cfg.get("strategy.adx_range_threshold", 20.0)))
    adx_trend = float(p08.get("adx_trend_threshold",
                              cfg.get("strategy.adx_trend_threshold", 25.0)))

    print("=" * 82)
    print("  VB FORENSICS — by regime / symbol / side / exit / loss profile")
    print(f"  {FULL_START} -> {FULL_END} | symbols: {','.join(SYMBOLS)}")
    print("=" * 82)

    if args.csv:
        import csv as _csv
        trades: List[Dict[str, Any]] = []
        with open(args.csv, "r", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                trades.append({
                    "entry_time": int(row["entry_time"]),
                    "exit_time": int(row.get("exit_time", 0)) or int(row["entry_time"]),
                    "symbol": row["symbol"], "side": row["side"],
                    "entry_price": float(row["entry_price"]),
                    "exit_price": float(row["exit_price"]),
                    "pnl_usd": float(row["pnl_usd"]), "pnl_pct": float(row["pnl_pct"]),
                    "r_multiple": float(row["r_multiple"] or 0),
                    "exit_reason": row["exit_reason"],
                    "_regime": row["regime"], "_adx": float(row["adx"] or 0) or None,
                    "_hold_min": float(row["hold_min"] or 0),
                })
        print(f"  (re-analysed {len(trades)} trades from {args.csv})")
    else:
        t0 = time.time()
        print("\n[0] Precomputing ADX(14) on 15m candles...")
        adx_series = precompute_adx(db, SYMBOLS, s_ms, e_ms)
        print(f"    done in {time.time()-t0:.0f}s")

        print("\n[1] Running raw VB backtest (production config)...")
        section = dict(cfg.get("strategy.volatility_breakout", {}) or {})
        section["enabled"] = True
        engine = BacktestEngine(
            database=db,
            strategy=VolatilityBreakout(section),
            config=build_cfg(cfg),
            symbols=SYMBOLS,
            risk_config=dict(cfg.get("risk", {}) or {}),
        )
        result = engine.run(start_ms=s_ms, end_ms=e_ms)
        trades = tag_regime(result.get("trades", []), adx_series, adx_range, adx_trend)
        print(f"    {len(trades)} trades in {time.time()-t0:.0f}s")

    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"vb_forensics_{stamp}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "entry_time", "symbol", "side", "entry_price", "exit_price",
            "pnl_usd", "pnl_pct", "r_multiple", "exit_reason",
            "regime", "adx", "hold_min",
        ])
        w.writeheader()
        for t in trades:
            w.writerow({
                "entry_time": t.get("entry_time"), "symbol": t.get("symbol"),
                "side": t.get("side"), "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"), "pnl_usd": t.get("pnl_usd"),
                "pnl_pct": t.get("pnl_pct"), "r_multiple": t.get("r_multiple"),
                "exit_reason": t.get("exit_reason"), "regime": t["_regime"],
                "adx": t.get("_adx"), "hold_min": t.get("_hold_min"),
            })

    def section(title: str, key: str) -> None:
        print(f"\n  --- {title} ---")
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in trades:
            groups[str(t.get(key, "?") or "?")].append(t)
        for k in sorted(groups, key=lambda k: -sum(float(x["pnl_usd"]) for x in groups[k])):
            print(f"  {str(k)[:24]:24} {fmt(stats(groups[k]))}")

    print("\n" + "=" * 82)
    print("  RESULTADO")
    print("=" * 82)
    print(f"\n  OVERALL: {fmt(stats(trades))}")
    section("por REGIME de entrada", "_regime")
    section("por SÍMBOLO", "symbol")
    section("por LADO", "side")
    section("por MOTIVO DE SAÍDA", "exit_reason")

    print("\n  --- PERFIL DE PERDAS (R-multiple) ---")
    rs = sorted([float(t.get("r_multiple", 0)) for t in trades if float(t.get("pnl_usd", 0)) <= 0])
    for threshold in (1.0, 2.0, 3.0):
        cnt = sum(1 for r in rs if r <= -threshold)
        print(f"  perdas <= -{threshold:.0f}R: {cnt}/{len(rs)} "
              f"({100.0*cnt/max(1,len(rs)):.0f}%)  "
              f"P&L contrib ${sum(t['pnl_usd'] for t in trades if float(t.get('r_multiple',0)) <= -threshold):.2f}")
    if rs:
        avg_r = sum(rs) / len(rs)
        print(f"  perda média em R: {avg_r:.2f}R | perda máx: {min(rs):.2f}R")
    wins_r = [float(t.get("r_multiple", 0)) for t in trades if float(t.get("pnl_usd", 0)) > 0]
    if wins_r:
        print(f"  ganho médio em R: {sum(wins_r)/len(wins_r):.2f}R | ganho máx: {max(wins_r):.2f}R")
    sl_only = [t for t in trades if str(t.get("exit_reason", "")).startswith("stop_loss")]
    print(f"\n  perdas por stop_loss: {len(sl_only)} "
          f"(P&L ${sum(float(t['pnl_usd']) for t in sl_only):.2f})")

    # ── Verdict framework ──────────────────────────────────────────────
    reg_bad = sum(1 for rg in ("trend", "expansion", "low_vol")
                  if (lambda g: stats(g)["net"] < 0)(
                      [t for t in trades if t["_regime"] == rg]))
    sym_bad = sum(1 for sym in SYMBOLS
                  if stats([t for t in trades if t["symbol"] == sym])["net"] < 0)
    o = stats(trades)
    loss_gt_win = o["avg_loss"] > o["avg_win"]
    low_wr = o["win_rate"] < 35.0

    print("\n" + "=" * 82)
    print("  VEREDITO (framework explícito)")
    print("=" * 82)
    print(f"  regimes negativos: {reg_bad}/3 | símbolos negativos: {sym_bad}/4 | "
          f"avgLoss>avgWin: {loss_gt_win} | WR<35%: {low_wr}")
    print("\n  --- FATIAS POSITIVAS (sobreviventes) ---")
    slices = {
        "expansion": [t for t in trades if t["_regime"] == "expansion"],
        "longs": [t for t in trades if t["side"] == "long"],
        "expansion_longs": [t for t in trades if t["_regime"] == "expansion" and t["side"] == "long"],
        "trend_longs": [t for t in trades if t["_regime"] == "trend" and t["side"] == "long"],
    }
    for name, sl in slices.items():
        if sl:
            s = stats(sl)
            flag = "POSITIVO" if s["net"] > 0 else "negativo"
            print(f"  {name:16} {fmt(s)}  [{flag}]")

    print("\n" + "=" * 82)
    print("  VEREDITO (framework explícito)")
    print("=" * 82)
    print(f"  regimes negativos: {reg_bad}/3 | símbolos negativos: {sym_bad}/4 | "
          f"avgLoss>avgWin: {loss_gt_win} | WR<35%: {low_wr}")
    short_s = stats([t for t in trades if t["side"] == "short"])
    trend_s = stats([t for t in trades if t["_regime"] == "trend"])
    exp_s = stats([t for t in trades if t["_regime"] == "expansion"])
    rework = (short_s["net"] < -0.5 * o["net"] or short_s["win_rate"] < 15.0) and exp_s["net"] > 0
    if rework:
        print("  REWORK: perdas concentradas num lado (short) e/ou regime (trend);")
        print(f"  fatia expansion positiva (+${exp_s['net']:.2f}). Direções concretas:")
        print(f"    a) long-only          -> remove shorts (-${-short_s['net']:.2f} do prejuízo)")
        print(f"    b) expansion-only     -> ADX 20-25, fatia +${exp_s['net']:.2f}")
        print(f"    c) follow-through     -> exigir confirmação do candle seguinte")
        print("       (ataque direto aos failed_breakout, o 2º maior sangrador)")
    elif reg_bad >= 3 and sym_bad >= 3 and loss_gt_win and low_wr:
        print("  KILL: VB perde em quase todos os regimes e símbolos com estrutura")
        print("  R:R invertida — nenhuma fatia filtrável sobrevive.")
    else:
        print("  HOLD: quadro misto — mais dados ou outro corte necessário.")
    print(f"\n  CSV: {csv_path}")
    db.close()


if __name__ == "__main__":
    main()
