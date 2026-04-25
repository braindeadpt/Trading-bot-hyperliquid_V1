"""
Audit Script - Verifica integridade do código e prioridade de Volume + OI
Como usar:
    python audit.py
"""
import sys
import ast
import os
from pathlib import Path
from typing import List, Dict, Set

sys.path.insert(0, str(Path(__file__).parent / "src"))

def audit_file(filepath: Path) -> Dict:
    """Audita um ficheiro Python"""
    issues = []
    warnings = []
    info = []
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
        
        tree = ast.parse(source)
        
        # Verificar imports problemáticos
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) or isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == 'asyncio' and 'aiohttp' not in source:
                        warnings.append("Usa asyncio mas não aiohttp — possível bloqueio")
        
        # Verificar funções críticas
        functions = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        
        # Verificar variáveis relacionadas com volume/OI
        volume_refs = source.count('volume') + source.count('Volume')
        oi_refs = source.count('oi') + source.count('OI') + source.count('open_interest')
        price_refs = source.count('price') + source.count('Price')
        
        info.append(f"Referências: volume={volume_refs}, oi={oi_refs}, price={price_refs}")
        
        if oi_refs < 5 and 'paper' not in filepath.name:
            warnings.append("Poucas referências a OI — pode não estar a usar Open Interest")
        
        # Verificar hardcoded values
        if '0.02' in source and 'stop_loss' in source.lower():
            warnings.append("Possível stop loss hardcoded em 2% — verificar config")
        
        # Verificar race conditions potenciais
        if 'threading' in source and 'Lock' not in source:
            warnings.append("Usa threading sem Lock — possível race condition")
        
    except Exception as e:
        issues.append(f"Erro a parsear: {e}")
    
    return {
        'file': filepath.name,
        'issues': issues,
        'warnings': warnings,
        'info': info
    }

def audit_strategy_priority():
    """Verifica se Volume + OI são métricas principais na estratégia"""
    print("\n" + "=" * 60)
    print("  🔍 AUDIT: Volume + OI Priority Check")
    print("=" * 60)
    
    strategy_path = Path(__file__).parent / "src" / "strategy.py"
    paper_path = Path(__file__).parent / "src" / "paper_trading.py"
    
    # Ler strategy.py
    with open(strategy_path, 'r', encoding='utf-8') as f:
        strategy_code = f.read()
    
    # Verificar ordem de prioridade nos checks
    checks = []
    
    # Procurar padrão de volume check
    if 'volume_ratio' in strategy_code or 'volume_ratio' in open(paper_path).read():
        checks.append(("✅", "Volume ratio check", "HIGH"))
    else:
        checks.append(("❌", "Volume ratio check", "MISSING"))
    
    # Procurar OI check
    if 'oi_change' in strategy_code or 'oi_change' in open(paper_path).read():
        checks.append(("✅", "OI change check", "HIGH"))
    else:
        checks.append(("❌", "OI change check", "MISSING"))
    
    # Verificar se funding é secundário (deve ser)
    if 'funding' in strategy_code:
        # Verificar se funding é usado como filtro (não como sinal principal)
        if 'funding_extreme' in strategy_code or 'max_funding' in strategy_code:
            checks.append(("✅", "Funding usado como filtro (não sinal principal)", "OK"))
        else:
            checks.append(("⚠️", "Funding pode ser usado como sinal principal", "CHECK"))
    
    # Verificar SMA
    if 'sma' in strategy_code.lower() or 'sma' in open(paper_path).read().lower():
        checks.append(("✅", "SMA usado para direção de trend", "OK"))
    
    for status, check, priority in checks:
        print(f"  {status} {check} — {priority}")
    
    # Verificar se OI é carregado em data_aggregator
    agg_path = Path(__file__).parent / "src" / "data_aggregator.py"
    if agg_path.exists():
        with open(agg_path, 'r', encoding='utf-8') as f:
            agg_code = f.read()
        
        if 'oi' in agg_code.lower() or 'open_interest' in agg_code.lower():
            print(f"\n  ✅ data_aggregator.py carrega OI")
        else:
            print(f"\n  ❌ data_aggregator.py NÃO carrega OI — PROBLEMA!")
    
    print("=" * 60)

def main():
    print("\n" + "=" * 60)
    print("  🔍 CODE AUDIT v1.0")
    print("=" * 60)
    
    src_dir = Path(__file__).parent / "src"
    
    files_to_audit = [
        src_dir / "strategy.py",
        src_dir / "paper_trading.py",
        src_dir / "data_aggregator.py",
        src_dir / "database.py",
        src_dir / "main.py",
    ]
    
    total_issues = 0
    total_warnings = 0
    
    for filepath in files_to_audit:
        if not filepath.exists():
            print(f"\n  ⚠️  Ficheiro não encontrado: {filepath.name}")
            continue
        
        result = audit_file(filepath)
        
        print(f"\n  📄 {result['file']}")
        
        if result['info']:
            for i in result['info']:
                print(f"     ℹ️  {i}")
        
        if result['warnings']:
            for w in result['warnings']:
                print(f"     ⚠️  {w}")
                total_warnings += 1
        
        if result['issues']:
            for i in result['issues']:
                print(f"     ❌ {i}")
                total_issues += 1
        
        if not result['warnings'] and not result['issues']:
            print(f"     ✅ OK")
    
    # Audit de prioridade Volume + OI
    audit_strategy_priority()
    
    # Resumo
    print("\n" + "=" * 60)
    print("  📊 RESUMO DO AUDIT")
    print("=" * 60)
    print(f"  Issues: {total_issues}")
    print(f"  Warnings: {total_warnings}")
    
    if total_issues == 0 and total_warnings <= 3:
        print(f"\n  ✅ Código auditado — Estado BOM")
    elif total_issues == 0:
        print(f"\n  ⚠️  Código auditado — Alguns warnings para revisar")
    else:
        print(f"\n  ❌ Código auditado — Issues críticos encontrados")
    
    print("=" * 60 + "\n")

if __name__ == "__main__":
    main()
