"""Scan all registry strategies for trade counts on W2/W3 (measurement only)."""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestEngine, build_backtest_config_from_yaml
from src.backtest.strategy_feed_requirements import STRATEGY_FEED_MAP, RequiredFeeds
from src.data.database import Database
from src.strategies.factory import (
    DirectStrategyRouter,
    _REGISTRY_BY_NAME,
    _instantiate_from_registry,
)
from src.utils.config import load_config

logging.basicConfig(level=logging.ERROR)
for n in (
    "src.core.volatility_circuit",
    "src.backtest.engine",
    "src.strategies",
    "src.core.risk_manager",
    "src.core.funding_blackout",
):
    logging.getLogger(n).setLevel(logging.ERROR)

FOLDS = {
    "W2": ("2026-06-13", "2026-07-10"),
    "W3": ("2026-07-11", "2026-08-07"),
}
PRIORITY = [
    "VWAPDeviation",
    "VolatilityBreakout",
    "CVDOrderFlow",
    "SpotPerpCarry",
    "FundingMomentum",
    "OrderBookScalper",
    "FundingArbitrage",
    "DonchianBreakout",
    "SFPReversion",
    "VARejection",
    "RangeGrid",
    "TrendPyramid",
    "LeadLag",
    "LiquidationCatcher",
    "ChecklistMeta",
    "SmartMoneyFlow",
    "MeanReversion",
]


def ms(s: str, end: bool = False) -> int:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def tier_note(name: str) -> Dict[str, Any]:
    req = STRATEGY_FEED_MAP.get(name, RequiredFeeds.HL_CANDLES)
    labels = []
    if req & RequiredFeeds.L2_SNAPSHOTS:
        labels.append("L2")
    if req & RequiredFeeds.TRADE_TAPE:
        labels.append("tape")
    if req & RequiredFeeds.TAKER_SPLIT:
        labels.append("taker_split")
    if req & RequiredFeeds.FUNDING:
        labels.append("funding")
    if req & RequiredFeeds.OI:
        labels.append("OI")
    if req & RequiredFeeds.LIQUIDATION:
        labels.append("liquidation")
    if req & RequiredFeeds.BINANCE_PERP:
        labels.append("binance_perp")
    if not labels:
        return {
            "tier": "tier_a_hl_ohlc",
            "extra_feeds": [],
            "conservative_vs_strategy": False,
            "note": "OHLC-only — Tier A in candle replay; baselines equally informed.",
        }
    return {
        "tier": "tier_b_missing_or_proxy",
        "extra_feeds": labels,
        "conservative_vs_strategy": True,
        "note": (
            f"Needs {labels} for Tier A; candle replay degrades the STRATEGY only. "
            "Baselines ignore those feeds → test is conservative against the strategy."
        ),
    }


def run_one(cfg, db, symbols, name: str, start: str, end: str) -> Dict[str, Any]:
    entry = _REGISTRY_BY_NAME.get(name)
    if entry is None:
        return {"error": "unknown", "n_trades": 0}
    path, cls = entry
    inst = _instantiate_from_registry(cfg, path, cls, force=True, shadow=False)
    if inst is None:
        return {"error": "instantiate_failed", "n_trades": 0}
    bt = build_backtest_config_from_yaml(cfg)
    bt.use_volatility_circuit = False
    bt.use_funding_blackout = False
    bt.max_daily_trades = 0
    bt.use_microstructure_proxy = True
    bt.exit_path_policy = "adverse_first"
    eng = BacktestEngine(
        database=db,
        strategy=DirectStrategyRouter([inst]),
        config=bt,
        symbols=symbols,
        risk_config=cfg,
    )
    t0 = time.time()
    try:
        result = eng.run(start_ms=ms(start), end_ms=ms(end, end=True))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200], "n_trades": 0, "elapsed_s": round(time.time() - t0, 1)}
    trades = [
        t
        for t in (result.get("trades") or [])
        if str(t.get("strategy") or "") == name
    ]
    pnls = [float(t.get("pnl_usd") or 0) for t in trades]
    return {
        "n_trades": len(trades),
        "total_pnl": round(sum(pnls), 2),
        "elapsed_s": round(time.time() - t0, 1),
        "error": None,
    }


def main() -> int:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    db_path = ROOT / "data" / "live" / "bot_ruleset_validate.db"
    db = Database(str(db_path if db_path.exists() else ROOT / "data" / "live" / "bot.db"))
    symbols = list(cfg.get("assets") or ["BTC", "ETH", "SOL", "HYPE"])

    # Feed availability quick checks
    con = db._conn()
    feed_counts = {}
    for table in ("binance_perp_prices", "liquidation_events", "funding_history"):
        try:
            feed_counts[table] = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except Exception:
            feed_counts[table] = -1

    names = [n for n in PRIORITY if n in _REGISTRY_BY_NAME]
    # alias SmartMoneyFlow = TrendFollow
    if "SmartMoneyFlow" not in _REGISTRY_BY_NAME and "TrendFollow" in [
        _REGISTRY_BY_NAME
    ]:
        pass
    # TrendFollow registers as SmartMoneyFlow via display name
    out: Dict[str, Any] = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "feed_row_counts": feed_counts,
        "strategies": {},
    }
    for name in names:
        print(f"\n=== {name} ===", flush=True)
        tier = tier_note(name)
        folds = {}
        for fk, (start, end) in FOLDS.items():
            print(f"  {fk}...", flush=True)
            folds[fk] = run_one(cfg, db, symbols, name, start, end)
            print(f"    {folds[fk]}", flush=True)
        out["strategies"][name] = {"tier": tier, "folds": folds}

    path = ROOT / "data" / "backtests" / "parity_diag" / "baseline_portfolio_scan.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {path}", flush=True)

    print("\n--- SUMMARY (n_trades) ---", flush=True)
    print(f"{'strategy':22} {'tier':28} {'W2':>5} {'W3':>5} testable?", flush=True)
    for name, body in out["strategies"].items():
        w2 = body["folds"]["W2"].get("n_trades", 0)
        w3 = body["folds"]["W3"].get("n_trades", 0)
        ok = "YES" if max(w2, w3) >= 30 else ("maybe" if max(w2, w3) >= 10 else "NO")
        print(
            f"{name:22} {body['tier']['tier']:28} {w2:5} {w3:5} {ok}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
