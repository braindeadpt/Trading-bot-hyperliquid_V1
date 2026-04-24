"""
Hyperliquid Bot — App Desktop (Tkinter + WebView)
Corre em background com ícone na system tray

Como usar:
    python app_desktop.py

Para criar .exe:
    python build_app.py
"""
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import json
import logging
import sys
import os
from pathlib import Path
from datetime import datetime

# Verificar dependências
try:
    import webview
except ImportError:
    print("❌ pywebview não instalado!")
    print("   Instala: python -m pip install pywebview")
    print("   (No Windows, pode precisar de: python -m pip install pywebview[edge])")
    sys.exit(1)

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    print("⚠️ pystray/pillow não instalado — system tray desativado")
    print("   Instala: python -m pip install pystray pillow")

# Adicionar src/ ao path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from bot_engine import BotEngine, get_bot_status, start_bot_engine, stop_bot_engine, app_state
from utils import load_config, setup_logging

logger = logging.getLogger(__name__)


class BotApp:
    """Aplicação desktop principal"""
    
    def __init__(self):
        self.config = load_config()
        setup_logging(level="INFO", log_file="logs/bot.log")
        
        # Janela principal (invisível — só serve para manter app viva)
        self.root = tk.Tk()
        self.root.title("Hyperliquid Bot")
        self.root.geometry("1x1+0+0")
        self.root.overrideredirect(True)  # Sem decoração
        self.root.withdraw()  # Esconder janela
        
        # Estado
        self.engine = None
        self.window = None
        self.tray_icon = None
        self._closing = False
        
        # System tray
        if HAS_TRAY:
            self._setup_tray()
        
        # Arrancar bot automaticamente
        self._start_bot()
        
        # Abrir dashboard
        self._open_dashboard()
        
        # Monitorização
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        
        logger.info("🚀 App Desktop iniciada")
    
    def _setup_tray(self):
        """Configura ícone na system tray"""
        # Criar ícone simples (círculo verde)
        width = 64
        height = 64
        image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        dc = ImageDraw.Draw(image)
        dc.ellipse([4, 4, width-4, height-4], fill="#00ff88", outline="#00cc66", width=2)
        
        menu = pystray.Menu(
            pystray.MenuItem("🚀 Abrir Dashboard", self._show_dashboard),
            pystray.MenuItem("▶ Iniciar Bot", self._start_bot),
            pystray.MenuItem("⏹ Parar Bot", self._stop_bot),
            pystray.MenuItem("📊 Status", self._show_status),
            pystray.MenuItem("───", lambda: None, enabled=False),
            pystray.MenuItem("❌ Sair", self._quit),
        )
        
        self.tray_icon = pystray.Icon(
            "hyperliquid-bot",
            image,
            "Hyperliquid Momentum Bot",
            menu
        )
        
        # Arrancar tray numa thread
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
        logger.info("📌 System tray ativo")
    
    def _start_bot(self):
        """Arranca o motor do bot"""
        if not app_state.get("bot_running"):
            success = start_bot_engine(self.config)
            if success:
                logger.info("✅ Bot iniciado")
                if self.tray_icon:
                    self.tray_icon.title = "🟢 Hyperliquid Bot (Running)"
            else:
                logger.error("❌ Falha a iniciar bot")
    
    def _stop_bot(self):
        """Para o motor do bot"""
        stop_bot_engine()
        if self.tray_icon:
            self.tray_icon.title = "🔴 Hyperliquid Bot (Stopped)"
    
    def _open_dashboard(self):
        """Abre janela do dashboard"""
        if self.window:
            try:
                self.window.show()
                return
            except:
                pass
        
        # Criar janela webview
        self.window = webview.create_window(
            title="Hyperliquid Momentum Bot — Dashboard",
            url=str(Path(__file__).parent / "dashboard.html"),
            width=1400,
            height=900,
            min_size=(1000, 600),
            text_select=True,
            confirm_close=False,
        )
        
        # Injectar API Python para o JavaScript
        self.window.expose(self._js_start_bot)
        self.window.expose(self._js_stop_bot)
        self.window.expose(self._js_get_status)
        self.window.expose(self._js_get_logs)
        self.window.expose(self._js_get_trades)
        self.window.expose(self._js_force_long)
        self.window.expose(self._js_force_short)
        self.window.expose(self._js_emergency_close)
        self.window.expose(self._js_save_config)
        self.window.expose(self._js_load_config)
        
        # Arrancar webview numa thread
        threading.Thread(target=webview.start, daemon=True).start()
    
    def _show_dashboard(self):
        """Mostra dashboard (chamado pelo tray)"""
        self._open_dashboard()
    
    def _show_status(self):
        """Mostra status numa messagebox"""
        status = get_bot_status()
        msg = (
            f"🤖 Bot: {'Running' if status['running'] else 'Stopped'}\n"
            f"💰 Capital: ${status['capital']:,.2f}\n"
            f"📊 Preço BTC: ${status['price']:,.2f}\n"
            f"🔄 Updates: {status['update_count']}\n"
        )
        messagebox.showinfo("Hyperliquid Bot — Status", msg)
    
    def _monitor_loop(self):
        """Loop de monitorização — actualiza estado global"""
        while not self._closing:
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
                    
                    # Actualizar equity history
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
    # API JavaScript ↔ Python
    # =============================================================================
    
    def _js_start_bot(self):
        """Chamado pelo JavaScript quando clica Iniciar Bot"""
        self._start_bot()
        return {"success": True, "message": "Bot iniciado"}
    
    def _js_stop_bot(self):
        """Chamado pelo JavaScript quando clica Parar Bot"""
        self._stop_bot()
        return {"success": True, "message": "Bot parado"}
    
    def _js_get_status(self):
        """Retorna estado actual para o dashboard"""
        status = get_bot_status()
        
        # Adicionar dados de mercado
        data = app_state.get("last_data", {})
        hl_data = data.get('exchanges_data', {}).get('hyperliquid', {})
        
        return {
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
            "latency": 0,  # TODO
        }
    
    def _js_get_logs(self, limit=100):
        """Retorna logs recentes"""
        logs = app_state.get("logs", [])
        return logs[-limit:] if logs else []
    
    def _js_get_trades(self, limit=50):
        """Retorna trades recentes"""
        return app_state.get("trades", [])[-limit:]
    
    def _js_force_long(self):
        """Força posição long"""
        trader = app_state.get("trader")
        if trader and app_state.get("bot_running"):
            # TODO: Implementar forçar long no PaperTrader
            return {"success": False, "message": "Ainda não implementado"}
        return {"success": False, "message": "Bot não está a correr"}
    
    def _js_force_short(self):
        """Força posição short"""
        trader = app_state.get("trader")
        if trader and app_state.get("bot_running"):
            return {"success": False, "message": "Ainda não implementado"}
        return {"success": False, "message": "Bot não está a correr"}
    
    def _js_emergency_close(self):
        """Fecha posição de emergência"""
        trader = app_state.get("trader")
        if trader:
            # TODO: Implementar emergency close
            return {"success": False, "message": "Ainda não implementado"}
        return {"success": False, "message": "Sem posição aberta"}
    
    def _js_save_config(self, cfg):
        """Guarda configuração"""
        try:
            config_path = Path(__file__).parent / "config" / "settings.json"
            with open(config_path, 'w') as f:
                json.dump(cfg, f, indent=2)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _js_load_config(self):
        """Carrega configuração"""
        try:
            config_path = Path(__file__).parent / "config" / "settings.json"
            if config_path.exists():
                with open(config_path) as f:
                    return {"success": True, "config": json.load(f)}
            return {"success": False, "error": "Ficheiro não encontrado"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _quit(self):
        """Sai da aplicação"""
        self._closing = True
        self._stop_bot()
        
        if self.tray_icon:
            self.tray_icon.stop()
        
        if self.window:
            try:
                self.window.destroy()
            except:
                pass
        
        self.root.quit()
        self.root.destroy()
        sys.exit(0)
    
    def run(self):
        """Loop principal da app"""
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self._quit()


def main():
    """Entry point"""
    print("=" * 60)
    print("  🚀 HYPERLIQUID MOMENTUM BOT — APP DESKTOP")
    print("=" * 60)
    print()
    print("  A iniciar...")
    print("  Dashboard vai abrir numa janela.")
    print("  Bot corre em background.")
    print()
    print("  Para sair: fecha a janela ou clica no ícone da tray")
    print("=" * 60)
    
    app = BotApp()
    app.run()


if __name__ == "__main__":
    main()
