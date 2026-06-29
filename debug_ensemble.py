"""Debug: verify ensemble parameter overrides take effect."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.utils.config import load_config
from src.strategies.factory import build_ensemble

cfg = load_config("config/settings.yaml")

# Override ensemble params
ens = dict(cfg.get("strategy.ensemble", {}) or {})
ens["threshold"] = 0.99
ens["min_agreeing"] = 2
ens["high_conviction_threshold"] = 0.99
ens["high_conviction_exclude"] = []
cfg.set("strategy.ensemble", ens)

# Disable most strategies
for path in ["strategy.trend_follow", "strategy.mean_reversion", "strategy.funding_arbitrage",
             "strategy.funding_momentum", "strategy.lead_lag", "strategy.liquidation_catcher",
             "strategy.cvd_orderflow", "strategy.spot_perp_carry", "strategy.range_grid",
             "strategy.orderbook_scalper"]:
    sec = dict(cfg.get(path, {}) or {})
    sec["enabled"] = False
    cfg.set(path, sec)

ensemble = build_ensemble(cfg)
print("Threshold:", ensemble._threshold)
print("Min agreeing:", ensemble._min_strategies_agreeing)
print("HC threshold:", ensemble._high_conviction_threshold)
print("HC exclude:", ensemble._high_conviction_exclude)
print("Sub-strategies:", [s.name for s in ensemble._strategies])
print("Weights:", [(w.name, w.weight) for w in ensemble._weights])