"""
Hyperliquid API Gateway — implementação das portas MarketDataProvider e ExchangeGateway.
Adapta a API externa (Hyperliquid) para o domínio.
"""
import time
import logging
from typing import List, Optional, Dict, Any
import requests

from ...domain.entities import MarketSnapshot
from ...domain.services import MarketDataProvider, ExchangeGateway

logger = logging.getLogger(__name__)


class HyperliquidAPIGateway(MarketDataProvider, ExchangeGateway):
    """
    Gateway para Hyperliquid.
    Implementa DUAS portas: MarketDataProvider + ExchangeGateway.
    """
    
    BASE_URL = "https://api.hyperliquid.xyz"
    
    def __init__(self, config: Dict[str, Any] = None, paper_trading: bool = True):
        self.config = config or {}
        self.paper_trading = paper_trading
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'HyperliquidBot/CleanArch/1.0'
        })
        self._last_request_time = 0
        self._min_interval = 0.5
    
    def _rate_limit(self):
        """Rate limiting básico."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()
    
    def _post(self, endpoint: str, payload: dict) -> Optional[dict]:
        """POST com retries e rate limit."""
        self._rate_limit()
        try:
            resp = self.session.post(
                f"{self.BASE_URL}{endpoint}",
                json=payload,
                timeout=10
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"[HyperliquidGateway] Erro {endpoint}: {e}")
            return None
    
    # ─── MarketDataProvider ──────────────────────────────────
    
    def get_snapshot(self, asset: str) -> Optional[MarketSnapshot]:
        """Busca snapshot de mercado para um asset."""
        payload = {"type": "allMids"}
        data = self._post("/info", payload)
        if not data or not isinstance(data, dict):
            return None
        
        # Parse preço
        all_mids = data.get("allMids", {})
        price_str = all_mids.get(asset)
        if not price_str:
            # Fallback: tentar metaAndAssetCtxs
            payload2 = {"type": "metaAndAssetCtxs"}
            data2 = self._post("/info", payload2)
            if data2 and isinstance(data2, list) and len(data2) >= 2:
                assets = data2[0].get("universe", [])
                ctxs = data2[1]
                for i, a in enumerate(assets):
                    if a.get("name") == asset:
                        ctx = ctxs[i] if i < len(ctxs) else {}
                        price_str = ctx.get("markPx", ctx.get("oraclePx", "0"))
                        oi = float(ctx.get("openInterest", 0))
                        funding = float(ctx.get("funding", 0))
                        volume = float(ctx.get("dayNtlVlm", 0))
                        mark = float(ctx.get("markPx", 0))
                        oracle = float(ctx.get("oraclePx", 0))
                        
                        return MarketSnapshot(
                            asset=asset,
                            price=float(price_str) if price_str else 0.0,
                            mark_price=mark,
                            oracle_price=oracle,
                            bid=0.0,  # Hyperliquid não expõe bid/ask no endpoint simples
                            ask=0.0,
                            volume_24h=volume,
                            oi_usd=oi * mark if oi > 0 else 0,
                            oi_change_pct=0.0,  # Requer histórico
                            funding_rate=funding,
                            funding_avg=funding,
                            volume_ratio=1.0,
                            timestamp=int(time.time()),
                            source="hyperliquid"
                        )
            return None
        
        return MarketSnapshot(
            asset=asset,
            price=float(price_str),
            mark_price=float(price_str),
            timestamp=int(time.time()),
            source="hyperliquid"
        )
    
    def get_candles(self, asset: str, interval: str, limit: int) -> List[dict]:
        """Retorna candles — delega para Binance se necessário."""
        # Simplificado: retorna lista vazia (implementação completa pode usar Binance)
        return []
    
    # ─── ExchangeGateway ─────────────────────────────────────
    
    def place_order(self, asset: str, direction: str, size: float,
                    price: float = None, order_type: str = "market") -> dict:
        """Coloca ordem (paper trading = simula)."""
        if self.paper_trading:
            return {
                "status": "filled",
                "asset": asset,
                "direction": direction,
                "size": size,
                "price": price or 0,
                "paper": True
            }
        # TODO: implementação real com chaves API
        raise NotImplementedError("Trading real não implementado nesta versão")
    
    def get_balance(self) -> float:
        return 10000.0  # Simplificado
    
    def is_healthy(self) -> bool:
        try:
            data = self._post("/info", {"type": "allMids"})
            return data is not None
        except:
            return False
