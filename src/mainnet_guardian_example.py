"""
🔌 GUIA DE INTEGRAÇÃO — MainnetGuardian v3
=============================================
Como integrar o MainnetGuardian no bot existente.

PASSOS:
  1. Importar MainnetGuardian
  2. Instanciar no startup
  3. Verificar segurança antes de iniciar
  4. Validar ordens antes de enviar
  5. Verificar circuit breaker no loop
  6. Configurar graceful shutdown
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from mainnet_guardian import MainnetGuardian
from utils import load_config


def integrate_guardian_into_paper_trading():
    """
    EXEMPLO: Integrar MainnetGuardian no PaperTrader
    """
    config = load_config()
    
    # 1. Criar guardian
    guardian = MainnetGuardian(config)
    
    # 2. Verificar segurança ANTES de iniciar
    if not guardian.verify_before_start():
        print("🚫 Bot NÃO pode iniciar — verificação de segurança falhou!")
        sys.exit(1)
    
    # 3. Configurar graceful shutdown
    # O PaperTrader precisa de um método que feche a posição atual
    # guardian.setup_graceful_shutdown(trader.close_current_position)
    
    # 4. No loop de trading:
    def trading_loop_example(trader):
        while True:
            # Verificar emergency stop e circuit breaker
            if guardian.should_stop(trader.capital):
                print("🛑 Parando bot por segurança...")
                break
            
            # Se há sinal de entrada:
            signal = 'long'  # Exemplo
            size = 50
            price = 50000
            
            # Validar ordem ANTES de enviar
            result = guardian.validate_order(
                asset='BTC',
                side=signal,
                size=size,
                market_price=price,
                account_balance=trader.capital,
            )
            
            if not result.is_valid:
                print(f"🚫 Ordem rejeitada: {result.reason}")
                continue
            
            # Enviar ordem (paper ou real)
            # trader.enter_position(...)
            
            # Após fill, validar slippage
            actual_fill = 50010  # Preço real de fill
            ok, slippage = guardian.validate_slippage(price, actual_fill, 'BUY')
            if not ok:
                print(f"⚠️  Slippage excessivo: {slippage*100:.2f}%")
    
    print("✅ Integração completa!")


def example_order_validation():
    """Exemplo de validação de ordem"""
    config = load_config()
    guardian = MainnetGuardian(config)
    
    # Ordem válida
    result = guardian.validate_order('BTC', 'BUY', 50, market_price=50000)
    print(f"Válida: {result.is_valid} — {result.reason}")
    
    # Ordem inválida (tamanho mínimo)
    result = guardian.validate_order('BTC', 'BUY', 5, market_price=50000)
    print(f"Inválida: {result.is_valid} — {result.reason}")
    
    # Ordem inválida (desvio de preço)
    result = guardian.validate_order('BTC', 'BUY', 50, price=53000, market_price=50000)
    print(f"Inválida (desvio): {result.is_valid} — {result.reason}")


def example_circuit_breaker():
    """Exemplo de circuit breaker"""
    config = load_config()
    guardian = MainnetGuardian(config)
    
    initial = 10000
    
    # Simular perdas
    for capital in [9800, 9500, 9200, 8999]:
        should_stop = guardian.should_stop(capital)
        loss_pct = (initial - capital) / initial * 100
        print(f"Capital: ${capital} | Perda: {loss_pct:.1f}% | Parar: {should_stop}")


def example_network_gate():
    """Exemplo de controlo de rede"""
    from mainnet_guardian import NetworkGate
    
    config = load_config()
    gate = NetworkGate(config)
    
    print(f"Network: {gate.get_network()}")
    print(f"Pode tradear: {gate.can_trade_real()}")
    
    # Para aprovar mainnet (MANUAL!):
    # gate.approve_mainnet()


if __name__ == "__main__":
    print("=" * 60)
    print("🔌 GUIA DE INTEGRAÇÃO — MainnetGuardian v3")
    print("=" * 60)
    
    print("\n📋 Exemplo 1: Validação de ordens")
    print("-" * 40)
    example_order_validation()
    
    print("\n📋 Exemplo 2: Circuit breaker")
    print("-" * 40)
    example_circuit_breaker()
    
    print("\n📋 Exemplo 3: Network gate")
    print("-" * 40)
    example_network_gate()
    
    print("\n" + "=" * 60)
    print("✅ Exemplos completos!")
    print("=" * 60)
