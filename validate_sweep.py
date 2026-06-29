"""Extract top 5 from sweep and validate on full window."""
import sys, os, logging, csv
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
logging.basicConfig(level=logging.WARNING)
for mod in ["src.backtest.engine","src.strategies","src.core.volatility_circuit"]:
    logging.getLogger(mod).setLevel(logging.ERROR)

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.database import Database
from src.strategies.factory import build_ensemble
from src.utils.config import load_config

EXCLUDE_MAP = {
    "current": ["VWAPDeviation"],
    "empty": [],
    "only_funding": ["FundingArbitrage","SpotPerpCarry","FundingMomentum","MeanReversion"],
}

def ms(d, end=False):
    dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end: dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)

def make_config(threshold, min_agreeing, hc_threshold, exclude_key):
    cfg = load_config("config/settings.yaml")
    ens = dict(cfg.get("strategy.ensemble", {}) or {})
    ens["threshold"] = threshold
    ens["min_agreeing"] = min_agreeing
    ens["high_conviction_threshold"] = hc_threshold
    ens["high_conviction_exclude"] = EXCLUDE_MAP[exclude_key]
    cfg.set("strategy.ensemble", ens)
    # Disable all except the 4 keepers
    for path in ["strategy.trend_follow","strategy.mean_reversion","strategy.funding_arbitrage",
                 "strategy.funding_momentum","strategy.lead_lag","strategy.liquidation_catcher",
                 "strategy.cvd_orderflow","strategy.spot_perp_carry","strategy.range_grid",
                 "strategy.orderbook_scalper"]:
        sec = dict(cfg.get(path, {}) or {}); sec["enabled"] = False; cfg.set(path, sec)
    return cfg

def run_bt(cfg, start_s, end_s):
    db = Database("data/live/bot.db")
    ensemble = build_ensemble(cfg)
    bt = BacktestEngine(
        database=db, strategy=ensemble,
        config=BacktestConfig(initial_capital=10_000, commission_pct=0.035/100,
            slippage_bps=2.0, max_positions=5, tca_enabled=True,
            paper_slippage_pct=0.05, use_regime_weights=False,
            use_cooldown=True, use_kelly=True, use_microstructure_proxy=True),
        symbols=list(cfg.get("assets", ["BTC","ETH","SOL"])),
    )
    result = bt.run(start_ms=ms(start_s), end_ms=ms(end_s, end=True))
    m = result.get("metrics", {})
    return {
        "n_trades": int(m.get("n_trades",0)),
        "sharpe": round(float(m.get("sharpe_ratio",0)),4),
        "expectancy": round(float(m.get("avg_trade",0)),4),
        "pf": round(float(m.get("profit_factor",0)),4),
        "max_dd": round(float(m.get("max_drawdown",0))*100,3),
        "win_rate": round(float(m.get("win_rate",0))*100,1),
        "ret": round(float(m.get("total_return",0))*100,4),
    }

# Parse sweep CSV
with open('data/backtests/ensemble_sweep_20260625_234431.csv') as f:
    sweep = list(csv.DictReader(f))

valid_sweep = [r for r in sweep if int(r['n_trades']) >= 3]
valid_sweep.sort(key=lambda r: float(r['sharpe']), reverse=True)
top5 = valid_sweep[:5]

# Show top 5 from sweep
print("TOP 5 FROM SWEEP (validation window Jun 23-25):")
print("  {:<5s} {:<3s} {:<5s} {:<15s} {:>4s} {:>7s} {:>8s}".format(
    "THR", "MA", "HCT", "EXCL", "n", "Sharpe", "Exp"))
print("  " + "-"*55)
for r in top5:
    print("  {:.2f} {:<3d} {:.2f} {:<15s} {:>4d} {:>7.3f} ${:>6.2f}".format(
        float(r['threshold']), int(r['min_agreeing']), float(r['hc_threshold']),
        r['exclude'], int(r['n_trades']), float(r['sharpe']), float(r['expectancy'])))

# Validate on FULL window
print("\nVALIDATION ON FULL WINDOW (May 18 - Jun 25):")
print("  {:<5s} {:<3s} {:<5s} {:<15s} {:>4s} {:>7s} {:>8s} {:>6s} {:>6s} {:>6s}".format(
    "THR", "MA", "HCT", "EXCL", "n", "Sharpe", "Exp", "DD%", "WR%", "Ret%"))
print("  " + "-"*70)

results = []
for r in top5:
    thr = float(r['threshold'])
    ma = int(r['min_agreeing'])
    hct = float(r['hc_threshold'])
    exc = r['exclude']
    print(f"  {thr:.2f} {ma:<3d} {hct:.2f} {exc:<15s} ...", end=" ")
    cfg = make_config(thr, ma, hct, exc)
    res = run_bt(cfg, "2026-05-18", "2026-06-25")
    results.append(res)
    print("{:>4d} {:>7.3f} ${:>6.2f} {:>6.3f}% {:>5.1f}% {:>6.3f}%".format(
        res['n_trades'], res['sharpe'], res['expectancy'],
        res['max_dd'], res['win_rate'], res['ret']))

# Best from full window
results.sort(key=lambda r: r['sharpe'], reverse=True)
best = results[0]
print(f"\nBEST ON FULL WINDOW: Sharpe={best['sharpe']} n={best['n_trades']} Exp=${best['expectancy']}")

# Compare with baseline on full window
print("\nBASELINE (current ensemble, all 11 strategies):")
cfg_baseline = load_config("config/settings.yaml")
base = run_bt(cfg_baseline, "2026-05-18", "2026-06-25")
print("  {:>4d} {:>7.3f} ${:>6.2f} {:>6.3f}% {:>5.1f}% {:>6.3f}%".format(
    base['n_trades'], base['sharpe'], base['expectancy'],
    base['max_dd'], base['win_rate'], base['ret']))