"""
Utilitários gerais
"""
import logging
import yaml
from pathlib import Path
from typing import Dict


def load_config(path: str = None) -> Dict:
    """Carrega configuração YAML"""
    # Se não passar path, resolve relativo a este ficheiro
    if path is None:
        path = Path(__file__).parent.parent / "config" / "settings.yaml"
    
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config não encontrado: {config_path}")
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"Erro a parsear YAML em {config_path}: {e}")
    
    if not isinstance(config, dict):
        raise ValueError(f"Config em {config_path} não é um dicionário válido")
    
    return config


def setup_logging(level: str = "INFO", log_file: str = None):
    """Configura logging"""
    handlers = [logging.StreamHandler()]
    
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers
    )
