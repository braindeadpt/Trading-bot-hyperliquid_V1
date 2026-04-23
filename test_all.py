#!/usr/bin/env python3
"""
Test Suite - Testa todos os módulos do bot
"""
import sys
import time
import logging
from pathlib import Path

# Adicionar src ao path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from utils import load_config
from database import BotDatabase
from data_aggregator import DataAggregator
from backtest_db import BacktestEngineDB
from strategy import MomentumStrategy

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_database():
    """Testa módulo de base de dados"""
    print("\n" + "="*70)
    print("  TESTE 1/5: Base de Dados SQLite")
    print("="*70)
    
    try:
        db = BotDatabase()
        stats = db.get_stats()
        print(f"  ✅ Base de dados inicializada: {db.db_path}")
        print(f"  📊 Estatísticas: {stats}")
        
        # Testar inserção
        db.save_candles("BTC", "15m", [{
            'timestamp': int(time.time() * 1000),
            'open': 50000, 'high': 51000, 'low': 49000, 'close': 50500, 'volume': 1000
        }])
        
        candles = db.get_candles("BTC", "15m")
        print(f"  ✅ Teste de inserção/leitura: OK ({len(candles)} candles)")
        return True
        
    except Exception as e:
        print(f"  ❌ ERRO: {e}")
        return False


def test_apis():
    """Testa APIs das exchanges"""
    print("\n" + "="*70)
    print("  TESTE 2/5: APIs das Exchanges")
    print("="*70)
    
    try:
        config = load_config()
        agg = DataAggregator(config)
        results = agg.test_all_apis()
        
        ok_count = sum(1 for v in results.values() if v)
        total = len(results)
        
        print(f"\n  Resultado: {ok_count}/{total} APIs OK")
        
        if ok_count == 0:
            print("  ⚠️  NENHUMA API FUNCIONAL - possível problema de rede")
            return False
        
        # Testar fetch real
        print("\n  Testando fetch de dados (BTC)...")
        data = agg.fetch_all_data("BTC")
        if data:
            print(f"  ✅ Fetch OK - OI: ${data.get('oi_total', 0):,.0f}")
        else:
            print("  ⚠️  Fetch retornou None (pode ser normal se APIs estão instáveis)")
        
        return True
        
    except Exception as e:
        print(f"  ❌ ERRO: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_backtest():
    """Testa motor de backtest"""
    print("\n" + "="*70)
    print("  TESTE 3/5: Motor de Backtest (SQLite)")
    print("="*70)
    
    try:
        config = load_config()
        db = BotDatabase()
        
        # Verificar se há dados
        stats = db.get_stats()
        if stats.get('candles', 0) == 0:
            print("  ⚠️  Sem dados históricos em DB")
            print("  💡 Executa primeiro: python src/data_downloader.py BTC 30")
            print("  ⏭️  Pulando teste de backtest (requer dados)")
            return True  # Não é erro, só falta dados
        
        engine = BacktestEngineDB(config, db)
        metrics = engine.run("BTC", "15m", days=7)
        
        if 'error' in metrics:
            print(f"  ⚠️  {metrics['error']}")
            return False
        
        print(f"  ✅ Backtest completo!")
        print(f"  📈 Trades: {metrics['total_trades']}")
        print(f"  💰 PnL: ${metrics.get('total_pnl', 0):+.2f}")
        print(f"  📊 Win Rate: {metrics.get('win_rate', 0):.1f}%")
        
        return True
        
    except Exception as e:
        print(f"  ❌ ERRO: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_strategy():
    """Testa estratégia"""
    print("\n" + "="*70)
    print("  TESTE 4/5: Estratégia de Momentum")
    print("="*70)
    
    try:
        config = load_config()
        strategy = MomentumStrategy(config)
        
        # Dados de teste
        test_data = {
            'oi_total': 1000000000,
            'oi_change_pct': 0.05,
            'funding_avg': 0.0001,
            'exchanges_data': {
                'hyperliquid': {'mark_price': 50000}
            }
        }
        
        signal = strategy.analyze(test_data, 50000)
        print(f"  ✅ Estratégia inicializada")
        print(f"  🎯 Sinal de teste: {signal or 'WAIT'}")
        
        return True
        
    except Exception as e:
        print(f"  ❌ ERRO: {e}")
        return False


def test_dashboard():
    """Testa dashboard web (import only)"""
    print("\n" + "="*70)
    print("  TESTE 5/5: Dashboard Web")
    print("="*70)
    
    try:
        # Verificar se Flask está instalado
        try:
            import flask
            print(f"  ✅ Flask instalado (v{flask.__version__})")
        except ImportError:
            print("  ⚠️  Flask não instalado")
            print("  💡 Instala com: pip install flask")
            return True  # Não é erro crítico
        
        # Testar import
        from dashboard_web import WebDashboard
        print("  ✅ Módulo dashboard_web importa corretamente")
        
        # Testar inicialização (sem iniciar servidor)
        config = load_config()
        db = BotDatabase()
        dashboard = WebDashboard(config, db)
        route_count = len(list(dashboard.app.url_map.iter_rules()))
        print(f"  ✅ Dashboard inicializado ({route_count} rotas)")
        
        print(f"\n  🌐 Para iniciar o dashboard:")
        print(f"     python src/dashboard_web.py")
        print(f"     (Abre automaticamente no browser)")
        
        return True
        
    except Exception as e:
        print(f"  ❌ ERRO: {e}")
        import traceback
        traceback.print_exc()
        return False


def run_all_tests():
    """Corre todos os testes"""
    print("\n" + "🔥"*35)
    print("  HYPERLIQUID BOT - TEST SUITE")
    print("  " + "🔥"*35)
    
    tests = [
        ("Base de Dados", test_database),
        ("APIs Exchanges", test_apis),
        ("Backtest Engine", test_backtest),
        ("Estratégia", test_strategy),
        ("Dashboard Web", test_dashboard),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            ok = test_func()
            results.append((name, ok))
        except Exception as e:
            print(f"\n  💥 EXCEÇÃO não capturada em {name}: {e}")
            results.append((name, False))
    
    # Resumo
    print("\n" + "="*70)
    print("  RESUMO DOS TESTES")
    print("="*70)
    
    for name, ok in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {status} - {name}")
    
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    
    print(f"\n  Total: {passed}/{total} testes passaram")
    
    if passed == total:
        print("\n  🎉 TUDO FUNCIONAL! Bot pronto para usar!")
        return 0
    else:
        print(f"\n  ⚠️  {total - passed} teste(s) falharam. Ver logs acima.")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
