import json
import logging
import os
import time
from typing import Optional, Dict, Any
import requests

logger = logging.getLogger(__name__)

def _load_hypertracker_token() -> str:
    """Carrega token do config/hypertracker.yaml (não commitado)."""
    import yaml
    from pathlib import Path
    
    config_path = Path(__file__).parent.parent / 'config' / 'hypertracker.yaml'
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
                if config and 'hypertracker' in config:
                    return config['hypertracker'].get('token', '')
        except Exception as e:
            logger.warning(f"[HyperTracker] Erro a ler config: {e}")
    
    # Fallback: variável de ambiente
    return os.environ.get('HYPERTRACKER_TOKEN', '')

class HyperTrackerClient:
    """
    Cliente para CoinMarketMan HyperTracker API.
    Usado como LAYER DE CONFIRMACAO para a estrategia Ghost Method.
    
    Limites:
    - 100 requests/dia (gratuito)
    - Chamamos a cada 1h (~48 requests/dia)
    - Se API falha ou limite atingido, bot continua com estrategia base
    """
    
    BASE_URL = "https://app.coinmarketman.com/hypertracker"
    
    def __init__(self, token: str = None):
        self.token = token or _load_hypertracker_token()
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/json',
            'User-Agent': 'HyperliquidBot/1.0'
        })
        
        # Rate limiting state
        self._calls_today = 0
        self._last_call_time = 0
        self._day_start = int(time.time() / 86400) * 86400  # Inicio do dia UTC
        self._last_data = None
        
        # Cache TTL: 1 hora
        self._cache_ttl = 3600
        
        if not self.token:
            logger.warning("[HyperTracker] Token não configurado. Cria config/hypertracker.yaml com o teu token.")
    
    def _check_rate_limit(self) -> bool:
        """Verifica se podemos fazer chamada (limite 100/dia, min 1h entre chamadas)."""
        now = int(time.time())
        current_day = int(now / 86400) * 86400
        
        # Reset diario
        if current_day > self._day_start:
            self._day_start = current_day
            self._calls_today = 0
            logger.info("[HyperTracker] Reset diario de rate limit")
        
        # Max 100/dia
        if self._calls_today >= 100:
            logger.warning("[HyperTracker] Limite diario atingido (100). Skipping.")
            return False
        
        # Min 1h entre chamadas (poupar requests)
        if now - self._last_call_time < self._cache_ttl:
            logger.debug("[HyperTracker] Cache ainda valido (<1h). Reusing.")
            return False  # Nao precisa chamar, usa cache
        
        return True
    
    def _call(self, endpoint: str) -> Optional[Dict]:
        """Faz chamada GET a API com retry simples."""
        if not self.token:
            return None
            
        url = f"{self.BASE_URL}{endpoint}"
        
        try:
            response = self.session.get(url, timeout=10)
            self._calls_today += 1
            self._last_call_time = int(time.time())
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                logger.warning("[HyperTracker] Rate limited (429). API returned 429.")
                return None
            elif response.status_code == 401:
                logger.error("[HyperTracker] Token invalido (401).")
                return None
            else:
                logger.warning(f"[HyperTracker] HTTP {response.status_code}: {response.text[:100]}")
                return None
                
        except requests.exceptions.Timeout:
            logger.warning("[HyperTracker] Timeout. API lenta ou indisponivel.")
            return None
        except requests.exceptions.ConnectionError:
            logger.warning("[HyperTracker] Connection error. API offline?")
            return None
        except Exception as e:
            logger.warning(f"[HyperTracker] Erro inesperado: {e}")
            return None
    
    def get_sentiment(self) -> Optional[Dict[str, Any]]:
        """
        Busca sentiment analysis do HLP (Hyperliquidity Provider).
        
        Retorna:
        {
            'z_score': float,           # Z-score do sentimento (-3 a +3)
            'retail_positioning': str,  # 'long_heavy', 'short_heavy', 'neutral'
            'confidence_boost': float   # -20 a +20 (para adicionar ao score)
        }
        """
        if not self._check_rate_limit():
            return self._last_data
        
        logger.info("[HyperTracker] Buscando sentiment...")
        data = self._call('/api/hlp/sentiment')
        
        if not data:
            return None
        
        try:
            z_score = data.get('z_score', 0)
            retail = data.get('retail_positioning', 'neutral')
            
            # Converter z_score em confidence_boost
            # z_score > +1.0 = retail esta muito LONG -> smart money provavelmente SHORT -> confirma SHORT
            # z_score < -1.0 = retail esta muito SHORT -> smart money provavelmente LONG -> confirma LONG
            if z_score > 1.5:
                boost = +15  # Retail muito LONG -> smart money SHORT (confirma SHORT do nosso bot se for SHORT)
            elif z_score > 0.5:
                boost = +5
            elif z_score < -1.5:
                boost = +15  # Retail muito SHORT -> smart money LONG (confirma LONG)
            elif z_score < -0.5:
                boost = +5
            else:
                boost = 0
            
            result = {
                'z_score': z_score,
                'retail_positioning': retail,
                'confidence_boost': boost,
                'raw': data
            }
            
            self._last_data = result
            logger.info(f"[HyperTracker] Sentiment: z={z_score:.2f}, retail={retail}, boost={boost:+d}")
            return result
            
        except Exception as e:
            logger.warning(f"[HyperTracker] Erro a parsear sentiment: {e}")
            return None
    
    def get_smart_money_signals(self, timeframe: str = '1h') -> Optional[Dict[str, Any]]:
        """
        Busca sinais dos top traders (smart money).
        
        Retorna:
        {
            'direction': str,        # 'long', 'short', 'mixed'
            'strength': float,       # 0.0 a 1.0
            'confidence_boost': float  # -15 a +15
        }
        """
        if not self._check_rate_limit():
            return None  # Ja usamos cache no sentiment
        
        logger.info(f"[HyperTracker] Buscando smart money signals ({timeframe})...")
        data = self._call(f'/api/smart_money/signals_{timeframe}.json')
        
        if not data:
            return None
        
        try:
            top_direction = data.get('top_10_direction', 'mixed')
            strength = data.get('top_10_strength', 0.5)
            
            # Converter direction/strength em confidence_boost
            if top_direction == 'long':
                boost = int(15 * strength)
            elif top_direction == 'short':
                boost = int(-15 * strength)
            else:
                boost = 0
            
            result = {
                'direction': top_direction,
                'strength': strength,
                'confidence_boost': boost,
                'raw': data
            }
            
            logger.info(f"[HyperTracker] Smart Money: {top_direction} (strength={strength:.2f}), boost={boost:+d}")
            return result
            
        except Exception as e:
            logger.warning(f"[HyperTracker] Erro a parsear smart money: {e}")
            return None
    
    def get_combined_confirmation(self) -> Optional[Dict[str, Any]]:
        """
        Combina sentiment + smart money num unico confirmation score.
        
        Chamado a cada 1h pelo DataAggregator.
        
        Retorna:
        {
            'hypertracker_active': bool,
            'sentiment_boost': float,      # -20 a +20
            'smart_money_boost': float,     # -15 a +15
            'total_boost': float,           # -35 a +35
            'recommendation': str           # 'confirm', 'neutral', 'contradict'
        }
        """
        # 1. Sentiment (usa cache se <1h)
        sentiment = self.get_sentiment()
        
        # 2. Smart Money (se ainda temos requests)
        smart = None
        if self._calls_today < 98:  # Deixa margem de seguranca
            smart = self.get_smart_money_signals('1h')
        
        if not sentiment and not smart:
            logger.debug("[HyperTracker] Sem dados. Bot continua com estrategia base.")
            return None
        
        # Calcular total boost
        s_boost = sentiment['confidence_boost'] if sentiment else 0
        sm_boost = smart['confidence_boost'] if smart else 0
        total = s_boost + sm_boost
        
        # Recomendacao
        if total >= 20:
            rec = 'strong_confirm'
        elif total >= 10:
            rec = 'confirm'
        elif total <= -20:
            rec = 'strong_contradict'
        elif total <= -10:
            rec = 'contradict'
        else:
            rec = 'neutral'
        
        result = {
            'hypertracker_active': True,
            'sentiment_boost': s_boost,
            'smart_money_boost': sm_boost,
            'total_boost': total,
            'recommendation': rec,
            'calls_today': self._calls_today,
            'calls_remaining': 100 - self._calls_today
        }
        
        logger.info(f"[HyperTracker] Confirmation: total_boost={total:+d}, rec={rec}")
        return result
