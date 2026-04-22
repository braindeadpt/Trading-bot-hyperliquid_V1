"""
Data Aggregator - Agrega OI, Volume e Funding Rate de múltiplas exchanges
"""
import requests
import time
from typing import Dict, Optional, List
import logging

logger = logging.getLogger(__name__)


class DataAggregator:
    """Busca e agrega dados de OI, volume e funding de várias exchanges"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.sources = config.get('data_sources', {})
        self.last_oi = {}  # cache do último OI por exchange
        self.last_update = 0
        
    def fetch_all_data(self, asset: str) -> Optional[Dict]:
        """
        Busca OI global agregado de todas as exchanges configuradas
        Retorna: dict com oi_total, oi_change_pct, volume_total, funding_avg
        """
        results = {
            'oi_total': 0,
            'oi_change_pct': 0,
            'volume_total': 0,
            'funding_avg': 0,
            'exchanges_data': {},
            'timestamp': time.time()
        }
        
        valid_sources = 0
        funding_values = []
        
        for source_name, source_config in self.sources.items():
            if not source_config.get('enabled', False):
                continue
                
            try:
                if source_name == 'binance':
                    data = self._fetch_binance(asset)
                elif source_name == 'bybit':
                    data = self._fetch_bybit(asset)
                elif source_name == 'okx':
                    data = self._fetch_okx(asset)
                elif source_name == 'hyperliquid':
                    data = self._fetch_hyperliquid(asset)
                else:
                    continue
                
                if data:
                    weight = source_config.get('weight', 0.25)
                    results['exchanges_data'][source_name] = data
                    results['oi_total'] += data.get('oi_usd', 0)
                    results['volume_total'] += data.get('volume_24h', 0)
                    if data.get('funding_rate') is not None:
                        funding_values.append(data['funding_rate'])
                    valid_sources += 1
                    
            except Exception as e:
                logger.warning(f"Erro a buscar dados de {source_name}: {e}")
                continue
        
        if valid_sources == 0:
            logger.error("Nenhuma exchange respondeu!")
            return None
            
        # Calcular funding médio
        if funding_values:
            results['funding_avg'] = sum(funding_values) / len(funding_values)
        
        # Calcular variação de OI (comparar com leitura anterior)
        if self.last_oi:
            oi_old = sum(self.last_oi.values())
            if oi_old > 0:
                results['oi_change_pct'] = (results['oi_total'] - oi_old) / oi_old
        
        # Guardar para próxima comparação
        self.last_oi = {k: v.get('oi_usd', 0) for k, v in results['exchanges_data'].items()}
        self.last_update = time.time()
        
        return results
    
    def _fetch_binance(self, asset: str) -> Optional[Dict]:
        """Busca OI e funding da Binance Futures"""
        base_url = self.sources['binance']['base_url']
        symbol = f"{asset}USDT"
        
        # Open Interest
        oi_resp = requests.get(
            f"{base_url}/fapi/v1/openInterest",
            params={"symbol": symbol},
            timeout=10
        )
        oi_resp.raise_for_status()
        oi_data = oi_resp.json()
        
        # Mark Price (para calcular OI em USD)
        price_resp = requests.get(
            f"{base_url}/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=10
        )
        price_data = price_resp.json()
        mark_price = float(price_data.get('markPrice', 0))
        
        oi_contracts = float(oi_data.get('openInterest', 0))
        oi_usd = oi_contracts * mark_price
        
        # Funding Rate
        funding_resp = requests.get(
            f"{base_url}/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 1},
            timeout=10
        )
        funding_data = funding_resp.json()
        funding_rate = float(funding_data[0].get('fundingRate', 0)) if funding_data else None
        
        # 24h Volume
        ticker_resp = requests.get(
            f"{base_url}/fapi/v1/ticker/24hr",
            params={"symbol": symbol},
            timeout=10
        )
        ticker_data = ticker_resp.json()
        volume = float(ticker_data.get('volume', 0)) * mark_price
        
        return {
            'oi_usd': oi_usd,
            'funding_rate': funding_rate,
            'volume_24h': volume,
            'mark_price': mark_price
        }
    
    def _fetch_bybit(self, asset: str) -> Optional[Dict]:
        """Busca OI e funding da Bybit"""
        base_url = self.sources['bybit']['base_url']
        symbol = f"{asset}USDT"
        
        # Open Interest
        oi_resp = requests.get(
            f"{base_url}/v5/market/recent-trade",
            params={"category": "linear", "symbol": symbol, "limit": 1},
            timeout=10
        )
        
        # Usar endpoint alternativo para OI
        oi_resp = requests.get(
            f"{base_url}/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
            timeout=10
        )
        oi_data = oi_resp.json()
        
        if oi_data.get('retCode') != 0:
            return None
            
        result = oi_data['result']['list'][0]
        oi_usd = float(result.get('openInterest', 0)) * float(result.get('lastPrice', 0))
        funding_rate = float(result.get('fundingRate', 0))
        volume = float(result.get('volume24h', 0))
        
        return {
            'oi_usd': oi_usd,
            'funding_rate': funding_rate,
            'volume_24h': volume,
            'mark_price': float(result.get('lastPrice', 0))
        }
    
    def _fetch_okx(self, asset: str) -> Optional[Dict]:
        """Busca OI e funding da OKX"""
        base_url = self.sources['okx']['base_url']
        symbol = f"{asset}-USDT-SWAP"
        
        # Open Interest
        oi_resp = requests.get(
            f"{base_url}/api/v5/public/open-interest",
            params={"instType": "SWAP", "instId": symbol},
            timeout=10
        )
        oi_data = oi_resp.json()
        
        if oi_data.get('code') != '0':
            return None
            
        oi_contracts = float(oi_data['data'][0].get('oi', 0))
        
        # Mark Price
        price_resp = requests.get(
            f"{base_url}/api/v5/public/mark-price",
            params={"instType": "SWAP", "instId": symbol},
            timeout=10
        )
        price_data = price_resp.json()
        mark_price = float(price_data['data'][0].get('markPx', 0))
        
        oi_usd = oi_contracts * mark_price
        
        # Funding Rate
        funding_resp = requests.get(
            f"{base_url}/api/v5/public/funding-rate",
            params={"instId": symbol},
            timeout=10
        )
        funding_data = funding_resp.json()
        funding_rate = float(funding_data['data'][0].get('fundingRate', 0)) if funding_data.get('data') else None
        
        # Volume
        ticker_resp = requests.get(
            f"{base_url}/api/v5/market/tickers",
            params={"instType": "SWAP", "instId": symbol},
            timeout=10
        )
        ticker_data = ticker_resp.json()
        volume = float(ticker_data['data'][0].get('volCcy24h', 0)) if ticker_data.get('data') else 0
        
        return {
            'oi_usd': oi_usd,
            'funding_rate': funding_rate,
            'volume_24h': volume,
            'mark_price': mark_price
        }
    
    def _fetch_hyperliquid(self, asset: str) -> Optional[Dict]:
        """Busca OI e funding da Hyperliquid"""
        base_url = self.sources['hyperliquid']['base_url']
        
        # Meta (inclui OI)
        meta_resp = requests.post(
            f"{base_url}/info",
            json={"type": "meta"},
            timeout=10
        )
        meta_data = meta_resp.json()
        
        # Encontrar o asset na lista
        universe = meta_data.get('universe', [])
        asset_idx = None
        for i, a in enumerate(universe):
            if a.get('name') == asset:
                asset_idx = i
                break
        
        if asset_idx is None:
            return None
        
        # Funding Rate
        funding_resp = requests.post(
            f"{base_url}/info",
            json={"type": "fundingRates"},
            timeout=10
        )
        funding_data = funding_resp.json()
        
        # Procurar funding do asset específico
        funding_rate = None
        for fr in funding_data:
            if fr.get('coin') == asset:
                funding_rate = float(fr.get('fundingRate', 0))
                break
        
        # Market Data para volume e preço
        mkt_resp = requests.post(
            f"{base_url}/info",
            json={"type": "allMids"},
            timeout=10
        )
        mkt_data = mkt_resp.json()
        mark_price = float(mkt_data.get(asset, 0))
        
        # OI da Hyperliquid (precisa de endpoint específico)
        # Por simplicidade, estimamos a partir dos dados disponíveis
        oi_resp = requests.post(
            f"{base_url}/info",
            json={"type": "openInterest"},
            timeout=10
        )
        
        # TODO: Hyperliquid OI endpoint precisa de ajuste
        # Por agora, retornamos com placeholder
        oi_usd = 0  # Será implementado com endpoint correto
        
        return {
            'oi_usd': oi_usd,
            'funding_rate': funding_rate,
            'volume_24h': 0,  # TODO: implementar
            'mark_price': mark_price
        }
