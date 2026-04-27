"""
BotEngine — Motor do bot Hyperliquid
Corre numa thread separada, gerido pela app desktop
"""
import threading
import time
import logging
import os, sys
from datetime import datetime

# ========== WINDOWS UTF-8 FIX ==========
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
# ======================================

logger = logging.getLogger(__name__)

# Estado global (partilhado entre threads)
app_state = {
    "bot_running": False,
    "trader": None,
    "aggregator": None,
    "db": None,
    "config": None,
    "last_price": 0,
    "last_data": {},
    "logs": [],
    "current_position": None,
    "equity_history": [10000],
    "trades": [],
    "capital": 10000,
    "update_count": 0,
}

# 🔒 LOCK para proteger app_state
app_state_lock = threading.Lock()


class BotEngine:
    """
    Motor do bot que corre numa thread separada.
    Integra: DataAggregator + PaperTrader + Base de dados
    """
    
    def __init__(self, config):
        from data_aggregator import DataAggregator
        from paper_trading import PaperTrader
        from database import BotDatabase
        
        self.config = config
        self.running = False
        self._thread = None
        self._stop_event = threading.Event()
        
        # Inicializar componentes
        self.aggregator = DataAggregator(config)
        self.trader = PaperTrader(config)
        self.db = BotDatabase()
        
        # Config
        self.assets = config.get('assets', ['BTC'])
        self.poll_interval = config.get('polling', {}).get('oi_interval', 60)
        self.price_interval = 5  # Preço a cada 5s
        
        # Estado
        self.last_price = 0
        self.update_count = 0
        
        logger.info("[BotEngine] inicializado")
    
    def start(self):
        """Arranca o motor numa thread"""
        if self.running:
            logger.warning("BotEngine já está a correr")
            return
        
        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="bot-engine")
        self._thread.start()
        logger.info("[OK] BotEngine iniciado")
    
    def stop(self):
        """Para o motor graciosamente"""
        if not self.running:
            return
        
        self.running = False
        self._stop_event.set()
        self.trader.stop_monitoring()
        
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        
        logger.info("[STOP] BotEngine parado")
    
    def _run(self):
        """Loop principal do motor"""
        last_oi_time = 0
        last_price_time = 0
        last_cycle_time = 0  # ⚡ FIX: Controla quando chamar run_cycle()
        cycle_interval = 900  # 15 minutos = 900 segundos
        
        # Iniciar monitorização rápida
        if self.assets:
            self.trader._start_monitor_thread(self.assets[0])
        
        # ⚡ FIX: Correr run_cycle IMEDIATAMENTE no arranque
        # para definir htf_direction sem esperar 15 minutos
        logger.info("[BotEngine] Ciclo inicial de trading (run_cycle)...")
        for asset in self.assets:
            try:
                self.trader.run_cycle(asset)
                logger.info(f"[CYCLE] Ciclo inicial completo para {asset}")
            except Exception as e:
                logger.error(f"Erro no ciclo inicial para {asset}: {e}")
        
        logger.info(f"[BotEngine] Ciclo de trading a cada {cycle_interval//60}min")
        
        while self.running and not self._stop_event.is_set():
            current_time = time.time()
            
            try:
                # ⚡ FIX 1: Chamar run_cycle() a cada 15 minutos para gerar sinais!
                if current_time - last_cycle_time >= cycle_interval:
                    for asset in self.assets:
                        try:
                            self.trader.run_cycle(asset)
                            logger.info(f"[CYCLE] Trading cycle completo para {asset}")
                        except Exception as e:
                            logger.error(f"Erro no ciclo de trading para {asset}: {e}")
                    last_cycle_time = current_time
                
                # Buscar preço rápido a cada 5 segundos
                if current_time - last_price_time >= self.price_interval:
                    for asset in self.assets:
                        price = self.aggregator.get_cached_price(asset, max_age_seconds=10)
                        if price == 0:
                            # Buscar direto da API
                            try:
                                hl_data = self.aggregator._fetch_hyperliquid(asset)
                                if hl_data:
                                    price = hl_data.get('mark_price', 0)
                            except Exception as e:
                                logger.warning(f"Erro a buscar preço: {e}")
                        
                        if price > 0:
                            self.last_price = price
                            with app_state_lock:
                                app_state["last_price"] = price
                            
                            # Guardar na DB
                            self.db.save_price(asset, price)
                    
                    last_price_time = current_time
                
                # Buscar dados completos a cada intervalo
                if current_time - last_oi_time >= self.poll_interval:
                    for asset in self.assets:
                        data = self.aggregator.fetch_all_data(asset)
                        if data:
                            with app_state_lock:
                                app_state["last_data"] = data
                            
                            # Guardar candles e OI na DB
                            self._save_market_data(asset, data)
                            
                            # Log resumo
                            oi = data.get('oi_total', 0)
                            vol = data.get('volume_total', 0)
                            funding = data.get('funding_avg', 0)
                            logger.info(
                                f"[HL] {asset} | ${self.last_price:,.2f} | "
                                f"OI: ${oi:,.0f} | Vol: ${vol:,.0f} | "
                                f"Funding: {funding*100:.4f}%"
                            )
                    
                    self.update_count += 1
                    with app_state_lock:
                        app_state["update_count"] = self.update_count
                    last_oi_time = current_time
                
                # 🔄 SINCRONIZAR ESTADO COM DASHBOARD (a cada 5 segundos)
                if int(current_time) % 5 == 0:
                    self._sync_dashboard_state()
                
                # Pequeno sleep para não esgotar CPU
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"Erro no loop do motor: {e}")
                time.sleep(5)
    
    def _save_market_data(self, asset, data):
        """Guarda dados de mercado na base de dados"""
        import time
        try:
            ts = int(time.time())
            # Guardar OI
            if data.get('oi_total', 0) > 0:
                self.db.save_open_interest(asset, ts, data['oi_total'])
            
            # Guardar funding
            if data.get('funding_avg', 0) != 0:
                self.db.save_funding_rate(asset, ts, data['funding_avg'])
                
        except Exception as e:
            logger.warning(f"Erro a guardar dados: {e}")
    
    def _sync_dashboard_state(self):
        """Sincroniza estado do PaperTrader com app_state (para dashboard)"""
        try:
            with app_state_lock:
                # Sincronizar posição aberta
                if self.trader.current_position:
                    app_state["current_position"] = {
                        "side": self.trader.current_position,
                        "entry_price": self.trader.entry_price,
                        "position_size": getattr(self.trader, 'position_size', 100),
                        "leverage": getattr(self.trader, 'current_leverage', 1),
                        "unrealized_pnl": self._calculate_unrealized_pnl()
                    }
                else:
                    app_state["current_position"] = None
                
                # Sincronizar capital
                app_state["capital"] = self.trader.capital
                
                # Sincronizar equity history
                if not app_state.get("equity_history"):
                    app_state["equity_history"] = [app_state["capital"]]
                app_state["equity_history"].append(self.trader.capital)
                # Manter só últimos 1000 pontos
                if len(app_state["equity_history"]) > 1000:
                    app_state["equity_history"] = app_state["equity_history"][-1000:]
                
                # Sincronizar trades
                recent_trades = self.db.get_recent_trades(limit=50) if self.db else []
                app_state["trades"] = recent_trades
                
        except Exception as e:
            logger.warning(f"Erro a sincronizar estado da dashboard: {e}")
    
    def _calculate_unrealized_pnl(self) -> float:
        """Calcula PnL não realizado da posição aberta"""
        try:
            if not self.trader.current_position or not self.trader.entry_price:
                return 0.0
            
            # Usar último preço conhecido
            current_price = self.last_price or app_state.get("last_price", 0)
            if current_price <= 0:
                return 0.0
            
            position_size = getattr(self.trader, 'position_size', 100)
            
            if self.trader.current_position == 'long':
                pnl = (current_price - self.trader.entry_price) / self.trader.entry_price * position_size
            else:  # short
                pnl = (self.trader.entry_price - current_price) / self.trader.entry_price * position_size
            
            return pnl
        except Exception as e:
            logger.warning(f"Erro a calcular unrealized PnL: {e}")
            return 0.0
    
    @property
    def is_running(self):
        return self.running


