"""
Hyperliquid Premium — Dashboard Web Server
Flask + Socket.IO for real-time push updates.
"""

import os
import time
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PUSH_INTERVAL_SEC = 2.0
MAX_TRADES_DEFAULT = 50
MAX_SIGNALS_DEFAULT = 50
MAX_EQUITY_POINTS = 500

# ---------------------------------------------------------------------------
# Mock data generators (used when engine is None or missing attributes)
# ---------------------------------------------------------------------------

_mock_counter = {"tick": 0}


def _mock_status() -> Dict[str, Any]:
    _mock_counter["tick"] += 1
    t = _mock_counter["tick"]
    return {
        "online": True,
        "mode": "PAPER",
        "circuit_breaker_tripped": False,
        "sync_warning": False,
        "version": "1.0.0",
        "uptime_sec": t * 2,
        "memory_mb": 42.0 + (t % 10),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "capital": 25000.0 + (t * 15.5),
        "initial_capital": 25000.0,
        "daily_pnl": 120.0 + (t % 50 - 25),
        "total_pnl": 450.0 + (t * 3.2),
        "win_rate": 62.5,
        "wins": 10,
        "losses": 6,
        "max_drawdown_pct": 4.2,
        "open_positions_count": 2,
        "max_positions": 5,
        "daily_trades_count": 3,
        "max_daily_trades": 5,
    }


def _mock_positions() -> List[Dict[str, Any]]:
    return [
        {
            "symbol": "BTC",
            "side": "long",
            "entry_price": 67500.0,
            "current_price": 68120.0,
            "size": 0.15,
            "unrealized_pnl": 93.0,
            "unrealized_pnl_pct": 0.92,
            "stop_loss": 66000.0,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "time_open_min": 45,
        },
        {
            "symbol": "ETH",
            "side": "short",
            "entry_price": 3520.0,
            "current_price": 3485.0,
            "size": 1.2,
            "unrealized_pnl": 42.0,
            "unrealized_pnl_pct": 1.19,
            "stop_loss": 3650.0,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "time_open_min": 12,
        },
    ]


def _mock_trades(limit: int = 50) -> List[Dict[str, Any]]:
    base = [
        {
            "time": "2026-05-06T14:32:00+00:00",
            "symbol": "BTC",
            "side": "long",
            "entry": 67200.0,
            "exit": 68100.0,
            "pnl_usd": 135.0,
            "pnl_pct": 1.34,
            "strategy": "Trend Follow",
            "reason": "Breakout + volume surge",
        },
        {
            "time": "2026-05-06T12:15:00+00:00",
            "symbol": "SOL",
            "side": "short",
            "entry": 148.5,
            "exit": 145.2,
            "pnl_usd": 66.0,
            "pnl_pct": 2.22,
            "strategy": "Mean Reversion",
            "reason": "Extreme funding + overcrowded longs",
        },
        {
            "time": "2026-05-06T10:00:00+00:00",
            "symbol": "ETH",
            "side": "long",
            "entry": 3480.0,
            "exit": 3460.0,
            "pnl_usd": -24.0,
            "pnl_pct": -0.57,
            "strategy": "Trend Follow",
            "reason": "Failed breakout",
        },
    ]
    return base[:limit]


def _mock_signals(limit: int = 50) -> List[Dict[str, Any]]:
    base = [
        {
            "time": "2026-05-06T14:30:00+00:00",
            "strategy": "Trend Follow",
            "symbol": "BTC",
            "side": "long",
            "confidence": 0.85,
            "reason": "Breakout + volume surge",
            "status": "EXECUTED",
        },
        {
            "time": "2026-05-06T12:10:00+00:00",
            "strategy": "Mean Reversion",
            "symbol": "SOL",
            "side": "short",
            "confidence": 0.78,
            "reason": "Extreme funding + overcrowded longs",
            "status": "EXECUTED",
        },
        {
            "time": "2026-05-06T09:55:00+00:00",
            "strategy": "Trend Follow",
            "symbol": "ETH",
            "side": "long",
            "confidence": 0.62,
            "reason": "Failed breakout",
            "status": "REJECTED",
        },
    ]
    return base[:limit]


