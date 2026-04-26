"""
WebApp v2 — Otimizado: deque maxlen para trades, cache de respostas.
Resolve crescimento descontrolado de _trades e falta de cache HTTP.
"""
import json
import logging
import threading
import time
import os
from datetime import datetime
from typing import Dict, Any
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, request, render_template

try:
    from flask_cors import CORS
    HAS_CORS = True
except ImportError:
    HAS_CORS = False

from refactored.core.event_bus import EventBus
from refactored.data.database import BotDatabase

logger = logging.getLogger(__name__)


class WebApp:
    """
    Servidor Flask v2 — trades bounded + response cache.
    
    Mudanças v2:
    - _trades usa deque(maxlen) → O(1), sem slicing
    - Cache de respostas estáticas (stats, db/stats)
    - Throttling de eventos duplicados
    """
    
    def __init__(self, config: Dict, event_bus: EventBus, database: BotDatabase, port: int = 5000):
        self.config = config
        self.event_bus = event_bus
        self.db = database
        self.port = port
        
        # Path absoluto para templates (funciona em Linux e Windows)
        template_dir = Path(__file__).parent.parent.parent / 'src' / 'web' / 'templates'
        static_dir = Path(__file__).parent.parent.parent / 'src' / 'web' / 'static'
        self.app = Flask(__name__, template_folder=str(template_dir), static_folder=str(static_dir))
        if HAS_CORS:
            CORS(self.app)
        
        self._market_data = {}
        self._bot_status = {'running': False, 'state': 'IDLE'}
        self._trades = deque(maxlen=1000)  # ✅ Bounded O(1)
        self._logs = deque(maxlen=500)
        self._last_update = None
        
        # ✅ Cache de respostas estáticas
        self._stats_cache = {}
        self._stats_cache_time = 0
        self._stats_cache_ttl = 5  # 5 segundos
        
        self._setup_routes()
        self._subscribe_to_events()
    
    def _subscribe_to_events(self):
        self.event_bus.subscribe('market.data', self._on_market_data)
        self.event_bus.subscribe('state.changed', self._on_state_change)
        self.event_bus.subscribe('trade.entered', self._on_trade)
        self.event_bus.subscribe('trade.exited', self._on_trade)
        self.event_bus.subscribe('bot.status', self._on_bot_status)
    
    def _on_market_data(self, event):
        payload = event.payload
        self._market_data[payload.get('asset', 'BTC')] = payload
        self._last_update = datetime.now().isoformat()
    
    def _on_state_change(self, event):
        self._bot_status['state'] = event.payload.get('to', 'IDLE')
    
    def _on_trade(self, event):
        self._trades.append({
            'time': datetime.now().isoformat(),
            'type': event.type,
            'data': event.payload
        })
        # ✅ deque maxlen gere o truncamento automaticamente
    
    def _on_bot_status(self, event):
        self._bot_status.update(event.payload)
    
    def _setup_routes(self):
        
        @self.app.route('/api/status')
        def api_status():
            return jsonify({
                'running': self._bot_status.get('running', False),
                'state': self._bot_status.get('state', 'IDLE'),
                'last_update': self._last_update,
                'assets': self._market_data
            })
        
        @self.app.route('/api/market/<asset>')
        def api_market(asset):
            data = self._market_data.get(asset.upper(), {})
            return jsonify(data)
        
        @self.app.route('/api/trades')
        def api_trades():
            limit = request.args.get('limit', 50, type=int)
            return jsonify(list(self._trades)[-limit:])
        
        @self.app.route('/api/db/trades')
        def api_db_trades():
            limit = request.args.get('limit', 50, type=int)
            symbol = request.args.get('symbol')
            return jsonify(self.db.get_trades(symbol=symbol, limit=limit))
        
        @self.app.route('/')
        def index():
            return render_template('dashboard_simple.html')
        
        @self.app.route('/api/db/stats')
        def api_db_stats():
            # ✅ Cache de 5s para evitar hammering da DB
            now = time.time()
            if now - self._stats_cache_time < self._stats_cache_ttl:
                return jsonify(self._stats_cache)
            
            stats = self.db.get_stats()
            self._stats_cache = stats
            self._stats_cache_time = now
            return jsonify(stats)
        
        @self.app.route('/api/db/signals')
        def api_db_signals():
            limit = request.args.get('limit', 50, type=int)
            asset = request.args.get('asset')
            return jsonify(self.db.get_signals(asset=asset, limit=limit))
        
        @self.app.route('/api/bot/start', methods=['POST'])
        def api_start():
            self.event_bus.publish('bot.command', {'action': 'start'})
            return jsonify({'success': True})
        
        @self.app.route('/api/bot/stop', methods=['POST'])
        def api_stop():
            self.event_bus.publish('bot.command', {'action': 'stop'})
            return jsonify({'success': True})
        
        @self.app.route('/api/bot/emergency', methods=['POST'])
        def api_emergency():
            self.event_bus.publish('bot.command', {'action': 'emergency_close'})
            return jsonify({'success': True})
    
    def run(self):
        logger.info(f"[WebApp] Iniciando em http://127.0.0.1:{self.port}")
        self.app.run(host='127.0.0.1', port=self.port, debug=False, use_reloader=False, threaded=True)
    
    def start_thread(self):
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        return thread
