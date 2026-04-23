"""
Data Downloader v2 - Descarrega dados da Binance e guarda em CSV + SQLite
"""
import requests
import csv
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)


class DataDownloader:
    """Descarrega candles, OI e funding rate históricos da Binance"""
    
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = "https://fapi.binance.com"
        
        # Importar database (lazy para evitar circular imports)
        self._db = None
    
    @property
    def db(self):
        if self._db is None:
            from database import BotDatabase
            self._db = BotDatabase()
        return self._db
    
    def download_candles(self, symbol: str, interval: str = "15m", 
                         days_back: int = 30, end_time: Optional[datetime] = None,
                         save_to_db: bool = True) -> str:
        """Descarrega candles e guarda em CSV + SQLite"""
        if end_time is None:
            end_time = datetime.now()
        
        start_time = end_time - timedelta(days=days_back)
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        
        filename = self.data_dir / f"{symbol.lower()}_{interval}.csv"
        
        logger.info(f"A descarregar candles {symbol} {interval} ({days_back} dias)...")
        
        all_candles = []
        current_start = start_ms
        
        while current_start < end_ms:
            try:
                resp = requests.get(
                    f"{self.base_url}/fapi/v1/klines",
                    params={
                        "symbol": symbol,
                        "interval": interval,
                        "startTime": current_start,
                        "limit": 1500
                    },
                    timeout=30
                )
                resp.raise_for_status()
                candles = resp.json()
                
                if not candles:
                    break
                
                all_candles.extend(candles)
                current_start = candles[-1][0] + 1
                time.sleep(0.05)
                
            except Exception as e:
                logger.error(f"Erro a descarregar candles: {e}")
                break
        
        # Guardar em CSV
        with open(filename, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'open', 'high', 'low', 'close', 'volume',
                           'close_time', 'quote_volume', 'trades', 'taker_buy_volume',
                           'taker_buy_quote_volume', 'ignore'])
            writer.writerows(all_candles)
        
        logger.info(f"Candles CSV: {filename} ({len(all_candles)} candles)")
        
        # Guardar em SQLite
        if save_to_db and all_candles:
            formatted = []
            for c in all_candles:
                formatted.append({
                    'timestamp': c[0],
                    'open': float(c[1]),
                    'high': float(c[2]),
                    'low': float(c[3]),
                    'close': float(c[4]),
                    'volume': float(c[5])
                })
            
            asset = symbol.replace('USDT', '')
            self.db.save_candles(asset, interval, formatted)
            logger.info(f"Candles SQLite: {len(formatted)} registos")
        
        return str(filename)
    
    def download_open_interest_history(self, symbol: str, 
                                       interval: str = "15m",
                                       days_back: int = 30,
                                       save_to_db: bool = True) -> str:
        """Descarrega OI history - usa OI atual como fallback se histórico falhar"""
        # OI só suporta: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d
        valid_oi_intervals = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']
        oi_interval = interval if interval in valid_oi_intervals else '1h'
        
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days_back)
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        
        filename = self.data_dir / f"{symbol.lower()}_oi_{interval}.csv"
        
        logger.info(f"A descarregar OI history {symbol} {interval} ({days_back} dias)...")
        
        all_data = []
        current_start = start_ms
        
        # Tentar endpoint histórico primeiro
        history_works = False
        while current_start < end_ms:
            try:
                resp = requests.get(
                    f"{self.base_url}/fapi/v1/openInterestHist",
                    params={
                        "symbol": symbol,
                        "period": oi_interval,
                        "startTime": current_start,
                        "limit": 500
                    },
                    timeout=30
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data:
                        history_works = True
                        all_data.extend(data)
                        current_start = int(data[-1]['timestamp']) + 1
                        time.sleep(0.1)
                    else:
                        break
                else:
                    logger.warning(f"OI history API retornou {resp.status_code} - usando fallback")
                    break
                
            except Exception as e:
                logger.warning(f"OI history indisponível: {e}")
                break
        
        # Fallback: se histórico falhou, usar OI atual como proxy
        if not history_works or not all_data:
            logger.info("A usar OI atual como fallback...")
            try:
                # Buscar OI atual + mark price
                oi_resp = requests.get(
                    f"{self.base_url}/fapi/v1/openInterest",
                    params={"symbol": symbol},
                    timeout=10
                )
                price_resp = requests.get(
                    f"{self.base_url}/fapi/v1/premiumIndex",
                    params={"symbol": symbol},
                    timeout=10
                )
                
                if oi_resp.status_code == 200 and price_resp.status_code == 200:
                    oi_data = oi_resp.json()
                    price_data = price_resp.json()
                    
                    oi_contracts = float(oi_data.get('openInterest', 0))
                    mark_price = float(price_data.get('markPrice', 0))
                    oi_usd = oi_contracts * mark_price
                    
                    # Criar entrada única com timestamp atual
                    all_data = [{
                        'symbol': symbol,
                        'timestamp': str(int(time.time() * 1000)),
                        'sumOpenInterest': str(oi_contracts),
                        'sumOpenInterestValue': str(oi_usd)
                    }]
                    
                    logger.info(f"OI atual: ${oi_usd:,.0f} (fallback)")
                    
            except Exception as e2:
                logger.error(f"Fallback OI também falhou: {e2}")
        
        with open(filename, 'w', newline='') as f:
            if all_data:
                writer = csv.DictWriter(f, fieldnames=all_data[0].keys())
                writer.writeheader()
                writer.writerows(all_data)
        
        logger.info(f"OI CSV: {filename} ({len(all_data)} registos)")
        
        # Guardar em SQLite
        if save_to_db and all_data:
            asset = symbol.replace('USDT', '')
            for item in all_data:
                self.db.save_oi(
                    asset,
                    int(item['timestamp']),
                    float(item['sumOpenInterestValue']),
                    'binance'
                )
            logger.info(f"OI SQLite: {len(all_data)} registos")
        
        return str(filename)
    
    def download_funding_rate_history(self, symbol: str, days_back: int = 30,
                                     save_to_db: bool = True) -> str:
        """Descarrega funding rate history"""
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days_back)
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        
        filename = self.data_dir / f"{symbol.lower()}_funding.csv"
        
        logger.info(f"A descarregar funding rate {symbol} ({days_back} dias)...")
        
        all_data = []
        current_start = start_ms
        
        while current_start < end_ms:
            try:
                resp = requests.get(
                    f"{self.base_url}/fapi/v1/fundingRate",
                    params={
                        "symbol": symbol,
                        "startTime": current_start,
                        "limit": 1000
                    },
                    timeout=30
                )
                resp.raise_for_status()
                data = resp.json()
                
                if not data:
                    break
                
                all_data.extend(data)
                current_start = int(data[-1]['fundingTime']) + 1
                time.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Erro a descarregar funding: {e}")
                break
        
        with open(filename, 'w', newline='') as f:
            if all_data:
                writer = csv.DictWriter(f, fieldnames=all_data[0].keys())
                writer.writeheader()
                writer.writerows(all_data)
        
        logger.info(f"Funding CSV: {filename} ({len(all_data)} registos)")
        
        # Guardar em SQLite
        if save_to_db and all_data:
            asset = symbol.replace('USDT', '')
            for item in all_data:
                self.db.save_funding(
                    asset,
                    int(item['fundingTime']),
                    float(item['fundingRate']),
                    'binance'
                )
            logger.info(f"Funding SQLite: {len(all_data)} registos")
        
        return str(filename)
    
    def download_all(self, symbol: str = "BTCUSDT", interval: str = "15m", 
                     days_back: int = 30) -> List[str]:
        """Descarrega tudo para um symbol"""
        files = []
        
        files.append(self.download_candles(symbol, interval, days_back))
        files.append(self.download_open_interest_history(symbol, interval, days_back))
        files.append(self.download_funding_rate_history(symbol, days_back))
        
        return files


if __name__ == "__main__":
    import sys
    
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    interval = sys.argv[3] if len(sys.argv) > 3 else "15m"
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    dl = DataDownloader()
    dl.download_all(symbol, interval, days_back=days)
    
    print(f"\n{'='*60}")
    print(f"✅ Dados descarregados para {symbol} ({days} dias, {interval})")
    print(f"   CSV: {dl.data_dir}")
    print(f"   SQLite: {dl.db.db_path}")
    print(f"{'='*60}")
