"""
PATCH: Volume Fix para data_aggregator.py + paper_trading.py
===============================================================

Como aplicar:
  1. Copiar este ficheiro para a pasta do bot
  2. Editar manualmente os ficheiros indicados (não executar este script)
  3. Verificar sintaxe: python -m py_compile src/data_aggregator.py

MUDANÇAS:
- get_intraday_volume() agora busca 21 candles (20 para MA + 1 atual)
- Média móvel real dos últimos 20 candles (excluindo o atual)
- Threshold 2.0x em vez de 4.4x
"""

# ============================================================
# PASSO 1: Substituir get_intraday_volume() em data_aggregator.py
# ============================================================

# LOCALIZAR (por volta da linha 510):
"""
    def get_intraday_volume(self, asset: str, interval: str = '15m') -> Optional[Dict]:
        '''
        Busca volume INTRADAY real (não 24h acumulado).
        Retorna o último candle com volume real.
        '''
        candles = self._fetch_binance_candles(asset, interval, limit=2)
        if candles and len(candles) >= 1:
            latest = candles[-1]
            # Calcular média dos últimos 20 para ratio
            avg_volume = sum(c['volume'] for c in candles) / len(candles)
            volume_ratio = latest['volume'] / max(avg_volume, 1)
            
            return {
                'volume': latest['volume'],
                'volume_ratio': volume_ratio,
                'close': latest['close'],
                'source': 'binance_klines'
            }
        return None
"""

# SUBSTITUIR POR:
"""
    def get_intraday_volume(self, asset: str, interval: str = '5m', ma_period: int = 20) -> Optional[Dict]:
        '''
        Busca volume INTRADAY real com média móvel.
        
        Args:
            asset: BTC, ETH, etc.
            interval: Timeframe dos candles (default '5m' para detetar spikes rápidos)
            ma_period: Quantos candles para média móvel (default 20)
        
        Retorna:
            volume, volume_ratio (vs MA), direction (proxy buy/sell)
        '''
        # Buscar ma_period + 1 candles (N para média + 1 atual)
        candles = self._fetch_binance_candles(asset, interval, limit=ma_period + 1)
        
        if not candles or len(candles) < 5:
            logger.warning(f"Volume: apenas {len(candles) if candles else 0} candles disponíveis")
            return None
        
        # Último candle (atual/incompleto)
        latest = candles[-1]
        latest_volume = latest['volume']
        latest_close = latest['close']
        latest_open = latest['open']
        
        # Candles anteriores para média (excluindo o último)
        previous_candles = candles[:-1]
        
        if len(previous_candles) < 3:
            logger.warning(f"Volume: apenas {len(previous_candles)} candles anteriores para média")
            return None
        
        # Média móvel dos candles anteriores
        avg_volume = sum(c['volume'] for c in previous_candles) / len(previous_candles)
        
        # Ratio: volume atual vs média
        volume_ratio = latest_volume / max(avg_volume, 1.0)
        
        # Proxy de direção (buy/sell pressure) baseado na cor do candle
        if latest_close < latest_open:
            # Candle vermelho → mais sell pressure
            direction = 'SELL'
            delta = -0.2  # Estimativa conservadora
        elif latest_close > latest_open:
            # Candle verde → mais buy pressure
            direction = 'BUY'
            delta = 0.2
        else:
            direction = 'NEUTRAL'
            delta = 0.0
        
        logger.info(
            f"📊 Volume {asset} | "
            f"Atual: {latest_volume:,.0f} | "
            f"MA{len(previous_candles)}: {avg_volume:,.0f} | "
            f"Ratio: {volume_ratio:.2f}x | "
            f"Dir: {direction}"
        )
        
        return {
            'volume': latest_volume,
            'volume_ratio': volume_ratio,
            'avg_volume': avg_volume,
            'close': latest_close,
            'open': latest_open,
            'direction': direction,
            'delta': delta,
            'samples': len(previous_candles),
            'source': f'binance_klines_{interval}'
        }
"""

# ============================================================
# PASSO 2: Atualizar config/settings.yaml
# ============================================================

