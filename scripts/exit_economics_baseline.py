"""Baseline exit economics — single backtest, full breakdown (ASCII only)."""
from __future__ import annotations
import sys, os, logging
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
logging.basicConfig(level=logging.WARNING)
logging.getLogger("src.backtest.engine").setLevel(logging.ERROR)
logging.getLogger("src.strategies").setLevel(logging.ERROR)

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.database import Database
from src.strategies.factory import build_ensemble
from src.utils.config import load_config

FROM = "2026-06-01"
TO = "2026-06-25"
def ms(d, end=False):
    dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end: dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)

cfg = load_config("config/settings.yaml")
db = Database("data/live/bot.db")
bt = BacktestEngine(
    database=db, strategy=build_ensemble(cfg),
    config=BacktestConfig(initial_capital=10_000, commission_pct=0.035/100,
        slippage_bps=2.0, max_positions=5, tca_enabled=True,
        paper_slippage_pct=0.05, use_regime_weights=False,
        use_cooldown=True, use_kelly=True, use_microstructure_proxy=True),
    symbols=list(cfg.get("assets", ["BTC","ETH","SOL"])),
)
print("Running baseline...")
result = bt.run(start_ms=ms(FROM), end_ms=ms(TO, end=True))
m = result["metrics"]
trades = result.get("trades", [])
print("\n======== BASELINE June 1-25 ========")
print(f"  n_trades={m.get('n_trades',0):4d}  return={m.get('total_return',0)*100:.4f}%  "
      f"Sharpe={m.get('sharpe_ratio',0):.4f}  DD={m.get('max_drawdown',0)*100:.2f}%  "
      f"WinRate={m.get('win_rate',0)*100:.1f}%  PF={m.get('profit_factor',0):.4f}")
print(f"  avg_trade=${m.get('avg_trade',0):.2f}")
fees_all = [abs(t.get("fees_paid",0)) for t in trades]
print(f"  Fees: total=${sum(fees_all):.2f}  avg/trade=${(sum(fees_all)/len(trades) if trades else 0):.4f}")

print("\n-------- EXITS PER STRATEGY --------")
print(f"  {'STRATEGY':22s} {'n':>3s} {'TP':>3s} {'SL':>3s} {'TRL':>3s} {'TIME':>4s} {'OTH':>3s}  "
      f"{'AvgW':>8s} {'AvgL':>8s} {'R:R':>5s}")
by_strat = defaultdict(list)
for t in trades:
    s = str(t.get("sub_strategy") or t.get("strategy","?")); by_strat[s].append(t)
for strat, st in sorted(by_strat.items(), key=lambda x: -len(x[1])):
    pnls = [t["pnl_usd"] for t in st]
    wins = [p for p in pnls if p>0]; losses = [p for p in pnls if p<=0]
    reasons = Counter(t.get("exit_reason","?") for t in st)
    tp=reasons.get("take_profit",0); sl=reasons.get("stop_loss",0)
    trail=sum(1 for r in reasons if "rail" in r.lower())
    time_ex=sum(1 for r in reasons if "ime" in r.lower() or "old" in r.lower())
    other=len(st)-tp-sl-trail-time_ex
    aw=(sum(wins)/len(wins)) if wins else 0; al=(sum(losses)/len(losses)) if losses else 0
    rr=abs(aw/al) if al else 0
    print(f"  {strat:22s} {len(st):3d} {tp:3d} {sl:3d} {trail:3d} {time_ex:4d} {other:3d}  "
          f"${aw:>6.2f} ${al:>6.2f} {rr:>4.2f}x")

print("\n-------- EXIT REASON BREAKDOWN (ALL) --------")
all_reasons = Counter(t.get("exit_reason","?") for t in trades)
for reason, count in all_reasons.most_common():
    pnls = [t["pnl_usd"] for t in trades if t.get("exit_reason","")==reason]
    print(f"  {reason:30s} n={count:3d}  total_pnl=${sum(pnls):>8.2f}  avg=${sum(pnls)/count:.2f}")

print("\n-------- TP/SL DISTANCE (first 15 trades) --------")
print(f"  {'STRATEGY':20s} {'SIDE':5s} {'Entry':>8s} {'Exit':>8s} {'PnL%':>7s} {'SL%':>6s} {'TP%':>6s} {'REASON':15s}")
for t in trades[:15]:
    entry=t.get("entry_price",0); exit_p=t.get("exit_price",0); side=t.get("side","")
    if entry and exit_p and side:
        pnl_pct=(exit_p-entry)/entry*100 if side=="long" else (entry-exit_p)/entry*100
        sl_p=t.get("stop_loss_price"); tp_p=t.get("take_profit_price")
        sl_pct=(entry-sl_p)/entry*100 if sl_p and side=="long" else ((sl_p-entry)/entry*100 if sl_p else 0)
        tp_pct=(tp_p-entry)/entry*100 if tp_p and side=="long" else ((entry-tp_p)/entry*100 if tp_p else 0)
        print(f"  {t.get('strategy','?'):20s} {side:5s} ${entry:>6.1f} ${exit_p:>6.1f} "
              f"{pnl_pct:+.2f}% {sl_pct:>5.2f}% {tp_pct:>5.2f}% {t.get('exit_reason','?'):15s}")