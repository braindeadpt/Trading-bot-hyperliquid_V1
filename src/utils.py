"""
Utilitários gerais
"""
import logging
import yaml
from pathlib import Path
from typing import Dict


def load_config(path: str = "config/settings.yaml") -> Dict:
    """Carrega configuração YAML"""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config não encontrado: {path}")
    
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


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