# Adicionar/substituir a secção volume:
"""
strategy:
  name: "ghost_method_v2"
  
  # Volume fix: usar candles de 5m com média móvel real
  volume:
    interval: "5m"           # Buscar candles de 5m (mais sensível a spikes)
    ma_period: 20            # Média móvel de 20 candles = 100 minutos
    spike_threshold: 2.0     # 2x acima da média = spike (era 4.4, impossível)
    min_ratio: 1.0           # Volume deve ser pelo menos 1x a média
    
    # Se volume < média, rejeita (mercado sem interesse)
    # Se volume 1x-2x, aceita com warning
    # Se volume >= 2x, spike confirmado → confiança aumenta
"""

# ============================================================
# PASSO 3: Atualizar paper_trading.py (onde usa volume_ratio)
# ============================================================

# LOCALIZAR (por volta da linha 600+):
"""
            # --- Volume check ---
            current_volume = tick_data.get('volume', 0)
            avg_volume = tick_data.get('avg_volume', current_volume)
            volume_ratio = current_volume / max(avg_volume, 1)
            
            # ... (código existente)
            
            volume_spike = volume_ratio >= self.tuner.volume_threshold
"""

# SUBSTITUIR POR:
"""
            # --- Volume check (FIX: usa média móvel real de 5m candles) ---
            vol_data = self.data_aggregator.get_intraday_volume(
                asset, 
                interval=self.config.get('strategy', {}).get('volume', {}).get('interval', '5m'),
                ma_period=self.config.get('strategy', {}).get('volume', {}).get('ma_period', 20)
            )
            
            if not vol_data:
                logger.warning(f"⚠️ {asset} | Dados de volume insuficientes — rejeitando sinal")
                return
            
            volume_ratio = vol_data['volume_ratio']
            direction = vol_data['direction']
            delta = vol_data['delta']
            
            # Thresholds
            min_ratio = self.config.get('strategy', {}).get('volume', {}).get('min_ratio', 1.0)
            spike_threshold = self.config.get('strategy', {}).get('volume', {}).get('spike_threshold', 2.0)
            
            volume_ok = volume_ratio >= min_ratio
            volume_spike = volume_ratio >= spike_threshold
            
            # Log detalhado
            logger.info(
                f"📊 {asset} | "
                f"Vol: {volume_ratio:.2f}x (threshold: {spike_threshold}x) | "
                f"Direction: {direction} | "
                f"MA{vol_data['samples']}: {vol_data['avg_volume']:,.0f}"
            )
            
            if not volume_ok:
                logger.info(f"❌ {asset} | REJEITADO: Volume {volume_ratio:.2f}x < mínimo {min_ratio}x")
                self._log_rejection(asset, direction, f"volume_too_low({volume_ratio:.1f}x < {min_ratio}x)")
                return
"""

# ============================================================
# PASSO 4: OI não bloqueante (se ainda não aplicado)
# ============================================================

# LOCALIZAR onde OI rejeita trades:
"""
            if not oi_ok:
                reject_trade("oi_insufficient")
"""

# SUBSTITUIR POR:
"""
            # OI informativo — penaliza confiança mas não bloqueia
            if not oi_ok:
                confidence *= 0.8  # Penaliza 20%
                logger.info(f"⚠️ OI não confirma direção — confiança penalizada para {confidence:.0%}")
"""

# ============================================================
# RESUMO DAS MUDANÇAS NO COMPORTAMENTO
# ============================================================
# 
# ANTES (quebrado):
#   - Busca 2 candles de 15m → média de 2 candles
#   - Threshold 4.4x → impossível de atingir
#   - OI bloqueia sozinho → "oi_insufficient"
#   → Resultado: 12 sinais SHORT todos rejeitados
#
# DEPOIS (corrigido):
#   - Busca 21 candles de 5m → média real dos 20 anteriores
#   - Threshold 2.0x → atingível em eventos reais
#   - OI penaliza confiança (-20%) mas não bloqueia
#   → Resultado: sinais passam quando volume real > média
"""
