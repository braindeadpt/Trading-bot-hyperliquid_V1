"""
Hyperliquid Premium Trading Bot — Entry Point

Orchestrates all components:
  1. Load configuration
  2. Initialize vault (secure credential storage)
  3. Initialize database
  4. Start WebSocket clients (Hyperliquid + Binance)
  5. Start CandleBuilder + DataBus
  6. Initialize strategies (TrendFollow + MeanReversion)
  7. Initialize RiskManager + ExecutionEngine + PortfolioState
  8. Start TradingEngine (main loop)
  9. Start Dashboard (Flask + Socket.IO)
  10. Graceful shutdown on SIGINT/SIGTERM

Usage:
    python main.py --config config/settings.yaml --mode paper
    python main.py --config config/settings.yaml --mode testnet
    python main.py --backtest --from 2024-01-01 --to 2024-03-01
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Ensure src/ is on the path
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
from utils.config import Config, ConfigError
from utils.logger import setup_logger
from utils.helpers import safe_ensure_dir

from data.database import Database
from exchanges.hyperliquid_ws import HyperliquidWSClient, DataBus
from exchanges.hyperliquid_rest import HyperliquidRESTClient
from exchanges.binance_api import BinanceRESTClient, BinanceWSClient
from data.candle_builder import CandleBuilder

from strategies.trend_follow import TrendFollow
from strategies.mean_reversion import MeanReversion

from core.portfolio import PortfolioState
from core.risk_manager import RiskManager
from core.execution import ExecutionEngine
from core.engine import TradingEngine

from dashboard.web import create_dashboard

# ---------------------------------------------------------------------------
# Globals for signal handling
# ---------------------------------------------------------------------------
_engine: Optional[TradingEngine] = None
_dashboard_socketio: Optional[Any] = None
_logger = None


def _resolve_path(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def _shutdown(signum: int, frame: Any) -> None:
    """Graceful shutdown handler."""
    if _logger:
        _logger.warning(f"Received signal {signum}, initiating shutdown...")
    if _engine:
        asyncio.run_coroutine_threadsafe(_engine.stop(), asyncio.get_event_loop())
    # Give dashboard a moment to finish
    time.sleep(1)
    sys.exit(0)


async def _run_backtest(cfg: Config, db: Database, logger: Any) -> Dict[str, Any]:
    """Run a historical backtest using the backtest engine."""
    from backtest.engine import BacktestEngine
    from backtest.metrics import calculate_metrics

    logger.info("=" * 60)
    logger.info("BACKTEST MODE")
    logger.info("=" * 60)

    initial_capital = cfg.get("backtest.initial_capital", 100_000.0)
    commission_pct = cfg.get("backtest.commission_pct", 0.04)
    slippage_bps = cfg.get("backtest.slippage_bps", 2.0)
    symbols = cfg.get("assets", ["BTC", "ETH", "SOL"])

    # Build strategies
    strategies = [
        TrendFollow(cfg.get("strategy.trend_follow", {})),
        MeanReversion(cfg.get("strategy.mean_reversion", {})),
    ]

    bt = BacktestEngine(
        db=db,
        strategies=strategies,
        initial_capital=initial_capital,
        commission_pct=commission_pct,
        slippage_bps=slippage_bps,
        symbols=symbols,
    )

    # Run
    equity_curve, trades = await bt.run()

    # Metrics
    metrics = calculate_metrics(equity_curve, trades)

    logger.info(f"Backtest complete — {metrics['n_trades']} trades")
    logger.info(f"Total return: {metrics['total_return']:.2%}")
    logger.info(f"Sharpe: {metrics['sharpe_ratio']:.3f}")
    logger.info(f"Max DD: {metrics['max_drawdown']:.2%}")
    logger.info(f"Win rate: {metrics['win_rate']:.1%}")

    return metrics


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Hyperliquid Premium Trading Bot")
    parser.add_argument("--config", default="config/settings.yaml", help="Path to YAML config")
    parser.add_argument("--mode", choices=["paper", "testnet", "mainnet"], default=None, help="Override trading mode")
    parser.add_argument("--backtest", action="store_true", help="Run backtest mode")
    parser.add_argument("--from-date", dest="from_date", help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--to-date", dest="to_date", help="Backtest end date (YYYY-MM-DD)")
    parser.add_argument("--audit", action="store_true", help="Run security audit and exit")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable web dashboard")
    args = parser.parse_args()

    # -----------------------------------------------------------------------
    # 0. Security audit (optional standalone)
    # -----------------------------------------------------------------------
    if args.audit:
        from security.audit import main as audit_main
        exit_code = audit_main(["--src-dir", str(PROJECT_ROOT / "src")])
        sys.exit(exit_code)

    # -----------------------------------------------------------------------
    # 1. Load configuration
    # -----------------------------------------------------------------------
    config_path = _resolve_path(args.config)
    try:
        from utils.config import load_config
        cfg = load_config(str(config_path))
    except ConfigError as e:
        print(f"[FATAL] Config error: {e}", file=sys.stderr)
        sys.exit(1)

    # Override mode from CLI if provided
    mode = args.mode or cfg.get("mode", "paper")
    cfg.raw["mode"] = mode

    # -----------------------------------------------------------------------
    # 2. Setup logging
    # -----------------------------------------------------------------------
    log_dir = PROJECT_ROOT / "logs"
    safe_ensure_dir(str(log_dir))
    logger = setup_logger(
        "Main",
        level=cfg.get("logging.level", "INFO"),
        log_file=str(log_dir / "bot.log"),
        json_format=cfg.get("logging.json", False),
    )
    global _logger
    _logger = logger

    logger.info("=" * 60)
    logger.info("HYPERLIQUID PREMIUM BOT STARTING")
    logger.info(f"Mode: {mode.upper()}")
    logger.info(f"Config: {config_path}")
    logger.info("=" * 60)

    # -----------------------------------------------------------------------
    # 3. Setup database
    # -----------------------------------------------------------------------
    db_path = _resolve_path(cfg.get("database.path", "data/live/bot.db"))
    safe_ensure_dir(str(db_path.parent))
    db = Database(str(db_path))
    logger.info(f"Database ready: {db_path}")

    # -----------------------------------------------------------------------
    # 4. Backtest mode (early exit after run)
    # -----------------------------------------------------------------------
    if args.backtest:
        metrics = await _run_backtest(cfg, db, logger)
        # Print summary
        print("\n" + "=" * 60)
        print("BACKTEST RESULTS")
        print("=" * 60)
        for k, v in metrics.items():
            print(f"  {k:20s}: {v}")
        print("=" * 60)
        return

    # -----------------------------------------------------------------------
    # 5. Initialize WebSocket / API clients
    # -----------------------------------------------------------------------
    data_bus = DataBus()

    hl_ws = HyperliquidWSClient(
        bus=data_bus,
        symbols=cfg.get("assets", ["BTC", "ETH", "SOL"]),
        ws_url=cfg.get("exchange.hyperliquid.ws_url", "wss://api.hyperliquid.xyz/ws"),
    )

    hl_rest = HyperliquidRESTClient(
        use_testnet=cfg.get("exchange.hyperliquid.testnet", False),
        max_requests_per_second=cfg.get("exchange.hyperliquid.max_requests_per_second", 5.0),
    )

    binance_rest = BinanceRESTClient(
        base_url=cfg.get("exchange.binance.rest_url", "https://api.binance.com"),
    )

    # Binance WS (optional — for volume flow)
    binance_ws: Optional[BinanceWSClient] = None
    if cfg.get("exchange.binance.ws_enabled", True):
        binance_ws = BinanceWSClient(
            symbols=cfg.get("assets", ["BTC", "ETH", "SOL"]),
            ws_base=cfg.get("exchange.binance.ws_url", "wss://stream.binance.com:9443/ws"),
        )

    candle_builder = CandleBuilder(bus=data_bus, symbols=cfg.get("assets", ["BTC", "ETH", "SOL"]), timeframes=["1m", "5m", "15m", "1h"])

    # -----------------------------------------------------------------------
    # 6. Initialize strategies
    # -----------------------------------------------------------------------
    strategies = [
        TrendFollow(cfg.get("strategy.trend_follow", {})),
        MeanReversion(cfg.get("strategy.mean_reversion", {})),
    ]
    logger.info(f"Loaded {len(strategies)} strategies: {[s.name for s in strategies]}")

    # -----------------------------------------------------------------------
    # 7. Initialize portfolio, risk, execution
    # -----------------------------------------------------------------------
    initial_capital = cfg.get("risk.initial_capital", 10_000.0)
    portfolio = PortfolioState(initial_capital=initial_capital)

    # Attempt DB recovery of portfolio state
    last_snapshot = db.get_latest_portfolio_snapshot()
    if last_snapshot:
        portfolio.from_dict(last_snapshot)
        logger.info(f"Recovered portfolio from DB: capital={portfolio.capital:.2f}")

    risk_mgr = RiskManager(
        config=cfg,
        db=db,
    )

    executor = ExecutionEngine(
        config=cfg,
        db=db,
        mode=mode,
    )

    # Load open trades from DB into executor + portfolio
    open_trades = db.get_open_trades()
    if open_trades:
        executor.load_open_trades(open_trades, portfolio)
        logger.info(f"Recovered {len(open_trades)} open trades from DB")

    # -----------------------------------------------------------------------
    # 8. Start TradingEngine
    # -----------------------------------------------------------------------
    engine = TradingEngine(
        config=cfg,
        db=db,
        data_bus=data_bus,
        strategies=strategies,
        risk_manager=risk_mgr,
        executor=executor,
    )
    global _engine
    _engine = engine

    await engine.start()
    logger.info("TradingEngine started")

    # -----------------------------------------------------------------------
    # 9. Start Dashboard (optional)
    # -----------------------------------------------------------------------
    if not args.no_dashboard:
        dashboard_cfg = {
            "mode": mode.upper(),
            "version": cfg.get("version", "1.0.0"),
            "host": cfg.get("dashboard.host", "0.0.0.0"),
            "port": cfg.get("dashboard.port", 5000),
        }
        app, socketio = create_dashboard(engine=engine, config=dashboard_cfg)
        global _dashboard_socketio
        _dashboard_socketio = socketio

        import threading
        def _run_dashboard():
            socketio.run(
                app,
                host=dashboard_cfg["host"],
                port=dashboard_cfg["port"],
                debug=False,
                use_reloader=False,
            )

        dashboard_thread = threading.Thread(target=_run_dashboard, daemon=True)
        dashboard_thread.start()
        logger.info(f"Dashboard started at http://{dashboard_cfg['host']}:{dashboard_cfg['port']}")

    # -----------------------------------------------------------------------
    # 10. Signal handlers
    # -----------------------------------------------------------------------
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # -----------------------------------------------------------------------
    # 11. Keep main thread alive
    # -----------------------------------------------------------------------
    logger.info("Bot running. Press Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await engine.stop()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n[FATAL] {e}", file=sys.stderr)
        sys.exit(1)
