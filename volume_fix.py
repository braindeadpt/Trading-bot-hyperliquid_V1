"""
volume_fix.py - Correção de Cálculo de Volume para o Bot Hyperliquid
===============================================================

PROBLEMA ORIGINAL:
- O bot usava: volume_24h / 288 = média teórica (falsa)
- Resultado: volume_ratio nunca passava de 1.5x mesmo em crashes
- Threshold de 4.4x era estatisticamente impossível de atingir

SOLUÇÃO:
- Média móvel dos últimos N candles reais (não teórica)
- Volume Delta (Buy Pressure vs Sell Pressure)
- Threshold realista: 2.0x (ajustável)
- OI: desbloqueado (informativo, não filtro)

Integração: Substituir a função _calculate_volume_metrics() no data_aggregator.py
"""

import time
import logging
from collections import deque
from typing import Dict, Optional, Tuple

logger = logging.getLogger('volume_fix')

class VolumeMetricsFix:
    """
    Calculadora de volume com média móvel real e delta.
    
    Uso:
        vm = VolumeMetricsFix(ma_period=20, spike_threshold=2.0)
        
        # A cada candle novo:
        ratio, delta, is_spike = vm.update(candle_volume=1500000, 
                                           buy_volume=900000, 
                                           sell_volume=600000)
        
        if is_spike and delta < -0.3:
            print("SPIKE DE VENDA DETETADO!")
    """
    
    def __init__(self, ma_period: int = 20, spike_threshold: float = 2.0,
                 delta_threshold: float = 0.3, min_samples: int = 5):
        """
        Args:
            ma_period: Quantos candles para média móvel (default: 20 = 5h em 15m)
            spike_threshold: Ratio para considerar spike (default: 2.0x)
            delta_threshold: Mínimo |delta| para considerar direcional (default: 30%)
            min_samples: Mínimo de candles antes de calcular ratio (default: 5)
        """
        self.ma_period = ma_period
        self.spike_threshold = spike_threshold
        self.delta_threshold = delta_threshold
        self.min_samples = min_samples
        
        # Históricos
        self.volume_history = deque(maxlen=ma_period)      # Volume total por candle
        self.delta_history = deque(maxlen=ma_period)       # Delta por candle
        self.timestamp_history = deque(maxlen=ma_period)   # Timestamps
        
        # Métricas atuais
        self.last_ratio = 0.0
        self.last_delta = 0.0
        self.last_direction = "NEUTRO"
        self.cooldown_until = 0  # Evita múltiplos spikes em sequência
        
        logger.info(f"📊 VolumeMetricsFix inicializado: MA={ma_period}, "
                   f"threshold={spike_threshold}x, delta_min={delta_threshold}")
    
    def update(self, candle_volume: float, buy_volume: float = 0.0,
               sell_volume: float = 0.0, timestamp: Optional[float] = None) -> Tuple[float, float, bool, str]:
        """
        Registra novo candle e calcula métricas.
        
        Args:
            candle_volume: Volume total do candle (em USDC/USDT)
            buy_volume: Volume de taker buys (compra agressiva)
            sell_volume: Volume de taker sells (venda agressiva)
            timestamp: Unix timestamp do candle (opcional)
            
        Returns:
            (volume_ratio, volume_delta_pct, is_spike, direction)
            
        Exemplo:
            (3.5, -0.45, True, "VENDA_AGRESSIVA")
            → Volume 3.5x acima da média, 45% venda agressiva, SPIKE confirmado
        """
        
        # Se não temos buy/sell separados, assume neutro
        total = buy_volume + sell_volume
        if total == 0:
            buy_volume = candle_volume * 0.5
            sell_volume = candle_volume * 0.5
            total = candle_volume
        
        # Calcula delta: (buy - sell) / total
        # Range: -1.0 (100% venda) → 0.0 (equilibrado) → +1.0 (100% compra)
        delta = (buy_volume - sell_volume) / total if total > 0 else 0.0
        
        # Registra no histórico
        self.volume_history.append(candle_volume)
        self.delta_history.append(delta)
        self.timestamp_history.append(timestamp or time.time())
        
        # Calcula média móvel (se temos dados suficientes)
        if len(self.volume_history) < self.min_samples:
            self.last_ratio = 1.0
            self.last_delta = delta
            self.last_direction = "NEUTRO"
            return 1.0, delta, False, "NEUTRO"
        
        volume_ma = sum(self.volume_history) / len(self.volume_history)
        
        # Evita divisão por zero
        if volume_ma == 0:
            volume_ma = 1.0
        
        # Ratio: volume atual vs média móvel
        ratio = candle_volume / volume_ma
        
        # Delta médio do período (tendência)
        avg_delta = sum(self.delta_history) / len(self.delta_history)
        
        # Determina direção
        if abs(delta) < 0.1:
            direction = "NEUTRO"
        elif delta > 0.3:
            direction = "COMPRA_AGRESSIVA"
        elif delta > 0.1:
            direction = "COMPRA_LEVE"
        elif delta < -0.3:
            direction = "VENDA_AGRESSIVA"
        elif delta < -0.1:
            direction = "VENDA_LEVE"
        else:
            direction = "NEUTRO"
        
        # Verifica se é spike
        is_spike = False
        now = time.time()
        
        if ratio >= self.spike_threshold and abs(delta) >= self.delta_threshold:
            # Cooldown de 5 minutos entre spikes (evita spam)
            if now >= self.cooldown_until:
                is_spike = True
                self.cooldown_until = now + 300  # 5 min cooldown
                logger.info(f"⚡ SPIKE DETETADO! Ratio={ratio:.2f}x | "
                           f"Delta={delta:+.1%} | Dir={direction}")
        
        # Guarda para referência
        self.last_ratio = ratio
        self.last_delta = delta
        self.last_direction = direction
        
        return ratio, delta, is_spike, direction
    
    def get_metrics(self) -> Dict:
        """Retorna métricas atuais para dashboard/logs."""
        return {
            'volume_ratio': round(self.last_ratio, 2),
            'volume_delta': round(self.last_delta, 3),
            'direction': self.last_direction,
            'samples': len(self.volume_history),
            'ma_period': self.ma_period,
            'threshold': self.spike_threshold,
            'status': 'READY' if len(self.volume_history) >= self.min_samples else 'WARMING_UP'
        }
    
    def is_ready(self) -> bool:
        """Verifica se já temos dados suficientes para decisões."""
        return len(self.volume_history) >= self.min_samples
    
    def reset(self):
        """Limpa histórico (útil para mudança de asset ou restart)."""
        self.volume_history.clear()
        self.delta_history.clear()
        self.timestamp_history.clear()
        self.last_ratio = 0.0
        self.last_delta = 0.0
        logger.info("📊 VolumeMetricsFix resetado")


