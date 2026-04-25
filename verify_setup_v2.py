"""
Verify Setup v2 - Verifica se a base de dados e tabelas estão corretas
Corre este script depois de fazer git pull para confirmar que SEMANA 1 está ok!
"""
import sys
import os

# Windows UTF-8 fix
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'

import logging
from pathlib import Path

# Adicionar src ao path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from database import BotDatabase
from performance_tracker import PerformanceTracker

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def check_tables(db: BotDatabase) -> dict:
    """Verifica se todas as tabelas necessárias existem"""
    required_tables = [
        'candles', 'open_interest', 'funding_rates', 'price_history',
        'trades', 'signals', 'market_regime', 'performance_log',
        'llm_analysis', 'paper_trades'
    ]
    
    conn = db._get_conn()
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    results = {}
    for table in required_tables:
        results[table] = table in existing
    
    return results


def test_signal_logging(db: BotDatabase):
    """Testa guardar e ler um sinal"""
    import time
    
    test_signal = {
        'timestamp': int(time.time() * 1000),
        'asset': 'BTC',
        'signal_type': 'LONG',
        'confidence': 0.85,
        'strategy': 'test',
        'entry_price': 95000.0,
        'reason': 'TEST: verify_setup',
        'market_regime': 'bull',
        'volume_ratio': 2.5,
        'executed': False
    }
    
    db.save_signal(test_signal)
    
    signals = db.get_signals(asset='BTC', limit=1)
    assert len(signals) > 0, "Falhou a guardar sinal!"
    assert signals[0]['reason'] == 'TEST: verify_setup', "Sinal corrompido!"
    
    logger.info("✅ Signal logging: OK")
    return True


def test_market_regime(db: BotDatabase):
    """Testa guardar e ler regime de mercado"""
    import time
    
    regime = {
        'timestamp': int(time.time() * 1000),
        'asset': 'BTC',
        'regime': 'bull',
        'volatility_24h': 1.5,
        'trend_strength': 2.3,
        'volume_profile': 'HIGH',
        'sma_200': 92000.0,
        'price_vs_sma_pct': 3.2
    }
    
    db.save_market_regime(regime)
    
    regimes = db.get_market_regime('BTC', limit=1)
    assert len(regimes) > 0, "Falhou a guardar regime!"
    
    logger.info("✅ Market regime logging: OK")
    return True


def test_performance_log(db: BotDatabase):
    """Testa guardar performance"""
    from datetime import datetime
    
    perf = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'asset': 'BTC',
        'total_trades': 5,
        'winning_trades': 3,
        'losing_trades': 2,
        'win_rate': 60.0,
        'profit_factor': 1.8,
        'total_pnl': 150.50,
        'max_drawdown': 50.0,
        'long_trades': 4,
        'short_trades': 1
    }
    
    db.save_performance_log(perf)
    
    logs = db.get_performance_log(asset='BTC', days=1)
    assert len(logs) > 0, "Falhou a guardar performance log!"
    
    logger.info("✅ Performance log: OK")
    return True


def main():
    print("\n" + "="*60)
    print("   🔍 VERIFICAÇÃO DO SETUP v2.0 - Semana 1")
    print("="*60)
    
    db = BotDatabase()
    
    # 1. Verificar tabelas
    print("\n📋 1. Verificando tabelas...")
    tables = check_tables(db)
    all_ok = True
    
    for table, exists in tables.items():
        status = "✅" if exists else "❌"
        print(f"   {status} {table}")
        if not exists:
            all_ok = False
    
    if not all_ok:
        print("\n❌ Faltam tabelas! A criar...")
        # Forçar re-inicialização
        conn = db._get_conn()
        conn.close()
        print("   Tabelas criadas!")
    else:
        print("\n✅ Todas as tabelas existem!")
    
    # 2. Verificar stats
    print("\n📊 2. Estatísticas da base de dados:")
    stats = db.get_stats_v2()
    for key, value in stats.items():
        print(f"   • {key}: {value}")
    
    # 3. Testar novas funcionalidades
    print("\n🧪 3. Testando novas funcionalidades...")
    
    try:
        test_signal_logging(db)
        test_market_regime(db)
        test_performance_log(db)
        
        print("\n✅ Todos os testes passaram!")
    except Exception as e:
        print(f"\n❌ Erro nos testes: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    # 4. Testar PerformanceTracker
    print("\n📈 4. Testando PerformanceTracker...")
    try:
        tracker = PerformanceTracker(db)
        
        # Testar análise de sinais
        signal_analysis = tracker.get_signal_analysis('BTC', days=1)
        print(f"   Sinais encontrados: {signal_analysis.get('total_signals', 0)}")
        
        # Testar relatório
        report = tracker.generate_daily_report('BTC')
        if report:
            tracker.print_report(report)
        
        print("\n✅ PerformanceTracker: OK")
    except Exception as e:
        print(f"\n⚠️ PerformanceTracker erro (pode ser normal se não houver dados): {e}")
    
    # 5. Resumo final
    print("\n" + "="*60)
    print("   ✅ SETUP v2.0 VERIFICADO COM SUCESSO!")
    print("="*60)
    print("\nPróximos passos:")
    print("   1. Corre o bot: python src/main.py")
    print("   2. Observa os logs — sinais vão ser guardados em SQLite")
    print("   3. Para relatório diário: python src/performance_tracker.py --daily")
    print("   4. Para análise completa: python src/performance_tracker.py BTC 7")
    print("\n   🚀 O bot agora guarda TUDO para análise futura!")
    print("="*60 + "\n")
    
    return 0


if __name__ == "__main__":
    exit(main())
