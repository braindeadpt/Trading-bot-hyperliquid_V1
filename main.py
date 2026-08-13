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
from utils.config import Config, ConfigError, get_strategy_section, get_trading_symbols, load_config, phase08_enabled
from utils.logger import setup_logger
from utils.helpers import safe_ensure_dir
from utils.instance_lock import acquire_instance_lock, release_instance_lock

from data.database import Database
from exchanges.hyperliquid_ws import HyperliquidWSClient, DataBus
from exchanges.hyperliquid_rest import HyperliquidRESTClient
from exchanges.binance_api import BinanceRESTClient, BinanceWSClient
from exchanges.binance_futures_feed import BinanceFuturesFeed
from exchanges.liquidation_aggregator import MultiVenueLiquidationAggregator
from data.candle_builder import CandleBuilder

from strategies.factory import (
    build_ensemble,
    build_sub_strategies,
    build_strategy_list,
    build_backtest_strategy,
    build_live_strategies,
)

from core.risk_manager import RiskManager
from core.execution import ExecutionEngine
from core.engine import TradingEngine

from alerts.notifier import AlertNotifier, AlertConfig
from alerts.telegram_bot import TelegramCommandBot

from dashboard.web import create_app as create_dashboard

# ---------------------------------------------------------------------------
# Globals for graceful shutdown
# ---------------------------------------------------------------------------
_engine: Optional[TradingEngine] = None
_telegram_bot: Optional[TelegramCommandBot] = None
_dashboard_socketio: Optional[Any] = None
_hl_ws: Optional[HyperliquidWSClient] = None
_binance_ws: Optional[BinanceWSClient] = None
_candle_builder: Optional[CandleBuilder] = None
_binance_futures_feed: Optional[BinanceFuturesFeed] = None
_liquidation_aggregator: Optional[MultiVenueLiquidationAggregator] = None
_research_sampler: Optional[Any] = None
_research_microstructure: Optional[Any] = None
_l2_book_recorder: Optional[Any] = None
_dvol_feed: Optional[Any] = None
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


_shutdown_requested = False


async def _run_backtest(
    cfg: Config,
    db: Database,
    logger: Any,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    *,
    use_research_db: bool = False,
) -> Dict[str, Any]:
    """Run a historical backtest using the backtest engine."""
    from backtest.engine import BacktestEngine, build_backtest_config_from_yaml
    from datetime import datetime, timezone
    from utils.config import resolve_kelly_enabled

    if use_research_db:
        from data.research_database import ResearchDatabase
        db = ResearchDatabase(ResearchDatabase.resolve_path(cfg))
        logger.info("Backtest using research DB: %s", db.db_path)

    logger.info("=" * 60)
    logger.info("BACKTEST MODE")
    logger.info("=" * 60)

    initial_capital = cfg.get("backtest.initial_capital", cfg.get("risk.initial_capital", 10_000.0))
    commission_pct = cfg.get("backtest.commission_pct", cfg.get("risk.taker_fee_pct", 0.035))
    slippage_bps = cfg.get("backtest.slippage_bps", 2.0)
    symbols = get_trading_symbols(cfg)

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
        "Backtest config: capital=$%.2f, commission=%.3f%%, slippage=%.1fbps, symbols=%s, kelly=%s",
        initial_capital, commission_pct, slippage_bps, symbols,
        resolve_kelly_enabled(cfg, for_backtest=True),
    )
    if from_date or to_date:
        logger.info("Date range: %s → %s", from_date or "start", to_date or "end")

    ensemble = build_backtest_strategy(cfg)
    bt_config = build_backtest_config_from_yaml(cfg)

    bt = BacktestEngine(
        database=db,
        strategy=ensemble,
        config=bt_config,
        symbols=symbols,
        risk_config=cfg,
    )

    result = bt.run(start_ms=start_ms, end_ms=end_ms)
    metrics = result["metrics"]
    manifest = result.get("manifest", {})

    logger.info("Backtest complete — %d trades", metrics.get("n_trades", 0))
    if manifest:
        logger.info(
            "Run manifest: commit=%s config_hash=%s sizing=%s fidelity=%s",
            manifest.get("git_commit"),
            manifest.get("config_hash"),
            manifest.get("sizing_version"),
            manifest.get("fidelity_tier"),
        )
    logger.info("Total return: %.2f%%", metrics.get("total_return", 0) * 100)
    logger.info("Sharpe: %.3f", metrics.get("sharpe_ratio", 0))
    logger.info("Max DD: %.2f%%", metrics.get("max_drawdown", 0) * 100)
    logger.info("Win rate: %.1f%%", metrics.get("win_rate", 0) * 100)

    return metrics


