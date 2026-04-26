#!/usr/bin/env python3
"""
Script de aplicação automática das otimizações v2.
Copia os módulos _v2 para as localizações corretas, criando backups.
"""
import shutil
import os
import sys
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent.parent
OPT_DIR = BASE_DIR / "refactored" / "optimized"
REF_DIR = BASE_DIR / "refactored"
BACKUP_SUFFIX = ".bak"

# Mapa: (origem_v2, destino)
# FASE 1 — Infraestrutura Base
PHASE1 = [
    ("event_bus_v2.py", REF_DIR / "core" / "event_bus.py"),
    ("cache_v2.py", REF_DIR / "data" / "cache.py"),
    ("database_v2.py", REF_DIR / "data" / "database.py"),
]

# FASE 2 — Dados + Agregação
PHASE2 = [
    ("aggregator_v2.py", REF_DIR / "data" / "aggregator.py"),
    ("strategy_v2.py", REF_DIR / "strategy" / "ghost.py"),
]

# FASE 3 — Motor + UI
PHASE3 = [
    ("terminal_v2.py", REF_DIR / "cli" / "terminal.py"),
    ("webapp_v2.py", REF_DIR / "web" / "app.py"),
    ("engine_v2.py", REF_DIR / "core" / "engine.py"),
]

ALL_PHASES = PHASE1 + PHASE2 + PHASE3


def backup_file(path: Path) -> Path:
    """Cria backup com timestamp."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = Path(str(path) + f"{BACKUP_SUFFIX}.{ts}")
    if path.exists():
        shutil.copy2(path, backup_path)
        print(f"  📦 Backup: {backup_path.name}")
    return backup_path


def apply_v2(source_name: str, dest_path: Path) -> bool:
    """Copia ficheiro v2 para destino, criando backup primeiro."""
    source = OPT_DIR / source_name
    
    if not source.exists():
        print(f"  ❌ Ficheiro fonte não encontrado: {source}")
        return False
    
    # Backup
    backup_file(dest_path)
    
    # Copiar
    shutil.copy2(source, dest_path)
    print(f"  ✅ Aplicado: {source_name} → {dest_path.relative_to(BASE_DIR)}")
    return True


def verify_imports(path: Path) -> bool:
    """Verifica se o ficheiro Python tem erros de sintaxe básicos."""
    try:
        content = path.read_text()
        compile(content, str(path), 'exec')
        return True
    except SyntaxError as e:
        print(f"  ⚠️  Erro de sintaxe em {path}: {e}")
        return False


def main():
    print("=" * 60)
    print("⚡ APLICAR OTIMIZAÇÕES v2 — Hyperliquid Bot")
    print("=" * 60)
    
    if not OPT_DIR.exists():
        print(f"❌ Pasta de otimizações não encontrada: {OPT_DIR}")
        sys.exit(1)
    
    results = {"ok": 0, "fail": 0}
    
    # FASE 1
    print("\n🏗️  FASE 1 — Infraestrutura Base (EventBus, Cache, Database)")
    for src, dst in PHASE1:
        if apply_v2(src, dst):
            results["ok"] += 1
        else:
            results["fail"] += 1
    
    # FASE 2
    print("\n📊 FASE 2 — Dados + Estratégia (Aggregator, Ghost)")
    for src, dst in PHASE2:
        if apply_v2(src, dst):
            results["ok"] += 1
        else:
            results["fail"] += 1
    
    # FASE 3
    print("\n🚀 FASE 3 — Motor + UI (Terminal, WebApp, Engine)")
    for src, dst in PHASE3:
        if apply_v2(src, dst):
            results["ok"] += 1
        else:
            results["fail"] += 1
    
    # Verificação
    print("\n🔍 VERIFICAÇÃO — Compilação de sintaxe")
    for _, dst in ALL_PHASES:
        if dst.exists():
            ok = verify_imports(dst)
            status = "✅ OK" if ok else "❌ FAIL"
            print(f"  {status} {dst.name}")
    
    # Resumo
    print("\n" + "=" * 60)
    print(f"📊 RESULTADO: {results['ok']}/{results['ok'] + results['fail']} otimizações aplicadas")
    if results["fail"] == 0:
        print("🎉 TODAS AS OTIMIZAÇÕES APLICADAS COM SUCESSO!")
        print("\nPróximo passo: python verify_refactored.py")
    else:
        print(f"⚠️  {results['fail']} falhas — verificar logs acima")
    print("=" * 60)


if __name__ == "__main__":
    main()
