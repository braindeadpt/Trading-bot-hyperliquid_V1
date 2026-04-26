#!/usr/bin/env python3
"""
Hyperliquid Bot v2.0 — Entry Point Unificado
Modos: web | cli | headless

Usage:
    python run.py web          # Flask + System Tray
    python run.py cli          # Terminal Rich
    python run.py headless     # Só o bot, sem UI
"""
import argparse
import sys
import time
import logging
import threading
from pathlib import Path

# Adicionar src refatorado ao path
sys.path.insert(0, str(Path(__file__).parent / "refactored"))

from utils.config import load_config
from core.event_bus import EventBus
from core.container import ServiceContainer
from core.state_machine import BotState, StateMachine
from web.app import WebApp
from cli.terminal import TerminalCLI

logger = logging.getLogger(__name__)


def setup_logging(config):
    """Configura logging com UTF-8."""
    log_cfg = config.get('logging', {})
    level = getattr(logging, log_cfg.get('level', 'INFO').upper())
    
    handlers = [logging.StreamHandler()]
    
    log_file = log_cfg.get('file')
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding='utf-8'))
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers
    )


class BotEngine:
    """
    Motor principal do bot.
    Orquestra: aggregator → strategy → trader → database
    Usa StateMachine + EventBus para comunicação desacoplada.
    """
    
    def __init__(self, config: dict, container: ServiceContainer):
        self.config = config
        self.container = container
        self.event_bus = container.event_bus
        self.state_machine = StateMachine(event_bus=self.event_bus)
        
        self.aggregator = container.aggregator
        self.strategy = container.strategy
        self.trader = container.trader
        
        self.assets = config.get('assets', ['BTC'])
        self.interval = config.get('polling', {}).get('oi_interval', 30)
        
        self._running = False
        self._thread = None
        
        # Subscrever a comandos
        self.event_bus.subscribe('bot.command', self._on_command)
    
    def _on_command(self, event):
        cmd = event.payload.get('action')
        if cmd == 'start':
            self.start()
        elif cmd == 'stop':
            self.stop()
        elif cmd == 'emergency_close':
            self._emergency_close()
    
    def start(self):
        """Arranca motor numa thread."""
        if self._running:
            logger.warning("[Engine] Já está a correr")
            return
        
        self._running = True
        self.state_machine.transition(BotState.SCANNING, "Bot iniciado")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="bot-engine")
        self._thread.start()
        
        self.event_bus.publish('bot.status', {'running': True})
        logger.info("[Engine] ✅ Motor iniciado")
    
    def stop(self):
        """Para motor graciosamente."""
        if not self._running:
            return
        
        self._running = False
        self.state_machine.transition(BotState.SHUTDOWN, "Shutdown solicitado")
        
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        
        self.event_bus.publish('bot.status', {'running': False})
        logger.info("[Engine] 🛑 Motor parado")
    
    def _loop(self):
        """Loop principal."""
        logger.info(f"[Engine] Ciclo a cada {self.interval}s")
        
        while self._running:
            try:
                for asset in self.assets:
                    # 1. Buscar dados
                    self.state_machine.transition(BotState.SCANNING, f"Buscando {asset}")
                    data = self.aggregator.get_all_data(asset)
                    
                    if not data:
                        logger.warning(f"[Engine] Sem dados para {asset}")
                        continue
                    
                    price = data.get('price', 0)
                    if price <= 0:
                        continue
                    
                    # 2. Analisar + Executar
                    self.state_machine.transition(BotState.ANALYZING, f"Analisando {asset}")
                    self.trader.on_market_data({**data, 'asset': asset})
                
                # 3. Log periódico
                status = self.trader.get_status()
                logger.info(
                    f"[Engine] 💰 ${status['capital']:,.2f} | "
                    f"Trades: {status['trade_count']} | "
                    f"Pos: {'SIM' if status['in_position'] else 'NÃO'}"
                )
                
                # 4. Aguardar
                for _ in range(self.interval):
                    if not self._running:
                        break
                    time.sleep(1)
                    
            except Exception as e:
                logger.error(f"[Engine] Erro no loop: {e}")
                self.state_machine.transition(BotState.ERROR, str(e))
                time.sleep(5)
    
    def _emergency_close(self):
        """Fecha posição de emergência."""
        logger.error("[Engine] 🚨 EMERGENCY CLOSE solicitado!")
        if self.trader.position:
            self.trader._exit_position(
                self.trader.position.asset,
                self.aggregator.get_latest_price(self.trader.position.asset),
                'EMERGENCY'
            )


def run_web(config, container):
    """Modo Web: Flask + System Tray."""
    logger.info("=" * 60)
    logger.info("  🚀 MODO WEB — Flask + Dashboard")
    logger.info("=" * 60)
    
    # Iniciar motor
    engine = BotEngine(config, container)
    engine.start()
    
    # Iniciar web
    web = WebApp(config, container.event_bus, container.database)
    web_thread = web.start_thread()
    
    # Manter vivo
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Ctrl+C — a encerrar...")
        engine.stop()


def run_cli(config, container):
    """Modo CLI: Terminal Rich."""
    logger.info("=" * 60)
    logger.info("  🖥️  MODO CLI — Terminal Rich")
    logger.info("=" * 60)
    
    engine = BotEngine(config, container)
    engine.start()
    
    cli = TerminalCLI(container.event_bus)
    cli.run()
    
    engine.stop()


def run_headless(config, container):
    """Modo Headless: Só o bot."""
    logger.info("=" * 60)
    logger.info("  🤖 MODO HEADLESS — Sem UI")
    logger.info("=" * 60)
    
    engine = BotEngine(config, container)
    engine.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Ctrl+C — a encerrar...")
        engine.stop()


def main():
    parser = argparse.ArgumentParser(description='Hyperliquid Bot v2.0')
    parser.add_argument('mode', choices=['web', 'cli', 'headless'], default='web', nargs='?',
                       help='Modo de execução')
    parser.add_argument('--config', '-c', default='config/settings.yaml',
                       help='Ficheiro de configuração')
    
    args = parser.parse_args()
    
    # Setup
    config = load_config(args.config)
    setup_logging(config)
    
    # Container DI
    event_bus = EventBus()
    container = ServiceContainer(config, event_bus=event_bus).boot()
    
    logger.info(f"[Main] Modo: {args.mode.upper()}")
    logger.info(f"[Main] Assets: {config['assets']}")
    logger.info(f"[Main] Paper Trading: {config['bot'].get('paper_trading', True)}")
    
    # Executar modo
    if args.mode == 'web':
        run_web(config, container)
    elif args.mode == 'cli':
        run_cli(config, container)
    else:
        run_headless(config, container)


if __name__ == '__main__':
    main()
