"""
BotEngine v2 — Otimizado: sleep único, estado global, throttling de transições.
Resolve 30x time.sleep(1) e transições de estado por asset.
"""
import time
import logging
import threading
from typing import Dict

from ..core.state_machine import BotState, StateMachine
from ..core.container import ServiceContainer
from ..core.event_bus import EventBus

logger = logging.getLogger(__name__)


class BotEngine:
    """
    Motor v2 — ciclo eficiente + estado global.
    
    Mudanças v2:
    - 1 time.sleep(interval) em vez de N x time.sleep(1)
    - Estado global: SCANNING → ANALYZING → IDLE (não por asset)
    - Throttling de transições: max 1 transição por segundo
    - Graceful shutdown com Event (não polling)
    """
    
    def __init__(self, config: dict, container: ServiceContainer):
        self.config = config
        self.container = container
        self.event_bus = container.event_bus
        self.state_machine = StateMachine(event_bus=self.event_bus)
        
        self.aggregator = container.aggregator
        self.strategy = container.strategy
        self.trader = container.trader
        
        self.assets = config.get('assets', ['BTC'])
        self.interval = config.get('polling', {}).get('oi_interval', 30)
        
        self._running = False
        self._thread = None
        self._shutdown_event = threading.Event()  # ✅ Event em vez de polling
        self._last_transition_time = 0
        self._transition_throttle = 1  # Segundos mínimos entre transições
        
        self.event_bus.subscribe('bot.command', self._on_command)
    
    def _throttled_transition(self, new_state: BotState, reason: str) -> bool:
        """Transição com throttling para evitar spam."""
        now = time.time()
        if now - self._last_transition_time < self._transition_throttle:
            return False
        self._last_transition_time = now
        return self.state_machine.transition(new_state, reason)
    
    def _on_command(self, event):
        cmd = event.payload.get('action')
        if cmd == 'start':
            self.start()
        elif cmd == 'stop':
            self.stop()
        elif cmd == 'emergency_close':
            self._emergency_close()
    
    def start(self):
        if self._running:
            logger.warning("[Engine] Já está a correr")
            return
        
        self._running = True
        self._shutdown_event.clear()
        self._throttled_transition(BotState.SCANNING, "Bot iniciado")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="bot-engine")
        self._thread.start()
        
        self.event_bus.publish('bot.status', {'running': True})
        logger.info("[Engine] ✅ Motor iniciado")
    
    def stop(self):
        if not self._running:
            return
        
        self._running = False
        self._shutdown_event.set()  # ✅ Sinaliza imediatamente
        self._throttled_transition(BotState.SHUTDOWN, "Shutdown solicitado")
        
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        
        self.event_bus.publish('bot.status', {'running': False})
        logger.info("[Engine] 🛑 Motor parado")
    
    def _loop(self):
        """Loop principal otimizado."""
        logger.info(f"[Engine] Ciclo a cada {self.interval}s")
        
        while self._running:
            try:
                # ✅ Estado global: um SCANNING para todo o ciclo
                self._throttled_transition(BotState.SCANNING, "Ciclo iniciado")
                
                for asset in self.assets:
                    data = self.aggregator.get_all_data(asset)
                    if not data:
                        logger.warning(f"[Engine] Sem dados para {asset}")
                        continue
                    
                    price = data.get('price', 0)
                    if price <= 0:
                        continue
                    
                    # Analisar e executar
                    self.trader.on_market_data({**data, 'asset': asset})
                
                # Log periódico
                status = self.trader.get_status()
                logger.info(
                    f"[Engine] 💰 ${status['capital']:,.2f} | "
                    f"Trades: {status['trade_count']} | "
                    f"Pos: {'SIM' if status['in_position'] else 'NÃO'}"
                )
                
                self._throttled_transition(BotState.IDLE, "Ciclo completo")
                
                # ✅ Sleep único — Event permite wake early
                interrupted = self._shutdown_event.wait(timeout=self.interval)
                if interrupted:
                    break
                    
            except Exception as e:
                logger.error(f"[Engine] Erro no loop: {e}")
                self._throttled_transition(BotState.ERROR, str(e))
                time.sleep(5)
    
    def _emergency_close(self):
        logger.error("[Engine] 🚨 EMERGENCY CLOSE!")
        if self.trader.position:
            self.trader._exit_position(
                self.trader.position.asset,
                self.aggregator.get_latest_price(self.trader.position.asset),
                'EMERGENCY'
            )