def start_bot_engine(config):
    """Inicia o motor do bot e actualiza estado global"""
    global app_state
    
    logger.info("[start_bot_engine] A iniciar...")
    
    if app_state.get("bot_running"):
        logger.warning("Bot ja esta a correr!")
        return False
    
    try:
        logger.info("[start_bot_engine] A criar BotEngine...")
        engine = BotEngine(config)
        
        logger.info("[start_bot_engine] A chamar engine.start()...")
        engine.start()
        
        logger.info("[start_bot_engine] A actualizar app_state...")
        with app_state_lock:
            app_state["bot_running"] = True
            app_state["engine"] = engine          # Guardar BotEngine para poder parar
            app_state["trader"] = engine.trader
            app_state["aggregator"] = engine.aggregator
            app_state["db"] = engine.db
            app_state["config"] = config
            app_state["capital"] = config.get('risk', {}).get('initial_capital', 10000)
            app_state["equity_history"] = [app_state["capital"]]
        
        logger.info("✅ [start_bot_engine] Bot iniciado com sucesso!")
        return True
    except Exception as e:
        logger.error(f"❌ [start_bot_engine] Erro a iniciar bot: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def stop_bot_engine():
    """Para o motor do bot"""
    global app_state
    
    # Parar o BotEngine (não o trader)
    engine = app_state.get("engine")
    if engine and hasattr(engine, 'stop'):
        engine.stop()
    
    with app_state_lock:
        app_state["bot_running"] = False
        app_state["engine"] = None
    logger.info("[STOP] Bot parado pelo utilizador")


def get_bot_status():
    """Retorna estado actual do bot para o frontend"""
    with app_state_lock:
        return {
            "running": app_state.get("bot_running", False),
            "price": app_state.get("last_price", 0),
            "asset": app_state.get("config", {}).get('assets', ['BTC'])[0] if app_state.get("config") else 'BTC',
            "update_count": app_state.get("update_count", 0),
            "capital": app_state.get("capital", 10000),
            "position": app_state.get("current_position"),
            "equity": app_state.get("equity_history", [10000])[-1] if app_state.get("equity_history") else 10000,
        }


def add_log(message, level="info"):
    """Adiciona log ao estado global"""
    with app_state_lock:
        app_state["logs"].append({
            "time": datetime.now().isoformat(),
            "message": message,
            "level": level
        })
        # Manter so ultimos 1000 logs
        if len(app_state["logs"]) > 1000:
            app_state["logs"] = app_state["logs"][-1000:]


# =============================================================================
# MAIN (para teste)
# =============================================================================
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent / "src"))
    
    from utils import load_config, setup_logging
    from pathlib import Path
    
    print("🔧 BotEngine — Teste standalone")
    print("=" * 50)
    
    config = load_config()
    setup_logging(level="INFO", log_file="logs/bot.log")
    
    engine = BotEngine(config)
    engine.start()
    
    try:
        while True:
            time.sleep(5)
            status = get_bot_status()
            print(f"Price: ${status['price']:,.2f} | Updates: {status['update_count']} | Running: {status['running']}")
    except KeyboardInterrupt:
        print("\n🛑 A parar...")
        engine.stop()
