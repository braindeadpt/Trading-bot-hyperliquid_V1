"""
Remote Analysis API — Adiciona endpoints ao bot Flask para análise completa remota.
Permite ler logs, ficheiros, base de dados, e configuração via ngrok.

Instruções:
1. Copiar para src/api_extensions.py
2. Em main.py ou onde o app Flask é criado, adicionar:
   from api_extensions import register_analysis_routes
   register_analysis_routes(app, project_dir)

Endpoints adicionados:
- GET /api/logs          → Últimas N linhas do log do bot
- GET /api/files         → Listar ficheiros num diretório
- GET /api/file          → Ler conteúdo de ficheiro
- GET /api/db            → Query à base de dados SQLite
- GET /api/config        → Ler settings.yaml
- GET /api/rejections    → Sinais rejeitados com detalhes completos
- GET /api/all_signals   → TODOS os sinais (não só recentes)
"""

import os
import glob
import sqlite3
from flask import jsonify, request
from datetime import datetime, timedelta


def register_analysis_routes(app, project_dir):
    """Regista todos os endpoints de análise no app Flask."""
    
    LOG_DIR = os.path.join(project_dir, "logs")
    DATA_DIR = os.path.join(project_dir, "data")
    CONFIG_DIR = os.path.join(project_dir, "config")
    SRC_DIR = os.path.join(project_dir, "src")
    
    @app.route("/api/logs", methods=["GET"])
    def api_logs():
        """Retorna as últimas N linhas do ficheiro de log do bot."""
        try:
            lines = int(request.args.get("lines", 200))
            log_file = request.args.get("file", "bot.log")
            
            log_path = os.path.join(LOG_DIR, log_file)
            
            if not os.path.exists(log_path):
                # Tentar encontrar qualquer ficheiro .log
                log_files = glob.glob(os.path.join(LOG_DIR, "*.log"))
                if log_files:
                    log_path = max(log_files, key=os.path.getmtime)
                else:
                    return jsonify({"error": f"Nenhum log encontrado em {LOG_DIR}"}), 404
            
            # Ler últimas N linhas
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
            
            return jsonify({
                "log_file": os.path.basename(log_path),
                "total_lines": len(all_lines),
                "returned_lines": len(recent),
                "lines": [line.rstrip() for line in recent]
            })
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route("/api/files", methods=["GET"])
    def api_files():
        """Lista ficheiros num diretório do projeto."""
        try:
            directory = request.args.get("dir", "src")
            max_depth = int(request.args.get("depth", 2))
            
            # Validar e construir path seguro
            base_dirs = {
                "src": SRC_DIR,
                "config": CONFIG_DIR,
                "data": DATA_DIR,
                "logs": LOG_DIR,
                "root": project_dir,
            }
            
            if directory in base_dirs:
                target_dir = base_dirs[directory]
            else:
                # Path relativo dentro do projeto
                target_dir = os.path.normpath(os.path.join(project_dir, directory))
                # Segurança: garantir que não sai do project_dir
                if not target_dir.startswith(os.path.normpath(project_dir)):
                    return jsonify({"error": "Acesso negado — fora do projeto"}), 403
            
            files = []
            for root, dirs, filenames in os.walk(target_dir):
                depth = root.count(os.sep) - target_dir.count(os.sep)
                if depth >= max_depth:
                    del dirs[:]
                    continue
                for f in filenames:
                    fpath = os.path.join(root, f)
                    files.append({
                        "name": f,
                        "path": os.path.relpath(fpath, project_dir),
                        "size": os.path.getsize(fpath),
                        "modified": datetime.fromtimestamp(os.path.getmtime(fpath)).isoformat()
                    })
            
            return jsonify({
                "directory": directory,
                "total_files": len(files),
                "files": sorted(files, key=lambda x: x["modified"], reverse=True)[:100]
            })
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route("/api/file", methods=["GET"])
    def api_file():
        """Ler conteúdo de um ficheiro do projeto."""
        try:
            filepath = request.args.get("path")
            if not filepath:
                return jsonify({"error": "Parâmetro 'path' é obrigatório"}), 400
            
            # Construir path seguro
            full_path = os.path.normpath(os.path.join(project_dir, filepath))
            
            # Segurança: garantir que não sai do project_dir
            if not full_path.startswith(os.path.normpath(project_dir)):
                return jsonify({"error": "Acesso negado — fora do projeto"}), 403
            
            if not os.path.exists(full_path) or os.path.isdir(full_path):
                return jsonify({"error": f"Ficheiro não encontrado: {filepath}"}), 404
            
            max_size = 500 * 1024  # 500KB max
            file_size = os.path.getsize(full_path)
            
            if file_size > max_size:
                # Ler apenas as últimas 500KB
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(max(0, file_size - max_size))
                    content = f.read()
                truncated = True
            else:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                truncated = False
            
            return jsonify({
                "path": filepath,
                "size": file_size,
                "truncated": truncated,
                "content": content
            })
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route("/api/db", methods=["GET"])
    def api_db():
        """Query à base de dados SQLite do bot."""
        try:
            db_file = request.args.get("file", "trading_bot.db")
            sql = request.args.get("sql", "SELECT * FROM signals ORDER BY id DESC LIMIT 20")
            
            db_path = os.path.join(DATA_DIR, db_file)
            
            if not os.path.exists(db_path):
                # Tentar encontrar qualquer .db
                db_files = glob.glob(os.path.join(DATA_DIR, "*.db"))
                if db_files:
                    db_path = db_files[0]
                else:
                    return jsonify({"error": f"Nenhuma base de dados em {DATA_DIR}"}), 404
            
            # Segurança: só SELECT permitido
            sql_clean = sql.strip().upper()
            if not sql_clean.startswith("SELECT"):
                return jsonify({"error": "Apenas queries SELECT são permitidas"}), 403
            
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            conn.close()
            
            # Converter para dict
            results = []
            for row in rows:
                results.append({key: row[key] for key in row.keys()})
            
            return jsonify({
                "database": os.path.basename(db_path),
                "query": sql,
                "rows_returned": len(results),
                "rows": results
            })
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route("/api/config", methods=["GET"])
    def api_config():
        """Ler settings.yaml e outras configs."""
        try:
            config_file = request.args.get("file", "settings.yaml")
            config_path = os.path.join(CONFIG_DIR, config_file)
            
            if not os.path.exists(config_path):
                return jsonify({"error": f"Config não encontrada: {config_file}"}), 404
            
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            # Tentar parsear como YAML
            try:
                import yaml
                parsed = yaml.safe_load(content)
            except ImportError:
                parsed = {"_note": "PyYAML não instalado — conteúdo em raw"}
            except Exception:
                parsed = None
            
            return jsonify({
                "file": config_file,
                "content": content,
                "parsed": parsed
            })
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route("/api/rejections", methods=["GET"])
    def api_rejections():
        """Sinais rejeitados com detalhes completos."""
        try:
            days = int(request.args.get("days", 7))
            limit = int(request.args.get("limit", 100))
            
            db_path = os.path.join(DATA_DIR, "trading_bot.db")
            if not os.path.exists(db_path):
                return jsonify({"error": "Base de dados não encontrada"}), 404
            
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Query sinais rejeitados
            cursor.execute("""
                SELECT * FROM signals 
                WHERE executed = 0 
                AND timestamp > ? 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (datetime.now() - timedelta(days=days), limit))
            
            rows = cursor.fetchall()
            conn.close()
            
            rejections = []
            for row in rows:
                rejections.append({key: row[key] for key in row.keys()})
            
            # Agrupar por motivo
            from collections import Counter
            reasons = Counter(r.get("reason", "unknown") for r in rejections)
            
            return jsonify({
                "days": days,
                "total_rejections": len(rejections),
                "by_reason": dict(reasons.most_common(20)),
                "rejections": rejections
            })
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route("/api/all_signals", methods=["GET"])
    def api_all_signals():
        """Todos os sinais (aceites e rejeitados) com filtros."""
        try:
            days = int(request.args.get("days", 7))
            limit = int(request.args.get("limit", 200))
            executed_only = request.args.get("executed", None)
            
            db_path = os.path.join(DATA_DIR, "trading_bot.db")
            if not os.path.exists(db_path):
                # Fallback: retornar do endpoint /api/signals existente
                return jsonify({"error": "Base de dados não disponível, use /api/signals"}), 404
            
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            query = "SELECT * FROM signals WHERE timestamp > ?"
            params = [(datetime.now() - timedelta(days=days))]
            
            if executed_only is not None:
                query += " AND executed = ?"
                params.append(int(executed_only))
            
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            conn.close()
            
            signals = []
            for row in rows:
                signals.append({key: row[key] for key in row.keys()})
            
            # Estatísticas
            total = len(signals)
            executed = sum(1 for s in signals if s.get("executed") == 1)
            rejected = total - executed
            
            return jsonify({
                "days": days,
                "total": total,
                "executed": executed,
                "rejected": rejected,
                "signals": signals
            })
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route("/api/analysis/summary", methods=["GET"])
    def api_analysis_summary():
        """Resumo completo do estado do bot para análise rápida."""
        try:
            summary = {
                "timestamp": datetime.now().isoformat(),
                "project_dir": project_dir,
            }
            
            # Verificar ficheiros existentes
            for name, dir_path in [
                ("src", SRC_DIR),
                ("config", CONFIG_DIR),
                ("data", DATA_DIR),
                ("logs", LOG_DIR),
            ]:
                if os.path.exists(dir_path):
                    files = glob.glob(os.path.join(dir_path, "**"), recursive=True)
                    files = [f for f in files if os.path.isfile(f)]
                    summary[name] = {
                        "exists": True,
                        "file_count": len(files),
                        "files": [os.path.relpath(f, project_dir) for f in files[:20]]
                    }
                else:
                    summary[name] = {"exists": False}
            
            # Último log
            log_files = glob.glob(os.path.join(LOG_DIR, "*.log"))
            if log_files:
                latest_log = max(log_files, key=os.path.getmtime)
                summary["latest_log"] = {
                    "file": os.path.basename(latest_log),
                    "size": os.path.getsize(latest_log),
                    "modified": datetime.fromtimestamp(os.path.getmtime(latest_log)).isoformat()
                }
            
            # Base de dados
            db_files = glob.glob(os.path.join(DATA_DIR, "*.db"))
            summary["databases"] = [os.path.basename(f) for f in db_files]
            
            return jsonify(summary)
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    print("✅ Endpoints de análise remota registados:")
    print("   GET /api/logs?lines=200&file=bot.log")
    print("   GET /api/files?dir=src&depth=2")
    print("   GET /api/file?path=src/paper_trading.py")
    print("   GET /api/db?sql=SELECT+*+FROM+signals+LIMIT+20")
    print("   GET /api/config?file=settings.yaml")
    print("   GET /api/rejections?days=7&limit=100")
    print("   GET /api/all_signals?days=7&limit=200")
    print("   GET /api/analysis/summary")
