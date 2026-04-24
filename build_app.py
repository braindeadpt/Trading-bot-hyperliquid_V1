"""
Build Script — Cria executável .exe para Windows

Como usar:
    python build_app.py

Output:
    dist/HyperliquidBot/     # Pasta com .exe + dependências
    dist/HyperliquidBot.exe  # Executável standalone

Requisitos:
    python -m pip install pyinstaller
"""
import subprocess
import sys
import shutil
from pathlib import Path


def main():
    print("=" * 60)
    print("  🏗️  BUILD — Hyperliquid Bot App Desktop")
    print("=" * 60)
    
    # Verificar PyInstaller
    try:
        import PyInstaller
    except ImportError:
        print("❌ PyInstaller não instalado!")
        print("   Instala: python -m pip install pyinstaller")
        sys.exit(1)
    
    # Limpar builds anteriores
    for folder in ["build", "dist"]:
        if Path(folder).exists():
            print(f"🧹 A limpar {folder}/...")
            shutil.rmtree(folder)
    
    # Ficheiros a incluir
    datas = [
        ("dashboard.html", "."),
        ("config/settings.yaml", "config"),
        ("src", "src"),
        ("data", "data"),
        ("logs", "logs"),
    ]
    
    # Argumentos do PyInstaller
    args = [
        "pyinstaller",
        "--name=HyperliquidBot",
        "--onefile",           # .exe único (ou --onedir para pasta)
        "--windowed",          # Sem consola
        "--icon=NONE",         # TODO: Adicionar ícone
        "--add-data", f"dashboard.html{os.pathsep}.",
        "--add-data", f"config/settings.yaml{os.pathsep}config",
        "--add-data", f"src{os.pathsep}src",
        "--hidden-import", "src.data_aggregator",
        "--hidden-import", "src.paper_trading",
        "--hidden-import", "src.database",
        "--hidden-import", "src.strategy",
        "--hidden-import", "src.risk_manager",
        "--hidden-import", "src.exchange_client",
        "--hidden-import", "src.utils",
        "app_desktop.py",
    ]
    
    print("🔨 A compilar...")
    print(f"   Comando: {' '.join(args)}")
    print()
    
    result = subprocess.run(args, capture_output=False)
    
    if result.returncode == 0:
        print()
        print("=" * 60)
        print("  ✅ BUILD CONCLUÍDO!")
        print("=" * 60)
        print()
        print("  Output: dist/HyperliquidBot.exe")
        print()
        print("  Como usar:")
        print("    1. Copia dist/HyperliquidBot.exe para o teu PC")
        print("    2. Double-click para iniciar")
        print("    3. Bot corre em background com ícone na tray")
        print()
    else:
        print()
        print("❌ Build falhou!")
        sys.exit(1)


if __name__ == "__main__":
    import os
    main()
