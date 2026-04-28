# PATCH: volume_fix para paper_trading.py
# =========================================
# 
# Este patch substitui o cálculo de volume QUEBRADO no paper_trading.py
# por um baseado em Média Móvel Real (não 24h/288 teórico).
#
# MUDANÇAS:
# 1. Substituir _calculate_volume_metrics() por versão com MA real
# 2. OI: de bloqueante → informativo (penaliza confiança, não rejeita)
# 3. Threshold volume: 4.4x → 2.0x
#
# Como aplicar:
#   1. Copiar volume_fix.py para src/volume_fix.py
#   2. No topo de paper_trading.py, adicionar: from src.volume_fix import calculate_volume_metrics_fixed
#   3. Substituir a chamada atual pela nova (ver abaixo)

# ============================================================
# PASSO 1: Adicionar import no topo do paper_trading.py
# ============================================================
# Localiza estas linhas no topo:
#     import logging
#     import time
#     ...
#
# Adicionar DEPOIS dos imports existentes:

from src.volume_fix import calculate_volume_metrics_fixed, VolumeMetricsFix

# ============================================================
# PASSO 2: Substituir _calculate_volume_metrics()
# ============================================================
# LOCALIZAR no paper_trading.py a função _calculate_volume_metrics()
# ou similar (onde calcula volume_ratio).
#
# DEVE HAVER algo como:
#     volume_24h = ...
#     volume_ratio = current_volume / (volume_24h / 288)
#
# SUBSTITUIR TUDO por:

    def _calculate_volume_metrics_fixed(self, candles=None):
        """
        Calcula volume com média móvel REAL em vez de média teórica 24h/288.
        
        Args:
            candles: Lista de candles [ts, open, high, low, close, volume]
                    Se None, usa candles armazenados internamente.
        
        Returns:
            dict: {ratio, delta, is_spike, direction, status, reason}
        """
        if candles is None:
            # Tenta obter candles do cache ou MTF
            candles = getattr(self, '_cached_candles', [])
        
        # Usa a função fix do volume_fix.py
        result = calculate_volume_metrics_fixed(
            candles=candles,
            ma_period=self.config.get('volume', {}).get('ma_period', 20),
            spike_threshold=self.config.get('volume', {}).get('spike_threshold', 2.0)
        )
        
        # Log para debug
        if result['is_spike']:
            self.logger.info(f"⚡ SPIKE! {result['reason']}")
        
        return result

# ============================================================
# PASSO 3: Substituir chamada no run_cycle() ou fetch_and_process_candle()
# ============================================================
# LOCALIZAR onde o bot decide se entra num trade.
# DEVE HAVER algo como:
#     volume_ratio = ...
#     if volume_ratio >= volume_threshold:
#         volume_ok = True
#
# SUBSTITUIR por:

    def _check_signal_validity_fixed(self, direction, candles=None):
        """
        Versão corrigida dos filtros de sinal.
        
        Antes: volume + OI + funding (todos bloqueavam)
        Agora: volume bloqueia, OI penaliza, funding bloqueia só se extremo
        """
        result = {
            'valid': True,
            'confidence': 1.0,
            'reasons': [],
            'warnings': []
        }
        
        # --- 1. VOLUME (bloqueante) ---
        vol_metrics = self._calculate_volume_metrics_fixed(candles)
        
        if vol_metrics['status'] == 'INSUFFICIENT_DATA':
            result['valid'] = False
            result['reasons'].append(f"VOLUME: {vol_metrics['reason']}")
            return result
        
        if not vol_metrics['is_spike']:
            # Volume não é spike — verifica se é pelo menos acima da média
            if vol_metrics['ratio'] < 1.0:
                result['valid'] = False
                result['reasons'].append(
                    f"VOLUME: {vol_metrics['ratio']:.1f}x < 1.0x (média) | "
                    f"MA={vol_metrics['ma_volume']:,.0f}"
                )
            else:
                # Volume acima da média mas não spike → warning
                result['warnings'].append(
                    f"Volume fraco: {vol_metrics['ratio']:.1f}x (threshold={self.config.get('volume', {}).get('spike_threshold', 2.0)}x)"
                )
                result['confidence'] *= 0.7  # Penaliza 30%
        else:
            # Volume é spike! Aumenta confiança
            result['confidence'] *= 1.2  # Bónus 20%
            result['warnings'].append(f"Volume SPIKE: {vol_metrics['ratio']:.1f}x")
        
        # --- 2. OI (informativo, NÃO bloqueante) ---
        oi = self._get_open_interest()  # método existente
        if oi is not None:
            oi_change = oi.get('change_1h', 0)
            
            # Verifica se OI confirma a direção
            oi_confirms_long = oi_change > 0.03   # +3%
            oi_confirms_short = oi_change < -0.03   # -3%
            
            if direction == 'LONG' and not oi_confirms_long:
                result['warnings'].append(f"OI não confirma LONG ({oi_change:+.1%})")
                result['confidence'] *= (1 - self.config.get('open_interest', {}).get('confidence_penalty', 0.2))
            
            elif direction == 'SHORT' and not oi_confirms_short:
                result['warnings'].append(f"OI não confirma SHORT ({oi_change:+.1%})")
                result['confidence'] *= (1 - self.config.get('open_interest', {}).get('confidence_penalty', 0.2))
            
            else:
                result['warnings'].append(f"OI confirma {direction} ({oi_change:+.1%}) ✅")
        
        # --- 3. FUNDING (bloqueia só se extremo) ---
        funding = self._get_funding_rate()  # método existente
        if abs(funding) > self.config.get('funding', {}).get('threshold_extreme', 0.01):
            result['valid'] = False
            result['reasons'].append(f"FUNDING EXTREMO: {funding:+.2%} (limite: 1%)")
        elif abs(funding) > self.config.get('funding', {}).get('threshold_warning', 0.005):
            result['warnings'].append(f"Funding alto: {funding:+.2%}")
            result['confidence'] *= 0.9
        
        # --- 4. Confiança mínima ---
        min_conf = self.config.get('strategy', {}).get('min_confidence', 0.6)
        if result['confidence'] < min_conf:
            result['valid'] = False
            result['reasons'].append(
                f"CONFIDÊNCIA BAIXA: {result['confidence']:.0%} < {min_conf:.0%}"
            )
        
        return result

