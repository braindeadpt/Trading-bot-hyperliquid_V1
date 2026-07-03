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

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap (project root + src/ for mixed import styles)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

# Fast-path: security audit without loading the full trading stack (CI + CLI)
if __name__ == "__main__" and "--audit" in sys.argv:
    from security.audit import main as audit_main

    raise SystemExit(audit_main(["--src-dir", str(PROJECT_ROOT / "src")]))

import argparse
import asyncio
import logging
import os
import signal
import time
from datetime import datetime
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
from utils.config import Config, ConfigError
from utils.logger import setup_logger
from utils.helpers import safe_ensure_dir, resolve_trade_stop_levels
from utils.instance_lock import acquire_instance_lock, release_instance_lock

from data.database import Database
from exchanges.hyperliquid_ws import HyperliquidWSClient, DataBus
from exchanges.hyperliquid_rest import HyperliquidRESTClient
from exchanges.binance_api import BinanceRESTClient, BinanceWSClient
from exchanges.binance_futures_feed import BinanceFuturesFeed
from data.candle_builder import CandleBuilder

from strategies.factory import build_ensemble, build_sub_strategies, build_strategy_list, build_backtest_strategy

from strategies.base import Position
from core.risk_manager import RiskManager
from core.execution import ExecutionEngine
from core.engine import TradingEngine

from alerts.notifier import AlertNotifier, AlertConfig
from alerts.telegram_bot import TelegramCommandBot

from dashboard.web import create_app as create_dashboard

# ---------------------------------------------------------------------------
# Globals for signal handling
# ---------------------------------------------------------------------------
_engine: Optional[TradingEngine] = None
_telegram_bot: Optional[TelegramCommandBot] = None
_dashboard_socketio: Optional[Any] = None
_hl_ws: Optional[HyperliquidWSClient] = None
_binance_ws: Optional[BinanceWSClient] = None
_candle_builder: Optional[CandleBuilder] = None
_binance_futures_feed: Optional[BinanceFuturesFeed] = None
_logger = None


def _resolve_telegram_credentials(cfg: Config) -> tuple[Optional[str], list[str]]:
    """Resolve Telegram token and allowed chat IDs from YAML + env."""
    import os

    token = (cfg.get("alerts.telegram_bot_token") or "").strip()
    if not token:
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()

    chat_ids: list[str] = []
    raw_chat = (cfg.get("alerts.telegram_chat_id") or "").strip()
    if not raw_chat:
        raw_chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if raw_chat:
        chat_ids.append(raw_chat)

    extra = (os.environ.get("TELEGRAM_CHAT_IDS") or "").strip()
    if extra:
        chat_ids.extend(c.strip() for c in extra.split(",") if c.strip())

    return token or None, chat_ids


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


