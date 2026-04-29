"""
Lógica de estratégia - Volume + OI + Preço
"""
import logging
from typing import Dict, Optional, List
from collections import deque

logger = logging.getLogger(__name__)


class MomentumStrategy:
    """
    Estratégia de momentum baseada em:
    - Spike de volume (> threshold da média)
    - OI global a subir (novo dinheiro a entrar)
    - Confirmação de preço (breakout)
    - Funding rate não extremo
    """
    
    def __init__(self, config: Dict):
        strat = config.get('strategy', {})
        self.volume_threshold = strat.get('volume_spike_threshold', 2.5)
        self.oi_threshold = strat.get('oi_change_threshold', 0.015)
        self.max_funding = strat.get('max_funding_rate', 0.01)
        self.min_funding = strat.get('min_funding_rate', -0.01)
        self.volume_lookback = strat.get('volume_lookback', 20)
        self.oi_exhaustion_threshold = strat.get('oi_exhaustion_threshold', -0.005)
        
        # Histórico de volume para média móvel
        self.volume_history = deque(maxlen=self.volume_lookback)
        
        # Estado atual
        self.in_position = False
        self.position_direction = None  # 'long' ou 'short'
        self.entry_price = 0
    
    def analyze(self, data: Dict, price: float, hypertracker_confirmation: Optional[Dict] = None) -> Optional[str]:
        """
        Analisa dados agregados e retorna sinal.
        
        NOVO: hypertracker_confirmation — layer externa de confirmacao (opcional).
        Se HyperTracker contradiz o sinal, baixa confianca ou skip.
        """
        
        oi_total = data.get('oi_total', 0)
        oi_change = data.get('oi_change_pct', 0)
        volume_total = data.get('volume_total', 0)
        funding_avg = data.get('funding_avg', 0)
        
        # Guardar volume no histórico (só se for válido)
        if volume_total > 0:
            self.volume_history.append(volume_total)
        else:
            logger.warning("Volume total = 0 — API pode estar com problemas. Ignorando este candle.")
            return None
        
        # Calcular média de volume (se temos dados suficientes)
        if len(self.volume_history) < self.volume_lookback // 2:
            logger.info(f"A recolher dados de volume... ({len(self.volume_history)}/{self.volume_lookback})")
            return None
        
        volume_avg = sum(self.volume_history) / len(self.volume_history)
        volume_ratio = volume_total / volume_avg if volume_avg > 0 else 0
        
        # === HYPERTRACKER CONFIRMATION LAYER ===
        ht_boost = 0
        ht_rec = 'neutral'
        if hypertracker_confirmation:
            ht_boost = hypertracker_confirmation.get('total_boost', 0)
            ht_rec = hypertracker_confirmation.get('recommendation', 'neutral')
            logger.info(
                f"[HT] Sentiment: {hypertracker_confirmation.get('sentiment_boost', 0):+d} | "
                f"SmartMoney: {hypertracker_confirmation.get('smart_money_boost', 0):+d} | "
                f"Total: {ht_boost:+d} | Rec: {ht_rec}"
            )
        
        logger.info(
            f"OI: ${oi_total:,.0f} | OI Δ: {oi_change*100:.2f}% | "
            f"Vol: {volume_ratio:.1f}x média | Funding: {funding_avg*100:.4f}% | "
            f"Preço: ${price:,.2f}"
        )
        
        # Verificar se funding está extremo (evitar overcrowding)
        funding_extreme = False
        if funding_avg > self.max_funding:
            logger.warning(f"Funding extremamente positivo ({funding_avg*100:.4f}%) — possível squeeze de baixo")
            funding_extreme = True
        
        if funding_avg < self.min_funding:
            logger.warning(f"Funding extremamente negativo ({funding_avg*100:.4f}%) — possível squeeze de cima")
            funding_extreme = True
        
        # === SINAL DE ENTRADA LONG ===
        if not self.in_position:
            ghost_score = 0
            
            # Ghost Method checks
            volume_ok = volume_ratio > self.volume_threshold
            oi_ok = oi_change > self.oi_threshold
            funding_ok = self.max_funding > funding_avg > self.min_funding
            
            if volume_ok:
                ghost_score += 40
            if oi_ok:
                ghost_score += 30
            if funding_ok:
                ghost_score += 30
            
            # Aplicar HyperTracker boost
            final_score = ghost_score + ht_boost
            final_score = max(0, min(100, final_score))  # Clamp 0-100
            
            logger.info(f"[SCORE] Ghost: {ghost_score} | HT boost: {ht_boost:+d} | FINAL: {final_score}")
            
            # Se HyperTracker contradiz fortemente, skip
            if ht_rec in ('strong_contradict', 'contradict') and ghost_score < 80:
                logger.warning(f"[HT] Sinal CONTRADITO pelo HyperTracker ({ht_rec}). Ghost score {ghost_score} < 80. SKIP.")
                return None
            
            # Entrada se score >= 70 (ajustável)
            if final_score >= 70:
                logger.info(f"[LAUNCH] SINAL LONG! Score={final_score} | Vol {volume_ratio:.1f}x, OI +{oi_change*100:.2f}%, Funding {funding_avg*100:.4f}%")
                self.in_position = True
                self.position_direction = 'long'
                self.entry_price = price
                return 'LONG'
        
        # === SINAL DE SAÍDA (trailing stop / exaustão) ===
        if self.in_position:
            # TODO: Implementar trailing stop e deteção de exaustão
            pass
        
        return None
    
    def should_exit(self, price: float, data: Dict) -> Optional[str]:
        """Verifica se devemos sair da posição atual"""
        if not self.in_position:
            return None
        
        oi_change = data.get('oi_change_pct', 0)
        
        # LONG: OI a descer enquanto preço sobe = momentum a esgotar-se
        if self.position_direction == 'long' and oi_change < self.oi_exhaustion_threshold:
            logger.info(f"[DOWN] OI a descer ({oi_change*100:.2f}%) — possível exaustão do momentum LONG")
            self._reset_position()
            return 'CLOSE_LONG'
        
        # SHORT: OI a subir enquanto preço desce = short squeeze possível
        if self.position_direction == 'short' and oi_change > abs(self.oi_exhaustion_threshold):
            logger.info(f"[UP] OI a subir ({oi_change*100:.2f}%) — possível exaustão do momentum SHORT")
            self._reset_position()
            return 'CLOSE_SHORT'
        
        # TODO: Trailing stop baseado em ATR
        # TODO: Stop loss fixo
        
        return None
    
    def _reset_position(self):
        """Reseta estado da posição"""
        self.in_position = False
        self.position_direction = None
        self.entry_price = 0
