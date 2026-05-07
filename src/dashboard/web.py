"""
Hyperliquid Premium — Real-Time Operations Dashboard
Simple, dense, informative. Shows what the bot is actually doing.
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit

logger = logging.getLogger(__name__)

# Globals set by main.py
_engine: Optional[Any] = None
_socketio: Optional[SocketIO] = None


def set_engine(engine: Any) -> None:
    global _engine
    _engine = engine


def get_engine() -> Optional[Any]:
    return _engine


def create_app(config: Dict[str, Any]) -> tuple:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.get("secret_key", "dev-secret-123")
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
    global _socketio
    _socketio = socketio

    # ── HTML Template — clean, dense, informative ──
    INDEX_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Hyperliquid Bot — Operations</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
            background: #0a0a0a;
            color: #e0e0e0;
            font-size: 13px;
            line-height: 1.4;
        }
        .header {
            background: #111;
            border-bottom: 1px solid #333;
            padding: 12px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            position: sticky;
            top: 0;
            z-index: 100;
        }
        .header h1 { font-size: 16px; font-weight: 600; color: #fff; }
        .status-badge {
            padding: 4px 12px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .status-ok { background: #1a472a; color: #4ade80; }
        .status-warn { background: #451a03; color: #fbbf24; }
        .status-err { background: #450a0a; color: #f87171; }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
            gap: 16px;
            padding: 16px;
        }
        .panel {
            background: #111;
            border: 1px solid #222;
            border-radius: 8px;
            overflow: hidden;
        }
        .panel-header {
            background: #161616;
            padding: 10px 14px;
            border-bottom: 1px solid #222;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            color: #888;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .panel-body { padding: 12px 14px; }

        table { width: 100%; border-collapse: collapse; font-size: 12px; }
        th { text-align: left; padding: 6px 8px; color: #666; font-weight: 500; border-bottom: 1px solid #222; }
        td { padding: 5px 8px; border-bottom: 1px solid #1a1a1a; }
        tr:hover td { background: #1a1a1a; }
        .num { font-family: "SF Mono", "Consolas", monospace; text-align: right; }
        .up { color: #4ade80; }
        .down { color: #f87171; }
        .muted { color: #666; }
        .highlight { color: #60a5fa; font-weight: 600; }

        .metric-row {
            display: flex;
            justify-content: space-between;
            padding: 6px 0;
            border-bottom: 1px solid #1a1a1a;
        }
        .metric-label { color: #888; }
        .metric-value { font-family: monospace; }

        .log-box {
            max-height: 200px;
            overflow-y: auto;
            font-family: monospace;
            font-size: 11px;
            line-height: 1.5;
            color: #aaa;
        }
        .log-box .log-time { color: #666; }
        .log-box .log-info { color: #60a5fa; }
        .log-box .log-warn { color: #fbbf24; }
        .log-box .log-err { color: #f87171; }

        .bar {
            height: 4px;
            background: #222;
            border-radius: 2px;
            margin-top: 4px;
            overflow: hidden;
        }
        .bar-fill {
            height: 100%;
            border-radius: 2px;
            transition: width 0.5s;
        }
        .bar-green { background: #4ade80; }
        .bar-yellow { background: #fbbf24; }
        .bar-red { background: #f87171; }

        .strategy-card {
            padding: 10px;
            margin-bottom: 8px;
            background: #161616;
            border-radius: 6px;
            border-left: 3px solid #333;
        }
        .strategy-active { border-left-color: #4ade80; }
        .strategy-inactive { border-left-color: #666; }
        .strategy-name { font-weight: 600; color: #fff; margin-bottom: 4px; }
        .strategy-desc { color: #888; font-size: 11px; }
        .strategy-params { margin-top: 6px; font-family: monospace; font-size: 11px; color: #aaa; }

        .pulse {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 6px;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.3; }
            100% { opacity: 1; }
        }
        .pulse-green { background: #4ade80; }
        .pulse-yellow { background: #fbbf24; }
        .pulse-red { background: #f87171; }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <span id="conn-indicator" class="pulse pulse-green"></span>
            <h1>Hyperliquid Bot — Operations Dashboard</h1>
        </div>
        <span id="status-badge" class="status-badge status-ok">Running</span>
    </div>

    <div class="grid">
        <!-- Bot Status -->
        <div class="panel">
            <div class="panel-header">Bot Status <span id="uptime" class="muted">--</span></div>
            <div class="panel-body">
                <div class="metric-row"><span class="metric-label">Mode</span><span id="mode" class="metric-value">--</span></div>
                <div class="metric-row"><span class="metric-label">Uptime</span><span id="uptime2" class="metric-value">--</span></div>
                <div class="metric-row"><span class="metric-label">Memory</span><span id="memory" class="metric-value">--</span></div>
                <div class="metric-row"><span class="metric-label">Last Event</span><span id="last-event" class="metric-value">--</span></div>
                <div class="metric-row"><span class="metric-label">Events/sec</span><span id="eps" class="metric-value">--</span></div>
                <div class="metric-row"><span class="metric-label">Circuit Breaker</span><span id="circuit" class="metric-value">--</span></div>
            </div>
        </div>

        <!-- Market Data Feed -->
        <div class="panel">
            <div class="panel-header">Market Data Feed <span class="muted">Real-time</span></div>
            <div class="panel-body">
                <table>
                    <thead><tr><th>Asset</th><th class="num">Price</th><th class="num">Funding</th><th class="num">Pred</th><th class="num">OI (M)</th><th>Candles</th><th>Last Tick</th></tr></thead>
                    <tbody id="market-data"></tr><td colspan="7" class="muted" style="text-align:center;">Waiting for data...</td></tr></tbody>
                </table>
            </div>
        </div>

        <!-- Strategies -->
        <div class="panel">
            <div class="panel-header">Strategies <span class="muted">Active</span></div>
            <div class="panel-body" id="strategies">
                <div class="muted" style="text-align:center;">Loading...</div>
            </div>
        </div>

        <!-- Signals -->
        <div class="panel">
            <div class="panel-header">Signals <span class="muted">Last 10</span></div>
            <div class="panel-body">
                <table>
                    <thead><tr><th>Time</th><th>Strat</th><th>Sym</th><th>Side</th><th class="num">Conf</th><th>Reason</th></tr></thead>
                    <tbody id="signals"><tr><td colspan="6" class="muted" style="text-align:center;">No signals yet</td></tr></tbody>
                </table>
            </div>
        </div>

        <!-- Portfolio -->
        <div class="panel">
            <div class="panel-header">Portfolio</div>
            <div class="panel-body">
                <div class="metric-row"><span class="metric-label">Capital</span><span id="capital" class="metric-value">--</span></div>
                <div class="metric-row"><span class="metric-label">Daily PnL</span><span id="daily-pnl" class="metric-value">--</span></div>
                <div class="metric-row"><span class="metric-label">Max Drawdown</span><span id="drawdown" class="metric-value">--</span></div>
                <div class="metric-row"><span class="metric-label">Open Positions</span><span id="positions-count" class="metric-value">--</span></div>
                <div class="metric-row"><span class="metric-label">Daily Trades</span><span id="daily-trades" class="metric-value">--</span></div>
            </div>
        </div>

        <!-- Open Positions -->
        <div class="panel">
            <div class="panel-header">Open Positions</div>
            <div class="panel-body">
                <table>
                    <thead><tr><th>Symbol</th><th>Side</th><th class="num">Size</th><th class="num">Entry</th><th class="num">Current</th><th class="num">PnL%</th><th class="num">Duration</th></tr></thead>
                    <tbody id="positions"><tr><td colspan="7" class="muted" style="text-align:center;">No open positions</td></tr></tbody>
                </table>
            </div>
        </div>

        <!-- Funding Comparison -->
        <div class="panel">
            <div class="panel-header">Funding Comparison <span class="muted">HL vs Aggregated</span></div>
            <div class="panel-body">
                <table>
                    <thead><tr><th>Asset</th><th class="num">HL Funding</th><th class="num">Agg Funding</th><th class="num">HL OI</th><th class="num">Agg OI</th><th>Exchanges</th></tr></thead>
                    <tbody id="funding-comp"><tr><td colspan="6" class="muted" style="text-align:center;">No data</td></tr></tbody>
                </table>
            </div>
        </div>

        <!-- Recent Logs -->
        <div class="panel">
            <div class="panel-header">Recent Logs <span class="muted">Last 20</span></div>
            <div class="panel-body">
                <div id="logs" class="log-box">Waiting for logs...</div>
            </div㹬/div>
    </div>

    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <script>
        const socket = io();
        let lastUpdate = Date.now();
        let eventCount = 0;
        let eventHistory = [];

        // Track events per second
        setInterval(() => {
            const now = Date.now();
            eventHistory = eventHistory.filter(t => now - t < 1000);
            document.getElementById("eps").textContent = eventHistory.length.toString();
        }, 1000);

        socket.on("connect", () => {
            document.getElementById("conn-indicator").className = "pulse pulse-green";
            document.getElementById("status-badge").textContent = "Connected";
            document.getElementById("status-badge").className = "status-badge status-ok";
        });

        socket.on("disconnect", () => {
            document.getElementById("conn-indicator").className = "pulse pulse-red";
            document.getElementById("status-badge").textContent = "DISCONNECTED";
            document.getElementById("status-badge").className = "status-badge status-err";
        });

        socket.on("status_update", (data) => {
            document.getElementById("mode").textContent = data.mode || "--";
            document.getElementById("uptime2").textContent = data.uptime || "--";
            document.getElementById("uptime").textContent = data.uptime || "--";
            document.getElementById("memory").textContent = data.memory || "--";
            document.getElementById("circuit").textContent = data.circuit_breaker || "OFF";
            if (data.circuit_breaker === "ON") {
                document.getElementById("circuit").style.color = "#f87171";
            }
        });

        socket.on("market_data", (data) => {
            eventHistory.push(Date.now());
            const tbody = document.getElementById("market-data");
            if (!data || data.length === 0) return;
            tbody.innerHTML = data.map(row => {
                const priceClass = row.price_change > 0 ? "up" : row.price_change < 0 ? "down" : "";
                return `<tr>
                    <td><span class="highlight">${row.symbol}</span></td>
                    <td class="num ${priceClass}">${row.price ? row.price.toLocaleString("en-US", {minimumFractionDigits: 2}) : "--"}</td>
                    <td class="num">${row.funding != null ? (row.funding * 100).toFixed(4) + "%" : "--"}</td>
                    <td class="num">${row.predicted != null ? (row.predicted * 100).toFixed(4) + "%" : "--"}</td>
                    <td class="num">${row.oi != null ? (row.oi / 1e6).toFixed(1) + "M" : "--"}</td>
                    <td>${row.candles || "--"}</td>
                    <td class="muted">${row.last_tick || "--"}</td>
                </tr>`;
            }).join("");
        });

        socket.on("strategies", (data) => {
            const container = document.getElementById("strategies");
            container.innerHTML = data.map(s => `
                <div class="strategy-card ${s.active ? "strategy-active" : "strategy-inactive"}">
                    <div class="strategy-name">${s.name} ${s.active ? "●" : "○"}</div>
                    <div class="strategy-desc">${s.description}</div>
                    <div class="strategy-params">${s.params || ""}</div>
                    <div style="margin-top:6px; font-size:11px;">
                        Last signal: ${s.last_signal || "never"} | 
                        Signals today: ${s.signals_today || 0}
                    </div>
                </div>
            `).join("");
        });

        socket.on("signals", (data) => {
            const tbody = document.getElementById("signals");
            if (!data || data.length === 0) {
                tbody.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;">No signals yet</td></tr>`;
                return;
            }
            tbody.innerHTML = data.map(s => {
                const sideClass = s.side === "long" ? "up" : s.side === "short" ? "down" : "";
                return `<tr>
                    <td class="muted">${s.time || "--"}</td>
                    <td>${s.strategy}</td>
                    <td>${s.symbol}</td>
                    <td class="${sideClass}">${s.side ? s.side.toUpperCase() : "--"}</td>
                    <td class="num">${s.confidence != null ? (s.confidence * 100).toFixed(0) + "%" : "--"}</td>
                    <td class="muted">${s.reason || ""}</td>
                </tr>`;
            }).join("");
        });

        socket.on("portfolio", (data) => {
            document.getElementById("capital").textContent = data.capital != null
                ? "$" + data.capital.toLocaleString("en-US", {minimumFractionDigits: 2})
                : "--";
            const pnlEl = document.getElementById("daily-pnl");
            pnlEl.textContent = data.daily_pnl != null
                ? (data.daily_pnl >= 0 ? "+" : "") + "$" + data.daily_pnl.toFixed(2)
                : "--";
            pnlEl.className = "metric-value " + (data.daily_pnl >= 0 ? "up" : data.daily_pnl < 0 ? "down" : "");
            document.getElementById("drawdown").textContent = data.max_drawdown != null
                ? data.max_drawdown.toFixed(2) + "%"
                : "--";
            document.getElementById("positions-count").textContent = data.open_positions != null
                ? data.open_positions.toString()
                : "--";
            document.getElementById("daily-trades").textContent = data.daily_trades != null
                ? data.daily_trades.toString()
                : "--";
        });

        socket.on("positions", (data) => {
            const tbody = document.getElementById("positions");
            if (!data || data.length === 0) {
                tbody.innerHTML = `<tr><td colspan="7" class="muted" style="text-align:center;">No open positions</td></tr>`;
                return;
            }
            tbody.innerHTML = data.map(p => {
                const pnlClass = p.pnl_pct > 0 ? "up" : p.pnl_pct < 0 ? "down" : "";
                return `<tr>
                    <td>${p.symbol}</td>
                    <td class="${p.side === "long" ? "up" : "down"}">${p.side ? p.side.toUpperCase() : "--"}</td>
                    <td class="num">${p.size != null ? p.size.toFixed(4) : "--"}</td>
                    <td class="num">${p.entry_price != null ? p.entry_price.toFixed(2) : "--"}</td>
                    <td class="num">${p.current_price != null ? p.current_price.toFixed(2) : "--"}</td>
                    <td class="num ${pnlClass}">${p.pnl_pct != null ? p.pnl_pct.toFixed(2) + "%" : "--"}</td>
                    <td class="num muted">${p.duration || "--"}</td>
                </tr>`;
            }).join("");
        });

        socket.on("funding_comp", (data) => {
            const tbody = document.getElementById("funding-comp");
            if (!data || data.length === 0) return;
            tbody.innerHTML = data.map(row => `
                <tr>
                    <td>${row.symbol}</td>
                    <td class="num">${row.hl_funding != null ? (row.hl_funding * 100).toFixed(4) + "%" : "--"}</td>
                    <td class="num">${row.agg_funding != null ? (row.agg_funding * 100).toFixed(4) + "%" : "--"}</td>
                    <td class="num">${row.hl_oi != null ? (row.hl_oi / 1e6).toFixed(1) + "M" : "--"}</td>
                    <td class="num">${row.agg_oi != null ? (row.agg_oi / 1e6).toFixed(1) + "M" : "--"}</td>
                    <td class="muted">${row.exchanges || "--"}</td>
                </tr>
            `).join("");
        });

        socket.on("logs", (data) => {
            const container = document.getElementById("logs");
            const entries = data.map(l => {
                let cls = "log-info";
                if (l.level === "WARNING" || l.level === "WARN") cls = "log-warn";
                if (l.level === "ERROR") cls = "log-err";
                return `<div><span class="log-time">${l.time}</span> <span class="${cls}">[${l.level}]</span> ${l.message}</div>`;
            }).join("");
            container.innerHTML = entries;
            container.scrollTop = container.scrollHeight;
        });

        // Initial load
        fetch("/api/status").then(r => r.json()).then(data => {
            if (data.mode) document.getElementById("mode").textContent = data.mode;
        });
    </script>
</body>
</html>
'''

    @app.route("/")
    def index():
        return INDEX_HTML

    # ── REST API ──

    @app.route("/api/status")
    def api_status():
        if _engine is None:
            return jsonify({"status": "no_engine", "online": False})
        return jsonify({
            "status": "ok",
            "online": True,
            "mode": "paper",  # TODO: pass from main
            "uptime": _engine.uptime_sec if hasattr(_engine, "uptime_sec") else 0,
            "memory_mb": _engine.memory_mb if hasattr(_engine, "memory_mb") else 0,
            "circuit_breaker": "ON" if (_engine.risk_manager.circuit_breaker_tripped if hasattr(_engine, "risk_manager") else False) else "OFF",
        })

    @app.route("/api/market")
    def api_market():
        """Return current market data for all tracked assets."""
        if _engine is None:
            return jsonify([])
        
        result = []
        for sym in getattr(_engine, "_symbols", []):
            price = getattr(_engine, "_latest_prices", {}).get(sym)
            ctx = getattr(_engine, "_latest_ctx", {}).get(sym)
            agg = getattr(_engine, "_latest_aggregated_funding", {}).get(sym)
            candles = getattr(_engine, "_latest_candles", {}).get(sym, {})
            
            candle_status = []
            for tf, label in [(60, "1m"), (300, "5m"), (900, "15m"), (3600, "1h")]:
                c = candles.get(tf)
                if c:
                    candle_status.append(label)
            
            result.append({
                "symbol": sym,
                "price": price,
                "funding": getattr(ctx, "funding_rate", None) if ctx else None,
                "predicted": getattr(ctx, "predicted_funding", None) if ctx else None,
                "oi": getattr(ctx, "open_interest", None) if ctx else None,
                "agg_funding": getattr(agg, "funding_weighted", None) if agg else None,
                "agg_oi": getattr(agg, "oi_total", None) if agg else None,
                "candles": ", ".join(candle_status) if candle_status else "none",
                "last_tick": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            })
        return jsonify(result)

    @app.route("/api/strategies")
    def api_strategies():
        if _engine is None:
            return jsonify([])
        strategies = getattr(_engine, "_strategies", [])
        result = []
        for s in strategies:
            result.append({
                "name": getattr(s, "name", "unknown"),
                "active": True,
                "description": getattr(s, "__doc__", "No description"),
                "params": str(getattr(s, "params", {})),
                "last_signal": "--",
                "signals_today": 0,
            })
        return jsonify(result)

    @app.route("/api/signals")
    def api_signals():
        if _engine is None:
            return jsonify([])
        db = getattr(_engine, "_db", None)
        if db is None:
            return jsonify([])
        try:
            rows = db.get_signals(limit=10)
            return jsonify(rows)
        except Exception:
            return jsonify([])

    @app.route("/api/portfolio")
    def api_portfolio():
        if _engine is None:
            return jsonify({})
        portfolio = getattr(_engine, "portfolio", None)
        if portfolio is None:
            return jsonify({})
        return jsonify({
            "capital": getattr(portfolio, "sync_capital", lambda: 0)(),
            "daily_pnl": getattr(portfolio, "sync_daily_pnl", lambda: 0)(),
            "max_drawdown": getattr(portfolio, "sync_max_drawdown_pct", lambda: 0)(),
            "open_positions": len(getattr(portfolio, "get_positions_sync", lambda: {})()),
            "daily_trades": getattr(portfolio, "sync_daily_trades", lambda: 0)(),
        })

    @app.route("/api/positions")
    def api_positions():
        if _engine is None:
            return jsonify([])
        portfolio = getattr(_engine, "portfolio", None)
        if portfolio is None:
            return jsonify([])
        positions = getattr(portfolio, "get_positions_sync", lambda: {})()
        result = []
        for sym, pos in positions.items():
            result.append({
                "symbol": sym,
                "side": getattr(pos, "side", "--"),
                "size": getattr(pos, "size", 0),
                "entry_price": getattr(pos, "entry_price", 0),
                "current_price": getattr(pos, "current_price", getattr(pos, "entry_price", 0)),
                "pnl_pct": getattr(pos, "unrealized_pnl", 0) / getattr(pos, "entry_price", 1) * 100 if getattr(pos, "entry_price", 0) else 0,
                "duration": "--",
            })
        return jsonify(result)

    @app.route("/api/funding")
    def api_funding():
        """Return funding comparison: Hyperliquid vs Aggregated."""
        if _engine is None:
            return jsonify([])
        result = []
        for sym in getattr(_engine, "_symbols", []):
            ctx = getattr(_engine, "_latest_ctx", {}).get(sym)
            agg = getattr(_engine, "_latest_aggregated_funding", {}).get(sym)
            result.append({
                "symbol": sym,
                "hl_funding": getattr(ctx, "funding_rate", None) if ctx else None,
                "agg_funding": getattr(agg, "funding_weighted", None) if agg else None,
                "hl_oi": getattr(ctx, "open_interest", None) if ctx else None,
                "agg_oi": getattr(agg, "oi_total", None) if agg else None,
                "exchanges": getattr(agg, "exchange_count", 0) if agg else 0,
            })
        return jsonify(result)

    @app.route("/api/logs")
    def api_logs():
        """Return last N log entries."""
        # Read from the log file
        log_path = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "bot.log")
        log_path = os.path.abspath(log_path)
        entries = []
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                for line in lines[-20:]:
                    line = line.strip()
                    if not line:
                        continue
                    # Parse log format: 2026-05-07 00:00:00 | LEVEL | module | message
                    parts = line.split(" | ")
                    if len(parts) >= 4:
                        entries.append({
                            "time": parts[0],
                            "level": parts[1].strip(),
                            "module": parts[2].strip(),
                            "message": " | ".join(parts[3:]),
                        })
                    else:
                        entries.append({
                            "time": "",
                            "level": "INFO",
                            "module": "",
                            "message": line,
                        })
        except Exception:
            pass
        return jsonify(entries)

    # ── Socket.IO emitters ──

    def emit_updates():
        """Called by main.py to push real-time updates via Socket.IO."""
        if _socketio is None or _engine is None:
            return
        try:
            # Status
            _socketio.emit("status_update", {
                "mode": "paper",
                "uptime": getattr(_engine, "uptime_sec", 0),
                "memory": getattr(_engine, "memory_mb", 0),
                "circuit_breaker": "ON" if getattr(getattr(_engine, "risk_manager", None), "circuit_breaker_tripped", False) else "OFF",
            })

            # Market data
            market_data = []
            for sym in getattr(_engine, "_symbols", []):
                price = getattr(_engine, "_latest_prices", {}).get(sym)
                ctx = getattr(_engine, "_latest_ctx", {}).get(sym)
                agg = getattr(_engine, "_latest_aggregated_funding", {}).get(sym)
                candles = getattr(_engine, "_latest_candles", {}).get(sym, {})
                candle_status = []
                for tf, label in [(60, "1m"), (300, "5m"), (900, "15m"), (3600, "1h")]:
                    if candles.get(tf):
                        candle_status.append(label)
                market_data.append({
                    "symbol": sym,
                    "price": price,
                    "funding": getattr(ctx, "funding_rate", None) if ctx else None,
                    "predicted": getattr(ctx, "predicted_funding", None) if ctx else None,
                    "oi": getattr(ctx, "open_interest", None) if ctx else None,
                    "candles": ", ".join(candle_status) if candle_status else "none",
                    "last_tick": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                })
            _socketio.emit("market_data", market_data)

            # Strategies
            strategies = getattr(_engine, "_strategies", [])
            strat_data = []
            for s in strategies:
                strat_data.append({
                    "name": getattr(s, "name", "unknown"),
                    "active": True,
                    "description": getattr(s, "__doc__", "No description"),
                    "params": str(getattr(s, "params", {})),
                    "last_signal": "--",
                    "signals_today": 0,
                })
            _socketio.emit("strategies", strat_data)

            # Portfolio
            portfolio = getattr(_engine, "portfolio", None)
            if portfolio:
                _socketio.emit("portfolio", {
                    "capital": getattr(portfolio, "sync_capital", lambda: 0)(),
                    "daily_pnl": getattr(portfolio, "sync_daily_pnl", lambda: 0)(),
                    "max_drawdown": getattr(portfolio, "sync_max_drawdown_pct", lambda: 0)(),
                    "open_positions": len(getattr(portfolio, "get_positions_sync", lambda: {})()),
                    "daily_trades": getattr(portfolio, "sync_daily_trades", lambda: 0)(),
                })

            # Positions
            positions = getattr(portfolio, "get_positions_sync", lambda: {})() if portfolio else {}
            pos_data = []
            for sym, pos in positions.items():
                pos_data.append({
                    "symbol": sym,
                    "side": getattr(pos, "side", "--"),
                    "size": getattr(pos, "size", 0),
                    "entry_price": getattr(pos, "entry_price", 0),
                    "current_price": getattr(pos, "current_price", getattr(pos, "entry_price", 0)),
                    "pnl_pct": getattr(pos, "unrealized_pnl", 0) / getattr(pos, "entry_price", 1) * 100 if getattr(pos, "entry_price", 0) else 0,
                    "duration": "--",
                })
            _socketio.emit("positions", pos_data)

            # Funding comparison
            funding_data = []
            for sym in getattr(_engine, "_symbols", []):
                ctx = getattr(_engine, "_latest_ctx", {}).get(sym)
                agg = getattr(_engine, "_latest_aggregated_funding", {}).get(sym)
                funding_data.append({
                    "symbol": sym,
                    "hl_funding": getattr(ctx, "funding_rate", None) if ctx else None,
                    "agg_funding": getattr(agg, "funding_weighted", None) if agg else None,
                    "hl_oi": getattr(ctx, "open_interest", None) if ctx else None,
                    "agg_oi": getattr(agg, "oi_total", None) if agg else None,
                    "exchanges": getattr(agg, "exchange_count", 0) if agg else 0,
                })
            _socketio.emit("funding_comp", funding_data)

            # Logs
            log_path = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "bot.log")
            log_path = os.path.abspath(log_path)
            log_entries = []
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                    for line in lines[-20:]:
                        line = line.strip()
                        if not line:
                            continue
                        parts = line.split(" | ")
                        if len(parts) >= 4:
                            log_entries.append({
                                "time": parts[0],
                                "level": parts[1].strip(),
                                "message": " | ".join(parts[3:]),
                            })
            except Exception:
                pass
            _socketio.emit("logs", log_entries)

        except Exception as e:
            logger.warning("emit_updates failed: %s", e)

    # ── Socket.IO events ──

    @socketio.on("connect")
    def on_connect(auth=None):
        logger.info("Dashboard client connected")
        emit("status_update", {
            "mode": "paper",
            "uptime": getattr(_engine, "uptime_sec", 0) if _engine else 0,
            "memory": getattr(_engine, "memory_mb", 0) if _engine else 0,
            "circuit_breaker": "OFF",
        })
        # Push immediate data
        emit_updates()

    @socketio.on("disconnect")
    def on_disconnect(reason=None):
        logger.info("Dashboard client disconnected: %s", reason)

    return app, socketio, emit_updates
