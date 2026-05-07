"""
Hyperliquid Premium — Real-Time Operations Dashboard v2
Complete rewrite for maximum visibility into bot internals.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable

from flask import Flask, jsonify
from flask_socketio import SocketIO, emit

logger = logging.getLogger(__name__)

# Globals set by main.py
_engine: Optional[Any] = None
_socketio: Optional[SocketIO] = None
_emit_fn: Optional[Callable] = None


def set_engine(engine: Any) -> None:
    global _engine
    _engine = engine


def get_engine() -> Optional[Any]:
    return _engine


# ═════════════════════════════════════════════════════════════════════════════
# Background emitter — pushes real-time data every second
# ═════════════════════════════════════════════════════════════════════════════

class DashboardEmitter:
    """Emits real-time dashboard updates every second via Socket.IO."""

    def __init__(self, socketio: SocketIO, interval: float = 1.0):
        self.socketio = socketio
        self.interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Dashboard emitter started (interval=%ss)", self.interval)

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                self._emit_all()
            except Exception as e:
                logger.warning("Dashboard emitter error: %s", e)
            time.sleep(self.interval)

    def _emit_all(self) -> None:
        if _engine is None or self.socketio is None:
            return

        try:
            self._emit_status()
            self._emit_live_data()
            self._emit_engine_monitor()
            self._emit_candles()
            self._emit_strategies()
            self._emit_signals()
            self._emit_decisions()
            self._emit_portfolio()
            self._emit_positions()
            self._emit_logs()
        except Exception as e:
            logger.warning("emit_all error: %s", e)

    def _safe_emit(self, event: str, data: dict) -> None:
        """Emit to all connected Socket.IO clients."""
        if self.socketio is None:
            return
        try:
            self.socketio.emit(event, data)
        except Exception as e:
            logger.warning("emit %s error: %s", event, e)

    # -- Emitters --

    def _emit_status(self) -> None:
        self._safe_emit("status", {
            "mode": getattr(_engine, "_config", {}).get("mode", "paper") if hasattr(_engine, "_config") else "paper",
            "uptime_sec": getattr(_engine, "uptime_sec", 0),
            "memory_mb": getattr(_engine, "memory_mb", 0),
            "circuit_breaker": "ON" if getattr(getattr(_engine, "_risk", None), "circuit_breaker_tripped", False) else "OFF",
            "running": getattr(_engine, "_running", False),
        })

    def _emit_live_data(self) -> None:
        """Raw market data feed — prices, funding, OI, volume, imbalance."""
        rows = []
        symbols = getattr(_engine, "_symbols", [])
        prices = getattr(_engine, "_latest_price", {})
        ctxs = getattr(_engine, "_latest_ctx", {})
        events = getattr(_engine, "_last_market_events", {})

        for sym in symbols:
            price = prices.get(sym)
            ctx = ctxs.get(sym)
            evt = events.get(sym, {})
            ob = getattr(_engine, "_latest_orderbook", {}).get(sym)

            rows.append({
                "symbol": sym,
                "price": getattr(price, "mid", None) if price else None,
                "bid": getattr(price, "bid", None) if price else None,
                "ask": getattr(price, "ask", None) if price else None,
                "spread_pct": round((getattr(price, "ask", 0) - getattr(price, "bid", 0)) / getattr(price, "mid", 1) * 100, 4) if price else None,
                "funding": getattr(ctx, "funding_rate", None) if ctx else None,
                "predicted": getattr(ctx, "predicted_funding", None) if ctx else None,
                "oi": getattr(ctx, "open_interest", None) if ctx else None,
                "volume_1m": evt.get("volume_1m"),
                "imbalance": evt.get("bid_ask_imbalance"),
                "vwap": evt.get("vwap_15m"),
                # Orderbook
                "ob_spread": evt.get("orderbook_spread_pct"),
                "ob_oir": evt.get("orderbook_oir"),
                "ob_depth": evt.get("orderbook_depth_quality"),
                "ob_bid_wall": evt.get("orderbook_largest_bid_wall"),
                "ob_ask_wall": evt.get("orderbook_largest_ask_wall"),
                "rvol": evt.get("last_realized_vol"),
                "last_update": evt.get("processed_at", 0),
            })
        self._safe_emit("live_data", rows)

    def _emit_engine_monitor(self) -> None:
        """Engine health — ticks/sec, total ticks, last error."""
        stats = getattr(_engine, "_tick_stats", {})
        last_err = getattr(_engine, "_last_error", None)
        last_events = getattr(_engine, "_last_market_events", {})

        # Last 3 events with timestamps
        recent = []
        for sym, evt in sorted(last_events.items(), key=lambda x: x[1].get("processed_at", 0), reverse=True)[:3]:
            recent.append({
                "symbol": sym,
                "price": evt.get("price"),
                "age_ms": int((time.time() - evt.get("processed_at", 0)) * 1000),
            })

        self._safe_emit("engine_monitor", {
            "ticks_per_second": stats.get("per_second", 0),
            "total_ticks": stats.get("total", 0),
            "last_error": last_err,
            "recent_events": recent,
            "symbols": getattr(_engine, "_symbols", []),
            "strategies": [getattr(s, "name", "unknown") for s in getattr(_engine, "_strategies", [])],
        })

    def _emit_candles(self) -> None:
        """Candle status — which timeframes have data, OHLCV if available."""
        symbols = getattr(_engine, "_symbols", [])
        candles = getattr(_engine, "_latest_candles", {})
        result = []
        for sym in symbols:
            sym_candles = candles.get(sym, {})
            for tf, label in [(60, "1m"), (300, "5m"), (900, "15m"), (3600, "1h")]:
                c = sym_candles.get(tf)
                if c:
                    result.append({
                        "symbol": sym,
                        "timeframe": label,
                        "open": getattr(c, "open", None),
                        "high": getattr(c, "high", None),
                        "low": getattr(c, "low", None),
                        "close": getattr(c, "close", None),
                        "volume": getattr(c, "volume", None),
                        "oi_delta": getattr(c, "oi_delta", None),
                        "buy_volume": getattr(c, "buy_volume", None),
                        "sell_volume": getattr(c, "sell_volume", None),
                        "vwap": getattr(c, "vwap", None),
                        "timestamp_ms": getattr(c, "timestamp_ms", None),
                    })
        self._safe_emit("candles", result)

    def _emit_strategies(self) -> None:
        """Strategy state — params, last signal, signals today."""
        strategies = getattr(_engine, "_strategies", [])
        sig_hist = getattr(_engine, "_signal_history", [])

        result = []
        for s in strategies:
            name = getattr(s, "name", "unknown")
            # Count signals today for this strategy
            today_count = sum(
                1 for sig in sig_hist
                if sig.get("strategy") == name and sig.get("time", "").startswith(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            )
            # Last signal
            last = next((sig for sig in sig_hist if sig.get("strategy") == name), None)

            result.append({
                "name": name,
                "enabled": getattr(s, "enabled", True),
                "description": (getattr(s, "__doc__", "") or "No description").split("\n")[0][:80],
                "params": str(getattr(s, "params", {})),
                "last_signal_time": last.get("time") if last else None,
                "last_signal_side": last.get("side") if last else None,
                "last_signal_confidence": last.get("confidence") if last else None,
                "last_signal_status": last.get("status") if last else None,
                "signals_today": today_count,
            })
        self._safe_emit("strategies", result)

    def _emit_signals(self) -> None:
        """Last 20 signals with full detail."""
        sig_hist = getattr(_engine, "_signal_history", [])[:20]
        self._safe_emit("signals", sig_hist)

    def _emit_decisions(self) -> None:
        """Risk + execution decisions."""
        decisions = getattr(_engine, "_decision_history", [])[:20]
        self._safe_emit("decisions", decisions)

    def _emit_portfolio(self) -> None:
        portfolio = getattr(_engine, "portfolio", None)
        if portfolio is None:
            return
        try:
            self._safe_emit("portfolio", {
                "capital": getattr(portfolio, "sync_capital", lambda: 0)(),
                "daily_pnl": getattr(portfolio, "sync_daily_pnl", lambda: 0)(),
                "max_drawdown_pct": getattr(portfolio, "sync_max_drawdown_pct", lambda: 0)(),
                "open_positions": len(getattr(portfolio, "get_positions_sync", lambda: {})()),
                "daily_trades": getattr(portfolio, "sync_daily_trades", lambda: 0)(),
                "total_trades": getattr(portfolio, "sync_total_trades", lambda: 0)(),
            })
        except Exception:
            pass

    def _emit_positions(self) -> None:
        portfolio = getattr(_engine, "portfolio", None)
        if portfolio is None:
            return
        try:
            positions = getattr(portfolio, "get_positions_sync", lambda: {})()
            result = []
            for sym, pos in positions.items():
                entry = getattr(pos, "entry_price", 0)
                current = getattr(pos, "current_price", entry)
                unrealized = getattr(pos, "unrealized_pnl", 0)
                pnl_pct = (unrealized / (entry * getattr(pos, "size", 0)) * 100) if entry and getattr(pos, "size", 0) else 0
                result.append({
                    "symbol": sym,
                    "side": getattr(pos, "side", "--"),
                    "size": getattr(pos, "size", 0),
                    "entry_price": entry,
                    "current_price": current,
                    "unrealized_pnl": unrealized,
                    "pnl_pct": pnl_pct,
                    "stop_loss": getattr(pos, "stop_loss_price", None),
                    "take_profit": getattr(pos, "take_profit_price", None),
                    "strategy": getattr(pos, "metadata", {}).get("strategy", "unknown"),
                })
            self._safe_emit("positions", result)
        except Exception:
            pass

    def _emit_logs(self) -> None:
        log_path = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "bot.log")
        log_path = os.path.abspath(log_path)
        entries = []
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                for line in lines[-50:]:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(" | ")
                    if len(parts) >= 4:
                        entries.append({
                            "time": parts[0],
                            "level": parts[1].strip(),
                            "module": parts[2].strip(),
                            "message": " | ".join(parts[3:]),
                        })
                    else:
                        entries.append({"time": "", "level": "INFO", "module": "", "message": line})
        except Exception:
            pass
        self._safe_emit("logs", entries)


# ═════════════════════════════════════════════════════════════════════════════
# Flask app + Socket.IO
# ═════════════════════════════════════════════════════════════════════════════

def create_app(config: Dict[str, Any]) -> tuple:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.get("secret_key", "dev-secret-123")
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

    global _socketio
    _socketio = socketio

    # Start background emitter
    emitter = DashboardEmitter(socketio)
    emitter.start()

    # ── HTML Template ──
    INDEX_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Hyperliquid Bot — Live Ops</title>
    <style>
        :root {
            --bg: #0a0a0a;
            --panel: #111;
            --panel-hover: #161616;
            --border: #222;
            --text: #e0e0e0;
            --muted: #666;
            --dim: #888;
            --green: #4ade80;
            --red: #f87171;
            --yellow: #fbbf24;
            --blue: #60a5fa;
            --purple: #c084fc;
            --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "SF Mono", Consolas, monospace;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: var(--font);
            background: var(--bg);
            color: var(--text);
            font-size: 12px;
            line-height: 1.4;
        }

        /* Header */
        .header {
            background: var(--panel);
            border-bottom: 1px solid var(--border);
            padding: 10px 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            position: sticky;
            top: 0;
            z-index: 100;
        }
        .header h1 { font-size: 14px; font-weight: 600; }
        .header .badges { display: flex; gap: 8px; align-items: center; }
        .badge {
            padding: 3px 10px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .badge-green { background: #1a472a; color: var(--green); }
        .badge-yellow { background: #451a03; color: var(--yellow); }
        .badge-red { background: #450a0a; color: var(--red); }
        .badge-blue { background: #1e3a5f; color: var(--blue); }

        /* Layout */
        .layout {
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 12px;
            padding: 12px;
        }
        @media (max-width: 1200px) { .layout { grid-template-columns: 1fr 1fr; } }
        @media (max-width: 800px) { .layout { grid-template-columns: 1fr; } }

        .panel {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 6px;
            overflow: hidden;
        }
        .panel-header {
            background: var(--panel-hover);
            padding: 8px 12px;
            border-bottom: 1px solid var(--border);
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.8px;
            color: var(--dim);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .panel-header .live-dot {
            width: 6px; height: 6px; border-radius: 50%; background: var(--green);
            animation: blink 1s infinite;
        }
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
        .panel-body { padding: 10px 12px; max-height: 320px; overflow-y: auto; }
        .panel-body::-webkit-scrollbar { width: 4px; }
        .panel-body::-webkit-scrollbar-thumb { background: #333; border-radius: 2px; }

        /* Tables */
        table { width: 100%; border-collapse: collapse; font-size: 11px; }
        th { text-align: left; padding: 5px 6px; color: var(--muted); font-weight: 500; border-bottom: 1px solid var(--border); font-size: 10px; text-transform: uppercase; }
        td { padding: 4px 6px; border-bottom: 1px solid #1a1a1a; vertical-align: top; }
        tr:hover td { background: var(--panel-hover); }
        .num { font-family: "SF Mono", Consolas, monospace; text-align: right; }
        .up { color: var(--green); }
        .down { color: var(--red); }
        .yellow { color: var(--yellow); }
        .blue { color: var(--blue); }
        .purple { color: var(--purple); }
        .muted { color: var(--muted); }
        .dim { color: var(--dim); }
        .highlight { color: var(--blue); font-weight: 600; }
        .tiny { font-size: 10px; }
        .mono { font-family: "SF Mono", Consolas, monospace; }

        /* Metrics */
        .metric-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #1a1a1a; }
        .metric-label { color: var(--dim); }
        .metric-value { font-family: monospace; }

        /* Signal status pills */
        .pill {
            display: inline-block;
            padding: 1px 6px;
            border-radius: 3px;
            font-size: 9px;
            font-weight: 700;
            text-transform: uppercase;
        }
        .pill-pending { background: #1e3a5f; color: var(--blue); }
        .pill-approved { background: #1a472a; color: var(--green); }
        .pill-rejected { background: #450a0a; color: var(--red); }
        .pill-executed { background: #365314; color: #a3e635; }

        /* Strategy card */
        .strategy-card {
            padding: 8px;
            margin-bottom: 6px;
            background: var(--panel-hover);
            border-radius: 4px;
            border-left: 3px solid var(--border);
        }
        .strategy-card.active { border-left-color: var(--green); cursor: pointer; }
        .strategy-card.inactive { border-left-color: var(--muted); opacity: 0.6; cursor: pointer; }
        .strategy-card:hover { opacity: 1.0; filter: brightness(1.2); }
        .strategy-name { font-weight: 600; font-size: 12px; margin-bottom: 2px; }
        .strategy-desc { color: var(--dim); font-size: 10px; }
        .strategy-params { margin-top: 4px; font-family: monospace; font-size: 10px; color: var(--muted); }

        /* Log box */
        .log-box {
            max-height: 200px;
            overflow-y: auto;
            font-family: monospace;
            font-size: 10px;
            line-height: 1.5;
            color: var(--dim);
        }
        .log-time { color: var(--muted); }
        .log-info { color: var(--blue); }
        .log-warn { color: var(--yellow); }
        .log-err { color: var(--red); }

        /* Engine tick indicator */
        .tick-bar {
            display: flex;
            gap: 2px;
            margin-top: 6px;
        }
        .tick-cell {
            flex: 1;
            height: 12px;
            background: #1a1a1a;
            border-radius: 2px;
            transition: background 0.3s;
        }
        .tick-cell.active { background: var(--green); }
        .tick-cell.recent { background: #365314; }

        /* Sparkline placeholder */
        .spark { height: 20px; background: linear-gradient(90deg, var(--green) 0%, var(--green) 60%, var(--red) 100%); border-radius: 2px; margin-top: 4px; opacity: 0.5; }
        /* Modal */
        .modal-overlay {
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.8); z-index: 1000; justify-content: center; align-items: center;
        }
        .modal-overlay.active { display: flex; }
        .modal-box {
            background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
            width: 90%; max-width: 600px; max-height: 80vh; overflow-y: auto; padding: 16px;
        }
        .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .modal-title { font-size: 14px; font-weight: 600; }
        .modal-close { cursor: pointer; font-size: 18px; color: var(--muted); }
        .modal-close:hover { color: var(--text); }
        .stat-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid var(--border); }
        .stat-label { color: var(--dim); }
        .stat-value { font-weight: 600; }

    </style>
</head>
<body>
    <div class="header">
        <div style="display:flex;align-items:center;gap:10px;">
            <span class="badge badge-green" id="conn-badge">LIVE</span>
            <h1>Hyperliquid Bot — Operations Dashboard</h1>
        </div>
        <div class="badges">
            <span class="badge badge-blue" id="mode-badge">PAPER</span>
            <span class="badge badge-green" id="status-badge">RUNNING</span>
            <span class="badge badge-yellow" id="circuit-badge">CB: OFF</span>
            <span class="badge badge-blue" id="uptime-badge">00:00:00</span>
        </div>
    </div>

    <div class="layout">
        <!-- Column 1: Live Data -->
        <div>
            <!-- Live Data Stream -->
            <div class="panel">
                <div class="panel-header">
                    <span>Live Data Stream</span>
                    <span class="live-dot"></span>
                </div>
                <div class="panel-body">
                    <table>
                        <thead>
                            <tr>
                                <th>Asset</th>
                                <th class="num">Price</th>
                                <th class="num">Bid/Ask</th>
                                <th class="num">Sprd%</th>
                                <th class="num">Funding</th>
                                <th class="num">Pred</th>
                                <th class="num">OI (M)</th>
                                <th class="num">Vol 1m</th>
                                <th class="num">Imbal</th>
                                <th class="num">OIR</th>
                                <th class="num">Depth</th>
                                <th class="num">RVol</th>
                                <th class="num">Age</th>
                            </tr>
                        </thead>
                        <tbody id="live-data-tbody">
                            <tr><td colspan="10" class="muted" style="text-align:center;">Waiting for data...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Candle Watch -->
            <div class="panel">
                <div class="panel-header">Candle Watch <span class="dim">OHLCV</span></div>
                <div class="panel-body">
                    <table>
                        <thead>
                            <tr>
                                <th>Sym</th>
                                <th>TF</th>
                                <th class="num">Open</th>
                                <th class="num">High</th>
                                <th class="num">Low</th>
                                <th class="num">Close</th>
                                <th class="num">Volume</th>
                                <th class="num">Buy%</th>
                                <th class="num">VWAP</th>
                            </tr>
                        </thead>
                        <tbody id="candles-tbody">
                            <tr><td colspan="9" class="muted" style="text-align:center;">No candles yet</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Column 2: Engine & Strategies -->
        <div>
            <!-- Engine Monitor -->
            <div class="panel">
                <div class="panel-header">Engine Monitor</div>
                <div class="panel-body">
                    <div class="metric-row">
                        <span class="metric-label">Ticks / second</span>
                        <span class="metric-value" id="ticks-per-sec">--</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Total ticks</span>
                        <span class="metric-value" id="total-ticks">--</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Memory</span>
                        <span class="metric-value" id="mem-mb">--</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Last error</span>
                        <span class="metric-value" id="last-error" style="color:var(--red);">--</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Assets</span>
                        <span class="metric-value" id="assets-list">--</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Strategies</span>
                        <span class="metric-value" id="strat-list">--</span>
                    </div>
                    <div style="margin-top:8px; font-size:10px; color:var(--dim);">Recent events:</div>
                    <div id="recent-events" style="font-size:10px; color:var(--muted); margin-top:4px;">--</div>
                </div>
            </div>

            <!-- Strategies Detail -->
            <div class="panel">
                <div class="panel-header">Strategies <span class="dim">Active</span></div>
                <div class="panel-body" id="strategies-container">
                    <div class="muted" style="text-align:center;">Loading...</div>
                </div>
            </div>

            <!-- Decision Log -->
            <div class="panel">
                <div class="panel-header">Decision Log <span class="dim">Risk + Execution</span></div>
                <div class="panel-body">
                    <table>
                        <thead>
                            <tr>
                                <th>Time</th>
                                <th>Type</th>
                                <th>Sym</th>
                                <th>Side</th>
                                <th>Result</th>
                                <th>Reason</th>
                            </tr>
                        </thead>
                        <tbody id="decisions-tbody">
                            <tr><td colspan="6" class="muted" style="text-align:center;">No decisions yet</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Column 3: Signals, Portfolio, Logs -->
        <div>
            <!-- Signal Stream -->
            <div class="panel">
                <div class="panel-header">Signal Stream <span class="dim">Last 20</span></div>
                <div class="panel-body">
                    <table>
                        <thead>
                            <tr>
                                <th>Time</th>
                                <th>Strat</th>
                                <th>Sym</th>
                                <th>Side</th>
                                <th class="num">Conf</th>
                                <th>Status</th>
                                <th>Reason</th>
                            </tr>
                        </thead>
                        <tbody id="signals-tbody">
                            <tr><td colspan="7" class="muted" style="text-align:center;">No signals yet</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Portfolio -->
            <div class="panel">
                <div class="panel-header">Portfolio</div>
                <div class="panel-body">
                    <div class="metric-row">
                        <span class="metric-label">Capital</span>
                        <span class="metric-value" id="capital">--</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Daily PnL</span>
                        <span class="metric-value" id="daily-pnl">--</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Max Drawdown</span>
                        <span class="metric-value" id="drawdown">--</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Open Positions</span>
                        <span class="metric-value" id="open-pos">--</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Daily Trades</span>
                        <span class="metric-value" id="daily-trades">--</span>
                    </div>
                </div>
            </div>

            <!-- Open Positions -->
            <div class="panel">
                <div class="panel-header">Open Positions</div>
                <div class="panel-body">
                    <table>
                        <thead>
                            <tr>
                                <th>Sym</th>
                                <th>Side</th>
                                <th class="num">Size</th>
                                <th class="num">Entry</th>
                                <th class="num">Current</th>
                                <th class="num">PnL%</th>
                                <th>Strat</th>
                            </tr>
                        </thead>
                        <tbody id="positions-tbody">
                            <tr><td colspan="7" class="muted" style="text-align:center;">No positions</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Live Logs -->
            <div class="panel">
                <div class="panel-header">Live Logs <span class="dim">Last 50</span></div>
                <div class="panel-body">
                    <div id="logs-container" class="log-box">Waiting for logs...</div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <script>
        const socket = io();

        function fmtNum(v, d=2) {
            if (v == null || isNaN(v)) return "--";
            return v.toLocaleString("en-US", {minimumFractionDigits:d, maximumFractionDigits:d});
        }
        function fmtPct(v, d=4) {
            if (v == null || isNaN(v)) return "--";
            return (v * 100).toFixed(d) + "%";
        }
        function fmtM(v) {
            if (v == null || isNaN(v)) return "--";
            return (v / 1e6).toFixed(2) + "M";
        }
        function fmtAge(seconds) {
            if (!seconds) return "--";
            if (seconds < 1) return (seconds * 1000).toFixed(0) + "ms";
            if (seconds < 60) return seconds.toFixed(1) + "s";
            return (seconds / 60).toFixed(1) + "m";
        }
        function timeNow() {
            const d = new Date();
            return d.toTimeString().split(" ")[0];
        }

        // ── Status ──
        socket.on("status", (d) => {
            document.getElementById("mode-badge").textContent = (d.mode || "PAPER").toUpperCase();
            const statusEl = document.getElementById("status-badge");
            statusEl.textContent = d.running ? "RUNNING" : "STOPPED";
            statusEl.className = "badge " + (d.running ? "badge-green" : "badge-red");
            const cbEl = document.getElementById("circuit-badge");
            cbEl.textContent = "CB: " + d.circuit_breaker;
            cbEl.className = "badge " + (d.circuit_breaker === "ON" ? "badge-red" : "badge-yellow");
            const uptime = d.uptime_sec || 0;
            const h = Math.floor(uptime / 3600).toString().padStart(2, "0");
            const m = Math.floor((uptime % 3600) / 60).toString().padStart(2, "0");
            const s = (uptime % 60).toString().padStart(2, "0");
            document.getElementById("uptime-badge").textContent = `${h}:${m}:${s}`;
        });

        // ── Live Data Stream ──
        socket.on("live_data", (rows) => {
            const tbody = document.getElementById("live-data-tbody");
            if (!rows || rows.length === 0) {
                tbody.innerHTML = `<tr><td colspan="13" class="muted" style="text-align:center;">Waiting for data...</td></tr>`;
                return;
            }
            const now = Date.now() / 1000;
            tbody.innerHTML = rows.map(r => {
                const priceClass = r.price > r.vwap ? "up" : r.price < r.vwap ? "down" : "";
                const age = r.last_update ? now - r.last_update : null;
                const ageClass = age && age > 5 ? "yellow" : age && age > 30 ? "red" : "";
                const oirClass = r.ob_oir > 0.5 ? "up" : r.ob_oir < -0.5 ? "down" : "";
                const rvolClass = r.rvol != null ? (r.rvol > 0.40 ? "yellow" : r.rvol < 0.20 ? "up" : "") : "";
                return `<tr>
                    <td><span class="highlight">${r.symbol}</span></td>
                    <td class="num ${priceClass}">${fmtNum(r.price)}</td>
                    <td class="num tiny muted">${fmtNum(r.bid, 2)} / ${fmtNum(r.ask, 2)}</td>
                    <td class="num tiny">${fmtNum(r.spread_pct, 4)}</td>
                    <td class="num">${fmtPct(r.funding, 4)}</td>
                    <td class="num">${fmtPct(r.predicted, 4)}</td>
                    <td class="num">${fmtM(r.oi)}</td>
                    <td class="num">${fmtNum(r.volume_1m)}</td>
                    <td class="num ${r.imbalance > 0 ? 'up' : r.imbalance < 0 ? 'down' : ''}">${fmtNum(r.imbalance, 3)}</td>
                    <td class="num ${oirClass}">${r.ob_oir != null ? r.ob_oir.toFixed(2) : "--"}</td>
                    <td class="num">${r.ob_depth != null ? (r.ob_depth * 100).toFixed(0) + "%" : "--"}</td>
                    <td class="num ${rvolClass}">${r.rvol != null ? (r.rvol * 100).toFixed(0) + "%" : "--"}</td>
                    <td class="num tiny ${ageClass}">${fmtAge(age)}</td>
                </tr>`;
            }).join("");
        });

        // ── Engine Monitor ──
        socket.on("engine_monitor", (d) => {
            document.getElementById("ticks-per-sec").textContent = d.ticks_per_second != null ? d.ticks_per_second.toFixed(1) : "--";
            document.getElementById("total-ticks").textContent = d.total_ticks != null ? d.total_ticks.toLocaleString() : "--";
            document.getElementById("mem-mb").textContent = d.memory_mb != null ? d.memory_mb.toFixed(1) + " MB" : "--";
            const errEl = document.getElementById("last-error");
            if (d.last_error) {
                errEl.textContent = d.last_error.substring(0, 60);
                errEl.style.display = "inline";
            } else {
                errEl.textContent = "None";
                errEl.style.color = "var(--green)";
            }
            document.getElementById("assets-list").textContent = (d.symbols || []).join(", ");
            document.getElementById("strat-list").textContent = (d.strategies || []).join(", ");

            const recentEl = document.getElementById("recent-events");
            if (d.recent_events && d.recent_events.length > 0) {
                recentEl.innerHTML = d.recent_events.map(e =>
                    `<span class="highlight">${e.symbol}</span> @ ${fmtNum(e.price)} (${e.age_ms}ms ago)`
                ).join("<br>");
            } else {
                recentEl.textContent = "No events yet";
            }
        });

        // ── Candles ──
        socket.on("candles", (rows) => {
            const tbody = document.getElementById("candles-tbody");
            if (!rows || rows.length === 0) {
                tbody.innerHTML = `<tr><td colspan="9" class="muted" style="text-align:center;">No candles yet</td></tr>`;
                return;
            }
            tbody.innerHTML = rows.map(r => {
                const chg = r.close != null && r.open != null ? r.close - r.open : 0;
                const chgClass = chg > 0 ? "up" : chg < 0 ? "down" : "";
                const buyPct = r.buy_volume != null && r.volume > 0 ? (r.buy_volume / r.volume * 100) : null;
                return `<tr>
                    <td><span class="highlight">${r.symbol}</span></td>
                    <td class="mono">${r.timeframe}</td>
                    <td class="num">${fmtNum(r.open)}</td>
                    <td class="num">${fmtNum(r.high)}</td>
                    <td class="num">${fmtNum(r.low)}</td>
                    <td class="num ${chgClass}">${fmtNum(r.close)}</td>
                    <td class="num">${fmtNum(r.volume)}</td>
                    <td class="num ${buyPct > 60 ? 'up' : buyPct < 40 ? 'down' : ''}">${buyPct != null ? buyPct.toFixed(0) + "%" : "--"}</td>
                    <td class="num">${fmtNum(r.vwap)}</td>
                </tr>`;
            }).join("");
        });

        // ── Strategies ──
        socket.on("strategies", (data) => {
            const container = document.getElementById("strategies-container");
            if (!data || data.length === 0) {
                container.innerHTML = `<div class="muted" style="text-align:center;">No strategies</div>`;
                return;
            }
            container.innerHTML = data.map(s => `
                <div class="strategy-card ${s.enabled ? 'active' : 'inactive'}" onclick="showStrategyDetail('${s.name}')">
                    <div class="strategy-name">${s.name} ${s.enabled ? "●" : "○"}</div>
                    <div class="strategy-desc">${s.description || "No description"}</div>
                    <div class="strategy-params">${s.params || ""}</div>
                    <div style="margin-top:4px; font-size:10px; color:var(--dim);">
                        Last signal: <span class="${s.last_signal_side === 'long' ? 'up' : s.last_signal_side === 'short' ? 'down' : ''}">
                            ${s.last_signal_side ? s.last_signal_side.toUpperCase() : "never"}
                        </span>
                        ${s.last_signal_confidence != null ? `(${Math.round(s.last_signal_confidence * 100)}%)` : ""}
                        <span class="pill pill-${s.last_signal_status || 'pending'}">${s.last_signal_status || "PENDING"}</span>
                        | Today: ${s.signals_today || 0}
                    </div>
                </div>
            `).join("");
        });

        // ── Signals ──
        socket.on("signals", (data) => {
            const tbody = document.getElementById("signals-tbody");
            if (!data || data.length === 0) {
                tbody.innerHTML = `<tr><td colspan="7" class="muted" style="text-align:center;">No signals yet</td></tr>`;
                return;
            }
            tbody.innerHTML = data.slice(0, 20).map(s => {
                const sideClass = s.side === "long" ? "up" : s.side === "short" ? "down" : "";
                const statusClass = s.status === "executed" ? "pill-executed" : s.status === "approved" ? "pill-approved" : s.status === "rejected" ? "pill-rejected" : "pill-pending";
                return `<tr>
                    <td class="muted tiny">${s.time || "--"}</td>
                    <td>${s.strategy}</td>
                    <td><span class="highlight">${s.symbol}</span></td>
                    <td class="${sideClass}">${s.side ? s.side.toUpperCase() : "--"}</td>
                    <td class="num">${s.confidence != null ? (s.confidence * 100).toFixed(0) + "%" : "--"}</td>
                    <td><span class="pill ${statusClass}">${s.status || "PENDING"}</span></td>
                    <td class="muted tiny">${s.reason ? s.reason.substring(0, 40) : ""}</td>
                </tr>`;
            }).join("");
        });

        // ── Decisions ──
        socket.on("decisions", (data) => {
            const tbody = document.getElementById("decisions-tbody");
            if (!data || data.length === 0) {
                tbody.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;">No decisions yet</td></tr>`;
                return;
            }
            tbody.innerHTML = data.slice(0, 20).map(d => {
                const resultClass = d.result === "approved" || d.result === "executed" ? "up" : d.result === "rejected" ? "down" : "";
                return `<tr>
                    <td class="muted tiny">${d.time || "--"}</td>
                    <td>${d.type || "--"}</td>
                    <td><span class="highlight">${d.symbol}</span></td>
                    <td class="${d.side === 'long' ? 'up' : d.side === 'short' ? 'down' : ''}">${d.side ? d.side.toUpperCase() : "--"}</td>
                    <td class="${resultClass}">${d.result || "--"}</td>
                    <td class="muted tiny">${d.reason ? d.reason.substring(0, 50) : ""}</td>
                </tr>`;
            }).join("");
        });

        // ── Portfolio ──
        socket.on("portfolio", (d) => {
            document.getElementById("capital").textContent = d.capital != null ? "$" + fmtNum(d.capital) : "--";
            const pnlEl = document.getElementById("daily-pnl");
            if (d.daily_pnl != null) {
                pnlEl.textContent = (d.daily_pnl >= 0 ? "+" : "") + "$" + fmtNum(d.daily_pnl);
                pnlEl.style.color = d.daily_pnl >= 0 ? "var(--green)" : "var(--red)";
            } else {
                pnlEl.textContent = "--";
            }
            document.getElementById("drawdown").textContent = d.max_drawdown_pct != null ? d.max_drawdown_pct.toFixed(2) + "%" : "--";
            document.getElementById("open-pos").textContent = d.open_positions != null ? d.open_positions.toString() : "--";
            document.getElementById("daily-trades").textContent = d.daily_trades != null ? d.daily_trades.toString() : "--";
        });

        // ── Positions ──
        socket.on("positions", (data) => {
            const tbody = document.getElementById("positions-tbody");
            if (!data || data.length === 0) {
                tbody.innerHTML = `<tr><td colspan="7" class="muted" style="text-align:center;">No positions</td></tr>`;
                return;
            }
            tbody.innerHTML = data.map(p => {
                const pnlClass = p.pnl_pct > 0 ? "up" : p.pnl_pct < 0 ? "down" : "";
                return `<tr>
                    <td><span class="highlight">${p.symbol}</span></td>
                    <td class="${p.side === 'long' ? 'up' : 'down'}">${p.side ? p.side.toUpperCase() : "--"}</td>
                    <td class="num">${fmtNum(p.size, 6)}</td>
                    <td class="num">${fmtNum(p.entry_price)}</td>
                    <td class="num">${fmtNum(p.current_price)}</td>
                    <td class="num ${pnlClass}">${p.pnl_pct != null ? p.pnl_pct.toFixed(2) + "%" : "--"}</td>
                    <td class="muted tiny">${p.strategy || "--"}</td>
                </tr>`;
            }).join("");
        });

        // ── Logs ──
        socket.on("logs", (data) => {
            const container = document.getElementById("logs-container");
            if (!data || data.length === 0) {
                container.innerHTML = "Waiting for logs...";
                return;
            }
            container.innerHTML = data.map(l => {
                let cls = "log-info";
                if (l.level === "WARNING" || l.level === "WARN") cls = "log-warn";
                if (l.level === "ERROR") cls = "log-err";
                return `<div><span class="log-time">${l.time}</span> <span class="${cls}">[${l.level}]</span> ${l.message}</div>`;
            }).join("");
            container.scrollTop = container.scrollHeight;
        });

        // ── Connection ──
        socket.on("connect", () => {
            document.getElementById("conn-badge").textContent = "LIVE";
            document.getElementById("conn-badge").className = "badge badge-green";
        });
        socket.on("disconnect", () => {
            document.getElementById("conn-badge").textContent = "OFFLINE";
            document.getElementById("conn-badge").className = "badge badge-red";
        });

        // Initial load via REST fallback
        fetch("/api/status").then(r => r.json()).then(d => {
            if (d.mode) document.getElementById("mode-badge").textContent = d.mode.toUpperCase();
        });
        // ── Strategy Drill-down (Task 5.3) ──
        async function showStrategyDetail(name) {
            const modal = document.getElementById('strategy-modal');
            const content = document.getElementById('modal-content');
            document.getElementById('modal-title').textContent = name;
            modal.classList.add('active');
            content.innerHTML = '<div class="muted" style="text-align:center;">Loading...</div>';
            try {
                const res = await fetch('/api/strategy/' + encodeURIComponent(name));
                const data = await res.json();
                if (data.error) {
                    content.innerHTML = '<div class="muted" style="text-align:center;color:var(--red)">' + data.error + '</div>';
                    return;
                }
                const s = data.stats;
                let html = '<div class="stat-row"><span class="stat-label">Signals (total / approved / rejected)</span><span class="stat-value">' + s.total_signals + ' / ' + s.approved_signals + ' / ' + s.rejected_signals + '</span></div>';
                html += '<div class="stat-row"><span class="stat-label">Trades (win / loss)</span><span class="stat-value">' + s.winning_trades + ' / ' + s.losing_trades + '</span></div>';
                html += '<div class="stat-row"><span class="stat-label">Win Rate</span><span class="stat-value" style="color:' + (s.win_rate >= 0.5 ? 'var(--green)' : 'var(--red)') + '">' + (s.win_rate * 100).toFixed(1) + '%</span></div>';
                html += '<div class="stat-row"><span class="stat-label">Total PnL</span><span class="stat-value" style="color:' + (s.total_pnl_pct >= 0 ? 'var(--green)' : 'var(--red)') + '">' + (s.total_pnl_pct >= 0 ? '+' : '') + s.total_pnl_pct.toFixed(2) + '%</span></div>';
                html += '<div class="stat-row"><span class="stat-label">Avg PnL per trade</span><span class="stat-value">' + (s.avg_pnl_pct >= 0 ? '+' : '') + s.avg_pnl_pct.toFixed(2) + '%</span></div>';
                if (data.signal_history && data.signal_history.length > 0) {
                    html += '<div style="margin-top:12px;font-weight:600;font-size:11px;">Recent Signals</div>';
                    html += '<table class="data-table"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Conf</th><th>Status</th></tr></thead><tbody>';
                    html += data.signal_history.map(sig => '<tr><td class="muted tiny">' + (sig.time || '--') + '</td><td>' + sig.symbol + '</td><td class="' + (sig.side === 'long' ? 'up' : 'down') + '">' + (sig.side || '--').toUpperCase() + '</td><td>' + (sig.confidence != null ? Math.round(sig.confidence * 100) + '%' : '--') + '</td><td><span class="pill pill-' + (sig.status || 'pending') + '">' + (sig.status || 'PENDING') + '</span></td></tr>').join('');
                    html += '</tbody></table>';
                }
                content.innerHTML = html;
            } catch (e) {
                content.innerHTML = '<div class="muted" style="text-align:center;color:var(--red)">Failed to load: ' + e.message + '</div>';
            }
        }
        function closeStrategyModal(e) {
            if (e.target === document.getElementById('strategy-modal')) {
                document.getElementById('strategy-modal').classList.remove('active');
            }
        }

    </script>
    <!-- Strategy Drill-down Modal (Task 5.3) -->
    <div id="strategy-modal" class="modal-overlay" onclick="closeStrategyModal(event)">
        <div class="modal-box" onclick="event.stopPropagation()">
            <div class="modal-header">
                <span class="modal-title" id="modal-title">Strategy Detail</span>
                <span class="modal-close" onclick="document.getElementById('strategy-modal').classList.remove('active')">×</span>
            </div>
            <div id="modal-content">
                <div class="muted" style="text-align:center;">Loading...</div>
            </div>
        </div>
    </div>

</body>
</html>
'''

    @app.route("/")
    def index():
        return INDEX_HTML

    # ── REST API (fallback + initial load) ──

    @app.route("/api/status")
    def api_status():
        if _engine is None:
            return jsonify({"status": "no_engine", "online": False})
        return jsonify({
            "status": "ok",
            "online": True,
            "mode": getattr(_engine, "_config", {}).get("mode", "paper") if hasattr(_engine, "_config") else "paper",
            "uptime": getattr(_engine, "uptime_sec", 0),
            "memory_mb": getattr(_engine, "memory_mb", 0),
            "circuit_breaker": "ON" if getattr(getattr(_engine, "_risk", None), "circuit_breaker_tripped", False) else "OFF",
            "running": getattr(_engine, "_running", False),
        })

    @app.route("/api/live_data")
    def api_live_data():
        if _engine is None:
            return jsonify([])
        rows = []
        for sym in getattr(_engine, "_symbols", []):
            price = getattr(_engine, "_latest_price", {}).get(sym)
            ctx = getattr(_engine, "_latest_ctx", {}).get(sym)
            evt = getattr(_engine, "_last_market_events", {}).get(sym, {})
            rows.append({
                "symbol": sym,
                "price": getattr(price, "mid", None) if price else None,
                "funding": getattr(ctx, "funding_rate", None) if ctx else None,
                "oi": getattr(ctx, "open_interest", None) if ctx else None,
            })
        return jsonify(rows)

    @app.route("/api/strategies")
    def api_strategies():
        if _engine is None:
            return jsonify([])
        return jsonify([
            {
                "name": getattr(s, "name", "unknown"),
                "enabled": getattr(s, "enabled", True),
            }
            for s in getattr(_engine, "_strategies", [])
        ])

    @app.route("/api/strategy/<name>")
    def api_strategy_detail(name):
        """Drill-down endpoint for a single strategy (Task 5.3).

        Returns signals, win rate, PnL, and parameters.
        """
        if _engine is None:
            return jsonify({"error": "Engine not running"}), 503

        # Find strategy
        strategy = None
        for s in getattr(_engine, "_strategies", []):
            if getattr(s, "name", "") == name:
                strategy = s
                break
        if strategy is None:
            return jsonify({"error": f"Strategy {name} not found"}), 404

        # Get stats from engine
        stats = getattr(_engine, "_strategy_stats", {}).get(name, {})

        return jsonify({
            "name": name,
            "enabled": getattr(strategy, "enabled", True),
            "description": (getattr(strategy, "__doc__", "") or "No description").split("\n")[0][:200],
            "params": getattr(strategy, "params", {}),
            "stats": {
                "total_signals": stats.get("total_signals", 0),
                "approved_signals": stats.get("approved_signals", 0),
                "rejected_signals": stats.get("rejected_signals", 0),
                "winning_trades": stats.get("winning_trades", 0),
                "losing_trades": stats.get("losing_trades", 0),
                "win_rate": stats.get("win_rate", 0.0),
                "total_pnl_pct": stats.get("total_pnl", 0.0) * 100,
                "avg_pnl_pct": stats.get("avg_pnl", 0.0) * 100,
            },
            "signal_history": stats.get("signal_history", [])[:20],
        })

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
            "open_positions": len(getattr(portfolio, "get_positions_sync", lambda: {})()),
        })

    @app.route("/api/logs")
    def api_logs():
        log_path = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "bot.log")
        log_path = os.path.abspath(log_path)
        entries = []
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                for line in lines[-50:]:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(" | ")
                    if len(parts) >= 4:
                        entries.append({
                            "time": parts[0],
                            "level": parts[1].strip(),
                            "module": parts[2].strip(),
                            "message": " | ".join(parts[3:]),
                        })
        except Exception:
            pass
        return jsonify(entries)

    # ── Socket.IO events ──

    @socketio.on("connect")
    def on_connect(auth=None):
        logger.info("Dashboard client connected")
        # Push everything immediately
        emitter._emit_all()

    @socketio.on("disconnect")
    def on_disconnect(reason=None):
        logger.info("Dashboard client disconnected: %s", reason)

    return app, socketio, emitter._emit_all