# ============================================================
# FUNÇÃO DROP-IN para substituir no data_aggregator.py
# ============================================================

def calculate_volume_metrics_fixed(candles: list, current_volume_24h: float = None,
                                   ma_period: int = 20, spike_threshold: float = 2.0) -> Dict:
    """
    Versão drop-in que substitui a função atual do data_aggregator.
    
    Args:
        candles: Lista de candles [timestamp, open, high, low, close, volume]
                 Ordem: do mais antigo ao mais recente
        current_volume_24h: Volume 24h (ignorado — usamos média móvel real)
        ma_period: Período da média móvel
        spike_threshold: Threshold para spike
        
    Returns:
        Dict com: ratio, delta, is_spike, direction, ma_volume, status
    """
    
    if not candles or len(candles) < 2:
        return {
            'ratio': 0.0,
            'delta': 0.0,
            'is_spike': False,
            'direction': 'NEUTRO',
            'ma_volume': 0.0,
            'status': 'INSUFFICIENT_DATA',
            'reason': 'Poucos candles para média móvel'
        }
    
    # Extrai volumes dos candles
    volumes = [c[5] for c in candles if len(c) >= 6 and c[5] is not None]
    
    if len(volumes) < 2:
        return {
            'ratio': 0.0,
            'delta': 0.0,
            'is_spike': False,
            'direction': 'NEUTRO',
            'ma_volume': 0.0,
            'status': 'INSUFFICIENT_DATA',
            'reason': 'Candles sem volume válido'
        }
    
    # Volume atual (último candle)
    current_volume = volumes[-1]
    
    # Média móvel dos candles anteriores (excluindo o atual)
    previous_volumes = volumes[-(ma_period+1):-1]  # Até ma_period candles antes do atual
    
    if len(previous_volumes) < 3:
        # Fallback: usa todos os candles disponíveis exceto o atual
        previous_volumes = volumes[:-1]
    
    ma_volume = sum(previous_volumes) / len(previous_volumes) if previous_volumes else 1.0
    
    if ma_volume == 0:
        ma_volume = 1.0
    
    # Ratio
    ratio = current_volume / ma_volume
    
    # Sem dados de buy/sell separados, usamos proxy:
    # Se o candle é vermelho (close < open) → assume mais sell pressure
    last_candle = candles[-1]
    if len(last_candle) >= 5:
        open_p, close_p = last_candle[1], last_candle[4]
        if close_p < open_p:
            # Candle vermelho → proxy de venda
            delta = -0.2  # 20% venda agressiva (estimativa conservadora)
            direction = "VENDA_LEVE"
        elif close_p > open_p:
            # Candle verde → proxy de compra
            delta = 0.2
            direction = "COMPRA_LEVE"
        else:
            delta = 0.0
            direction = "NEUTRO"
    else:
        delta = 0.0
        direction = "NEUTRO"
    
    # Spike detection
    is_spike = ratio >= spike_threshold
    
    status = 'SPIKE' if is_spike else 'NORMAL'
    
    return {
        'ratio': round(ratio, 2),
        'delta': round(delta, 3),
        'is_spike': is_spike,
        'direction': direction,
        'ma_volume': round(ma_volume, 2),
        'current_volume': round(current_volume, 2),
        'status': status,
        'samples': len(previous_volumes),
        'reason': f"Ratio {ratio:.1f}x vs MA{ma_period}" + (" → SPIKE!" if is_spike else "")
    }


