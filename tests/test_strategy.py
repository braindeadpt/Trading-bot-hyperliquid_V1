"""
Testes unitários da estratégia
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strategy import MomentumStrategy


def test_volume_spike_detection():
    """Testa deteção de spike de volume"""
    config = {
        'strategy': {
            'volume_spike_threshold': 1.5,
            'oi_change_threshold': 0.015,
            'max_funding_rate': 0.01,
            'min_funding_rate': -0.01,
            'volume_lookback': 20
        }
    }
    
    strategy = MomentumStrategy(config)
    
    # Simular histórico de volume
    for i in range(15):
        strategy.volume_history.append(1000000)  # Volume base
    
    # Dados com spike
    data = {
        'oi_total': 1000000000,
        'oi_change_pct': 0.02,  # +2%
        'volume_total': 2000000,  # 2x média
        'funding_avg': 0.005
    }
    
    signal = strategy.analyze(data, 65000.0)
    
    print(f"Sinal: {signal}")
    assert signal == 'LONG', f"Esperado LONG, obtido {signal}"
    print(" Teste passou!")


if __name__ == "__main__":
    test_volume_spike_detection()