# ============================================================
# PASSO 4: No run_cycle(), substituir a lógica de rejeição
# ============================================================
# LOCALIZAR algo como:
#     if not volume_ok or not oi_ok or not funding_ok:
#         self.logger.info("SINAL REJEITADO: ...")
#         return
#
# SUBSTITUIR por:

    def run_cycle_fixed(self, asset):
        """
        Ciclo principal com filtros corrigidos.
        """
        self.logger.info(f"\n📡 Analisando {asset}...")
        
        # 1. Fetch candles (método existente)
        candles = self._fetch_candles(asset, timeframe='15m', limit=25)
        if not candles or len(candles) < 20:
            self.logger.warning(f"Poucos candles: {len(candles) if candles else 0}")
            return
        
        # 2. Gera sinal da estratégia (método existente)
        signal = self._generate_signal(candles)
        if not signal or signal.get('direction') == 'HOLD':
            self.logger.info(f"SINAL: HOLD (sem oportunidade)")
            return
        
        direction = signal['direction']
        self.logger.info(f"🎯 SINAL CRU: {direction} | Confiança base: {signal.get('confidence', 0):.0%}")
        
        # 3. Validação corrigida dos filtros
        validation = self._check_signal_validity_fixed(direction, candles)
        
        if not validation['valid']:
            # Rejeição com detalhes
            self.logger.info(
                f"❌ SINAL REJEITADO: {direction} | "
                f"Motivos: {' | '.join(validation['reasons'])}"
            )
            # Guarda na DB para análise posterior
            self._log_rejected_signal(asset, direction, validation)
            return
        
        # 4. Sinal aceite!
        final_confidence = min(validation['confidence'], 1.0)
        self.logger.info(
            f"✅ SINAL ACEITE: {direction} | "
            f"Confiança final: {final_confidence:.0%} | "
            f"Warnings: {validation['warnings']}"
        )
        
        # 5. Entra na posição (método existente)
        self._enter_position(asset, direction, confidence=final_confidence)

# ============================================================
# PASSO 5: Adicionar método para log de rejeições
# ============================================================

    def _log_rejected_signal(self, asset, direction, validation):
        """
        Guarda sinais rejeitados para análise posterior.
        Permite ver: 'tivemos um bom sinal mas volume estava fraco'
        """
        try:
            import json
            from datetime import datetime
            
            rejection_data = {
                'timestamp': datetime.now().isoformat(),
                'asset': asset,
                'direction': direction,
                'reasons': validation['reasons'],
                'warnings': validation['warnings'],
                'confidence': round(validation['confidence'], 2)
            }
            
            # Guarda em ficheiro ou DB
            if hasattr(self, 'db') and self.db:
                self.db.execute(
                    """INSERT INTO rejected_signals 
                       (timestamp, asset, direction, reasons, confidence) 
                       VALUES (?, ?, ?, ?, ?)""",
                    (rejection_data['timestamp'], asset, direction,
                     json.dumps(validation['reasons']),
                     rejection_data['confidence'])
                )
            
            self.logger.debug(f"Rejeição logged: {rejection_data}")
            
        except Exception as e:
            self.logger.warning(f"Erro a log rejeição: {e}")

# ============================================================
# RESUMO DAS MUDANÇAS
# ============================================================
# 
# ANTES (quebrado):
#   - volume_ratio = vol_atual / (vol_24h / 288)  → média falsa
#   - threshold 4.4x → impossível de atingir
#   - OI bloqueia trades (oi_insufficient)
#   - Rejeição: "vol_low(0.4x < 4.4) | oi_insufficient"
#
# DEPOIS (corrigido):
#   - volume_ratio = vol_atual / MA(últimos 20 candles)  → média real
#   - threshold 2.0x → atingível em eventos reais
#   - OI penaliza confiança (não bloqueia)
#   - Rejeição: só se volume < média OU funding extremo OU confiança < 60%
#
# IMPACTO ESPERADO:
#   - Taxa de execução: 0% → ~30-50% (depende do mercado)
#   - Sinais SHORT rejeitados por "vol_low" → passam a ser aceites se volume real > média
#   - OI nunca mais bloqueia sozinho
