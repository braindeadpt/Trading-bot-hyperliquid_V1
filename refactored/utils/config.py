"""
Config — Carregamento e validação de configuração.
Versão refatorada: suporta YAML/JSON, validação estruturada.
"""
import os
import yaml
import json
import logging
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    'bot': {
        'name': 'Hyperliquid Bot',
        'version': '2.0.0',
        'paper_trading': True,
    },
    'assets': ['BTC'],
    'timeframes': {'primary': '15m'},
    'strategy': {
        'volume_spike_threshold': 4.0,
        'oi_change_threshold': 0.01,
        'max_funding_rate': 0.01,
        'min_funding_rate': -0.01,
        'price_sma_period': 100,
        'min_bullish_candles': 2,
        'min_bearish_candles': 2,
        'vp_enabled': True,
        'vp_lookback': 96,
        'entry_cooldown_seconds': 300,
    },
    'risk': {
        'initial_capital': 10000.0,
        'max_position_size_usd': 100,
        'max_leverage': 3,
        'adaptive_leverage': True,
        'stop_loss_pct': 0.035,
        'trailing_activation_pct': 0.02,
        'trailing_stop_pct': 0.02,
        'daily_loss_limit_pct': 0.05,
        'daily_loss_hard_stop_pct': 0.10,
        'max_daily_trades': 5,
    },
    'polling': {
        'oi_interval': 30,
        'price_interval': 5,
    },
    'cache': {
        'ttl': 10,
    },
    'database': {
        'path': 'data/trading_bot.db',
    },
    'api': {
        'rate_limit_interval': 0.5,
    },
    'logging': {
        'level': 'INFO',
        'file': 'logs/bot.log',
    }
}

REQUIRED_KEYS = [
    'bot',
    'assets',
    'strategy',
    'risk',
]


def load_config(path: str = None) -> Dict[str, Any]:
    """
    Carrega configuração de ficheiro YAML/JSON.
    Faz merge com defaults e valida estrutura.
    """
    if path is None:
        # Procurar em locais padrão
        candidates = [
            Path('config/settings.yaml'),
            Path('config/settings.json'),
            Path('config.yaml'),
            Path(__file__).parent.parent.parent / 'config' / 'settings.yaml',
        ]
        for candidate in candidates:
            if candidate.exists():
                path = str(candidate)
                break
    
    path = Path(path) if path else None
    
    if not path or not path.exists():
        logger.warning("Config não encontrado — usando defaults")
        return dict(DEFAULT_CONFIG)
    
    # Carregar
    try:
        with open(path, 'r', encoding='utf-8') as f:
            if path.suffix in ('.yaml', '.yml'):
                user_config = yaml.safe_load(f)
            else:
                user_config = json.load(f)
    except Exception as e:
        raise ValueError(f"Erro a carregar config de {path}: {e}")
    
    # Merge com defaults (user overrides defaults)
    config = _deep_merge(dict(DEFAULT_CONFIG), user_config or {})
    
    # Validar
    for key in REQUIRED_KEYS:
        if key not in config:
            raise ValueError(f"Config obrigatória em falta: '{key}'")
    
    logger.info(f"[Config] Carregado: {path}")
    return config


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """Merge recursivo de dicionários."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
