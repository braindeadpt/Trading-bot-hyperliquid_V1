"""Grid search over ensemble parameters — find optimal config for the reduced strategy set.

Sweeps threshold, min_agreeing, high_conviction_threshold, and exclude list on a
SHORT validation window (Jun 23-25, ~72 combos), then validates top 5 on FULL window (May 18-Jun 25).
"""
from __future__ import annotations
import sys, os, logging, itertools, csv
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
logging.basicConfig(level=logging.WARNING)
for mod in ["src.backtest.engine","src.strategies","src.core.volatility_circuit"]:
    logging.getLogger(mod).setLevel(logging.ERROR)

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.database import Database
from src.strategies.factory import build_ensemble
from src.utils.config import load_config


WINDOWS = {
    "train": ("2026-05-18", "2026-06-25"),
    "valid": ("2026-06-23", "2026-06-25"),
}

GRID = {
    "threshold": [0.10, 0.15, 0.20, 0.25],
    "min_agreeing": [1, 2],
    "hc_threshold": [0.65, 0.70, 0.75],
    "exclude": [
        "current",     # ["VWAPDeviation"]
        "empty",       # []
        "only_funding", # ["FundingArbitrage","SpotPerpCarry","FundingMomentum","MeanReversion"]
    ],
}

def ms(d, end=False):
    dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end: dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)

EXCLUDE_MAP = {
    "current": ["VWAPDeviation"],
    "empty": [],
    "only_funding": ["FundingArbitrage","SpotPerpCarry","FundingMomentum","MeanReversion"],
}

def run_one(cfg_orig: Any, db: Database, params: Dict, window: str) -> Dict:
    """Run backtest with ensemble params overridden in memory."""
    start_s, end_s = WINDOWS[window]
    cfg = load_config("config/settings.yaml")  # fresh copy each run

    # Override ensemble section
    ens = dict(cfg.get("strategy.ensemble", {}) or {})
    ens["threshold"] = params["threshold"]
    ens["min_agreeing"] = params["min_agreeing"]
    ens["high_conviction_threshold"] = params["hc_threshold"]
    ens["high_conviction_exclude"] = EXCLUDE_MAP[params["exclude"]]
    cfg.set("strategy.ensemble", ens)

    # Disable all strategies except the keepers
    keep = {"VWAPDeviation", "VolatilityBreakout", "TrendPyramid", "DonchianBreakout"}
    for path, cls in [
        ("strategy.trend_follow", None),
        ("strategy.mean_reversion", None),
        ("strategy.funding_arbitrage", None),
        ("strategy.funding_momentum", None),
        ("strategy.lead_lag", None),
        ("strategy.liquidation_catcher", None),
        ("strategy.cvd_orderflow", None),
        ("strategy.spot_perp_carry", None),
        ("strategy.range_grid", None),
        ("strategy.orderbook_scalper", None),
    ]:
        sec = dict(cfg.get(path, {}) or {})
        sec["enabled"] = False
        cfg.set(path, sec)

    ensemble = build_ensemble(cfg)
    bt = BacktestEngine(
        database=db,
        strategy=ensemble,
        config=BacktestConfig(
            initial_capital=10_000, commission_pct=0.035/100,
            slippage_bps=2.0, max_positions=5, tca_enabled=True,
            paper_slippage_pct=0.05, use_regime_weights=False,
            use_cooldown=True, use_kelly=True, use_microstructure_proxy=True,
        ),
        symbols=list(cfg.get("assets", ["BTC","ETH","SOL"])),
    )
    try:
        result = bt.run(start_ms=ms(start_s), end_ms=ms(end_s, end=True))
    except Exception as exc:
        return {"n_trades": 0, "sharpe": 0.0, "expectancy": 0.0, "pf": 0.0,
                "max_dd": 0.0, "win_rate": 0.0, "error": str(exc)}

    m = result.get("metrics", {})
    return {
        "n_trades": int(m.get("n_trades", 0)),
        "sharpe": round(float(m.get("sharpe_ratio", 0)), 4),
        "expectancy": round(float(m.get("avg_trade", 0)), 4),
        "pf": round(float(m.get("profit_factor", 0)), 4),
        "max_dd": round(float(m.get("max_drawdown", 0)) * 100, 3),
        "win_rate": round(float(m.get("win_rate", 0)) * 100, 1),
        "total_return": round(float(m.get("total_return", 0)) * 100, 4),
    }


