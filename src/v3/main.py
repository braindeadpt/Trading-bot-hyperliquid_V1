"""
Hyperliquid Bot v3.0 — Entry Point Unificado
Integra 3 sistemas:
  1. Legado src/      — main.py, data_aggregator.py, paper_trading.py
  2. Refatorado refactored/ — EventBus, Container DI, Engine v2, Terminal v2, Web v2
  3. Clean clean/     — Domain entities, Use Cases, Repositories, Gateway

Modos de arranque:
  python -m src.v3.main              # Terminal Rich + Flask (default)
  python -m src.v3.main --mode web   # Só Flask
  python -m src.v3.main --mode cli   # Só Terminal Rich

Features:
  ✅ Paper trading por default
  ✅ Config por ficheiro YAML (config/settings.yaml)
  ✅ Terminal Rich com dashboard ao vivo
  ✅ Flask Dashboard em http://127.0.0.1:5000
  ✅ EventBus desacoplado (refactored)
  ✅ Clean Architecture use cases opcionalmente ativos
  ✅ Dados Hyperliquid correctos (BTC/ETH)
  ✅ Arranque via .bat no Windows
"""
import argparse
import sys
import time
import logging
import threading
import os
from pathlib import Path

# ========== PATH SETUP ==========
# Adicionar raiz do projeto ao path para imports funcionarem
PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# ========== WINDOWS UTF-8 FIX ==========
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
# ======================================

from refactored.utils.config import load_config
from refactored.core.event_bus import EventBus
from refactored.core.container import ServiceContainer
from refactored.core.state_machine import BotState, StateMachine
from refactored.optimized.engine_v2 import BotEngine
from refactored.optimized.terminal_v2 import TerminalCLI
from refactored.optimized.webapp_v2 import WebApp

logger = logging.getLogger(__name__)


def setup_logging(config):
    """Configura logging com UTF-8 e handlers múltiplos."""
    log_cfg = config.get('logging', {})
    level = getattr(logging, log_cfg.get('level', 'INFO').upper())

    handlers = [logging.StreamHandler()]

    log_file = log_cfg.get('file')
    if log_file:
        log_path = PROJECT_ROOT / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(str(log_path), encoding='utf-8'))

    # Limpar handlers existentes para evitar duplicação
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers,
        force=True
    )

    logger.info(f"[Logging] Nível: {log_cfg.get('level', 'INFO')} | Ficheiro: {log_file or 'N/A'}")


def load_clean_architecture(config, event_bus):
    """
    Carrega componentes Clean Architecture opcionalmente.
    Retorna dict com use cases e gateway, ou None se falhar.
    """
    try:
        from clean.infrastructure.main import create_app
        app = create_app(config)
        logger.info("[CleanArch] ✅ Componentes Clean Architecture carregados")
        return app
    except Exception as e:
        logger.warning(f"[CleanArch] ⚠️ Não foi possível carregar Clean Arch: {e}")
        return None


def run_unified(config, container, clean_app=None):
    """
    Arranca motor + terminal + web simultaneamente.
    """
    logger.info("=" * 65)
    logger.info("  🔥 HYPERLIQUID BOT v3.0 — UNIFIED ENTRY POINT")
    logger.info("=" * 65)
    logger.info(f"  Assets: {config.get('assets', ['BTC'])}")
    logger.info(f"  Paper Trading: {config['bot'].get('paper_trading', True)}")
    logger.info(f"  Intervalo: {config.get('polling', {}).get('oi_interval', 30)}s")
    logger.info(f"  Capital inicial: ${config.get('risk', {}).get('initial_capital', 10000):,.2f}")
    logger.info("=" * 65)

    # ─── Motor principal (Engine v2 otimizado) ─────────────────
    engine = BotEngine(config, container)

    # ─── Terminal Rich (TerminalCLI v2 otimizado) ──────────────
    terminal = TerminalCLI(container.event_bus)

    # ─── Web Dashboard (WebApp v2 otimizado) ───────────────────
    web = WebApp(
        config,
        container.event_bus,
        container.database,
        port=config.get('web', {}).get('port', 5000)
    )

    # ─── Arranque em threads ───────────────────────────────────
    engine.start()

    # Web em thread daemon
    web_thread = web.start_thread()
    logger.info(f"[Web] 🌐 Dashboard: http://127.0.0.1:{web.port}")

    # Terminal na thread principal (blocante — Live gere o refresh)
    logger.info("[Terminal] 🖥️  A iniciar dashboard Rich...")
    try:
        terminal.run()
    except KeyboardInterrupt:
        logger.info("Ctrl+C no terminal — a encerrar...")
    finally:
        engine.stop()
        logger.info("[Unified] 🛑 Bot encerrado")