def _request_shutdown(signum: int, frame: Any) -> None:
    """Signal handler for graceful SIGINT/SIGTERM shutdown."""
    global _shutdown_requested
    _shutdown_requested = True
    if _logger:
        _logger.warning("Shutdown requested via signal %s", signum)


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Hyperliquid Premium Trading Bot")
    parser.add_argument("--config", default="config/settings.yaml", help="Path to YAML config")
    parser.add_argument("--mode", choices=["paper", "testnet", "mainnet"], default=None, help="Override trading mode")
    parser.add_argument("--backtest", action="store_true", help="Run backtest mode")
    parser.add_argument(
        "--research-db",
        action="store_true",
        help="Use research hyperliquid.db (separate from live bot.db)",
    )
    parser.add_argument("--from-date", dest="from_date", help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--to-date", dest="to_date", help="Backtest end date (YYYY-MM-DD)")
    parser.add_argument("--audit", action="store_true", help="Run security audit and exit")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable web dashboard")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _request_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_shutdown)

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
    symbols = get_trading_symbols(cfg)

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

    if not args.backtest and not args.audit and phase08_enabled(cfg):
        p08 = cfg.get("strategy.phase08", {}) or {}
        if bool(p08.get("paper_only", True)) and mode != "paper":
            logger.error(
                "Phase08 requires paper mode — tier_a_hl_ohlc certifies data only, not edge. "
                "Refusing %s execution.",
                mode,
            )
            raise SystemExit(1)
        from src.research.phase08_preregister import (
            assert_config_matches_preregister as assert_phase08_preregister,
            persist_preregister_manifest as persist_phase08_preregister,
        )
        from src.research.phase10_preregister import (
            assert_config_matches_preregister as assert_phase10_preregister,
        )

        persist_phase08_preregister(cfg)
        assert_phase08_preregister(cfg)
        logger.info("Phase08 preregister manifest verified (immutable, OOS pending)")
        assert_phase10_preregister(cfg)
        logger.info("Phase10 frozen-window preregister verified (assert active)")

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
        metrics = await _run_backtest(
            cfg, db, logger, args.from_date, args.to_date,
            use_research_db=args.research_db,
        )
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
        symbols=symbols,
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
            symbols=symbols,
            ws_base=cfg.get("exchange.binance.ws_url", "wss://stream.binance.com:9443/ws"),
        )

    candle_builder = CandleBuilder(bus=data_bus, symbols=symbols, timeframes=[60, 300, 900, 3600])

    global _hl_ws, _binance_ws, _candle_builder
    _hl_ws = hl_ws
    _binance_ws = binance_ws
    _candle_builder = candle_builder

    # -----------------------------------------------------------------------
    # 6. Initialize strategies (Phase08: VB+VWAP execution, rest shadow)
    # -----------------------------------------------------------------------
    shadow_strategies: list = []
    if phase08_enabled(cfg):
        strategies, shadow_strategies = build_live_strategies(cfg)
        logger.info(
            "Phase08 edge isolation — execution=%s shadow=%s",
            [s.name for s in strategies],
            [s.name for s in shadow_strategies],
        )
    else:
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
                symbols,
            ),
            name="binance_price_bridge",
        )
        logger.info("Binance price bridge task started (aggTrade -> DataBus)")

    await candle_builder.start()
    logger.info("CandleBuilder started")

    # Binance USD-M futures feed (legacy @forceOrder + long/short ratio).
    # fstream liquidations are blocked on this network; LS ratio still polls REST.
    global _binance_futures_feed, _liquidation_aggregator
    liq_source = str(cfg.get("market_data.liquidation_source", "auto")).lower()
    ls_enabled = bool(cfg.get("market_data.long_short_ratio_enabled", True))
    if liq_source != "proxy" or ls_enabled:
        _binance_futures_feed = BinanceFuturesFeed(
            bus=data_bus,
            symbols=symbols,
            poll_interval_sec=float(cfg.get("market_data.long_short_poll_sec", 300)),
        )
        await _binance_futures_feed.start()
        logger.info("BinanceFuturesFeed started (liquidation_source=%s)", liq_source)

    # Multi-venue REAL liquidations (OKX + Bybit WS; Coinalyze verify-only).
    # Start after we have symbols; silence beats wired once engine exists (below).
    if liq_source in ("real", "auto", "binance"):
        _md = cfg.get("market_data", {}) or {}
        _liquidation_aggregator = MultiVenueLiquidationAggregator(
            bus=data_bus,
            symbols=symbols,
            enable_okx=bool(_md.get("liquidation_okx_enabled", True)),
            enable_bybit=bool(_md.get("liquidation_bybit_enabled", True)),
            enable_coinalyze_check=bool(_md.get("liquidation_coinalyze_check", True)),
            coinalyze_api_key=str(_md.get("coinalyze_api_key") or "") or None,
            coinalyze_poll_sec=float(_md.get("liquidation_coinalyze_poll_sec", 900)),
        )
        logger.info("LiquidationAggregator constructed (mode=%s)", liq_source)

    # Binance USD-M perp mark prices for LeadLag (basis-corrected vs HL perp)
    lead_lag_cfg = cfg.get("strategy.lead_lag", {}) or {}
    if bool(lead_lag_cfg.get("enabled", False)) or bool(lead_lag_cfg.get("auto_enable", False)):
        from exchanges.binance_perp_price_bridge import forward_binance_perp_prices

        asyncio.create_task(
            forward_binance_perp_prices(
                data_bus,
                symbols,
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
        shadow_strategies=shadow_strategies,
    )
    engine.set_ws_client(hl_ws)
    global _engine
    _engine = engine

    await engine.start()
    logger.info("TradingEngine started")

    if _liquidation_aggregator is not None:
        def _cz_silence_beat(ts_ms: int) -> None:
            if getattr(engine, "_feed_silence_enabled", False):
                engine._feed_silence.beat("liquidation_coinalyze_check", ts_ms)

        _liquidation_aggregator._on_coinalyze_check = _cz_silence_beat
        await _liquidation_aggregator.start()
        logger.info(
            "MultiVenueLiquidationAggregator started — %s",
            _liquidation_aggregator.stats().get("coinalyze_budget_note"),
        )

    global _research_sampler, _research_microstructure, _l2_book_recorder, _dvol_feed
    if not args.backtest and not args.audit:
        try:
            from data.research_microstructure import start_microstructure_recorder_from_config

            _research_microstructure = start_microstructure_recorder_from_config(data_bus, cfg)
            if _research_microstructure is not None:
                _research_microstructure.attach_ws_client(hl_ws)
                await _research_microstructure.start()
                logger.info(
                    "ResearchMicrostructureRecorder active — raw WS tape + L2 → research DB",
                )
        except Exception as exc:
            logger.warning("ResearchMicrostructureRecorder failed to start: %s", exc)
        try:
            from data.l2_book_recorder import start_l2_book_recorder_from_config

            def _l2_silence_beat(ts_ms: int) -> None:
                if getattr(engine, "_feed_silence_enabled", False):
                    engine._feed_silence.beat("l2_book_recording", ts_ms)

            _l2_book_recorder = start_l2_book_recorder_from_config(
                data_bus,
                cfg,
                project_root=PROJECT_ROOT,
                on_persist=_l2_silence_beat,
            )
            if _l2_book_recorder is not None:
                started = await _l2_book_recorder.start()
                if started:
                    logger.info(
                        "L2BookRecorder active - top-K levels -> %s (stats=%s)",
                        _l2_book_recorder.stats.get("path"),
                        _l2_book_recorder.stats,
                    )
                else:
                    logger.error(
                        "L2BookRecorder failed to start (disk?) — trading continues; "
                        "disabling l2_book_recording silence contract"
                    )
                    _l2_book_recorder = None
                    if getattr(engine, "_feed_silence_enabled", False):
                        engine._feed_silence.disable_feed("l2_book_recording")
            elif getattr(engine, "_feed_silence_enabled", False):
                # Recorder off — do not contract silence for this feed
                engine._feed_silence.disable_feed("l2_book_recording")
        except Exception as exc:
            logger.warning("L2BookRecorder failed to start: %s", exc)
            if getattr(engine, "_feed_silence_enabled", False):
                engine._feed_silence.disable_feed("l2_book_recording")
        research_cfg = cfg.get("research", {}) or {}
        if bool(research_cfg.get("rest_sampling_enabled", False)):
            try:
                from data.research_sampler import start_research_sampler_from_config

                _research_sampler = start_research_sampler_from_config(cfg)
                if _research_sampler is not None:
                    await _research_sampler.start()
                    logger.warning(
                        "ResearchSampler REST fallback active (60s poll) — not Tier A",
                    )
            except Exception as exc:
                logger.warning("ResearchSampler failed to start: %s", exc)
        try:
            from data.dvol_feed import start_dvol_feed_from_config

            _dvol_feed = start_dvol_feed_from_config(cfg)
            if _dvol_feed is not None:
                await _dvol_feed.start()
                logger.info("DvolFeed active — Deribit DVOL daily → research DB")
        except Exception as exc:
            logger.warning("DvolFeed failed to start: %s", exc)

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
    # 10. Main loop (Ctrl+C → KeyboardInterrupt → finally cleanup)
    # -----------------------------------------------------------------------
    logger.info("Bot running. Press Ctrl+C to stop.")
    try:
        while not _shutdown_requested:
            await asyncio.sleep(0.5)
    except (asyncio.CancelledError, KeyboardInterrupt):
        if _logger:
            _logger.warning("Shutdown requested (Ctrl+C)")
    finally:
        try:
            if _telegram_bot is not None:
                await _telegram_bot.stop()
        except Exception:
            logger.exception("Telegram bot stop failed")
        try:
            if engine is not None:
                await engine.stop()
        except Exception:
            logger.exception("Engine stop failed")
        try:
            if candle_builder is not None:
                await candle_builder.stop()
        except Exception:
            logger.exception("CandleBuilder stop failed")
        try:
            if _research_microstructure is not None:
                await _research_microstructure.stop()
        except Exception:
            logger.exception("ResearchMicrostructureRecorder stop failed")
        try:
            if _l2_book_recorder is not None:
                await _l2_book_recorder.stop()
        except Exception:
            logger.exception("L2BookRecorder stop failed")
        try:
            if _research_sampler is not None:
                await _research_sampler.stop()
        except Exception:
            logger.exception("ResearchSampler stop failed")
        try:
            if _dvol_feed is not None:
                await _dvol_feed.stop()
        except Exception:
            logger.exception("DvolFeed stop failed")
        try:
            if _binance_futures_feed is not None:
                await _binance_futures_feed.stop()
        except Exception:
            logger.exception("BinanceFuturesFeed stop failed")
        try:
            if _liquidation_aggregator is not None:
                await _liquidation_aggregator.stop()
        except Exception:
            logger.exception("LiquidationAggregator stop failed")
        try:
            if hl_ws is not None:
                hl_ws._shutdown = True
                if hl_ws._ws:
                    await hl_ws._ws.close()
        except Exception:
            logger.exception("Hyperliquid WS close failed")
        try:
            if binance_ws is not None:
                binance_ws._shutdown = True
                if binance_ws._ws:
                    await binance_ws._ws.close()
        except Exception:
            logger.exception("Binance WS close failed")
        try:
            if notifier is not None:
                await notifier.close()
        except Exception:
            logger.exception("Notifier close failed")
        release_instance_lock(instance_lock_path)
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
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
