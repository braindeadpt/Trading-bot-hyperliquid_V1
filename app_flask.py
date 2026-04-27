"""
Hyperliquid Bot — App Flask + System Tray
Corre em background, abre dashboard no browser nativo, ícone na tray

Como usar:
    python app_flask.py

Requisitos (já tens):
    pip install flask pystray pillow
"""
import threading
import time
import json
import logging
import sys
import os
import webbrowser
from pathlib import Path
from datetime import datetime

# ========== WINDOWS UTF-8 FIX ==========
# Forçar UTF-8 no ambiente Python
import os, sys
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
# ======================================

from flask import Flask, jsonify, send_from_directory, request

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    print("⚠️ pystray/pillow não instalado — system tray desativado")

# Adicionar src/ e raiz ao path
sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from bot_engine import BotEngine, get_bot_status, start_bot_engine, stop_bot_engine, app_state
from utils import load_config, setup_logging
from database import BotDatabase
from performance_tracker import PerformanceTracker

logger = logging.getLogger(__name__)

# Flask app
flask_app = Flask(__name__, static_folder=None)  # Segurança: não expor ficheiros do projeto

# Estado
engine = None
config = None

# =============================================================================
# FLASK ROUTES
# =============================================================================

@flask_app.route('/')
def index():
    """Serve o dashboard.html"""
    return send_from_directory('.', 'dashboard.html')

@flask_app.route('/bridge.js')
def bridge_js():
    """Serve o bridge.js (vazio em modo Flask — usamos fetch API)"""
    return "// Modo Flask — não necessário\n", 200, {'Content-Type': 'application/javascript'}

@flask_app.route('/api/status')
def api_status():
    """Retorna estado actual do bot"""
    status = get_bot_status()
    data = app_state.get("last_data", {})
    hl_data = data.get('exchanges_data', {}).get('hyperliquid', {})
    
    return jsonify({
        "running": status["running"],
        "price": status["price"],
        "mark_price": hl_data.get('mark_price', status["price"]),
        "oracle_price": hl_data.get('oracle_price', status["price"]),
        "oi": data.get('oi_total', 0),
        "oi_usd": data.get('oi_total', 0) * status["price"] if status["price"] > 0 else 0,
        "funding": data.get('funding_avg', 0),
        "volume": data.get('volume_total', 0),
        "asset": status["asset"],
        "update_count": status["update_count"],
        "capital": status["capital"],
        "equity": status["equity"],
        "position": app_state.get("current_position"),
    })

@flask_app.route('/api/logs')
def api_logs():
    """Retorna logs recentes"""
    limit = request.args.get('limit', 100, type=int)
    logs = app_state.get("logs", [])
    return jsonify(logs[-limit:] if logs else [])

@flask_app.route('/api/trades')
def api_trades():
    """Retorna trades recentes"""
    limit = request.args.get('limit', 50, type=int)
    db = app_state.get("db")
    if db:
        try:
            trades = db.get_recent_trades(limit=limit)
            return jsonify(trades)
        except:
            pass
    return jsonify(app_state.get("trades", [])[-limit:])

@flask_app.route('/api/start', methods=['POST'])
def api_start():
    """Inicia o bot"""
    global engine, config
    if not app_state.get("bot_running"):
        success = start_bot_engine(config)
        return jsonify({"success": success, "message": "Bot iniciado" if success else "Falha"})
    return jsonify({"success": False, "message": "Já está a correr"})

@flask_app.route('/api/stop', methods=['POST'])
def api_stop():
    """Para o bot"""
    stop_bot_engine()
    return jsonify({"success": True, "message": "Bot parado"})

@flask_app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """GET/POST configuração"""
    if request.method == 'POST':
        cfg = request.get_json()
        # Guardar config
        config_path = Path(__file__).parent / "config" / "settings.json"
        try:
            with open(config_path, 'w') as f:
                json.dump(cfg, f, indent=2)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    else:
        # Retorna config actual
        return jsonify(config or {})

@flask_app.route('/api/force/long', methods=['POST'])
def api_force_long():
    """Força posição long"""
    trader = app_state.get("trader")
    if trader and app_state.get("bot_running"):
        return jsonify({"success": False, "message": "Ainda não implementado"})
    return jsonify({"success": False, "message": "Bot não está a correr"})

@flask_app.route('/api/force/short', methods=['POST'])
def api_force_short():
    """Força posição short"""
    trader = app_state.get("trader")
    if trader and app_state.get("bot_running"):
        return jsonify({"success": False, "message": "Ainda não implementado"})
    return jsonify({"success": False, "message": "Bot não está a correr"})

