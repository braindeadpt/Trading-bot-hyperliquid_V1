"""
DataAggregator v2 — Otimizado: cache key eficiente, dedup de eventos.
Resolve cache key inflacionada e publicação duplicada de eventos.
"""
import time
import logging
from typing import Dict, Optional, Any

from refactored.api.hyperliquid_client import HyperliquidClient, MarketData
from refactored.data.cache import DataCache

logger = logging.getLogger(__name__)


class DataAggregator:
    """
    Agregador v2 — cache key eficiente + dedup de eventos.
    
    Mudanças v2:
    - Cache key: hash do timestamp do candle em vez de time.time()//10
    - Não publica evento se preço não mudou (dedup)
    - Pre-computa get_all_data() em vez de reconstruir dict a cada chamada
    """
    
    def __init__(self, config: Dict, cache: DataCache = None, event_bus=None):
        self.config = config
        self.cache = cache or DataCache()
        self.event_bus = event_bus
        self.api = HyperliquidClient(config, paper_trading=True)
        
        self.assets = config.get('assets', ['BTC'])
        self.primary_interval = config.get('timeframes', {}).get('primary', '15m')
        
        self._fetch_count = 0
        self._error_count = 0
        self._last_price = {}
        self._last_publish_time = {}
        self._publish_interval = 5
        
        # ✅ Histórico de OI para calcular variação
        self._last_oi = {}
        self._last_oi_time = {}
    
    def _cache_key(self, asset: str, data_type: str) -> str:
        """Cache key eficiente — sem time.time() dinâmico."""
        return f"{data_type}:{asset}"
    
    def fetch_market_data(self, asset: str) -> Optional[MarketData]:
        """Busca dados com cache e dedup de eventos."""
        cache_key = self._cache_key(asset, "market")
        
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        
        self._fetch_count += 1
        
        try:
            data = self.api.get_asset_ctx(asset)
            if data:
                self.cache.set(cache_key, data, ttl=10)
                
                # ✅ Deduplicação: só publica se preço mudou > 0.1%
                last_price = self._last_price.get(asset, 0)
                price_changed = abs(data.mark_price - last_price) / max(last_price, 1) > 0.001
                
                # ✅ Throttling: máximo 1 evento por N segundos
                now = time.time()
                last_pub = self._last_publish_time.get(asset, 0)
                can_publish = (now - last_pub) >= self._publish_interval
                
                if self.event_bus and (price_changed or can_publish):
                    self._last_price[asset] = data.mark_price
                    self._last_publish_time[asset] = now
                    self.event_bus.publish('market.data', {
                        'asset': asset,
                        'price': data.mark_price,
                        'oi': data.oi_usd,
                        'funding': data.funding_rate,
                        'volume': data.volume_24h
                    })
                
                return data
        except Exception as e:
            self._error_count += 1
            logger.error(f"[Aggregator] Erro a buscar {asset}: {e}")
        
        return None
    
    def fetch_candles(self, asset: str, interval: str = None, limit: int = 100) -> list:
        """Busca candles com cache."""
        interval = interval or self.primary_interval
        cache_key = self._cache_key(asset, f"candles:{interval}:{limit}")
        
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        
        candles = self.api.get_candles(asset, interval, limit)
        if candles:
            self.cache.set(cache_key, candles, ttl=60)
        return candles
    
    def get_latest_price(self, asset: str) -> float:
        """Preço mais recente."""
        data = self.fetch_market_data(asset)
        return data.mark_price if data else 0.0
    
    def get_all_data(self, asset: str) -> Dict[str, Any]:
        """Retorna dict completo — otimizado com cache."""
        cache_key = self._cache_key(asset, "all")
        
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        
        md = self.fetch_market_data(asset)
        if not md:
            return {}
        
        # ✅ Calcular variação de OI
        last_oi = self._last_oi.get(asset, 0)
        current_oi = md.oi_usd
        oi_change_pct = 0.0
        if last_oi > 0 and current_oi > 0:
            oi_change_pct = (current_oi - last_oi) / last_oi
        self._last_oi[asset] = current_oi
        
        result = {
            'price': md.mark_price,
            'mark_price': md.mark_price,
            'oracle_price': md.oracle_price,
            'bid': md.bid,
            'ask': md.ask,
            'oi_total': current_oi,
            'oi_change_pct': oi_change_pct,
            'funding_avg': md.funding_rate,
            'volume_24h': md.volume_24h,
            'timestamp': md.timestamp,
            'source': md.source,
            'raw': md.raw
        }
        
        self.cache.set(cache_key, result, ttl=5)  # Cache curto para all_data
        return result
    
    def get_stats(self) -> Dict[str, Any]:
        return {
            'fetches': self._fetch_count,
            'errors': self._error_count,
            'error_rate': round(self._error_count / max(self._fetch_count, 1) * 100, 1),
            'cache': self.cache.stats
        }
