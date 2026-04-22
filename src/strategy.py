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
        self.volume_threshold = config['strategy']['volume_spike_threshold']
        self.oi_threshold = config['strategy']['oi_change_threshold']
        self.max_funding = config['strategy']['max_funding_rate']
        self.min_funding = config['strategy']['min_funding_rate']
        self.volume_lookback = config['strategy']['volume_lookback']
        
        # Histórico de volume para média móvel
        self.volume_history = deque(maxlen=self.volume_lookback)
        
        # Estado atual
        self.in_position = False
        self.position_direction = None  # 'long' ou 'short'
        self.entry_price = 0
    
    def analyze(self, data: Dict, price: float) -> Optional[str]:
        """
        Analisa dados agregados e retorna sinal:
        - 'LONG': entrar posição comprada
        - 'SHORT': entrar posição vendida
        - 'CLOSE_LONG': fechar posição comprada
        - 'CLOSE_SHORT': fechar posição vendida
        - None: nenhuma ação
        """
        
        oi_total = data.get('oi_total', 0)
        oi_change = data.get('oi_change_pct', 0)
        volume_total = data.get('volume_total', 0)
        funding_avg = data.get('funding_avg', 0)
        
        # Guardar volume no histórico
        self.volume_history.append(volume_total)
        
        # Calcular média de volume (se temos dados suficientes)
        if len(self.volume_history) < self.volume_lookback // 2:
            logger.info(f"A recolher dados de volume... ({len(self.volume_history)}/{self.volume_lookback})")
            return None
        
        volume_avg = sum(self.volume_history) / len(self.volume_history)
        volume_ratio = volume_total / volume_avg if volume_avg > 0 else 0
        
        logger.info(
            f"OI: ${oi_total:,.0f} | OI Δ: {oi_change*100:.2f}% | "
            f"Vol: {volume_ratio:.1f}x média | Funding: {funding_avg*100:.4f}% | "
            f"Preço: ${price:,.2f}"
        )
        
        # Verificar se funding está extremo (evitar overcrowding)
        if funding_avg > self.max_funding:
            logger.warning(f"Funding extremamente positivo ({funding_avg*100:.4f}%) — possível squeeze de baixo")
            # Podemos considerar SHORT aqui, mas por agora só evitamos LONG
        
        if funding_avg < self.min_funding:
            logger.warning(f"Funding extremamente negativo ({funding_avg*100:.4f}%) — possível squeeze de cima")
        
        # SINAL DE ENTRADA LONG
        if not self.in_position:
            if (volume_ratio > self.volume_threshold and 
                oi_change > self.oi_threshold and
                self.max_funding > funding_avg > self.min_funding):
                
                logger.info(f"🚀 SINAL LONG! Volume {volume_ratio:.1f}x, OI +{oi_change*100:.2f}%, Funding {funding_avg*100:.4f}%")
                self.in_position = True
                self.position_direction = 'long'
                self.entry_price = price
                return 'LONG'
        
        # SINAL DE SAÍDA (trailing stop / exaustão)
        if self.in_position:
            # TODO: Implementar trailing stop e deteção de exaustão
            pass
        
        return None
    
    def should_exit(self, price: float, data: Dict) -> Optional[str]:
        """Verifica se devemos sair da posição atual"""
        if not self.in_position:
            return None
        
        oi_change = data.get('oi_change_pct', 0)
        
        # OI a descer enquanto preço ainda sobe = momentum a esgotar-se
        if self.position_direction == 'long' and oi_change < -0.005:
            logger.info(f"📉 OI a descer ({oi_change*100:.2f}%) — possível exaustão do momentum")
            self._reset_position()
            return 'CLOSE_LONG'
        
        # TODO: Trailing stop baseado em ATR
        # TODO: Stop loss fixo
        
        return None
    
    def _reset_position(self):
        """Reseta estado da posição"""
        self.in_position = False
        self.position_direction = None
        self.entry_price = 0
