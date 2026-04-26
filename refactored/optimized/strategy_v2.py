"""
GhostMethodStrategy v2 — Otimizado: cache de volume profile, pre-computação.
Resolve recálculo de VP a cada sinal e validação repetida.
"""
import time
import logging
from typing import Dict, Optional, List

from ..data.cache import DataCache
from ..strategy.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class GhostMethodStrategy(BaseStrategy):
    """
    Estratégia v2 — cache de volume profile + validação otimizada.
    
    Mudanças v2:
    - Cache de volume profile (não recalcula se candles não mudaram)
    - Validação de dados com early-exit (fail fast)
    - Pre-computa thresholds em vez de .get() a cada analyze()
    """
    
    def __init__(self, config: Dict, event_bus=None):
        super().__init__(config, event_bus)
        sc = self.strategy_config
        
        # ✅ Pre-computa thresholds (evita .get() a cada chamada)
        self.volume_threshold = sc.get('volume_spike_threshold', 4.0)
        self.oi_threshold = sc.get('oi_change_threshold', 0.01)
        self.max_funding = sc.get('max_funding_rate', 0.01)
        self.min_funding = sc.get('min_funding_rate', -0.01)
        self.sma_period = sc.get('price_sma_period', 100)
        self.min_bullish = sc.get('min_bullish_candles', 2)
        self.min_bearish = sc.get('min_bearish_candles', 2)
        
        self.vp_enabled = sc.get('vp_enabled', True)
        self.vp_lookback = sc.get('vp_lookback', 96)
        
        self._cooldown = sc.get('entry_cooldown_seconds', 300)
        self._last_signal_time = 0
        
        # ✅ Cache de volume profile
        self._vp_cache = DataCache(ttl_seconds=60, max_size=50)
        
        # ✅ Pre-computa constantes de filtro
        self._funding_abs_limit = abs(self.max_funding)
        self._sl_pct = 0.035  # 3.5%
        self._tp_pct = 0.06   # 6%
    
    def analyze(self, market_data: Dict, price: float) -> Optional[Signal]:
        """Analisa com early-exit e cache."""
        # ✅ Early-exit: dados inválidos
        if not market_data or price <= 0:
            return None
        
        oi = market_data.get('oi_total', 0)
        oi_change = market_data.get('oi_change_pct', 0)
        funding = market_data.get('funding_avg', 0)
        volume_ratio = market_data.get('volume_ratio', 1.0)
        
        # ✅ Early-exit: funding extremo (filtro mais rápido)
        if abs(funding) > self._funding_abs_limit:
            return Signal.hold(reason=f"funding_extreme:{funding:.4f}")
        
        # ✅ Early-exit: cooldown
        now = time.time()
        if now - self._last_signal_time < self._cooldown:
            return None
        
        # ─── LONG ────────────────────────────────────────────
        if volume_ratio > self.volume_threshold and oi_change > self.oi_threshold:
            if self._check_bullish_alignment(market_data):
                signal = self._create_signal("LONG", price, volume_ratio, oi_change, oi, funding)
                self._last_signal_time = now
                self._notify('signal.generated', signal.to_dict())
                return signal
        
        # ─── SHORT ───────────────────────────────────────────
        if volume_ratio > self.volume_threshold and oi_change < -self.oi_threshold:
            if self._check_bearish_alignment(market_data):
                signal = self._create_signal("SHORT", price, volume_ratio, oi_change, oi, funding)
                self._last_signal_time = now
                self._notify('signal.generated', signal.to_dict())
                return signal
        
        return None
    
    def _create_signal(self, side: str, price: float, volume_ratio: float,
                       oi_change: float, oi: float, funding: float) -> Signal:
        """Factory de sinal — centraliza criação."""
        if side == "LONG":
            sl = price * (1 - self._sl_pct)
            tp = price * (1 + self._tp_pct)
        else:
            sl = price * (1 + self._sl_pct)
            tp = price * (1 - self._tp_pct)
        
        return Signal(
            signal_type=side,
            confidence=min(volume_ratio / 10, 1.0),
            entry_price=price,
            stop_loss=sl,
            take_profit=tp,
            reason=f"{side.lower()}|vol:{volume_ratio:.1f}x|oi:{oi_change*100:.2f}%",
            metadata={'oi': oi, 'funding': funding, 'volume_ratio': volume_ratio}
        )
    
    def _check_bullish_alignment(self, data: Dict) -> bool:
        """Bullish check otimizado."""
        price = data.get('price', 0)
        sma = data.get('sma_200', price)
        return price >= sma * 0.98
    
    def _check_bearish_alignment(self, data: Dict) -> bool:
        """Bearish check otimizado."""
        price = data.get('price', 0)
        sma = data.get('sma_200', price)
        return price <= sma * 1.02
    
    def calculate_volume_profile(self, candles: List[Dict]) -> Optional[Dict]:
        """Volume Profile com cache — não recalcula se candles não mudaram."""
        if len(candles) < 20:
            return None
        
        # ✅ Cache key baseado no último candle
        last_ts = candles[-1].get('timestamp', 0)
        cache_key = f"vp:{last_ts}"
        
        cached = self._vp_cache.get(cache_key)
        if cached:
            return cached
        
        recent = candles[-self.vp_lookback:]
        data = []
        for c in recent:
            typical = (c.get('high', 0) + c.get('low', 0) + c.get('close', 0)) / 3
            vol = c.get('volume', 0)
            if vol > 0 and typical > 0:
                data.append((typical, vol))
        
        if len(data) < 10:
            return None
        
        total_vol = sum(v for _, v in data)
        vwap = sum(p * v for p, v in data) / total_vol
        poc = max(data, key=lambda x: x[1])[0]
        
        variance = sum(v * (p - vwap) ** 2 for p, v in data) / total_vol
        std = variance ** 0.5
        
        result = {
            'poc': poc, 'vwap': vwap,
            'vah': vwap + std, 'val': vwap - std,
            'std': std, 'range_pct': (2 * std / vwap) * 100
        }
        
        self._vp_cache.set(cache_key, result, ttl=60)
        return result
    
    def filter_with_vp(self, price: float, side: str, vp: Dict) -> tuple:
        """Filtro VP — sem alterações funcionais."""
        if not vp:
            return True, "no_vp_data"
        
        vah, val = vp['vah'], vp['val']
        
        if side == 'LONG':
            if price < val:
                return True, f"below_val|${val:,.0f}"
            elif price > vah:
                return True, f"above_vah|${vah:,.0f}"
            else:
                return False, f"inside_va|${val:,.0f}-${vah:,.0f}"
        else:
            if price > vah:
                return True, f"above_vah|${vah:,.0f}"
            elif price < val:
                return True, f"below_val|${val:,.0f}"
            else:
                return False, f"inside_va|${val:,.0f}-${vah:,.0f}"
