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
import logging
import os
import signal
import sys
import time
from datetime import datetime
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
from strategies.funding_arbitrage import FundingArbitrage
from strategies.vwap_deviation import VWAPDeviation
from strategies.liquidation_catcher import LiquidationCatcher
from strategies.ensemble import StrategyEnsemble, StrategyWeight

from strategies.base import Position
from core.portfolio import PortfolioState
from core.risk_manager import RiskManager
from core.execution import ExecutionEngine
from core.engine import TradingEngine

from alerts.notifier import AlertNotifier, AlertConfig

from dashboard.web import create_app as create_dashboard

# ---------------------------------------------------------------------------
# Globals for signal handling
# ---------------------------------------------------------------------------
_engine: Optional[TradingEngine] = None
_dashboard_socketio: Optional[Any] = None
_hl_ws: Optional[HyperliquidWSClient] = None
_binance_ws: Optional[BinanceWSClient] = None
_candle_builder: Optional[CandleBuilder] = None
_logger = None


def _resolve_path(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def _shutdown(signum: int, frame: Any) -> None:
    """Graceful shutdown handler — waits up to 10s for components to stop."""
    if _logger:
        _logger.warning(f"Received signal {signum}, initiating shutdown...")
    loop = asyncio.get_event_loop()
    tasks = []
    if _engine:
        tasks.append(asyncio.run_coroutine_threadsafe(_engine.stop(), loop))
    if _hl_ws:
        tasks.append(asyncio.run_coroutine_threadsafe(_hl_ws.stop(), loop))
    if _binance_ws:
        tasks.append(asyncio.run_coroutine_threadsafe(_binance_ws.stop(), loop))
    if _candle_builder:
        tasks.append(asyncio.run_coroutine_threadsafe(_candle_builder.stop(), loop))
    # Wait up to 10s for graceful shutdown
    timeout = 10.0
    start = time.time()
    while tasks and (time.time() - start) < timeout:
        remaining = [t for t in tasks if not t.done()]
        if not remaining:
            break
        time.sleep(0.2)
    for t in tasks:
        if not t.done():
            t.cancel()
    if _logger:
        _logger.info("Shutdown complete.")
    sys.exit(0)


async def _run_backtest(cfg: Config, db: Database, logger: Any) -> Dict[str, Any]:
    """Run a historical backtest using the backtest engine."""
    from backtest.engine import BacktestEngine
    from backtest.metrics import calculate_metrics

    logger.info("=" * 60)
    logger.info("BACKTEST MODE")
    logger.info("=" * 60)

    initial_capital = cfg.get("backtest.initial_capital", cfg.get("risk.initial_capital", 10_000.0))
    commission_pct = cfg.get("backtest.commission_pct", 0.04)
    slippage_bps = cfg.get("backtest.slippage_bps", 2.0)
    symbols = cfg.get("assets", ["BTC", "ETH", "SOL"])

    logger.info("Backtest config: capital=$%.2f, commission=%.3f%%, slippage=%.1fbps",
        initial_capital, commission_pct, slippage_bps)

    # Build strategies
    strategies = [
        TrendFollow(cfg.get("strategy.trend_follow", {})),
        MeanReversion(cfg.get("strategy.mean_reversion", {})),
        FundingArbitrage(cfg.get("strategy.funding_arbitrage", {})),
        VWAPDeviation(cfg.get("strategy.vwap_deviation", {})),
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
    cfg.set("mode", mode)  # HIGH-010: use setter instead of raw dict mutation

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
    # Route all other loggers to the same handlers
    root = logging.getLogger()
    root.setLevel(logger.level)
    for handler in logger.handlers:
        root.addHandler(handler)
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

    candle_builder = CandleBuilder(bus=data_bus, symbols=cfg.get("assets", ["BTC", "ETH", "SOL"]), timeframes=[60, 300, 900, 3600])

    global _hl_ws, _binance_ws, _candle_builder
    _hl_ws = hl_ws
    _binance_ws = binance_ws
    _candle_builder = candle_builder

    # -----------------------------------------------------------------------
    # 6. Initialize strategies
    # -----------------------------------------------------------------------
    # Individual sub-strategies for ensemble composition
    sub_strategies = [
        TrendFollow(cfg.get("strategy.trend_follow", {})),
        MeanReversion(cfg.get("strategy.mean_reversion", {})),
        FundingArbitrage(cfg.get("strategy.funding_arbitrage", {})),
        VWAPDeviation(cfg.get("strategy.vwap_deviation", {})),
        LiquidationCatcher(cfg.get("strategy.liquidation_catcher", {})),
    ]

    # Ensemble with professional weighting
    ensemble_weights = [
        StrategyWeight("TrendFollow",         0.25, min_confidence=0.40),
        StrategyWeight("FundingExtreme",      0.25, min_confidence=0.40),
        StrategyWeight("VWAPDeviation",        0.20, min_confidence=0.40),
        StrategyWeight("FundingArbitrage",    0.15, min_confidence=0.35),
        StrategyWeight("LiquidationCatcher",  0.15, min_confidence=0.40),
    ]

    strategies = [
        StrategyEnsemble(
            strategies=sub_strategies,
            weights=ensemble_weights,
            threshold=cfg.get("strategy.ensemble.threshold", 0.40),
            min_strategies_agreeing=cfg.get("strategy.ensemble.min_agreeing", 1),
        ),
    ]
    logger.info(
        "StrategyEnsemble loaded: %d sub-strategies, threshold=%.2f, min_agreeing=%d",
        len(sub_strategies),
        cfg.get("strategy.ensemble.threshold", 0.40),
        cfg.get("strategy.ensemble.min_agreeing", 1),
    )
    logger.info("Sub-strategies: %s", [s.name for s in sub_strategies])

    # -----------------------------------------------------------------------
    # 7. Initialize portfolio, risk, execution
    # -----------------------------------------------------------------------
    initial_capital = cfg.get("risk.initial_capital", 10_000.0)
    portfolio = PortfolioState(initial_capital=initial_capital)

    # Attempt DB recovery of portfolio state
    last_snapshot = db.get_latest_portfolio_snapshot()
    if last_snapshot:
        await portfolio.from_dict(last_snapshot)
        logger.info(f"Recovered portfolio from DB: capital={portfolio.sync_capital():.2f}")

    # Alert notifier (telegram / discord)
    alert_cfg = AlertConfig(
        enabled=cfg.get("alerts.enabled", False),
        telegram_bot_token=cfg.get("alerts.telegram_bot_token"),
        telegram_chat_id=cfg.get("alerts.telegram_chat_id"),
        discord_webhook_url=cfg.get("alerts.discord_webhook_url"),
        min_level=cfg.get("alerts.min_level", "info"),
    )
    notifier = AlertNotifier(alert_cfg)
    logger.info("AlertNotifier ready (enabled=%s)", alert_cfg.enabled)

    risk_mgr = RiskManager(
        config=cfg,
        db=db,
        notifier=notifier,
    )

    executor = ExecutionEngine(
        config=cfg,
        db=db,
        mode=mode,
    )

    # Load open trades from DB into executor + portfolio
    open_trades = db.get_open_trades()
    if open_trades:
        loaded = await executor.load_open_trades()
        logger.info(f"Recovered {len(open_trades)} open trades from DB")

        # --- SYNC: mirror executor open trades into PortfolioState ---
        # Without this, the portfolio thinks it has 0 positions while
        # the executor tracks N open trades → dashboard / risk mismatch.
        for trade in loaded:
            notional = trade.entry_price * trade.size
            # entry_fee may be 0 if not persisted in DB schema (acceptable)
            total_cost = notional + getattr(trade, 'entry_fee', 0.0)
            pos = Position(
                symbol=trade.symbol,
                side=trade.side,
                entry_price=trade.entry_price,
                size=trade.size,
                entry_time_ms=int(trade.timestamp_ms),
                stop_loss_price=None,
                take_profit_price=None,
                unrealized_pnl=0.0,
                metadata={
                    "strategy": trade.reason.replace("restored_from_db", "unknown"),
                    "trade_id": trade.trade_id,
                    "restored_from_db": True,
                },
            )
            try:
                await portfolio.add_position(pos, cost=total_cost)
                logger.info(
                    "Restored position into portfolio: %s %s size=%.6f @ %.2f (id=%d)",
                    trade.symbol, trade.side, trade.size,
                    trade.entry_price, trade.trade_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to restore position %s into portfolio: %s",
                    trade.symbol, exc,
                )

    # -----------------------------------------------------------------------
    # 8. Start data pipeline (WS + CandleBuilder BEFORE engine)
    # -----------------------------------------------------------------------
    # Run WS clients as background tasks (Python 3.14 work-around)
    hl_ws_task = asyncio.create_task(hl_ws.start())
    logger.info("Hyperliquid WebSocket task started")

    if binance_ws is not None:
        binance_ws_task = asyncio.create_task(binance_ws.start())
        logger.info("Binance WebSocket task started")

    await candle_builder.start()
    logger.info("CandleBuilder started")

    # -----------------------------------------------------------------------
    # 9. Start TradingEngine
    # -----------------------------------------------------------------------
    engine = TradingEngine(
        config=cfg,
        db=db,
        data_bus=data_bus,
        strategies=strategies,
        risk_manager=risk_mgr,
        executor=executor,
        notifier=notifier,
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
            # CRIT-003: Default to localhost only (was 0.0.0.0 = all interfaces)
            "host": cfg.get("dashboard.host", "127.0.0.1"),
            "port": cfg.get("dashboard.port", 5000),
            "secret_key": cfg.get("dashboard.secret_key"),
            "dashboard_password": cfg.get("dashboard.password"),
        }
        from dashboard.web import create_app as create_dashboard, set_engine
        app, socketio, emit_fn = create_dashboard(config=dashboard_cfg)
        set_engine(engine)
        engine.on_dashboard_tick = emit_fn  # CRIT-004: use validated setter

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
                allow_unsafe_werkzeug=True,  # Required for local/paper mode (not production)
            )

        dashboard_thread = threading.Thread(target=_run_dashboard, daemon=True)
        dashboard_thread.start()
        logger.info(f"Dashboard started at http://{dashboard_cfg['host']}:{dashboard_cfg['port']}")
        if not dashboard_cfg.get("dashboard_password"):
            logger.warning(
                "Dashboard has NO password protection. "
                "Set dashboard.password in config to secure access."
            )

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
        if engine is not None:
            await engine.stop()
        if candle_builder is not None:
            await candle_builder.stop()
        if hl_ws is not None:
            hl_ws._shutdown = True
            if hl_ws._ws:
                await hl_ws._ws.close()
        if binance_ws is not None:
            binance_ws._shutdown = True
            if binance_ws._ws:
                await binance_ws._ws.close()
        if notifier is not None:
            await notifier.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(0)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        # Log to file for post-mortem analysis
        with open("logs/fatal_errors.log", "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"FATAL ERROR at {datetime.now().isoformat()}\n")
            f.write(f"{'='*60}\n")
            f.write(tb)
            f.write(f"\nError: {e}\n")
        traceback.print_exc()
        print(f"\n[FATAL] {e}", file=sys.stderr)
        print(f"\n[DEBUG] Full traceback saved to logs/fatal_errors.log", file=sys.stderr)
        # Notify fatal error (best-effort, may fail if event loop is broken)
        try:
            if 'notifier' in locals() and notifier is not None:
                asyncio.run(notifier.error(f"FATAL ERROR: {e}\n{tb[:500]}"))
        except Exception:
            pass
        sys.exit(1)