def main():
    db = Database("data/live/bot.db")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = f"data/backtests/ensemble_sweep_{ts}.csv"

    # Build param list
    keys = ["threshold", "min_agreeing", "hc_threshold", "exclude"]
    param_list = [
        dict(zip(keys, combo))
        for combo in itertools.product(
            GRID["threshold"], GRID["min_agreeing"],
            GRID["hc_threshold"], GRID["exclude"],
        )
    ]
    print(f"Grid: {len(param_list)} combinations")

    # ── Phase 1: Sweep on SHORT validation window ──
    print("\n--- Phase 1: Sweep validation window (Jun 23-25) ---")
    results = []
    for i, p in enumerate(param_list):
        label = f"T={p['threshold']} MA={p['min_agreeing']} HCT={p['hc_threshold']} EX={p['exclude']}"
        print(f"  [{i+1}/{len(param_list)}] {label}", end=" ... ")
        r = run_one(None, db, p, "valid")
        r.update(p)
        results.append(r)
        print(f"n={r['n_trades']} S={r['sharpe']} E=${r['expectancy']}")

    # Save full grid
    with open(csv_path, "w", newline="") as f:
        if results:
            cols = list(results[0].keys())
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader(); w.writerows(results)

    # ── Phase 2: Validate top 5 on FULL window ──
    print(f"\n--- Phase 2: Validate top 5 on full window (May 18-Jun 25) ---")

    # Rank by Sharpe descending, filter to >=3 trades
    ranked = [r for r in results if r["n_trades"] >= 3]
    ranked.sort(key=lambda r: r["sharpe"], reverse=True)
    top5 = ranked[:5]

    print(f"\n  Top 5 by Sharpe (validation):")
    print(f"  {'#':2s} {'T':5s} {'MA':3s} {'HCT':4s} {'EX':15s} {'n':>4s} {'Sharpe':>7s} {'Exp':>8s} {'PF':>6s} {'DD%':>6s} {'WR%':>5s}")
    print(f"  {'-'*2} {'-'*5} {'-'*3} {'-'*4} {'-'*15} {'-'*4} {'-'*7} {'-'*8} {'-'*6} {'-'*6} {'-'*5}")
    for i, r in enumerate(top5):
        print(f"  {i+1:2d} {r['threshold']:5.2f} {r['min_agreeing']:3d} {r['hc_threshold']:4.2f} "
              f"{r['exclude']:15s} {r['n_trades']:4d} {r['sharpe']:7.3f} ${r['expectancy']:>6.2f} "
              f"{r['pf']:6.3f} {r['max_dd']:6.3f}% {r['win_rate']:5.1f}%")

    print(f"\n  Full window validation:")
    print(f"  {'#':2s} {'T':5s} {'MA':3s} {'HCT':4s} {'EX':15s} {'n':>4s} {'Sharpe':>7s} {'Exp':>8s} {'PF':>6s} {'DD%':>6s} {'WR%':>5s}")
    print(f"  {'-'*2} {'-'*5} {'-'*3} {'-'*4} {'-'*15} {'-'*4} {'-'*7} {'-'*8} {'-'*6} {'-'*6} {'-'*5}")
    full_results = []
    for i, p in enumerate(top5):
        label = f"T={p['threshold']} MA={p['min_agreeing']} HCT={p['hc_threshold']} EX={p['exclude']}"
        print(f"  [{i+1}/5] {label}", end=" ... ")
        r = run_one(None, db, p, "train")
        r.update({k: p[k] for k in keys})
        full_results.append(r)
        print(f"n={r['n_trades']} S={r['sharpe']} E=${r['expectancy']}")
        print(f"  {'':3s} {r['threshold']:5.2f} {r['min_agreeing']:3d} {r['hc_threshold']:4.2f} "
              f"{r['exclude']:15s} {r['n_trades']:4d} {r['sharpe']:7.3f} ${r['expectancy']:>6.2f} "
              f"{r['pf']:6.3f} {r['max_dd']:6.3f}% {r['win_rate']:5.1f}%")

    # ── Phase 3: Recommendation ──
    print(f"\n{'='*60}")
    print(f"  RECOMMENDATION")
    print(f"{'='*60}")

    # Best on full window with Sharpe > 0 and >= 5 trades
    valid_full = [r for r in full_results if r["n_trades"] >= 5 and r["sharpe"] > 0]
    if valid_full:
        best = valid_full[0]
        print(f"\n  Best config (validated on full window):")
        print(f"    threshold                = {best['threshold']}")
        print(f"    min_agreeing             = {best['min_agreeing']}")
        print(f"    high_conviction_threshold = {best['hc_threshold']}")
        print(f"    high_conviction_exclude   = {best['exclude']}")
        print(f"    n_trades = {best['n_trades']}  Sharpe = {best['sharpe']}  "
              f"Expectancy = ${best['expectancy']}  PF = {best['pf']}  DD = {best['max_dd']}%")

        if best["sharpe"] > 2.0 and best["n_trades"] > 50:
            print(f"\n  WARNING: Sharpe={best['sharpe']} com n={best['n_trades']} na janela completa "
                  f"e {top5[0]['sharpe']} na validação curta.")
            print(f"  Diferenca grande entre janelas sugeri overfit ao periodo de treino.")
            print(f"  Considere usar a config #2 ou #3 como alternativa mais conservadora.")
    else:
        print("\n  No config with Sharpe > 0 and >= 5 trades on full window.")

    print(f"\nCSV: {csv_path}  ({len(results)} rows)")


if __name__ == "__main__":
    main()