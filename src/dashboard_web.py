"""
Dashboard Web - Interface visual no browser
Abre automaticamente numa janela/janela separada do sistema
"""
import json
import webbrowser
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional

from flask import Flask, render_template_string, jsonify

from utils import load_config
from database import BotDatabase
from data_aggregator import DataAggregator
from strategy import MomentumStrategy

logger = logging.getLogger(__name__)

# Template HTML do dashboard
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hyperliquid Bot Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', system-ui, sans-serif; 
            background: #0a0e27; 
            color: #fff;
            min-height: 100vh;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px 40px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header h1 { font-size: 24px; }
        .header .status {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(255,255,255,0.1);
            padding: 8px 16px;
            border-radius: 20px;
        }
        .status-dot {
            width: 10px; height: 10px;
            border-radius: 50%;
            background: #00ff88;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .container {
            padding: 30px 40px;
            max-width: 1400px;
            margin: 0 auto;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 20px;
            transition: transform 0.2s;
        }
        .stat-card:hover { transform: translateY(-2px); }
        .stat-label { color: #8892b0; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
        .stat-value { font-size: 28px; font-weight: 700; margin-top: 8px; }
        .stat-change { font-size: 14px; margin-top: 4px; }
        .positive { color: #00ff88; }
        .negative { color: #ff4757; }
        .neutral { color: #8892b0; }
        .assets-section { margin-top: 30px; }
        .section-title { font-size: 20px; margin-bottom: 20px; color: #ccd6f6; }
        .assets-table {
            width: 100%;
            border-collapse: collapse;
            background: rgba(255,255,255,0.03);
            border-radius: 12px;
            overflow: hidden;
        }
        .assets-table th {
            text-align: left;
            padding: 15px 20px;
            background: rgba(255,255,255,0.05);
            color: #8892b0;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .assets-table td {
            padding: 15px 20px;
            border-top: 1px solid rgba(255,255,255,0.05);
        }
        .assets-table tr:hover { background: rgba(255,255,255,0.02); }
        .asset-name { font-weight: 600; color: #ccd6f6; }
        .price { font-family: 'Courier New', monospace; font-size: 16px; }
        .badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
        }
        .badge-long { background: rgba(0,255,136,0.15); color: #00ff88; }
        .badge-wait { background: rgba(136,146,176,0.15); color: #8892b0; }
        .oi-bar, .vol-bar {
            height: 6px;
            background: rgba(255,255,255,0.1);
            border-radius: 3px;
            margin-top: 8px;
            overflow: hidden;
        }
        .oi-fill, .vol-fill {
            height: 100%;
            border-radius: 3px;
            transition: width 0.3s;
        }
        .oi-fill { background: #667eea; }
        .vol-fill { background: #f093fb; }
        .update-time {
            text-align: center;
            color: #8892b0;
            font-size: 12px;
            margin-top: 30px;
            padding: 20px;
        }
        .log-section {
            margin-top: 30px;
            background: rgba(255,255,255,0.03);
            border-radius: 12px;
            padding: 20px;
        }
        .log-entry {
            font-family: 'Courier New', monospace;
            font-size: 12px;
            color: #8892b0;
            padding: 4px 0;
            border-bottom: 1px solid rgba(255,255,255,0.03);
        }
        .log-entry:last-child { border-bottom: none; }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>🔥 Hyperliquid Momentum Bot</h1>
            <p style="opacity: 0.7; margin-top: 4px;">Paper Trading Dashboard v1.0</p>
        </div>
        <div class="status">
            <div class="status-dot"></div>
            <span>{{ status }}</span>
        </div>
    </div>
    
    <div class="container">
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Capital Inicial</div>
                <div class="stat-value">$10,000</div>
                <div class="stat-change neutral">Simulação</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Capital Atual</div>
                <div class="stat-value {{ 'positive' if current_pnl > 0 else 'negative' if current_pnl < 0 else 'neutral' }}">
                    ${{ "%.2f"|format(10000 + current_pnl) }}
                </div>
                <div class="stat-change {{ 'positive' if current_pnl > 0 else 'negative' if current_pnl < 0 else 'neutral' }}">
                    {{ "+%.2f"|format(current_pnl) if current_pnl > 0 else "%.2f"|format(current_pnl) }} USD
                </div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Sinais Hoje</div>
                <div class="stat-value">{{ total_signals }}</div>
                <div class="stat-change neutral">{{ assets|length }} assets monitorizados</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Último Trade</div>
                <div class="stat-value">{{ last_trade_time }}</div>
                <div class="stat-change {{ 'positive' if last_trade_pnl > 0 else 'negative' if last_trade_pnl < 0 else 'neutral' }}">
                    {{ "+%.2f"|format(last_trade_pnl) if last_trade_pnl > 0 else "%.2f"|format(last_trade_pnl) if last_trade_pnl else "Sem trades" }} USD
                </div>
            </div>
        </div>
        
        <div class="assets-section">
            <h2 class="section-title">📊 Assets em Monitorização</h2>
            <table class="assets-table">
                <thead>
                    <tr>
                        <th>Asset</th>
                        <th>Preço</th>
                        <th>OI Global</th>
                        <th>OI Δ</th>
                        <th>Volume</th>
                        <th>Funding</th>
                        <th>Sinal</th>
                    </tr>
                </thead>
                <tbody>
                    {% for asset, data in assets.items() %}
                    <tr>
                        <td class="asset-name">{{ asset }}</td>
                        <td class="price">${{ "%.2f"|format(data.price) if data.price else "N/A" }}</td>
                        <td>
                            {{ format_oi(data.oi_total) }}
                            <div class="oi-bar"><div class="oi-fill" style="width: {{ min(data.oi_total / 1000000000 * 100, 100) }}%"></div></div>
                        </td>
                        <td class="{{ 'positive' if data.oi_change_pct > 0 else 'negative' }}">
                            {{ format_pct(data.oi_change_pct) }}%
                        </td>
                        <td>
                            {{ "%.1f"|format(data.volume_ratio) }}x
                            <div class="vol-bar"><div class="vol-fill" style="width: {{ min(data.volume_ratio / 5 * 100, 100) }}%"></div></div>
                        </td>
                        <td>{{ "%.4f"|format(data.funding_avg * 100) }}%</td>
                        <td>
                            {% if data.signal == 'LONG' %}
                                <span class="badge badge-long">🚀 LONG</span>
                            {% else %}
                                <span class="badge badge-wait">⏳ WAIT</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        
        <div class="update-time">
            Última atualização: {{ last_update }} | Dashboard atualiza a cada {{ poll_interval }}s
        </div>
    </div>
    
    <script>
        // Auto-refresh a cada 30 segundos
        setInterval(() => {
            window.location.reload();
        }, 30000);
    </script>
</body>
</html>
"""

def _format_oi(value: float) -> str:
    """Formata OI em notação legível: 1.2B, 450M, 30K"""
    if value == 0 or value is None:
        return "N/A"
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def _format_pct(value: float) -> str:
    """Formata percentagem de forma segura"""
    if value is None:
        return "0.00"
    return f"{value * 100:+.2f}"


class WebDashboard:
    """Dashboard web que abre no browser"""
    
    def __init__(self, config: Dict, db: Optional[BotDatabase] = None):
        self.config = config
        self.db = db or BotDatabase()
        self.app = Flask(__name__)
        
        self.aggregator = DataAggregator(config)
        self.strategy = MomentumStrategy(config)
        
        self.assets = config.get('assets', ['BTC', 'ETH'])
        self.poll_interval = config.get('polling', {}).get('oi_interval', 60)
        
        # Estado
        self.current_data = {}
        self.total_signals = 0
        self.current_pnl = 0
        self.last_trade_pnl = 0
        self.last_trade_time = "Sem trades"
        self.last_update = "Nunca"
        
        self._setup_routes()
    
    def _setup_routes(self):
        """Configura rotas do Flask"""
        
        @self.app.route("/")
        def dashboard():
            self._update_data()
            return render_template_string(
                DASHBOARD_TEMPLATE,
                status="LIVE",
                assets=self.current_data,
                total_signals=self.total_signals,
                current_pnl=self.current_pnl,
                last_trade_pnl=self.last_trade_pnl,
                last_trade_time=self.last_trade_time,
                last_update=self.last_update,
                poll_interval=self.poll_interval,
                min=min,
                max=max,
                format_oi=_format_oi,
                format_pct=_format_pct
            )
        
        @self.app.route("/api/data")
        def api_data():
            self._update_data()
            return jsonify({
                'assets': self.current_data,
                'total_signals': self.total_signals,
                'last_update': self.last_update,
                'status': 'running'
            })
        
        @self.app.route("/api/stats")
        def api_stats():
            return jsonify(self.db.get_stats())
    
    def _update_data(self):
        """Atualiza dados das APIs"""
        for asset in self.assets:
            try:
                data = self.aggregator.fetch_all_data(asset)
                
                # LOG EXTREMO para debug
                if data:
                    hl_data = data['exchanges_data'].get('hyperliquid', {})
                    raw_price = hl_data.get('mark_price', 0)
                    logger.info(
                        f"🖥️ DASHBOARD | {asset} | "
                        f"hl_data={hl_data} | "
                        f"raw_price={raw_price} | "
                        f"exchanges={list(data['exchanges_data'].keys())}"
                    )
                    price = raw_price
                else:
                    price = 0
                    logger.warning(f"🖥️ DASHBOARD | {asset} | fetch_all_data retornou NONE")
                
                # Calcular volume ratio (simplificado)
                volume_ratio = 1.0  # Placeholder
                
                self.current_data[asset] = {
                    'price': price,
                    'oi_total': data.get('oi_total', 0) if data else 0,
                    'oi_change_pct': data.get('oi_change_pct', 0) if data else 0,
                    'volume_ratio': volume_ratio,
                    'funding_avg': data.get('funding_avg', 0) if data else 0,
                    'signal': ''
                }
                
                # Verificar sinal
                if price > 0:
                        signal = self.strategy.analyze(data, price)
                        if signal:
                            self.current_data[asset]['signal'] = signal
                            self.total_signals += 1
                            
            except Exception as e:
                logger.warning(f"Erro a atualizar {asset}: {e}")
                self.current_data[asset] = {
                    'price': 0, 'oi_total': 0, 'oi_change_pct': 0,
                    'volume_ratio': 0, 'funding_avg': 0, 'signal': ''
                }
        
        self.last_update = datetime.now().strftime("%H:%M:%S")
    
    def run(self, host: str = "127.0.0.1", port: int = 5000, debug: bool = False):
        """Inicia o servidor web e abre o browser"""
        url = f"http://{host}:{port}"
        
        print(f"\n{'='*60}")
        print(f"  🚀 Dashboard iniciado em {url}")
        print(f"{'='*60}")
        
        # Abrir browser numa thread separada
        def open_browser():
            import time
            time.sleep(1.5)  # Aguardar servidor iniciar
            webbrowser.open(url)
            print(f"  🌐 Browser aberto em janela separada!")
        
        threading.Thread(target=open_browser, daemon=True).start()
        
        # Iniciar servidor Flask
        self.app.run(host=host, port=port, debug=debug, use_reloader=False)


def main():
    """Entry point"""
    config = load_config()
    
    print("="*60)
    print("  HYPERLIQUID BOT - DASHBOARD WEB")
    print("="*60)
    print("  A iniciar servidor...")
    
    dashboard = WebDashboard(config)
    dashboard.run()


if __name__ == "__main__":
    main()
