"""
Flask Web App — infrastructure layer.
Compõe controllers e expõe rotas HTTP.
"""
import logging
from flask import Flask, jsonify, request

try:
    from flask_cors import CORS
    HAS_CORS = True
except ImportError:
    HAS_CORS = False

from clean.interface_adapters.controllers.web_api_controller import WebAPIController

logger = logging.getLogger(__name__)


class FlaskWebApp:
    """Servidor Flask — outermost layer."""
    
    def __init__(self, controller: WebAPIController, port: int = 5000):
        self.controller = controller
        self.port = port
        self.app = Flask(__name__)
        if HAS_CORS:
            CORS(self.app)
        self._setup_routes()
    
    def _setup_routes(self):
        
        @self.app.route('/api/status')
        def status():
            return jsonify({"status": "ok", "mode": "clean_arch"})
        
        @self.app.route('/api/market/<asset>')
        def market(asset):
            return jsonify(self.controller.get_market_data(asset.upper()))
        
        @self.app.route('/api/signal/<asset>', methods=['POST'])
        def signal(asset):
            return jsonify(self.controller.generate_signal(asset.upper()))
        
        @self.app.route('/api/portfolio')
        def portfolio():
            return jsonify(self.controller.get_portfolio())
        
        @self.app.route('/api/bot/emergency', methods=['POST'])
        def emergency():
            return jsonify(self.controller.emergency_close())
    
    def run(self):
        logger.info(f"[FlaskWebApp] Iniciando em http://127.0.0.1:{self.port}")
        self.app.run(host='127.0.0.1', port=self.port, debug=False, use_reloader=False)