def _mock_equity() -> List[Dict[str, Any]]:
    points = []
    base = 25000.0
    for i in range(MAX_EQUITY_POINTS):
        points.append({
            "time": f"2026-05-06T{10 + i // 60:02d}:{i % 60:02d}:00+00:00",
            "value": base + (i * 2.5) + (5 if i % 7 == 0 else -3),
        })
    return points


def _mock_metrics() -> Dict[str, Any]:
    return {
        "sharpe": 1.85,
        "sortino": 2.40,
        "max_drawdown_pct": 4.2,
        "win_rate": 62.5,
        "profit_factor": 1.55,
        "avg_trade_pnl": 18.5,
        "avg_win": 65.0,
        "avg_loss": -28.0,
        "total_trades": 16,
        "total_return_pct": 2.1,
    }


def _mock_funding() -> List[Dict[str, Any]]:
    return [
        {"symbol": "BTC", "rate": 0.00025, "predicted": 0.00030, "has_position": True, "position_side": "long"},
        {"symbol": "ETH", "rate": -0.00015, "predicted": -0.00010, "has_position": True, "position_side": "short"},
        {"symbol": "SOL", "rate": 0.00085, "predicted": 0.00090, "has_position": False, "position_side": None},
    ]


def _mock_oi() -> List[Dict[str, Any]]:
    return [
        {"symbol": "BTC", "oi": 1_200_000_000, "oi_delta_24h": 0.05, "long_ratio": 0.55, "overcrowded_score": 0.35},
        {"symbol": "ETH", "oi": 850_000_000, "oi_delta_24h": -0.02, "long_ratio": 0.48, "overcrowded_score": 0.20},
        {"symbol": "SOL", "oi": 420_000_000, "oi_delta_24h": 0.18, "long_ratio": 0.72, "overcrowded_score": 0.82},
    ]


def _mock_prices() -> Dict[str, float]:
    t = _mock_counter["tick"]
    return {
        "BTC": 68120.0 + (t % 5 - 2) * 50,
        "ETH": 3485.0 + (t % 3 - 1) * 10,
        "SOL": 146.2 + (t % 4 - 2) * 1.5,
    }


def _mock_candles() -> List[Dict[str, Any]]:
    return [
        {"symbol": "BTC", "timeframe": "15m", "open": 68000.0, "high": 68200.0, "low": 67900.0, "close": 68120.0, "volume": 1200.0},
        {"symbol": "ETH", "timeframe": "15m", "open": 3490.0, "high": 3495.0, "low": 3475.0, "close": 3485.0, "volume": 8500.0},
        {"symbol": "SOL", "timeframe": "15m", "open": 147.0, "high": 147.5, "low": 145.0, "close": 146.2, "volume": 45000.0},
    ]


# ---------------------------------------------------------------------------
# Safe attribute access helpers
# ---------------------------------------------------------------------------

def _safe(obj: Any, attr: str, default: Any = None) -> Any:
    return getattr(obj, attr, default) if obj else default


def _call(obj: Any, method: str, *args, **kwargs) -> Any:
    if obj is None:
        return None
    fn = getattr(obj, method, None)
    if callable(fn):
        return fn(*args, **kwargs)
    return None


# ---------------------------------------------------------------------------
# Dashboard factory
# ---------------------------------------------------------------------------

