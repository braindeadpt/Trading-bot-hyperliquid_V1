"""
Bot principal - Loop de execução
"""
import time
import logging
from pathlib import Path

from utils import load_config, setup_logging
from data_aggregator import DataAggregator
from strategy import MomentumStrategy
from risk_manager import RiskManager
from exchange_client import HyperliquidClient

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
    
    logger.info("=" * 50)
    logger.info(f"🚀 {config['bot']['name']} v{config['bot']['version']}")
    logger.info(f"📊 Paper Trading: {config['bot']['paper_trading']}")
    logger.info("=" * 50)
    
    # Inicializar componentes
    aggregator = DataAggregator(config)
    strategy = MomentumStrategy(config)
    risk = RiskManager(config)
    client = HyperliquidClient(config, paper_trading=config['bot']['paper_trading'])
    
    # Configurações
    assets = config['assets']
    oi_interval = config['polling']['oi_interval']
    price_interval = config['polling']['price_interval']
    
    logger.info(f"Assets: {assets}")
    logger.info(f"OI polling: {oi_interval}s | Price: {price_interval}s")
    
    # Loop principal
    last_oi_time = 0
    
    try:
        while True:
            current_time = time.time()
            
            # Buscar dados agregados de OI a cada intervalo
            if current_time - last_oi_time >= oi_interval:
                for asset in assets:
                    logger.info(f"\n📡 Analisando {asset}...")
                    
                    data = aggregator.fetch_all_data(asset)
                    
                    if data is None:
                        logger.error(f"❌ Falha a buscar dados de {asset}")
                        continue
                    
                    # Preço atual (da Hyperliquid, mais rápido)
                    hl_data = data['exchanges_data'].get('hyperliquid', {})
                    current_price = hl_data.get('mark_price', 0)
                    
                    if current_price == 0:
                        logger.warning(f"⚠️ Preço não disponível para {asset}")
                        continue
                    
                    # Analisar estratégia
                    signal = strategy.analyze(data, current_price)
                    
                    if signal == 'LONG' and risk.can_trade():
                        size = risk.calculate_position_size(current_price)
                        order = client.place_order(asset, 'BUY', size, current_price)
                        logger.info(f"✅ Ordem executada: {order}")
                        risk.record_trade()
                    
                    # Verificar saída
                    exit_signal = strategy.should_exit(current_price, data)
                    if exit_signal:
                        client.close_position(asset)
                        logger.info(f"📤 Posição fechada: {exit_signal}")
                
                last_oi_time = current_time
            
            # Sleep para não esgotar CPU
            time.sleep(price_interval)
            
    except KeyboardInterrupt:
        logger.info("\n🛑 Bot interrompido pelo utilizador")
    except Exception as e:
        logger.error(f"💥 Erro fatal: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