async def _run_backtest(
    cfg: Config,
    db: Database,
    logger: Any,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a historical backtest using the backtest engine."""
    from backtest.engine import BacktestEngine, BacktestConfig
    from datetime import datetime, timezone

    logger.info("=" * 60)
    logger.info("BACKTEST MODE")
    logger.info("=" * 60)

    initial_capital = cfg.get("backtest.initial_capital", cfg.get("risk.initial_capital", 10_000.0))
    commission_pct = cfg.get("backtest.commission_pct", cfg.get("risk.taker_fee_pct", 0.035))
    slippage_bps = cfg.get("backtest.slippage_bps", 2.0)
    symbols = cfg.get("assets", ["BTC", "ETH", "SOL"])

    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    if from_date:
        start_ms = int(
            datetime.strptime(from_date, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp() * 1000
        )
    if to_date:
        end_ms = int(
            datetime.strptime(to_date, "%Y-%m-%d")
            .replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            .timestamp() * 1000
        )

    logger.info(
        "Backtest config: capital=$%.2f, commission=%.3f%%, slippage=%.1fbps, symbols=%s",
        initial_capital, commission_pct, slippage_bps, symbols,
    )
    if from_date or to_date:
        logger.info("Date range: %s → %s", from_date or "start", to_date or "end")

    ensemble = build_backtest_strategy(cfg)
    bt_config = BacktestConfig(
        initial_capital=float(initial_capital),
        commission_pct=float(commission_pct),
        slippage_bps=float(slippage_bps),
        max_positions=int(cfg.get("risk.max_positions", 5)),
        tca_enabled=bool(cfg.get("execution.tca_enabled", True)),
        min_edge_buffer_pct=float(cfg.get("execution.min_edge_buffer_pct", 0.05)),
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.05)),
        use_regime_weights=bool(cfg.get("backtest.use_regime_weights", True)),
        use_cooldown=bool(cfg.get("backtest.use_cooldown", True)),
        use_kelly=bool(cfg.get("backtest.use_kelly", True)),
        use_microstructure_proxy=bool(cfg.get("backtest.use_microstructure_proxy", True)),
        regime_weights=cfg.get("strategy.regime_weights", {}),
        adx_trend_threshold=float(cfg.get("strategy.adx_trend_threshold", 25.0)),
        adx_range_threshold=float(cfg.get("strategy.adx_range_threshold", 20.0)),
        cooldown_base_ms=int(cfg.get("strategy.cooldown.base_minutes", 60) * 60_000),
        max_daily_trades=int(cfg.get("risk.max_daily_trades", 5)),
    )

    bt = BacktestEngine(
        database=db,
        strategy=ensemble,
        config=bt_config,
        symbols=symbols,
    )

    result = bt.run(start_ms=start_ms, end_ms=end_ms)
    metrics = result["metrics"]

    logger.info("Backtest complete — %d trades", metrics.get("n_trades", 0))
    logger.info("Total return: %.2f%%", metrics.get("total_return", 0) * 100)
    logger.info("Sharpe: %.3f", metrics.get("sharpe_ratio", 0))
    logger.info("Max DD: %.2f%%", metrics.get("max_drawdown", 0) * 100)
    logger.info("Win rate: %.1f%%", metrics.get("win_rate", 0) * 100)

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
        max_bytes=int(cfg.get("logging.max_bytes", 10_485_760)),
        backup_count=int(cfg.get("logging.backup_count", 14)),
        rotation_when=str(cfg.get("logging.rotation_when", "midnight")),
        rotation_interval=int(cfg.get("logging.rotation_interval", 1)),
        utc=bool(cfg.get("logging.rotation_utc", False)),
    )
    # Route all other loggers to the same handlers
    root = logging.getLogger()
    root.setLevel(logger.level)
    for handler in logger.handlers:
        root.addHandler(handler)
    # "Main" would otherwise emit via its own handlers AND propagate to root
    # (same handlers) — every Main line logged twice.
    logger.propagate = False
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
    instance_lock_path = db_path.parent / "bot.lock"
    acquire_instance_lock(instance_lock_path)
    db = Database(str(db_path))
    logger.info(f"Database ready: {db_path}")

    # -----------------------------------------------------------------------
    # 3b. Candle backfill for fast strategy warm-up (live/paper/testnet)
    # -----------------------------------------------------------------------
    if not args.backtest:
        from src.data.candle_backfill import ensure_candle_history

        saved = ensure_candle_history(db, cfg, logger)
        if saved:
            logger.info("Candle backfill complete: %d bars stored", saved)

    # -----------------------------------------------------------------------
    # 4. Backtest mode (early exit after run)
    # -----------------------------------------------------------------------
    if args.backtest:
        metrics = await _run_backtest(cfg, db, logger, args.from_date, args.to_date)
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
    # QW4 (v3.1.13): per-topic rate-limit overrides so that high-frequency
    # trade:* ticks on BTC/ETH/SOL don't get dropped by the B13 backpressure
    # gate (BTC trade bursts exceed 200 msg/s). Other noisy topics stay
    # protected by the 200 Hz global cap.
    data_bus = DataBus(
        rate_limit_hz=cfg.get("exchange.databus.rate_limit_hz", 200),
        topic_rate_limits={
            "trade:": cfg.get("exchange.databus.trade_rate_limit_hz", 2000),
        },
    )

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
    # 6. Initialize strategies (same ensemble as backtest)
    # -----------------------------------------------------------------------
    sub_strategies = build_sub_strategies(cfg)
    strategies = build_strategy_list(cfg)
    ens_enabled = bool(cfg.get("strategy.ensemble.enabled", True))
    if ens_enabled:
        logger.info(
            "StrategyEnsemble loaded: %d sub-strategies, threshold=%.2f, min_agreeing=%d",
            len(sub_strategies),
            cfg.get("strategy.ensemble.threshold", 0.40),
            cfg.get("strategy.ensemble.min_agreeing", 1),
        )
    else:
        logger.info(
            "Phase 1 direct mode: %d sub-strategies (ensemble disabled)",
            len(strategies),
        )
    logger.info("Active strategies: %s", [s.name for s in strategies])

    # -----------------------------------------------------------------------
    # 7. Initialize risk, execution (portfolio is owned by TradingEngine)
    # -----------------------------------------------------------------------
    initial_capital = cfg.get("risk.initial_capital", 10_000.0)

    # Alert notifier (telegram / discord)
    tg_token, tg_chat_ids = _resolve_telegram_credentials(cfg)
    alert_cfg = AlertConfig(
        enabled=cfg.get("alerts.enabled", False),
        telegram_bot_token=tg_token,
        telegram_chat_id=tg_chat_ids[0] if tg_chat_ids else None,
        discord_webhook_url=cfg.get("alerts.discord_webhook_url"),
        min_level=cfg.get("alerts.min_level", "info"),
        trade_alerts=cfg.get("alerts.trade_alerts", True),
    )
    notifier = AlertNotifier(alert_cfg)
    logger.info(
        "AlertNotifier ready (enabled=%s trade_alerts=%s telegram=%s)",
        alert_cfg.enabled,
        alert_cfg.trade_alerts,
        bool(tg_token and tg_chat_ids),
    )

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

    # Load open trades from DB into executor (MUST be before engine start)
    open_trades = db.get_open_trades()
    if open_trades:
        await executor.load_open_trades()
        logger.info(f"Recovered {len(open_trades)} open trades from DB")

    # -----------------------------------------------------------------------
    # 8. Start data pipeline (WS + CandleBuilder BEFORE engine)
    # -----------------------------------------------------------------------
    hl_ws_task = asyncio.create_task(hl_ws.start())
    logger.info("Hyperliquid WebSocket task started")

    if binance_ws is not None:
        binance_ws_task = asyncio.create_task(binance_ws.start())
        logger.info("Binance WebSocket task started")
        from exchanges.binance_price_bridge import forward_binance_prices

        asyncio.create_task(
            forward_binance_prices(
                binance_ws,
                data_bus,
                list(cfg.get("assets", ["BTC", "ETH", "SOL"])),
            ),
            name="binance_price_bridge",
        )
        logger.info("Binance price bridge task started (aggTrade -> DataBus)")

    await candle_builder.start()
    logger.info("CandleBuilder started")

    # Binance USD-M futures feed (liquidations + long/short ratio)
    global _binance_futures_feed
    liq_source = str(cfg.get("market_data.liquidation_source", "auto")).lower()
    ls_enabled = bool(cfg.get("market_data.long_short_ratio_enabled", True))
    if liq_source != "proxy" or ls_enabled:
        _binance_futures_feed = BinanceFuturesFeed(
            bus=data_bus,
            symbols=cfg.get("assets", ["BTC", "ETH", "SOL"]),
            poll_interval_sec=float(cfg.get("market_data.long_short_poll_sec", 300)),
        )
        await _binance_futures_feed.start()
        logger.info("BinanceFuturesFeed started (liquidation_source=%s)", liq_source)

    # Binance USD-M perp mark prices for LeadLag (basis-corrected vs HL perp)
    lead_lag_cfg = cfg.get("strategy.lead_lag", {}) or {}
    if bool(lead_lag_cfg.get("enabled", False)) or bool(lead_lag_cfg.get("auto_enable", False)):
        from exchanges.binance_perp_price_bridge import forward_binance_perp_prices

        asyncio.create_task(
            forward_binance_perp_prices(
                data_bus,
                list(cfg.get("assets", ["BTC", "ETH", "SOL"])),
            ),
            name="binance_perp_price_bridge",
        )
        logger.info("Binance perp mark-price bridge started (markPrice@1s -> DataBus)")

    # -----------------------------------------------------------------------
    # 9. Start TradingEngine (this creates & loads the portfolio snapshot)
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

    # --- SYNC: mirror executor open trades into PortfolioState ---
    # Must happen AFTER engine.start() so the engine's portfolio snapshot
    # is loaded first, then we inject open trades from the executor.
    # We use engine._portfolio here because the engine owns the portfolio
    # (created in __init__ at line 151 of engine.py).
    if open_trades:
        portfolio = engine._portfolio
        db_open_by_id = {int(row["id"]): row for row in open_trades}
        existing = await portfolio.positions
        for trade in list(executor._open_trades.values()):
            if trade.symbol in existing:
                logger.info(
                    "Position %s already in portfolio — skip duplicate restore",
                    trade.symbol,
                )
                continue
            notional = trade.entry_price * trade.size
            total_cost = notional + getattr(trade, 'entry_fee', 0.0)
            db_row = db_open_by_id.get(int(trade.trade_id), {})
            restored_strategy = str(
                db_row.get("strategy")
                or (trade.reason.split(":", 1)[1] if ":" in trade.reason else "unknown")
            )
            sl_price, tp_price = resolve_trade_stop_levels(
                entry_price=trade.entry_price,
                side=trade.side,
                signal_metadata=db_row.get("signal_metadata"),
            )
            pos = Position(
                symbol=trade.symbol,
                side=trade.side,
                entry_price=trade.entry_price,
                size=trade.size,
                entry_time_ms=int(trade.timestamp_ms),
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
                unrealized_pnl=0.0,
                metadata={
                    "strategy": restored_strategy,
                    "sub_strategy": restored_strategy,
                    "trade_id": trade.trade_id,
                    "restored_from_db": True,
                },
            )
            try:
                await portfolio.add_position(pos, cost=total_cost)
                logger.info(
                    "Restored position into portfolio: %s %s size=%.6f @ %.2f "
                    "(id=%d sl=%s tp=%s)",
                    trade.symbol, trade.side, trade.size,
                    trade.entry_price, trade.trade_id,
                    f"{sl_price:.4f}" if sl_price else "None",
                    f"{tp_price:.4f}" if tp_price else "None",
                )
            except Exception as exc:
                logger.warning(
                    "Failed to restore position %s into portfolio: %s",
                    trade.symbol, exc,
                )

    # -----------------------------------------------------------------------
    # 9. Start Dashboard (optional)
    # -----------------------------------------------------------------------
    if not args.no_dashboard:
        dashboard_cfg = {
            "mode": mode.upper(),
            "version": cfg.get("version", "1.0.0"),
            "host": cfg.get("dashboard.host", "127.0.0.1"),
            "port": cfg.get("dashboard.port", 5000),
            "secret_key": cfg.get("dashboard.secret_key"),
            "password": cfg.get("dashboard.password"),
            "token": cfg.get("dashboard.token"),
            "auth_enabled": cfg.get("dashboard.auth_enabled"),
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

    # -----------------------------------------------------------------------
    # 9b. Telegram command bot + digests (Phase A+B)
    # -----------------------------------------------------------------------
    global _telegram_bot
    if (
        cfg.get("alerts.telegram_commands_enabled", True)
        and tg_token
        and tg_chat_ids
    ):
        _telegram_bot = TelegramCommandBot(
            token=tg_token,
            allowed_chat_ids=tg_chat_ids,
            db=db,
            engine_getter=lambda: _engine,
            notifier=notifier,
            poll_interval_sec=cfg.get("alerts.telegram_poll_interval_sec", 2.0),
            digest_hours_utc=cfg.get("alerts.telegram_digest_hours_utc", [8, 20]),
            weekly_digest_day=cfg.get("alerts.telegram_weekly_digest_day", 0),
            weekly_digest_hour_utc=cfg.get("alerts.telegram_weekly_digest_hour_utc", 8),
        )
        await _telegram_bot.start()
        logger.info("Telegram command bot started (/status, /positions, /pnl, …)")

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
        if _telegram_bot is not None:
            await _telegram_bot.stop()
        if engine is not None:
            await engine.stop()
        if candle_builder is not None:
            await candle_builder.stop()
        if _binance_futures_feed is not None:
            await _binance_futures_feed.stop()
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
        release_instance_lock(instance_lock_path)
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
