"""Measure the CURRENT production ruleset over the full candle history.

Runs the live execution strategies (ChecklistMeta + VWAPDeviation) with the
production config over 2026-05-18 -> 2026-08-07, producing a homogeneous
sample (unlike the fragmented live trade history, which spans ~10 config
versions).

Outputs: console report + CSV/JSON in data/backtests/.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.backtest.monte_carlo import bootstrap_metrics
from src.data.database import Database
from src.strategies.checklist_meta import ChecklistMeta
from src.strategies.vwap_deviation import VWAPDeviation
from src.utils.config import load_config

logging.basicConfig(level=logging.ERROR)
for _n in (
    "src.core.volatility_circuit", "src.backtest.engine", "src.strategies",
    "src.core.risk_manager", "src.core.funding_blackout", "src.core.phase08_regime_router",
):
    logging.getLogger(_n).setLevel(logging.ERROR)

FULL_START, FULL_END = "2026-05-18", "2026-08-07"
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]

STRATEGIES = [
    ("ChecklistMeta", ChecklistMeta, "strategy.checklist_meta"),
    ("VWAPDeviation", VWAPDeviation, "strategy.vwap_deviation"),
]


def ms(s: str, end: bool = False) -> int:
    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        d = d.replace(hour=23, minute=59, second=59)
    return int(d.timestamp() * 1000)


def build_cfg(cfg: Any) -> BacktestConfig:
    """Backtest config mirroring live production settings."""
    return BacktestConfig(
        initial_capital=float(cfg.get("risk.initial_capital", 10_000.0)),
        commission_pct=float(cfg.get("risk.taker_fee_pct", 0.035)),
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
        use_phase08_regime_router=bool(cfg.get("strategy.phase08.enabled", True)),
        max_daily_trades=int(cfg.get("risk.max_daily_trades", 0)),
    )


def run_one(
    cfg: Any, db: Database, cls: Any, section_path: str,
    start_ms: int, end_ms: int, symbols: List[str],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    section = dict(cfg.get(section_path, {}) or {})
    section["enabled"] = True
    engine = BacktestEngine(
        database=db,
        strategy=cls(section),
        config=build_cfg(cfg),
        symbols=symbols,
        risk_config=dict(cfg.get("risk", {}) or {}),
    )
    try:
        result = engine.run(start_ms=start_ms, end_ms=end_ms)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200], "n_trades": 0}, []

    m = result.get("metrics", {})
    trades = result.get("trades", [])
    pnl = sum(float(t.get("pnl_usd", 0.0)) for t in trades)
    fees = sum(float(t.get("fees", t.get("fee_usd", 0.0)) or 0.0) for t in trades)
    wins = [float(t["pnl_usd"]) for t in trades if float(t.get("pnl_usd", 0)) > 0]
    losses = [float(t["pnl_usd"]) for t in trades if float(t.get("pnl_usd", 0)) <= 0]
    n = len(trades)
    return {
        "n_trades": n,
        "win_rate": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "expectancy": round(pnl / n, 2) if n else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) else 0.0,
        "sharpe": round(float(m.get("sharpe_ratio", 0)), 3),
        "max_dd_pct": round(float(m.get("max_drawdown", 0)) * 100, 2),
        "net_pnl": round(pnl, 2),
        "fees": round(fees, 2),
        "gross_pnl": round(pnl + fees, 2),
    }, trades


def rolling_windows(start: str, end: str, span_days: int = 14, step_days: int = 7):
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    cur = d0
    while cur + timedelta(days=span_days) <= d1:
        w_end = cur + timedelta(days=span_days - 1)
        yield (cur.strftime("%m-%d") + "_" + w_end.strftime("%m-%d"),
               cur.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d"))
        cur += timedelta(days=step_days)


def main() -> None:
    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    db = Database(cfg.get("database.path", "data/live/bot.db"))
    out: Dict[str, Any] = {"generated": datetime.now(timezone.utc).isoformat(),
                           "window": [FULL_START, FULL_END], "symbols": SYMBOLS}

    print("=" * 78)
    print("  MEDICAO DO RULESET ACTUAL — historico completo")
    print(f"  {FULL_START} -> {FULL_END} | simbolos: {','.join(SYMBOLS)}")
    print("=" * 78)

    s_ms, e_ms = ms(FULL_START), ms(FULL_END, True)

    # ── 1. Full history, per strategy ──────────────────────────────
    print("\n[1] FULL HISTORY (por estrategia)\n")
    hdr = f"{'strategy':16}{'n':>5}{'WR%':>7}{'avgW':>9}{'avgL':>9}{'E[x]':>8}{'PF':>7}{'Sharpe':>8}{'maxDD%':>8}{'net':>10}"
    print(hdr); print("-" * len(hdr))
    all_trades: Dict[str, List[Dict]] = {}
    out["full_history"] = {}
    for name, cls, path in STRATEGIES:
        t0 = time.time()
        res, trades = run_one(cfg, db, cls, path, s_ms, e_ms, SYMBOLS)
        all_trades[name] = trades
        out["full_history"][name] = res
        if res.get("error"):
            print(f"{name:16} ERRO: {res['error'][:50]}")
            continue
        print(f"{name:16}{res['n_trades']:>5}{res['win_rate']:>7}{res['avg_win']:>9}"
              f"{res['avg_loss']:>9}{res['expectancy']:>8}{res['profit_factor']:>7}"
              f"{res['sharpe']:>8}{res['max_dd_pct']:>8}{res['net_pnl']:>10}  ({time.time()-t0:.0f}s)")

    # ── 2. Per symbol ──────────────────────────────────────────────
    print("\n[2] POR SIMBOLO\n")
    out["per_symbol"] = {}
    for name, cls, path in STRATEGIES:
        out["per_symbol"][name] = {}
        for sym in SYMBOLS:
            res, _ = run_one(cfg, db, cls, path, s_ms, e_ms, [sym])
            out["per_symbol"][name][sym] = res
            if res.get("error") or res["n_trades"] == 0:
                print(f"  {name:16}{sym:6} sem trades")
                continue
            print(f"  {name:16}{sym:6} n={res['n_trades']:>4} WR={res['win_rate']:>5}% "
                  f"E[x]={res['expectancy']:>7} PF={res['profit_factor']:>6} net={res['net_pnl']:>9}")

    # ── 3. Rolling windows (consistencia temporal) ─────────────────
    print("\n[3] JANELAS ROLANTES (14d, passo 7d) — consistencia temporal\n")
    out["rolling"] = {}
    for name, cls, path in STRATEGIES:
        out["rolling"][name] = []
        print(f"  --- {name} ---")
        for label, w0, w1 in rolling_windows(FULL_START, FULL_END):
            res, _ = run_one(cfg, db, cls, path, ms(w0), ms(w1, True), SYMBOLS)
            res["window"] = label
            out["rolling"][name].append(res)
            flag = "+" if res.get("expectancy", 0) > 0 else "-"
            print(f"   {flag} {label}  n={res['n_trades']:>4} WR={res['win_rate']:>5}% "
                  f"E[x]={res['expectancy']:>7} PF={res['profit_factor']:>6} net={res['net_pnl']:>9}")
        rows = [r for r in out["rolling"][name] if r["n_trades"] > 0]
        pos = sum(1 for r in rows if r["expectancy"] > 0)
        print(f"   => janelas com E[x]>0: {pos}/{len(rows)}\n")

    # ── 4. Monte Carlo ─────────────────────────────────────────────
    print("\n[4] MONTE CARLO (bootstrap 2000 iter)\n")
    out["monte_carlo"] = {}
    cap = float(cfg.get("risk.initial_capital", 10_000.0))
    for name, trades in all_trades.items():
        if len(trades) < 10:
            print(f"  {name:16} amostra insuficiente ({len(trades)})")
            continue
        mc = bootstrap_metrics(trades, initial_capital=cap, n_iter=2000, seed=42)
        d = mc.__dict__ if hasattr(mc, "__dict__") else dict(mc)
        out["monte_carlo"][name] = {k: (round(v, 4) if isinstance(v, float) else v)
                                    for k, v in d.items()}
        print(f"  {name:16} " + " ".join(
            f"{k}={round(v,3) if isinstance(v,float) else v}" for k, v in list(d.items())[:8]))

    outdir = ROOT / "data" / "backtests"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = outdir / f"current_ruleset_{stamp}.json"
    p.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON: {p}")


if __name__ == "__main__":
    main()
