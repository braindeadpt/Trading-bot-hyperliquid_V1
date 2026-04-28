"""
Aplica Remote Analysis API ao bot Flask.
Adiciona endpoints para leitura remota de logs, ficheiros, DB e config.

Uso:
    python apply_remote_analysis.py

O que faz:
    1. Verifica se main.py existe
    2. Adiciona import de api_extensions
    3. Adiciona register_analysis_routes(app, project_dir)
    4. Cria backup de main.py
"""

import os
import sys
import shutil

def apply():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    main_path = os.path.join(base_dir, "src", "main.py")
    
    if not os.path.exists(main_path):
        print(f"❌ main.py não encontrado em {main_path}")
        return False
    
    # Backup
    backup = main_path + ".backup_analysis"
    if not os.path.exists(backup):
        shutil.copy2(main_path, backup)
        print(f"✅ Backup criado: {backup}")
    
    with open(main_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Verificar se já está aplicado
    if "api_extensions" in content:
        print("⚠️  api_extensions já importado — pulando")
        return True
    
    # Adicionar import
    if "from flask import" in content:
        # Adicionar após o último import
        lines = content.split("\n")
        import_idx = None
        for i, line in enumerate(lines):
            if line.startswith("from ") or line.startswith("import "):
                import_idx = i
        
        if import_idx is not None:
            lines.insert(import_idx + 1, "from api_extensions import register_analysis_routes")
            print("✅ Import adicionado")
        else:
            print("❌ Não encontrou local para import")
            return False
    else:
        print("❌ Flask não encontrado em main.py")
        return False
    
    # Encontrar onde app.run() ou app = Flask() é chamado
    content = "\n".join(lines)
    
    # Procurar por padrões comuns de inicialização Flask
    patterns = [
        "app.run(",
        "if __name__ == '__main__':",
        "if __name__ == \"__main__\":",
    ]
    
    applied = False
    for pattern in patterns:
        if pattern in content:
            # Inserir antes do app.run ou do if __name__
            idx = content.find(pattern)
            if idx != -1:
                # Inserir linha de registo antes
                content = content[:idx] + "    # Register remote analysis endpoints\n    register_analysis_routes(app, os.path.dirname(os.path.abspath(__file__)))\n\n    " + content[idx:]
                applied = True
                print("✅ register_analysis_routes() adicionado")
                break
    
    if not applied:
        print("❌ Não conseguiu encontrar local para registar rotas")
        return False
    
    with open(main_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    print("✅ main.py atualizado com sucesso!")
    print("\nPróximo passo: reiniciar o bot")
    return True

if __name__ == "__main__":
    if apply():
        sys.exit(0)
    else:
        sys.exit(1)
