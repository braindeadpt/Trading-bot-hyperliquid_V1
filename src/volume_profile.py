"""
Volume Profile simples — POC + Value Area para filtro de qualidade de entradas.

Usa candles OHLCV existentes (não precisa de tick data).
Conceptos:
- POC: Preço com maior volume (proxy: typical price de cada candle)
- VAH/VAL: ±1 std dev do VWAP (~68% do volume)
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class VolumeProfile:
    """Calcula Volume Profile a partir de candles OHLCV."""

    def __init__(self, lookback: int = 96):
        """
        lookback: número de candles para analisar (96 de 15m = 24h)
        """
        self.lookback = lookback

    def calculate(self, candles: List[Dict]) -> Optional[Dict]:
        """
        Calcula POC, VWAP, VAH, VAL a partir de candles.

        Candles esperado: list of dicts com 'high', 'low', 'close', 'volume'
        Retorna dict ou None se dados insuficientes.
        """
        if len(candles) < 20:
            return None

        # Usar últimos N candles
        recent = candles[-self.lookback:]

        # Preço típico de cada candle (proxy do "preço onde negociou")
        data = []
        for c in recent:
            typical = (c.get('high', 0) + c.get('low', 0) + c.get('close', 0)) / 3
            vol = c.get('volume', 0)
            if vol > 0 and typical > 0:
                data.append((typical, vol))

        if len(data) < max(10, self.lookback // 4):
            return None

        # Volume total
        total_vol = sum(v for _, v in data)
        if total_vol == 0:
            return None

        # VWAP (Volume Weighted Average Price)
        vwap = sum(p * v for p, v in data) / total_vol

        # POC: preço com maior volume individual
        # (aproximação: candle com maior volume)
        poc = max(data, key=lambda x: x[1])[0]

        # Std dev ponderado por volume
        variance = sum(v * (p - vwap) ** 2 for p, v in data) / total_vol
        std = variance ** 0.5

        # Value Area: ±1 std dev do VWAP
        # Em distribuição normal, ~68% dos dados caem dentro de ±1σ
        vah = vwap + std
        val = vwap - std

        # Range percentual (quão larga é a VA)
        range_pct = ((vah - val) / vwap) * 100 if vwap > 0 else 0

        return {
            'poc': poc,
            'vwap': vwap,
            'vah': vah,
            'val': val,
            'std': std,
            'range_pct': range_pct,
            'lookback': len(data),
        }

    def filter_entry(self, price: float, side: str, profile: Optional[Dict]) -> tuple:
        """
        Filtra se uma entrada é válida segundo o Volume Profile.

        Retorna: (allowed: bool, reason: str)
        - allowed=True: entrada aprovada
        - allowed=False: entrada bloqueada, reason explica porquê
        """
        if not profile:
            return True, "No VP data"

        vah = profile['vah']
        val = profile['val']
        poc = profile['poc']
        range_pct = profile['range_pct']

        # Se VA é muito estreita (< 0.5%), o mercado está em equilíbrio
        # → evitar entradas (whipsaw provável)
        if range_pct < 0.5:
            return False, f"VA too tight ({range_pct:.2f}%) — balance market"

        if side == 'long':
            # LONG ideal:
            # 1. Preço < VAL → "barato", acima do valor justo
            # 2. Preço > VAH → breakout para cima (momentum)
            # Evitar: VAL < preço < VAH → "meio da VA", sem edge claro

            if price < val:
                return True, f"Below VAL (${val:,.0f}) — discount zone"
            elif price > vah:
                return True, f"Above VAH (${vah:,.0f}) — breakout"
            else:
                return False, f"Inside VA (${val:,.0f}-${vah:,.0f}) — no edge"

        else:  # short
            # SHORT ideal:
            # 1. Preço > VAH → "caro", acima do valor justo
            # 2. Preço < VAL → breakdown para baixo
            # Evitar: VAL < preço < VAH → "meio da VA"

            if price > vah:
                return True, f"Above VAH (${vah:,.0f}) — premium zone"
            elif price < val:
                return True, f"Below VAL (${val:,.0f}) — breakdown"
            else:
                return False, f"Inside VA (${val:,.0f}-${vah:,.0f}) — no edge"

    def format_profile(self, profile: Optional[Dict]) -> str:
        """Formata para log."""
        if not profile:
            return "VP: N/A"
        return (f"VP: POC=${profile['poc']:,.0f} VA=[${profile['val']:,.0f}-${profile['vah']:,.0f}] "
                f"({profile['range_pct']:.2f}%) VWAP=${profile['vwap']:,.0f}")