# ============================================================
# TESTE / EXEMPLO
# ============================================================

if __name__ == "__main__":
    # Simula candles com um pico no final
    print("=" * 60)
    print("TESTE: Volume Fix")
    print("=" * 60)
    
    candles = []
    base_time = time.time() - (25 * 15 * 60)  # 25 candles atrás (15m cada)
    
    # 20 candles calmos (volume ~100k)
    for i in range(20):
        candles.append([
            base_time + i * 15 * 60,  # timestamp
            50000,  # open
            50100,  # high
            49900,  # low
            50050,  # close
            100000 + (i * 1000)  # volume crescente suave
        ])
    
    # 4 candles de build-up
    for i in range(4):
        candles.append([
            base_time + (20 + i) * 15 * 60,
            50050,
            50200,
            49950,
            50100,
            150000 + (i * 20000)  # volume a subir
        ])
    
    # 1 candle de EXPLOSÃO (crash)
    candles.append([
        base_time + 24 * 15 * 60,
        50100,
        50150,
        48000,  # queda violenta!
        48500,  # close baixo
        800000  # volume 8x!
    ])
    
    print(f"\nCandles gerados: {len(candles)}")
    print(f"Volume do último candle: {candles[-1][5]:,.0f}")
    print(f"Volume médio dos 20 anteriores: {sum(c[5] for c in candles[0:20])/20:,.0f}")
    print(f"Expected ratio: ~{candles[-1][5] / (sum(c[5] for c in candles[0:20])/20):.1f}x")
    print()
    
    # Testa a função fix
    result = calculate_volume_metrics_fixed(candles, ma_period=20, spike_threshold=2.0)
    
    print("Resultado:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    
    print()
    if result['is_spike']:
        print("✅ SPIKE DETETADO CORRETAMENTE!")
    else:
        print("❌ Spike NÃO detetado — verificar lógica")
    
    print()
    print("=" * 60)
    print("TESTE: VolumeMetricsFix (classe com estado)")
    print("=" * 60)
    
    vm = VolumeMetricsFix(ma_period=20, spike_threshold=2.0)
    
    # Feed candles um a um
    for i, candle in enumerate(candles):
        vol = candle[5]
        # Proxy buy/sell baseado na cor do candle
        if candle[4] < candle[1]:  # vermelho
            buy, sell = vol * 0.4, vol * 0.6
        elif candle[4] > candle[1]:  # verde
            buy, sell = vol * 0.6, vol * 0.4
        else:
            buy, sell = vol * 0.5, vol * 0.5
        
        ratio, delta, is_spike, direction = vm.update(vol, buy, sell)
        
        if is_spike:
            print(f"\n🔥 Candle {i}: SPIKE!")
            print(f"   Ratio: {ratio:.2f}x | Delta: {delta:+.1%} | {direction}")
        elif i >= 19 and i % 5 == 0:
            print(f"Candle {i}: Ratio={ratio:.2f}x | {direction}")
    
    print(f"\n📊 Métricas finais: {vm.get_metrics()}")