def create_dashboard(engine: Any, config: Dict[str, Any]):
    """
    Create the Flask app + Socket.IO server.
    
    Args:
        engine: The main trading engine (may be None for standalone testing).
        config: Dict with at least { 'mode': str, 'version': str }.
    """
    app = Flask(__name__)
    socketio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode="threading",
        ping_interval=25,
        ping_timeout=60,
    )

    # Resolve dashboard directory
    dashboard_dir = os.path.dirname(os.path.abspath(__file__))
    static_dir = os.path.join(dashboard_dir, "static")

    # -----------------------------------------------------------------------
    # Helpers to read from engine (with graceful fallbacks)
    # -----------------------------------------------------------------------

    def engine_mode() -> str:
        return _safe(config, "mode", "PAPER").upper()

    def engine_version() -> str:
        return _safe(config, "version", "1.0.0")

    def get_portfolio() -> Any:
        return _safe(engine, "portfolio", None)

    def get_risk_manager() -> Any:
        return _safe(engine, "risk_manager", None)

    def get_database() -> Any:
        return _safe(engine, "database", None)

    def get_data_bus() -> Any:
        return _safe(engine, "data_bus", None)

    def is_online() -> bool:
        if engine is None:
            return True  # Mock mode — pretend online
        ws = _safe(engine, "websocket_client", None)
        if ws is None:
            return True
        return getattr(ws, "connected", False)

    def circuit_breaker_tripped() -> bool:
        rm = get_risk_manager()
        return getattr(rm, "circuit_breaker_tripped", False) if rm else False

    def sync_warning() -> bool:
        # True if DB and engine state diverge
        if engine is None:
            return False
        db_trade_count = 0
        db = get_database()
        if db:
            db_trade_count = _call(db, "count_trades_today") or 0
        engine_trade_count = getattr(engine, "daily_trade_count", 0)
        return abs(db_trade_count - engine_trade_count) > 2

    def build_status() -> Dict[str, Any]:
        """Build the full engine state payload."""
        if engine is None:
            return _mock_status()

        portfolio = get_portfolio()
        risk_mgr = get_risk_manager()
        db = get_database()

        capital = getattr(portfolio, "capital", 0.0) if portfolio else 0.0
        initial = getattr(portfolio, "initial_capital", capital) if portfolio else capital
        total_pnl = getattr(portfolio, "total_pnl", 0.0) if portfolio else 0.0

        wins = getattr(risk_mgr, "daily_wins", 0) if risk_mgr else 0
        losses = getattr(risk_mgr, "daily_losses", 0) if risk_mgr else 0
        total_trades = wins + losses
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

        daily_pnl = getattr(risk_mgr, "daily_pnl", 0.0) if risk_mgr else 0.0
        max_dd = getattr(portfolio, "max_drawdown_pct", 0.0) if portfolio else 0.0
        open_count = len(getattr(portfolio, "positions", [])) if portfolio else 0
        daily_trades = getattr(risk_mgr, "daily_trade_count", 0) if risk_mgr else 0

        return {
            "online": is_online(),
            "mode": engine_mode(),
            "circuit_breaker_tripped": circuit_breaker_tripped(),
            "sync_warning": sync_warning(),
            "version": engine_version(),
            "uptime_sec": getattr(engine, "uptime_sec", 0),
            "memory_mb": getattr(engine, "memory_mb", 0.0),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "capital": capital,
            "initial_capital": initial,
            "daily_pnl": daily_pnl,
            "total_pnl": total_pnl,
            "win_rate": round(win_rate, 1),
            "wins": wins,
            "losses": losses,
            "max_drawdown_pct": round(max_dd, 2),
            "open_positions_count": open_count,
            "max_positions": getattr(risk_mgr, "max_positions", 5) if risk_mgr else 5,
            "daily_trades_count": daily_trades,
            "max_daily_trades": getattr(risk_mgr, "max_daily_trades", 5) if risk_mgr else 5,
        }

    def build_positions() -> List[Dict[str, Any]]:
        if engine is None:
            return _mock_positions()
        portfolio = get_portfolio()
        if portfolio is None:
            return []
        positions = getattr(portfolio, "positions", {})
        prices = build_prices()
        result = []
        for symbol, pos in (positions.items() if isinstance(positions, dict) else []):
            current = prices.get(symbol, getattr(pos, "entry_price", 0.0))
            entry = getattr(pos, "entry_price", 0.0)
            size = getattr(pos, "size", 0.0)
            side = getattr(pos, "side", "long")
            unrealized = 0.0
            if side == "long":
                unrealized = (current - entry) * size
            else:
                unrealized = (entry - current) * size
            unrealized_pct = (unrealized / (entry * size) * 100) if (entry * size) != 0 else 0.0
            opened = getattr(pos, "opened_at", None)
            time_open_min = 0
            if opened:
                if isinstance(opened, (int, float)):
                    opened_dt = datetime.fromtimestamp(opened / 1000, tz=timezone.utc)
                elif hasattr(opened, "isoformat"):
                    opened_dt = opened
                else:
                    opened_dt = datetime.now(timezone.utc)
                delta = datetime.now(timezone.utc) - opened_dt
                time_open_min = int(delta.total_seconds() / 60)
            result.append({
                "symbol": symbol,
                "side": side,
                "entry_price": entry,
                "current_price": current,
                "size": size,
                "unrealized_pnl": round(unrealized, 2),
                "unrealized_pnl_pct": round(unrealized_pct, 2),
                "stop_loss": getattr(pos, "stop_loss", None),
                "opened_at": opened.isoformat() if hasattr(opened, "isoformat") else str(opened),
                "time_open_min": time_open_min,
            })
        return result

    def build_trades(limit: int = 50) -> List[Dict[str, Any]]:
        if engine is None:
            return _mock_trades(limit)
        db = get_database()
        if db is None:
            return []
        rows = _call(db, "get_recent_trades", limit) or []
        result = []
        for row in rows:
            if isinstance(row, dict):
                result.append({
                    "time": row.get("closed_at") or row.get("opened_at") or row.get("time"),
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "entry": row.get("entry_price"),
                    "exit": row.get("exit_price"),
                    "pnl_usd": row.get("pnl"),
                    "pnl_pct": row.get("pnl_pct"),
                    "strategy": row.get("strategy"),
                    "reason": row.get("reason"),
                })
            else:
                # tuple fallback (assumed column order)
                try:
                    result.append({
                        "time": row[0],
                        "symbol": row[1],
                        "side": row[2],
                        "entry": row[3],
                        "exit": row[4],
                        "pnl_usd": row[5],
                        "pnl_pct": row[6],
                        "strategy": row[7],
                        "reason": row[8],
                    })
                except Exception:
                    pass
        return result

    def build_signals(limit: int = 50) -> List[Dict[str, Any]]:
        if engine is None:
            return _mock_signals(limit)
        db = get_database()
        if db is None:
            return []
        rows = _call(db, "get_recent_signals", limit) or []
        result = []
        for row in rows:
            if isinstance(row, dict):
                result.append({
                    "time": row.get("created_at") or row.get("time"),
                    "strategy": row.get("strategy"),
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "confidence": row.get("confidence"),
                    "reason": row.get("reason"),
                    "status": row.get("status", "ACTIVE"),
                })
            else:
                try:
                    result.append({
                        "time": row[0],
                        "strategy": row[1],
                        "symbol": row[2],
                        "side": row[3],
                        "confidence": row[4],
                        "reason": row[5],
                        "status": row[6] if len(row) > 6 else "ACTIVE",
                    })
                except Exception:
                    pass
        return result

    def build_equity() -> List[Dict[str, Any]]:
        if engine is None:
            return _mock_equity()
        db = get_database()
        if db is None:
            return []
        rows = _call(db, "get_equity_curve", MAX_EQUITY_POINTS) or []
        result = []
        for row in rows:
            if isinstance(row, dict):
                result.append({"time": row.get("time"), "value": row.get("value")})
            else:
                try:
                    result.append({"time": row[0], "value": row[1]})
                except Exception:
                    pass
        return result

    def build_metrics() -> Dict[str, Any]:
        if engine is None:
            return _mock_metrics()
        db = get_database()
        if db is None:
            return {}
        return _call(db, "get_metrics") or {}

    def build_funding() -> List[Dict[str, Any]]:
        if engine is None:
            return _mock_funding()
        data_bus = get_data_bus()
        if data_bus is None:
            return []
        funding_map = getattr(data_bus, "funding_rates", {}) or {}
        predicted_map = getattr(data_bus, "predicted_funding", {}) or {}
        positions = build_positions()
        pos_symbols = {p["symbol"]: p["side"] for p in positions}
        result = []
        for symbol, rate in (funding_map.items() if isinstance(funding_map, dict) else []):
            result.append({
                "symbol": symbol,
                "rate": rate,
                "predicted": predicted_map.get(symbol),
                "has_position": symbol in pos_symbols,
                "position_side": pos_symbols.get(symbol),
            })
        return result

    def build_oi() -> List[Dict[str, Any]]:
        if engine is None:
            return _mock_oi()
        data_bus = get_data_bus()
        if data_bus is None:
            return []
        oi_map = getattr(data_bus, "oi_data", {}) or {}
        result = []
        for symbol, data in (oi_map.items() if isinstance(oi_map, dict) else []):
            if isinstance(data, dict):
                long_ratio = data.get("long_ratio", 0.5)
                overcrowded = abs(long_ratio - 0.5) * 2  # 0 = balanced, 1 = all one side
                result.append({
                    "symbol": symbol,
                    "oi": data.get("total", 0),
                    "oi_delta_24h": data.get("delta_24h", 0),
                    "long_ratio": long_ratio,
                    "overcrowded_score": round(overcrowded, 2),
                })
        return result

    def build_prices() -> Dict[str, float]:
        if engine is None:
            return _mock_prices()
        data_bus = get_data_bus()
        if data_bus is None:
            return {}
        return getattr(data_bus, "prices", {}) or {}

    def build_candles() -> List[Dict[str, Any]]:
        if engine is None:
            return _mock_candles()
        data_bus = get_data_bus()
        if data_bus is None:
            return []
        latest = getattr(data_bus, "latest_candles", {}) or {}
        result = []
        for (symbol, tf), c in (latest.items() if isinstance(latest, dict) else []):
            if isinstance(c, dict):
                result.append({
                    "symbol": symbol,
                    "timeframe": tf,
                    "open": c.get("open"),
                    "high": c.get("high"),
                    "low": c.get("low"),
                    "close": c.get("close"),
                    "volume": c.get("volume"),
                })
            else:
                try:
                    result.append({
                        "symbol": symbol,
                        "timeframe": tf,
                        "open": c[0],
                        "high": c[1],
                        "low": c[2],
                        "close": c[3],
                        "volume": c[4],
                    })
                except Exception:
                    pass
        return result

    # -----------------------------------------------------------------------
    # REST endpoints (fallback when Socket.IO unavailable)
    # -----------------------------------------------------------------------

    @app.route("/")
    def index():
        return send_from_directory(dashboard_dir, "index.html")

    @app.route("/api/status")
    def api_status():
        return jsonify(build_status())

    @app.route("/api/trades")
    def api_trades():
        limit = request.args.get("limit", MAX_TRADES_DEFAULT, type=int)
        return jsonify(build_trades(limit))

    @app.route("/api/positions")
    def api_positions():
        return jsonify(build_positions())

    @app.route("/api/signals")
    def api_signals():
        limit = request.args.get("limit", MAX_SIGNALS_DEFAULT, type=int)
        return jsonify(build_signals(limit))

    @app.route("/api/equity")
    def api_equity():
        return jsonify(build_equity())

    @app.route("/api/metrics")
    def api_metrics():
        return jsonify(build_metrics())

    @app.route("/api/funding")
    def api_funding():
        return jsonify(build_funding())

    @app.route("/api/oi")
    def api_oi():
        return jsonify(build_oi())

    # -----------------------------------------------------------------------
    # Socket.IO events
    # -----------------------------------------------------------------------

    @socketio.on("connect")
    def on_connect():
        # Emit full state immediately on connection
        emit("status_update", build_status())
        emit("price_update", build_prices())
        emit("trade_update", {"trades": build_trades(10)})
        emit("signal_update", {"signals": build_signals(10)})
        emit("funding_update", build_funding())
        emit("candle_update", build_candles())

    @socketio.on("disconnect")
    def on_disconnect():
        pass  # No action needed; client auto-reconnects

    @socketio.on("request_status")
    def on_request_status():
        emit("status_update", build_status())

    @socketio.on("request_trades")
    def on_request_trades(data: Optional[Dict] = None):
        limit = (data or {}).get("limit", MAX_TRADES_DEFAULT)
        emit("trade_update", {"trades": build_trades(limit)})

    @socketio.on("request_signals")
    def on_request_signals(data: Optional[Dict] = None):
        limit = (data or {}).get("limit", MAX_SIGNALS_DEFAULT)
        emit("signal_update", {"signals": build_signals(limit)})

    @socketio.on("request_positions")
    def on_request_positions():
        emit("positions_update", build_positions())

    # -----------------------------------------------------------------------
    # Background push loop
    # -----------------------------------------------------------------------

    _push_running = False
    _push_thread: Optional[threading.Thread] = None

    def _push_loop():
        """Push updates to all connected clients every PUSH_INTERVAL_SEC."""
        nonlocal _push_running
        _push_running = True
        prev_prices: Dict[str, float] = {}
        while _push_running:
            try:
                time.sleep(PUSH_INTERVAL_SEC)
                if not _push_running:
                    break

                status = build_status()
                prices = build_prices()
                funding = build_funding()
                candles = build_candles()
                positions = build_positions()

                # Detect price changes for flash animation
                price_changes = {}
                for sym, price in prices.items():
                    prev = prev_prices.get(sym)
                    if prev is not None and prev != 0:
                        change = price - prev
                        price_changes[sym] = "up" if change > 0 else "down" if change < 0 else "flat"
                    else:
                        price_changes[sym] = "flat"
                prev_prices = dict(prices)

                payload = {
                    "prices": prices,
                    "changes": price_changes,
                }

                socketio.emit("status_update", status)
                socketio.emit("price_update", payload)
                socketio.emit("funding_update", funding)
                socketio.emit("candle_update", candles)
                socketio.emit("positions_update", positions)

            except Exception:
                # Never crash the push loop
                pass

    def start_push_loop():
        nonlocal _push_thread
        if _push_thread is None or not _push_thread.is_alive():
            _push_thread = threading.Thread(target=_push_loop, daemon=True)
            _push_thread.start()

    def stop_push_loop():
        nonlocal _push_running
        _push_running = False

    # Start push loop immediately
    start_push_loop()

    # -----------------------------------------------------------------------
    # Cross-thread event emitter for external callers (engine callbacks)
    # -----------------------------------------------------------------------

    def emit_trade(trade_data: Dict[str, Any]) -> None:
        """Call from engine/execution when a trade is filled or closed."""
        try:
            socketio.emit("trade_update", {"trades": [trade_data]})
        except Exception:
            pass

    def emit_signal(signal_data: Dict[str, Any]) -> None:
        """Call from strategy when a new signal is generated."""
        try:
            socketio.emit("signal_update", {"signals": [signal_data]})
        except Exception:
            pass

    # Attach helpers to socketio for external access
    socketio.emit_trade = emit_trade
    socketio.emit_signal = emit_signal
    socketio.start_push_loop = start_push_loop
    socketio.stop_push_loop = stop_push_loop

    return app, socketio
