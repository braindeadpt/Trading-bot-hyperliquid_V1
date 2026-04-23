"""
OI Historical Downloader - Usa CCXT para descarregar Open Interest histórico
"""
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def download_oi_history(symbol: str = 'BTC/USDT:USDT', interval: str = '15m', 
                        days: int = 90, db=None):
    """
    Descarrega OI histórico via CCXT e guarda na base de dados
    
    Args:
        symbol: Símbolo CCXT (ex: BTC/USDT:USDT para futures USD-M)
        interval: Timeframe (5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d)
        days: Número de dias para descarregar
        db: Instância de BotDatabase (opcional)
    """
    try:
        import ccxt
    except ImportError:
        logger.error("CCXT não instalado! Instala com: python -m pip install ccxt")
        return 0
    
    if db is None:
        from database import BotDatabase
        db = BotDatabase()
    
    # Inicializar exchange
    exchange = ccxt.binance({'enableRateLimit': True})
    
    # Calcular timestamp alvo (N dias atrás)
    end_time = int(datetime.now().timestamp() * 1000)
    target_start = end_time - (days * 24 * 60 * 60 * 1000)
    
    logger.info(f"Descarregando OI histórico: {symbol} {interval} ({days} dias)")
    logger.info(f"Alvo: desde {datetime.fromtimestamp(target_start/1000)}")
    
    total_saved = 0
    last_end_time = None
    
    # Paginação - CCXT retorna max 500 registos por chamada
    # Usar params={'endTime': ts} para paginar para trás
    while True:
        try:
            params = {}
            if last_end_time:
                params['endTime'] = last_end_time - 1
                logger.info(f"Buscando OI antes de {datetime.fromtimestamp(last_end_time/1000)}...")
            else:
                logger.info(f"Buscando OI mais recente...")
            
            oi_data = exchange.fetchOpenInterestHistory(
                symbol=symbol,
                timeframe=interval,
                since=None,
                limit=500,
                params=params
            )
            
            if not oi_data:
                logger.warning("Sem dados retornados — fim do histórico")
                break
            
            # Guardar na base de dados
            for oi in oi_data:
                timestamp = oi['timestamp']
                oi_value = oi.get('openInterestValue', 0)  # OI em USDT
                
                # Guardar na SQLite
                db.save_oi(
                    symbol=symbol.split('/')[0],  # BTC
                    timestamp=timestamp,
                    oi_usd=oi_value,
                    exchange='binance'
                )
                total_saved += 1
            
            first_ts = oi_data[0]['timestamp']
            last_ts = oi_data[-1]['timestamp']
            
            logger.info(f"  Guardados {len(oi_data)} registos | Total: {total_saved} | "
                       f"Range: {datetime.fromtimestamp(first_ts/1000)} -> {datetime.fromtimestamp(last_ts/1000)}")
            
            # Verificar se já temos dados suficientes
            if first_ts <= target_start:
                logger.info(f"✅ Atingido alvo de {days} dias!")
                break
            
            # Próxima página = antes do primeiro timestamp desta página
            last_end_time = first_ts
            
            # Rate limiting
            time.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Erro a buscar OI: {e}")
            time.sleep(2)
            break
    
    logger.info(f"✅ OI descarregado: {total_saved} registos guardados")
    return total_saved


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Defaults
    symbol = 'BTC/USDT:USDT'
    interval = '15m'
    days = 90
    
    if len(sys.argv) > 1:
        interval = sys.argv[1]
    if len(sys.argv) > 2:
        days = int(sys.argv[2])
    
    download_oi_history(symbol, interval, days)
