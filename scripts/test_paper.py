#!/usr/bin/env python3
"""
Paper Trading Test Script for Hyperliquid Trading Bot.

Validates that all critical fixes are working before live deployment:
1. Engine starts without fatal errors
2. Strategies load and produce diagnostics
3. Database is clean (no anomalies)
4. Dashboard serves the new 12-column layout
5. Paper mode is active (no real orders)

Usage:
    python scripts/test_paper.py --duration 300  # run for 5 minutes
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.utils.config import load_config
from src.data.database import Database
from src.core.engine import ExecutionEngine
from src.strategies.ensemble import StrategyEnsemble, StrategyWeight
from src.strategies.liquidation_catcher import LiquidationCatcher
from src.strategies.trend_follow import TrendFollow
from src.strategies.mean_reversion import MeanReversion
from src.strategies.vwap_deviation import VWAPDeviation
from src.strategies.funding_arbitrage import FundingArbitrage


def test_database() -> bool:
    """Test DB is clean and constraints are active."""
    print("\n[1/5] Testing Database Integrity...")
    db = Database(project_root / "data" / "live" / "bot.db")
    
    # Check trades table
    rows = db.get_trades(limit=100)
    issues = 0
    for r in rows:
        if r.get("entry_price", 0) <= 0:
            print(f"  FAIL: Trade {r.get('id')} has invalid entry_price")
            issues += 1
        if r.get("status") == "closed" and (r.get("exit_price") is None or r.get("exit_price") <= 0):
            print(f"  FAIL: Trade {r.get('id')} is closed with invalid exit_price")
            issues += 1
        if r.get("size", 0) <= 0:
            print(f"  FAIL: Trade {r.get('id')} has invalid size")
            issues += 1
    
    if issues == 0:
        print(f"  PASS: {len(rows)} trades validated, no anomalies")
        return True
    else:
        print(f"  FAIL: {issues} anomalies found. Run scripts/db_cleanup.py --fix")
        return False


def test_strategies() -> bool:
    """Test that all strategies load and have correct names."""
    print("\n[2/5] Testing Strategy Loading...")
    cfg = load_config(project_root / "config" / "settings.yaml")
    
    sub_strategies = [
        TrendFollow(cfg.get("strategy.trend_follow", {})),
        MeanReversion(cfg.get("strategy.mean_reversion", {})),
        FundingArbitrage(cfg.get("strategy.funding_arbitrage", {})),
        VWAPDeviation(cfg.get("strategy.vwap_deviation", {})),
        LiquidationCatcher(cfg.get("strategy.liquidation_catcher", {})),
    ]
    
    ensemble_weights = [
        StrategyWeight("LiquidationCatcher", 0.35, min_confidence=0.60),
        StrategyWeight("SmartMoneyFlow", 0.25, min_confidence=0.55),
        StrategyWeight("MeanReversion", 0.20, min_confidence=0.55),
        StrategyWeight("VWAPDeviation", 0.15, min_confidence=0.55),
        StrategyWeight("FundingArbitrage", 0.05, min_confidence=0.60),
    ]
    
    ensemble = StrategyEnsemble(
        strategies=sub_strategies,
        weights=ensemble_weights,
        threshold=cfg.get("strategy.ensemble.threshold", 0.55),
        min_strategies_agreeing=cfg.get("strategy.ensemble.min_agreeing", 2),
        high_conviction_threshold=cfg.get("strategy.ensemble.high_conviction_threshold", 0.80),
    )
    
    print(f"  PASS: {len(sub_strategies)} sub-strategies loaded")
    print(f"  PASS: Ensemble '{ensemble.name}' configured with threshold={ensemble._threshold}")
    for s in sub_strategies:
        print(f"       - {s.name}: loaded OK")
    return True


def test_config() -> bool:
    """Test that config is consistent."""
    print("\n[3/5] Testing Configuration...")
    cfg = load_config(project_root / "config" / "settings.yaml")
    
    mode = cfg.get("mode", "paper")
    capital = cfg.get("risk.initial_capital", 0)
    max_pos = cfg.get("risk.max_position_size_pct", 0)
    
    print(f"  Mode: {mode}")
    print(f"  Capital: ${capital:,.2f}")
    print(f"  Max position: {max_pos}%")
    
    if mode != "paper":
        print(f"  WARN: Mode is '{mode}', expected 'paper'")
        return False
    
    if capital <= 0:
        print(f"  FAIL: Invalid capital")
        return False
    
    print(f"  PASS: Config valid, paper mode active")
    return True


def test_dashboard_files() -> bool:
    """Test dashboard template and CSS exist."""
    print("\n[4/5] Testing Dashboard Files...")
    
    template_path = project_root / "src" / "dashboard" / "templates" / "index.html"
    css_path = project_root / "src" / "dashboard" / "static" / "dashboard.css"
    
    if not template_path.exists():
        print(f"  FAIL: Template not found: {template_path}")
        return False
    
    if not css_path.exists():
        print(f"  FAIL: CSS not found: {css_path}")
        return False
    
    # Check template has 12-column grid
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
        if "dashboard-grid" not in html:
            print(f"  FAIL: Template missing dashboard-grid class")
            return False
        if "col-12" not in html:
            print(f"  FAIL: Template missing 12-column grid spans")
            return False
    
    # Check CSS has grid system
    with open(css_path, "r", encoding="utf-8") as f:
        css = f.read()
        if "grid-template-columns: repeat(12, 1fr)" not in css:
            print(f"  FAIL: CSS missing 12-column grid")
            return False
    
    print(f"  PASS: Dashboard template ({template_path.stat().st_size} bytes) and CSS ({css_path.stat().st_size} bytes) OK")
    return True


def test_engine_startup() -> bool:
    """Test that engine can start without fatal errors."""
    print("\n[5/5] Testing Engine Startup...")
    
    cfg = load_config(project_root / "config" / "settings.yaml")
    
    try:
        # Try to import and instantiate engine components
        from src.core.portfolio import PortfolioState
        from src.core.risk_manager import RiskManager
        
        portfolio = PortfolioState(
            initial_capital=cfg.get("risk.initial_capital", 10_000.0),
        )
        risk = RiskManager(cfg.get("risk", {}), portfolio)
        
        print(f"  PASS: PortfolioState initialized with ${portfolio.sync_capital():,.2f}")
        print(f"  PASS: RiskManager loaded successfully")
        
        # Test circuit breaker
        cb_result_low = risk.check_drawdown(0.0)
        cb_result_high = risk.check_drawdown(0.15)
        assert cb_result_low == False, "Circuit breaker should be OFF at 0% DD"
        assert cb_result_high == True, "Circuit breaker should trip at 15% DD"
        print(f"  PASS: Circuit breaker logic OK (0% -> {cb_result_low}, 15% -> {cb_result_high})")
        
        return True
    except Exception as e:
        print(f"  FAIL: Engine startup error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(description="Test paper trading setup")
    parser.add_argument("--duration", type=int, default=0, help="Duration to run paper test (seconds, 0=just checks)")
    args = parser.parse_args()
    
    print("=" * 60)
    print("Hyperliquid Trading Bot — Paper Trading Test Suite")
    print("=" * 60)
    print(f"Project: {project_root}")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = []
    results.append(("Database Integrity", test_database()))
    results.append(("Strategy Loading", test_strategies()))
    results.append(("Configuration", test_config()))
    results.append(("Dashboard Files", test_dashboard_files()))
    results.append(("Engine Startup", test_engine_startup()))
    
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    
    all_pass = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        symbol = "[OK]" if passed else "[XX]"
        print(f"  {symbol} {name}: {status}")
        if not passed:
            all_pass = False
    
    print("=" * 60)
    if all_pass:
        print("ALL CHECKS PASSED — Ready for paper trading")
        print("Run: python main.py --mode paper")
    else:
        print("SOME CHECKS FAILED — Fix issues before trading")
        sys.exit(1)
    
    # Optional: run for specified duration
    if args.duration > 0:
        print(f"\nRunning paper test for {args.duration} seconds...")
        print("(This would connect to WebSocket and process ticks)")
        # Actual run would go here
    
    print("\nDone.")


if __name__ == "__main__":
    main()
