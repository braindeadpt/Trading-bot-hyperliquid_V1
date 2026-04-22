"""
Data Downloader - Descarrega dados históricos da Binance para backtest
Binance API pública: gratuito, não precisa de autenticação
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
    
    def download_candles(self, symbol: str, interval: str = "5m", 
                         days_back: int = 30, end_time: Optional[datetime] = None) -> str:
        """
        Descarrega candles históricos da Binance Futures
        
        Args:
            symbol: BTCUSDT, ETHUSDT, etc.
            interval: 1m, 5m, 15m, 1h, 4h, 1d
            days_back: Quantos dias de histórico
            end_time: Data final (default: agora)
        
        Returns:
            Path do ficheiro CSV guardado
        """
        if end_time is None:
            end_time = datetime.now()
        
        start_time = end_time - timedelta(days=days_back)
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        
        filename = self.data_dir / f"{symbol.lower()}_{interval}.csv"
        
        logger.info(f"A descarregar candles {symbol} {interval} ({days_back} dias)...")
        
        all_candles = []
        current_start = start_ms
        
        # Binance limita a 1500 candles por request
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
                
                # Último candle timestamp + 1 intervalo
                current_start = candles[-1][0] + 1
                
                # Rate limit: não exceder 1200 requests/min
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
        
        logger.info(f"Candles guardados: {filename} ({len(all_candles)} candles)")
        return str(filename)
    
    def download_open_interest_history(self, symbol: str, 
                                       interval: str = "5m",
                                       days_back: int = 30) -> str:
        """
        Descarrega histórico de Open Interest da Binance
        
        Args:
            symbol: BTCUSDT, ETHUSDT
            interval: 5m, 15m, 1h, 4h, 1d
            days_back: Dias de histórico
        
        Returns:
            Path do ficheiro CSV
        """
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days_back)
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        
        filename = self.data_dir / f"{symbol.lower()}_oi_{interval}.csv"
        
        logger.info(f"A descarregar OI history {symbol} {interval} ({days_back} dias)...")
        
        all_data = []
        current_start = start_ms
        
        while current_start < end_ms:
            try:
                resp = requests.get(
                    f"{self.base_url}/fapi/v1/openInterestHist",
                    params={
                        "symbol": symbol,
                        "period": interval,
                        "startTime": current_start,
                        "limit": 500
                    },
                    timeout=30
                )
                resp.raise_for_status()
                data = resp.json()
                
                if not data:
                    break
                
                all_data.extend(data)
                current_start = int(data[-1]['timestamp']) + 1
                time.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Erro a descarregar OI: {e}")
                break
        
        with open(filename, 'w', newline='') as f:
            if all_data:
                writer = csv.DictWriter(f, fieldnames=all_data[0].keys())
                writer.writeheader()
                writer.writerows(all_data)
        
        logger.info(f"OI guardado: {filename} ({len(all_data)} registos)")
        return str(filename)
    
    def download_funding_rate_history(self, symbol: str, days_back: int = 30) -> str:
        """
        Descarrega histórico de Funding Rate da Binance
        
        Args:
            symbol: BTCUSDT, ETHUSDT
            days_back: Dias de histórico
        
        Returns:
            Path do ficheiro CSV
        """
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
        
        logger.info(f"Funding guardado: {filename} ({len(all_data)} registos)")
        return str(filename)
    
    def download_all(self, symbol: str = "BTCUSDT", interval: str = "5m", 
                     days_back: int = 30) -> List[str]:
        """
        Descarrega tudo (candles + OI + funding) para um symbol
        
        Returns:
            Lista de paths dos ficheiros descarregados
        """
        files = []
        
        files.append(self.download_candles(symbol, interval, days_back))
        files.append(self.download_open_interest_history(symbol, interval, days_back))
        files.append(self.download_funding_rate_history(symbol, days_back))
        
        return files


if __name__ == "__main__":
    import sys
    
    # CLI simples
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    
    logging.basicConfig(level=logging.INFO)
    
    dl = DataDownloader()
    dl.download_all(symbol, days_back=days)
    
    print(f"\nDados descarregados para {symbol} ({days} dias)")
    print(f"Diretório: {dl.data_dir.absolute()}")