def run_web_only(config, container, clean_app=None):
    """Modo web: só Flask dashboard."""
    logger.info("=" * 65)
    logger.info("  🌐 MODO WEB — Flask Dashboard")
    logger.info("=" * 65)

    engine = BotEngine(config, container)
    web = WebApp(
        config,
        container.event_bus,
        container.database,
        port=config.get('web', {}).get('port', 5000)
    )

    engine.start()
    web_thread = web.start_thread()

    logger.info(f"[Web] Dashboard: http://127.0.0.1:{web.port}")
    logger.info("Pressione Ctrl+C para parar")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Ctrl+C — a encerrar...")
        engine.stop()


def run_cli_only(config, container, clean_app=None):
    """Modo CLI: só Terminal Rich."""
    logger.info("=" * 65)
    logger.info("  🖥️  MODO CLI — Terminal Rich")
    logger.info("=" * 65)

    engine = BotEngine(config, container)
    terminal = TerminalCLI(container.event_bus)

    engine.start()

    try:
        terminal.run()
    except KeyboardInterrupt:
        logger.info("Ctrl+C — a encerrar...")
    finally:
        engine.stop()


def main():
    parser = argparse.ArgumentParser(description='Hyperliquid Bot v3.0 — Unified Entry Point')
    parser.add_argument(
        '--mode', '-m',
        choices=['unified', 'web', 'cli'],
        default='unified',
        help='Modo de execução: unified (terminal+web), web (só flask), cli (só terminal)'
    )
    parser.add_argument(
        '--config', '-c',
        default='config/settings.yaml',
        help='Caminho para ficheiro de configuração YAML'
    )
    parser.add_argument(
        '--assets', '-a',
        nargs='+',
        default=None,
        help='Assets a monitorizar (override do config). Ex: BTC ETH'
    )
    parser.add_argument(
        '--paper', '-p',
        action='store_true',
        default=True,
        help='Forçar paper trading (default: True)'
    )
    parser.add_argument(
        '--no-paper',
        action='store_false',
        dest='paper',
        help='Desativar paper trading (⚠️ ATENÇÃO: trading real!)'
    )

    args = parser.parse_args()

    # ─── Carregar config ───────────────────────────────────────
    config_path = PROJECT_ROOT / args.config
    config = load_config(str(config_path))

    # Override de CLI
    if args.assets:
        config['assets'] = args.assets
    config['bot']['paper_trading'] = args.paper

    # ─── Setup logging ──────────────────────────────────────────
    setup_logging(config)

    # ─── Container DI ──────────────────────────────────────────
    event_bus = EventBus()
    container = ServiceContainer(config, event_bus=event_bus).boot()

    logger.info(f"[Main] Modo: {args.mode.upper()}")
    logger.info(f"[Main] Project root: {PROJECT_ROOT}")

    # ─── Clean Architecture (opcional) ─────────────────────────
    clean_app = load_clean_architecture(config, event_bus)

    # ─── Executar modo ─────────────────────────────────────────
    if args.mode == 'unified':
        run_unified(config, container, clean_app)
    elif args.mode == 'web':
        run_web_only(config, container, clean_app)
    elif args.mode == 'cli':
        run_cli_only(config, container, clean_app)


if __name__ == '__main__':
    main()
