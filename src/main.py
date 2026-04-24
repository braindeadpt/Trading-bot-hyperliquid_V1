"""
Bot principal - Loop de execução com PaperTrader
Integra: DataAggregator + PaperTrader + Dashboard Web
"""
import time
import logging
import threading
import os, sys
from pathlib import Path

# ========== WINDOWS UTF-8 FIX ==========
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
# ======================================

from utils import load_config, setup_logging
from data_aggregator import DataAggregator
from paper_trading import PaperTrader
from dashboard_web import create_dashboard_app

logger = logging.getLogger(__name__)


def main():
    """Loop principal do bot"""
    
    # Carregar config
    config = load_config()
    
    # Setup logging
    log_config = config.get('logging', {})
    setup_logging(
        level=log_config.get('level', 'INFO'),
        log_file=log_config.get('file')
    )
    
    bot_config = config['bot']
    logger.info("=" * 50)
    logger.info(f"BOT: {bot_config.get('name', 'Hyperliquid Momentum Bot')} v{bot_config.get('version', '0.1.0')}")
    logger.info(f"Paper Trading: {bot_config.get('paper_trading', True)}")
    logger.info(f"Capital inicial: ${config.get('risk', {}).get('initial_capital', 10000):,.2f}")
    logger.info("=" * 50)
    
    # Inicializar componentes
    assets = config['assets']
    logger.info(f"Assets monitorados: {assets}")
    
    # Paper Trader (com crash recovery automatico)
    trader = PaperTrader(config)
    
    # Dashboard Web
    dashboard = create_dashboard_app(config)
    dashboard_thread = threading.Thread(
        target=lambda: dashboard.run(host='0.0.0.0', port=5000, debug=False),
        daemon=True,
        name='dashboard'
    )
    dashboard_thread.start()
    logger.info("Dashboard Web: http://127.0.0.1:5000")
    
    # Iniciar monitorizacao
    logger.info("A iniciar monitorizacao...")
    trader._start_monitor_thread(assets[0])
    
    # Loop principal
    logger.info("Bot operacional! Ctrl+C para parar (graceful shutdown)")
    
    try:
        while not trader.is_shutdown_requested():
            # O PaperTrader corre em threads separadas
            # Aqui só mantemos o processo vivo e verificamos estado
            time.sleep(1)
            
            # Log periódico do estado
            if int(time.time()) % 60 == 0:  # A cada minuto
                status = {
                    'position': trader.current_position,
                    'capital': trader.capital,
                    'trades': trader.trade_count,
                    'price': trader._last_price,
                }
                logger.info(f"📊 Status | Capital: ${status['capital']:,.2f} | Trades: {status['trades']} | Pos: {status['position'] or 'FLAT'}")
    
    except KeyboardInterrupt:
        logger.info("Ctrl+C recebido...")
    finally:
        logger.info("A parar bot...")
        # Graceful shutdown ja e tratado pelo signal handler do PaperTrader
        logger.info("Bot parado. Boa noite!")


if __name__ == "__main__":
    main()