@flask_app.route('/api/emergency', methods=['POST'])
def api_emergency():
    """Fecha posição de emergência"""
    trader = app_state.get("trader")
    if trader:
        return jsonify({"success": False, "message": "Ainda não implementado"})
    return jsonify({"success": False, "message": "Sem posição aberta"})


# =============================================================================
# API DE DEBUG — Logs MTF em tempo real
# =============================================================================

@flask_app.route('/api/mtf-debug')
def api_mtf_debug():
    """Retorna últimos logs MTF do ficheiro de logs para debug remoto"""
    try:
        log_file = Path(__file__).parent / "logs" / "bot.log"
        if not log_file.exists():
            return jsonify({"error": "Ficheiro de logs não encontrado"})
        
        # Ler últimas 200 linhas
        lines = []
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            all_lines = f.readlines()
            # Pegar últimas 200 linhas
            lines = all_lines[-200:] if len(all_lines) > 200 else all_lines
        
        # Filtrar linhas relevantes (MTF, signal, entry, skip, HTF, position)
        keywords = ['MTF', 'SIGNAL', 'ENTRY', 'EXIT', 'skip', 'HTF', 'Position', 
                    'OPENED', 'CLOSED', 'entry', 'cooldown', 'momentum',
                    'volume_ratio', 'bull', 'bear', 'long', 'short']
        
        filtered = []
        for line in lines:
            line = line.strip()
            if line and any(kw in line for kw in keywords):
                filtered.append(line)
        
        # Limitar a 100 linhas filtradas
        filtered = filtered[-100:] if len(filtered) > 100 else filtered
        
        return jsonify({
            "mtf_logs": filtered,
            "total_lines_read": len(lines),
            "filtered_lines": len(filtered),
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        return jsonify({"error": str(e)})


# =============================================================================
# NOVOS ENDPOINTS v2.0 — DADOS REAIS DA BASE DE DADOS
# =============================================================================

@flask_app.route('/api/performance')
def api_performance():
    """Retorna performance real da base de dados"""
    asset = request.args.get('asset', 'BTC')
    days = request.args.get('days', 7, type=int)
    
    db = BotDatabase()
    tracker = PerformanceTracker(db)
    
    # Sumário dos últimos N dias
    summary = tracker.generate_summary(asset, days)
    
    # Último relatório diário
    latest = tracker.generate_daily_report(asset)
    
    return jsonify({
        'summary': summary,
        'latest': latest,
        'asset': asset,
        'days': days
    })

@flask_app.route('/api/signals')
def api_signals():
    """Retorna sinais da base de dados (aceites e rejeitados)"""
    asset = request.args.get('asset', 'BTC')
    days = request.args.get('days', 1, type=int)
    executed_only = request.args.get('executed_only', 'false').lower() == 'true'
    limit = request.args.get('limit', 100, type=int)
    
    # Calcular timestamp de início
    from datetime import datetime, timedelta
    start_time = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    
    db = BotDatabase()
    signals = db.get_signals(
        asset=asset,
        start_time=start_time,
        executed_only=executed_only,
        limit=limit
    )
    
    # Análise rápida
    total = len(signals)
    executed = len([s for s in signals if s.get('executed')])
    rejected = total - executed
    
    # Razões de rejeição
    rejection_reasons = {}
    for s in signals:
        if not s.get('executed'):
            reason = s.get('reason', 'unknown')
            if 'FILTER:' in reason:
                key = reason.replace('FILTER:', '').strip().split('|')[0].strip()
            elif 'VP_BLOCK' in reason:
                key = 'volume_profile'
            else:
                key = reason
            rejection_reasons[key] = rejection_reasons.get(key, 0) + 1
    
    return jsonify({
        'signals': signals,
        'stats': {
            'total': total,
            'executed': executed,
            'rejected': rejected,
            'execution_rate': round((executed / total * 100), 1) if total > 0 else 0,
            'rejection_reasons': rejection_reasons,
            'long_signals': len([s for s in signals if s.get('signal_type') == 'LONG']),
            'short_signals': len([s for s in signals if s.get('signal_type') == 'SHORT']),
            'exit_signals': len([s for s in signals if s.get('signal_type') == 'EXIT'])
        },
        'asset': asset,
        'days': days
    })

@flask_app.route('/api/regime')
def api_regime():
    """Retorna regime de mercado recente"""
    asset = request.args.get('asset', 'BTC')
    limit = request.args.get('limit', 24, type=int)
    
    db = BotDatabase()
    regimes = db.get_market_regime(asset, limit=limit)
    
    return jsonify({
        'regimes': regimes,
        'count': len(regimes),
        'asset': asset
    })

@flask_app.route('/api/stats')
def api_stats():
    """Retorna estatísticas gerais da base de dados"""
    db = BotDatabase()
    stats = db.get_stats_v2()
    
    return jsonify(stats)


# =============================================================================
# SYSTEM TRAY
# =============================================================================

def create_tray_icon():
    """Cria ícone para system tray"""
    width = 64
    height = 64
    image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    dc = ImageDraw.Draw(image)
    dc.ellipse([4, 4, width-4, height-4], fill="#00ff88", outline="#00cc66", width=2)
    return image

def setup_tray():
    """Configura system tray icon"""
    if not HAS_TRAY:
        return None
    
    def on_open(icon, item):
        webbrowser.open('http://127.0.0.1:5000')
    
    def on_start(icon, item):
        api_start()
    
    def on_stop(icon, item):
        api_stop()
    
    def on_status(icon, item):
        status = get_bot_status()
        print(f"🤖 Bot: {'Running' if status['running'] else 'Stopped'}")
        print(f"💰 Capital: ${status['capital']:,.2f}")
        print(f"📊 Price: ${status['price']:,.2f}")
    
    def on_quit(icon, item):
        api_stop()
        icon.stop()
        os._exit(0)
    
    menu = pystray.Menu(
        pystray.MenuItem("🚀 Abrir Dashboard", on_open),
        pystray.MenuItem("▶ Iniciar Bot", on_start),
        pystray.MenuItem("⏹ Parar Bot", on_stop),
        pystray.MenuItem("📊 Status (terminal)", on_status),
        pystray.MenuItem("───", lambda icon, item: None, enabled=False),
        pystray.MenuItem("❌ Sair", on_quit),
    )
    
    icon = pystray.Icon(
        "hyperliquid-bot",
        create_tray_icon(),
        "🟢 Hyperliquid Bot (Running)" if app_state.get("bot_running") else "🔴 Hyperliquid Bot (Stopped)",
        menu
    )
    
    threading.Thread(target=icon.run, daemon=True).start()
    return icon

# =============================================================================
# MONITOR LOOP
# =============================================================================

def monitor_loop():
    """Actualiza estado global a cada 2 segundos"""
    while True:
        try:
            if app_state.get("bot_running") and app_state.get("trader"):
                trader = app_state["trader"]
                
                # Actualizar posição
                if trader.current_position:
                    app_state["current_position"] = {
                        "direction": trader.current_position.upper(),
                        "entryPrice": trader.entry_price,
                        "size": trader.position_size,
                        "stopLoss": trader.entry_price * (1 - trader.stop_loss_pct),
                        "trailingStop": trader.trailing_stop,
                        "openTime": trader.entry_time,
                    }
                else:
                    app_state["current_position"] = None
                
                # Actualizar capital
                app_state["capital"] = trader.capital
                
                # Equity history
                if not app_state.get("equity_history"):
                    app_state["equity_history"] = [trader.capital]
                elif app_state["equity_history"][-1] != trader.capital:
                    app_state["equity_history"].append(trader.capital)
                    if len(app_state["equity_history"]) > 500:
                        app_state["equity_history"] = app_state["equity_history"][-500:]
                
                # Actualizar trades
                db = app_state.get("db")
                if db:
                    try:
                        recent_trades = db.get_recent_trades(limit=50)
                        app_state["trades"] = recent_trades
                    except:
                        pass
            
            time.sleep(2)
        except Exception as e:
            logger.warning(f"Erro no monitor: {e}")
            time.sleep(5)

# =============================================================================
# MAIN
# =============================================================================

def main():
    global config, engine
    
    print("=" * 60)
    print("  🚀 HYPERLIQUID MOMENTUM BOT — APP FLASK + TRAY")
    print("=" * 60)
    print()
    print("  A iniciar...")
    print()
    
    # Config
    config = load_config()
    setup_logging(level="INFO", log_file="logs/bot.log")
    
    # System tray
    tray_icon = setup_tray()
    
    # Iniciar bot automaticamente
    success = start_bot_engine(config)
    if success:
        logger.info("✅ Bot iniciado automaticamente")
        if tray_icon:
            tray_icon.title = "🟢 Hyperliquid Bot (Running)"
    
    # Monitor loop
    threading.Thread(target=monitor_loop, daemon=True).start()
    
    # Abrir browser
    print("  🌐 A abrir dashboard no browser...")
    webbrowser.open('http://127.0.0.1:5000')
    
    print()
    print("  ✅ Bot a correr em http://127.0.0.1:5000")
    print("  📌 System tray ativo (ícone verde no canto inferior direito)")
    print("  🛑 Para sair: clica no ícone da tray → Sair")
    print()
    print("=" * 60)
    
    # Arrancar Flask
    flask_app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
