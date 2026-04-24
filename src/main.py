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
    
    # Validar config obrigatória
    REQUIRED_KEYS = [
        ('bot', dict),
        ('assets', list),
        ('polling', dict),
        ('risk', dict),
        ('strategy', dict),
    ]
    
    for key, expected_type in REQUIRED_KEYS:
        if key not in config:
            raise ValueError(f"Config obrigatória em falta: '{key}'. Verifica config/settings.yaml")
        if not isinstance(config[key], expected_type):
            raise ValueError(f"Config '{key}' deve ser {expected_type.__name__}, não {type(config[key]).__name__}")
    
    # Setup logging
    log_config = config.get('logging', {})
    setup_logging(
        level=log_config.get('level', 'INFO'),
        log_file=log_config.get('file')
    )
    
    bot_config = config['bot']
    logger.info("=" * 50)
    logger.info(f"BOT: {bot_config.get('name', 'Unknown')} v{bot_config.get('version', '0.0.0')}")
    logger.info(f"Paper Trading: {bot_config.get('paper_trading', True)}")
    logger.info("=" * 50)
    
    # Inicializar componentes
    aggregator = DataAggregator(config)
    strategy = MomentumStrategy(config)
    risk = RiskManager(config)
    client = HyperliquidClient(config, paper_trading=bot_config.get('paper_trading', True))
    
    # Configurações
    assets = config['assets']
    polling = config['polling']
    oi_interval = polling.get('oi_interval', 60)
    price_interval = polling.get('price_interval', 10)
    
    logger.info(f"Assets: {assets}")
    logger.info(f"OI polling: {oi_interval}s | Price: {price_interval}s")
    
    # Loop principal
    last_oi_time = 0
    consecutive_errors = 0
    MAX_ERRORS = 5
    BACKOFF_BASE = 5
    
    try:
        while True:
            current_time = time.time()
            
            # Buscar dados agregados de OI a cada intervalo
            if current_time - last_oi_time >= oi_interval:
                try:
                    for asset in assets:
                        logger.info(f"\n[SAT] Analisando {asset}...")
                        
                        data = aggregator.fetch_all_data(asset)
                        
                        if data is None:
                            logger.error(f" Falha a buscar dados de {asset}")
                            continue
                        
                        # Preço atual (da Hyperliquid, mais rápido)
                        hl_data = data.get('exchanges_data', {}).get('hyperliquid', {})
                        current_price = hl_data.get('mark_price', 0)
                        
                        if current_price == 0:
                            logger.warning(f" Preço não disponível para {asset}")
                            continue
                        
                        # Analisar estratégia
                        signal = strategy.analyze(data, current_price)
                        
                        if signal == 'LONG' and risk.can_trade():
                            size = risk.calculate_position_size(current_price)
                            order = client.place_order(asset, 'BUY', size, current_price)
                            logger.info(f" Ordem executada: {order}")
                            risk.record_trade()
                        
                        # Verificar saída
                        exit_signal = strategy.should_exit(current_price, data)
                        if exit_signal:
                            client.close_position(asset)
                            logger.info(f"[OUT] Posição fechada: {exit_signal}")
                    
                    last_oi_time = current_time
                    consecutive_errors = 0  # Reset após sucesso
                    
                except Exception as e:
                    consecutive_errors += 1
                    backoff = BACKOFF_BASE * consecutive_errors
                    logger.error(f"Erro no loop principal ({consecutive_errors}/{MAX_ERRORS}): {e}")
                    if consecutive_errors >= MAX_ERRORS:
                        logger.critical("Muitos erros consecutivos. A parar o bot.")
                        break
                    logger.info(f"A aguardar {backoff}s antes de tentar novamente...")
                    time.sleep(backoff)
            
            # Sleep para não esgotar CPU
            time.sleep(min(oi_interval, price_interval))
            
    except KeyboardInterrupt:
        logger.info("\n[STOP] Bot interrompido pelo utilizador")
    except Exception as e:
        logger.error(f" Erro fatal: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
