"""
HyperliquidClient — Cliente API robusto com retries, circuit breaker e rate limiting.
Versão refatorada: resolve problemas de fetch inconsistente do legado.
"""
import time
import json
import logging
from typing import Dict, Optional, Any
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


@dataclass
class MarketData:
    """Dados de mercado normalizados."""
    asset: str
    mark_price: float = 0.0
    oracle_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    volume_24h: float = 0.0
    oi_usd: float = 0.0
    funding_rate: float = 0.0
    timestamp: float = 0.0
    source: str = "hyperliquid"
    raw: Dict[str, Any] = None


class CircuitBreaker:
    """Circuit breaker simples: após N falhas, pára temporariamente."""
    
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.last_failure_time = 0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    
    def can_execute(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "HALF_OPEN"
                logger.info("[CircuitBreaker] HALF_OPEN — testando recuperação")
                return True
            return False
        return True  # HALF_OPEN
    
    def record_success(self):
        self.failures = 0
        self.state = "CLOSED"
    
    def record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning(f"[CircuitBreaker] OPEN após {self.failures} falhas")


class HyperliquidClient:
    """
    Cliente para API da Hyperliquid.
    
    Features:
    - Rate limiting automático
    - Retries exponenciais com backoff
    - Circuit breaker para falhas em cascata
    - Normalização de dados (resolve problemas de valores errados do legado)
    """
    
    BASE_URL = "https://api.hyperliquid.xyz"
    
    # Constantes de validação
    MIN_ORDER_SIZE = 10.0
    MAX_ORDER_SIZE = 100_000.0
    MAX_SLIPPAGE = 0.005
    MAX_PRICE_DEVIATION = 0.02
    
    def __init__(self, config: Dict, paper_trading: bool = True):
        self.config = config
        self.paper_trading = paper_trading
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'HyperliquidBot/2.0'
        })
        
        # Rate limiting
        self._last_request_time = 0
        self._min_interval = config.get('api', {}).get('rate_limit_interval', 0.5)
        
        # Circuit breaker
        self._circuit = CircuitBreaker()
        
        # Cache de meta info (não muda frequentemente)
        self._meta_cache = None
        self._meta_cache_time = 0
        self._meta_cache_ttl = 300  # 5 minutos
        
        logger.info(f"[HyperliquidClient] Iniciado | Paper: {paper_trading}")
    
    def _rate_limit(self):
        """Garante intervalo mínimo entre requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()
    
    def _post(self, endpoint: str, payload: Dict, max_retries: int = 3) -> Optional[Dict]:
        """POST com retries e circuit breaker."""
        if not self._circuit.can_execute():
            logger.warning("[HyperliquidClient] Circuit breaker OPEN — skipping request")
            return None
        
        url = f"{self.BASE_URL}{endpoint}"
        
        for attempt in range(max_retries):
            try:
                self._rate_limit()
                response = self.session.post(url, json=payload, timeout=10)
                response.raise_for_status()
                
                data = response.json()
                self._circuit.record_success()
                return data
                
            except requests.exceptions.Timeout:
                logger.warning(f"[HyperliquidClient] Timeout (tentativa {attempt + 1}/{max_retries})")
                time.sleep(2 ** attempt)  # Backoff exponencial
            except requests.exceptions.HTTPError as e:
                logger.error(f"[HyperliquidClient] HTTP {e.response.status_code}: {e}")
                self._circuit.record_failure()
                return None
            except Exception as e:
                logger.error(f"[HyperliquidClient] Erro: {e}")
                self._circuit.record_failure()
                time.sleep(2 ** attempt)
        
        logger.error(f"[HyperliquidClient] Falhou após {max_retries} tentativas")
        return None
    
    def get_meta(self) -> Optional[Dict]:
        """Busca meta info (lista de assets, etc). Com cache."""
        now = time.time()
        if self._meta_cache and (now - self._meta_cache_time) < self._meta_cache_ttl:
            return self._meta_cache
        
        data = self._post("/info", {"type": "meta"})
        if data:
            self._meta_cache = data
            self._meta_cache_time = now
        return data
    
    def get_all_mids(self) -> Optional[Dict[str, float]]:
        """Busca preços medianos de todos os assets."""
        data = self._post("/info", {"type": "allMids"})
        if data:
            return data
        return None
    
    def get_asset_ctx(self, asset: str) -> Optional[MarketData]:
        """
        Busca contexto de mercado para um asset.
        Resolve o problema do legado de valores errados de BTC.
        """
        # 1. Buscar meta para mapear nome do asset
        meta = self.get_meta()
        if not meta or 'universe' not in meta:
            logger.error("[HyperliquidClient] Meta não disponível")
            return None
        
        # Encontrar índice do asset
        asset_idx = None
        for i, asset_info in enumerate(meta.get('universe', [])):
            if asset_info.get('name') == asset:
                asset_idx = i
                break
        
        if asset_idx is None:
            logger.error(f"[HyperliquidClient] Asset '{asset}' não encontrado na meta")
            return None
        
        # 2. Buscar metaAndAssetCtxs
        ctx_data = self._post("/info", {"type": "metaAndAssetCtxs"})
        if not ctx_data or len(ctx_data) < 2:
            logger.error("[HyperliquidClient] metaAndAssetCtxs falhou")
            return None
        
        try:
            meta_resp, ctxs = ctx_data
            if asset_idx >= len(ctxs):
                logger.error(f"[HyperliquidClient] Índice {asset_idx} fora de alcance")
                return None
            
            ctx = ctxs[asset_idx]
            
            # 3. Extrair e VALIDAR preço
            mark_price = float(ctx.get('markPx', 0))
            oracle_price = float(ctx.get('oraclePx', 0))
            
            # Validação: preço deve ser razoável para BTC
            if asset == "BTC":
                if not (10_000 <= mark_price <= 200_000):
                    logger.error(f"[HyperliquidClient] Preço BTC inválido: {mark_price}")
                    return None
            elif asset == "ETH":
                if not (500 <= mark_price <= 20_000):
                    logger.error(f"[HyperliquidClient] Preço ETH inválido: {mark_price}")
                    return None
            
            # 4. Construir objeto normalizado
            md = MarketData(
                asset=asset,
                mark_price=mark_price,
                oracle_price=oracle_price,
                bid=float(ctx.get('bid', 0)),
                ask=float(ctx.get('ask', 0)),
                volume_24h=float(ctx.get('dayNtlVlm', 0)),
                oi_usd=float(ctx.get('oi', 0)) * mark_price if ctx.get('oi') else 0,
                funding_rate=float(ctx.get('funding', 0)),
                timestamp=time.time(),
                raw=ctx
            )
            
            logger.debug(f"[HyperliquidClient] {asset} | Mark: ${mark_price:,.2f} | OI: ${md.oi_usd:,.0f}")
            return md
            
        except (KeyError, IndexError, ValueError) as e:
            logger.error(f"[HyperliquidClient] Erro a parsear dados: {e}")
            return None
    
    def get_candles(self, asset: str, interval: str = "15m", limit: int = 100) -> list:
        """Busca candles históricos."""
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": asset,
                "interval": interval,
                "startTime": int((time.time() - limit * self._interval_to_seconds(interval)) * 1000),
                "endTime": int(time.time() * 1000)
            }
        }
        
        data = self._post("/info", payload)
        if data and isinstance(data, list):
            return [{
                'timestamp': c[0],
                'open': float(c[1]),
                'high': float(c[2]),
                'low': float(c[3]),
                'close': float(c[4]),
                'volume': float(c[5])
            } for c in data]
        return []
    
    def _interval_to_seconds(self, interval: str) -> int:
        mapping = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        return mapping.get(interval, 900)
    
    def validate_order(self, asset: str, side: str, size: float, 
                       price: float = None, market_price: float = None) -> tuple:
        """Validação completa de ordem. Retorna (is_valid, reason)."""
        if size < self.MIN_ORDER_SIZE:
            return False, f"Mínimo ${self.MIN_ORDER_SIZE} (recebido ${size:.2f})"
        if size > self.MAX_ORDER_SIZE:
            return False, f"Máximo ${self.MAX_ORDER_SIZE} (recebido ${size:.2f})"
        if side not in ('BUY', 'SELL'):
            return False, f"Side inválido: {side}"
        if market_price and price:
            deviation = abs(price - market_price) / market_price
            if deviation > self.MAX_PRICE_DEVIATION:
                return False, f"Desvio excessivo: {deviation*100:.2f}%"
        return True, "OK"
    
    def close_position(self, asset: str) -> Dict:
        """Fecha posição (paper ou real)."""
        if self.paper_trading:
            logger.info(f"[PAPER] Close position {asset}")
            return {'status': 'PAPER_CLOSED', 'asset': asset}
        raise NotImplementedError("Real trading requires wallet")
    
    def place_order(self, asset: str, side: str, size: float, 
                    price: float = None, market_price: float = None) -> Dict:
        """Coloca ordem (paper ou real)."""
        is_valid, reason = self.validate_order(asset, side, size, price, market_price)
        if not is_valid:
            logger.error(f"[HyperliquidClient] Ordem rejeitada: {reason}")
            return {'status': 'REJECTED', 'reason': reason}
        
        if self.paper_trading:
            logger.info(f"[PAPER] {side} {asset} | ${size:.2f} | @ ${price or market_price:.2f}")
            return {
                'status': 'PAPER_FILLED',
                'asset': asset, 'side': side, 'size': size,
                'price': price or market_price or 0,
                'order_id': f'paper_{int(time.time()*1000)}'
            }
        
        # TODO: Implementar ordens reais com assinatura
        raise NotImplementedError("Trading real requer wallet + assinatura")
