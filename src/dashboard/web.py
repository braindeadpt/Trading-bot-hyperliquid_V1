"""
Hyperliquid Premium - Real-Time Operations Dashboard v2
Complete rewrite for maximum visibility into bot internals.
"""

import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable

from flask import Flask, jsonify, request, abort, render_template
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


def _get_db():
    """Safely get the database instance from the engine."""
    if _engine is None:
        return None
    return getattr(_engine, "_db", None)


# ═════════════════════════════════════════════════════════════════════════════
# Background emitter - pushes real-time data every second
# ═════════════════════════════════════════════════════════════════════════════

class DashboardEmitter:
    """Emits real-time dashboard updates via Socket.IO.

    Two-tier cadence (B9):
      * fast (default 0.5s) — high-churn: live_data (prices), engine_monitor
      * slow (default 5.0s) — slow-churn: status, strategies, positions,
        portfolio, trades, signals, decisions, candles, market_data_health,
        funding_update, logs
    Emits are skipped when the serialized payload hash is unchanged
    (coalesced push — avoids redundant network traffic on quiet markets).
    """

    _FAST_EVENTS: tuple = ("live_data", "engine_monitor")
    _SLOW_EVENTS: tuple = (
        "status",
        "candles",
        "strategies",
        "signals",
        "decisions",
        "portfolio",
        "positions",
        "trades",
        "logs",
        "market_data_health",
        "funding_update",
    )

    def __init__(
        self,
        socketio: SocketIO,
        fast_interval: float = 0.5,
        slow_interval: float = 5.0,
    ):
        self.socketio = socketio
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_slow_emit: float = 0.0
        self._last_payload_hash: Dict[str, int] = {}

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(
            "Dashboard emitter started (fast=%.1fs slow=%.1fs)",
            self.fast_interval,
            self.slow_interval,
        )

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                self._emit_all_with_cadence()
                now = time.time()
                if now - self._last_slow_emit >= self.slow_interval:
                    self._last_slow_emit = now
            except Exception as e:
                logger.warning("Dashboard emitter error: %s", e)
            time.sleep(self.fast_interval)

    def _emit_all_with_cadence(self) -> None:
        """Emit slow data once per slow_interval, fast every tick.

        Skips emissions whose payload hash matches the previous one.
        """
        if _engine is None or self.socketio is None:
            return
        # Fast events every tick
        for event in self._FAST_EVENTS:
            self._emit_cached(event)
        # Slow events gated by cadence
        if time.time() - self._last_slow_emit < self.slow_interval:
            return
        for event in self._SLOW_EVENTS:
            self._emit_cached(event)

    def _emit_cached(self, event: str) -> None:
        """Dispatch to the matching ``_emit_<event>`` method, hashing the
        returned payload to skip no-op broadcasts."""
        method = getattr(self, f"_emit_{event}", None)
        if method is None:
            return
        try:
            payload = method()
        except Exception as e:
            logger.warning("%s error: %s", event, e)
            return
        try:
            blob = json.dumps(payload, sort_keys=True, default=str)
        except (TypeError, ValueError):
            blob = repr(payload)
        h = hash(blob)
        if self._last_payload_hash.get(event) == h:
            return
        self._last_payload_hash[event] = h
        self._safe_emit(event, payload)

    def _emit_all(self) -> None:
        # Backwards-compat shim for callers that still invoke _emit_all()
        # directly (e.g. manual flush). Delegates to the new cadence path.
        self._emit_all_with_cadence()

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
        """Raw market data feed - prices, funding, OI, volume, imbalance."""
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

    def _emit_market_data_health(self) -> None:
        summary = getattr(_engine, "_market_data_health_summary", None)
        if summary is not None and hasattr(summary, "to_dict"):
            self._safe_emit("market_data_health", summary.to_dict())
            return
        health = getattr(_engine, "_market_data_health", {}) or {}
        rows = [h.to_dict() for h in health.values()]
        overall = "red"
        if rows and all(r.get("status") == "green" for r in rows):
            overall = "green"
        elif rows and not any(r.get("status") == "red" for r in rows):
            overall = "yellow"
        self._safe_emit("market_data_health", {"overall": overall, "symbols": {r["symbol"]: r for r in rows}})

    def _emit_funding_update(self) -> None:
        """Merged funding/OI + feed status for dashboard cards."""
        symbols = getattr(_engine, "_symbols", [])
        ctxs = getattr(_engine, "_latest_ctx", {})
        health_map = getattr(_engine, "_market_data_health", {}) or {}
        agg_map = getattr(_engine, "_latest_agg_funding", {})
        hl_client = getattr(_engine, "_hl_predicted", None)
        payload: Dict[str, Any] = {}

        for sym in symbols:
            ctx = ctxs.get(sym)
            feed = health_map.get(sym)
            agg = agg_map.get(sym)
            hl_snap = hl_client.get(sym) if hl_client else None
            pred = None
            if hl_snap and hl_snap.predicted_funding_hl_8h is not None:
                pred = hl_snap.predicted_funding_hl_8h
            elif agg and agg.predicted_funding_avg is not None:
                pred = agg.predicted_funding_avg

            current = None
            if feed and feed.funding_hl_ws is not None:
                current = feed.funding_hl_ws
            elif agg and agg.funding_avg is not None:
                current = agg.funding_avg
            elif ctx and getattr(ctx, "funding_rate", None) is not None:
                current = ctx.funding_rate

            payload[sym] = {
                "current": current,
                "predicted": pred,
                "cex_avg": feed.funding_cex_avg_8h if feed else (agg.funding_avg if agg else None),
                "hl_predicted_8h": feed.funding_hl_predicted_8h if feed else None,
                "oi_total": feed.oi_cex_usd if feed else (agg.oi_total if agg else None),
                "oi_hl": feed.oi_hl if feed else (getattr(ctx, "open_interest", None) if ctx else None),
                "health": feed.status if feed else "unknown",
                "cex_stale": feed.cex_stale if feed else True,
                "hl_stale": feed.hl_predicted_stale if feed else True,
                "cex_age_sec": feed.cex_age_sec if feed else None,
                "hl_age_sec": feed.hl_predicted_age_sec if feed else None,
                "cex_exchanges": feed.cex_exchanges if feed else [],
                "failure_rate_1h": feed.failure_rate_1h if feed else 0.0,
            }

        summary = getattr(_engine, "_market_data_health_summary", None)
        if summary is not None:
            payload["_overall"] = {
                "status": summary.overall,
                "red_since_sec": summary.red_since_sec,
                "failure_rate_1h": summary.failure_rate_1h,
                "polls_1h": summary.polls_1h,
            }
        self._safe_emit("funding_update", payload)

    def _emit_engine_monitor(self) -> None:
        """Engine health - ticks/sec, total ticks, last error."""
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

        # Collect all strategy names (ensemble + sub-strategies)
        all_strategy_names = []
        for s in getattr(_engine, "_strategies", []):
            all_strategy_names.append(getattr(s, "name", "unknown"))
            sub_strategies = getattr(s, "_strategies", None)
            if sub_strategies and isinstance(sub_strategies, dict):
                for sub_name, sub in sub_strategies.items():
                    all_strategy_names.append(getattr(sub, "name", sub_name))

        self._safe_emit("engine_monitor", {
            "ticks_per_second": stats.get("per_second", 0),
            "total_ticks": stats.get("total", 0),
            "last_error": last_err,
            "recent_events": recent,
            "symbols": getattr(_engine, "_symbols", []),
            "strategies": all_strategy_names,
        })

    def _emit_candles(self) -> None:
        """Candle status - which timeframes have data, OHLCV if available."""
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
        """Strategy state — ensemble + sub-strategies, params, last signal, signals today."""
        strategies = getattr(_engine, "_strategies", [])
        sig_hist = getattr(_engine, "_signal_history", [])
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        def _build_strategy_info(s):
            name = getattr(s, "name", "unknown")
            today_count = sum(
                1 for sig in sig_hist
                if sig.get("strategy") == name and sig.get("time", "").startswith(today_str)
            )
            last = next((sig for sig in sig_hist if sig.get("strategy") == name), None)
            return {
                "name": name,
                "enabled": getattr(s, "enabled", True),
                "description": (getattr(s, "__doc__", "") or "No description").split("\n")[0][:80],
                "params": str(getattr(s, "params", {})),
                "last_signal_time": last.get("time") if last else None,
                "last_signal_side": last.get("side") if last else None,
                "last_signal_confidence": last.get("confidence") if last else None,
                "last_signal_status": last.get("status") if last else None,
                "signals_today": today_count,
            }

        result = []
        for s in strategies:
            result.append(_build_strategy_info(s))
            # If this is an ensemble, also emit its sub-strategies
            sub_strategies = getattr(s, "_strategies", None)
            if sub_strategies and isinstance(sub_strategies, dict):
                for sub in sub_strategies.values():
                    result.append(_build_strategy_info(sub))

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

    def _emit_trades(self) -> None:
        """Emit last 50 trades from the DB (open + closed)."""
        db = getattr(_engine, "_db", None)
        if db is None:
            return
        try:
            rows = db.get_trades(limit=50)
            result = []
            for r in rows:
                result.append({
                    "id": r.get("id"),
                    "symbol": r.get("symbol"),
                    "side": r.get("side"),
                    "entry_price": r.get("entry_price"),
                    "exit_price": r.get("exit_price"),
                    "entry_time": r.get("entry_time"),
                    "exit_time": r.get("exit_time"),
                    "size": r.get("size"),
                    "pnl_usd": r.get("pnl_usd"),
                    "pnl_pct": r.get("pnl_pct"),
                    "strategy": r.get("strategy"),
                    "exit_reason": r.get("exit_reason"),
                    "status": r.get("status"),
                })
            self._safe_emit("trades", result)
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
    # DASHBOARD LAYOUT FIX: Configure template and static folders so the
    # reorganized 12-column grid dashboard (index.html + dashboard.css)
    # is served correctly instead of the old inline INDEX_HTML string.
    template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "templates"))
    static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))
    app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    @app.after_request
    def add_header(response):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    # CRIT-001: SECRET_KEY - generate random if not configured (no hardcoded fallback)
    secret_key = config.get("secret_key")
    if not secret_key:
        secret_key = secrets.token_urlsafe(32)
        logger.warning(
            "Dashboard SECRET_KEY not configured - generated one-time key. "
            "Set dashboard.secret_key in config for persistence across restarts."
        )
    app.config["SECRET_KEY"] = secret_key

    # CRIT-002: CORS - restrict to localhost by default
    allowed_origins = config.get("cors_allowed_origins", ["http://localhost:5000", "http://127.0.0.1:5000"])
    if isinstance(allowed_origins, str):
        allowed_origins = [allowed_origins]
    socketio = SocketIO(app, cors_allowed_origins=allowed_origins, async_mode="threading")

    global _socketio
    _socketio = socketio

    # CRIT-003: Dashboard auth - token-based guard (REST + Socket.IO)
    from src.dashboard.auth import (
        DashboardAuthConfig,
        resolve_dashboard_auth,
        validate_dashboard_token,
    )

    auth_cfg: DashboardAuthConfig = resolve_dashboard_auth(config)
    _dashboard_token = auth_cfg.token
    _auth_enabled = auth_cfg.enabled

    def _extract_token() -> Optional[str]:
        auth_header = request.headers.get("X-Dashboard-Token", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip() or None
        return (
            auth_header.strip()
            or request.args.get("token")
            or None
        )

    if _auth_enabled and _dashboard_token:
        logger.info("Dashboard auth enabled (token required for API and WebSocket)")

    if _auth_enabled and _dashboard_token:

        @app.before_request
        def _require_auth():
            if request.path in ("/", "/health", "/api/auth/check"):
                return None
            if request.path.startswith(("/socket.io", "/static/")):
                return None
            token = _extract_token()
            if not validate_dashboard_token(token, _dashboard_token):
                abort(401)

        @socketio.on("connect")
        def _socket_connect(auth=None):
            token = None
            if isinstance(auth, dict):
                token = auth.get("token")
            if not token:
                token = request.args.get("token")
            if not validate_dashboard_token(token, _dashboard_token):
                logger.warning("Socket.IO connect rejected — invalid or missing token")
                return False
            return True

    # Start background emitter (B9) — honor configured cadence
    _dash_cfg = (config or {}).get("dashboard", {}) or {}
    _push_sec = float(_dash_cfg.get("push_interval_sec", 2.0) or 2.0)
    emitter = DashboardEmitter(
        socketio,
        fast_interval=min(0.5, max(0.1, _push_sec / 4.0)),
        slow_interval=max(1.0, _push_sec * 2.5),
    )
    emitter.start()


    @app.route("/")
    def index():
        return render_template(
            "index.html",
            auth_required=_auth_enabled and bool(_dashboard_token),
        )

    @app.route("/health")
    def health():
        body: Dict[str, Any] = {"status": "ok"}
        if _engine is not None:
            last_tick = getattr(_engine, "last_tick_age_sec", None)
            cb = getattr(getattr(_engine, "_risk", None), "circuit_breaker_tripped", False)
            running = getattr(_engine, "_running", False)
            data_bus_lag = None
            bus = getattr(_engine, "_bus", None)
            if bus is not None and hasattr(bus, "last_publish_age_sec"):
                data_bus_lag = bus.last_publish_age_sec()
            body.update({
                "running": running,
                "uptime_sec": getattr(_engine, "uptime_sec", 0),
                "last_tick_age_sec": last_tick,
                "data_bus_lag_sec": data_bus_lag,
                "circuit_breaker": bool(cb),
                "ws_healthy": bool(
                    getattr(getattr(_engine, "_hl_ws_client", None), "is_healthy", True)
                ),
            })
            if not running:
                body["status"] = "starting"
            elif last_tick is not None and last_tick > 30:
                body["status"] = "stale"
        return jsonify(body)

    @app.route("/api/auth/check", methods=["GET", "POST"])
    def api_auth_check():
        if not _auth_enabled or not _dashboard_token:
            return jsonify({"ok": True, "auth_required": False})
        token = _extract_token()
        if request.method == "POST" and request.is_json:
            body = request.get_json(silent=True) or {}
            token = token or body.get("token")
        ok = validate_dashboard_token(token, _dashboard_token)
        if not ok:
            abort(401)
        return jsonify({"ok": True, "auth_required": True})

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

    @app.route("/api/market_data_health")
    def api_market_data_health():
        if _engine is None:
            return jsonify({"feeds": [], "overall": "red"})
        summary = getattr(_engine, "_market_data_health_summary", None)
        if summary is not None and hasattr(summary, "to_dict"):
            body = summary.to_dict()
            body["feeds"] = list(body.get("symbols", {}).values())
            return jsonify(body)
        health = getattr(_engine, "_market_data_health", {}) or {}
        rows = [h.to_dict() for h in health.values()]
        overall = "green"
        if any(r.get("status") == "red" for r in rows):
            overall = "red"
        elif any(r.get("status") == "yellow" for r in rows):
            overall = "yellow"
        return jsonify({"feeds": rows, "overall": overall, "symbols": {r["symbol"]: r for r in rows}})

    @app.route("/api/live_data")
    def api_live_data():
        if _engine is None:
            return jsonify([])
        rows = []
        for sym in getattr(_engine, "_symbols", []):
            price = getattr(_engine, "_latest_price", {}).get(sym)
            ctx = getattr(_engine, "_latest_ctx", {}).get(sym)
            evt = getattr(_engine, "_last_market_events", {}).get(sym, {})
            agg = getattr(_engine, "_latest_agg_funding", {}).get(sym)
            hl = getattr(_engine, "_hl_predicted", None)
            hl_snap = hl.get(sym) if hl else None
            feed = getattr(_engine, "_market_data_health", {}).get(sym)
            rows.append({
                "symbol": sym,
                "price": getattr(price, "mid", None) if price else None,
                "funding": getattr(ctx, "funding_rate", None) if ctx else None,
                "predicted": (
                    hl_snap.predicted_funding_hl_8h if hl_snap else None
                ),
                "funding_cex_avg": agg.funding_avg if agg else None,
                "oi": getattr(ctx, "open_interest", None) if ctx else None,
                "oi_cex_usd": agg.oi_total if agg else None,
                "feed_status": feed.status if feed else "unknown",
                "feed_stale": bool(agg.stale) if agg else True,
            })
        return jsonify(rows)

    @app.route("/api/strategy_pnl")
    def api_strategy_pnl():
        """Per-strategy closed-trade PnL aggregate.

        Query params:
          days: optional int, filter to last N days of exits (default: all time)
          strategy: optional str, restrict to a single strategy name
        """
        if _engine is None:
            return jsonify([])
        try:
            days = int(request.args.get("days", "0"))
        except ValueError:
            days = 0
        strategy = request.args.get("strategy") or None
        since_ms = None
        if days and days > 0:
            since_ms = int((time.time() - days * 86400) * 1000)
        db = getattr(_engine, "_db", None)
        if db is None or not hasattr(db, "get_strategy_pnl"):
            return jsonify([])
        rows = db.get_strategy_pnl(since_ms=since_ms, strategy=strategy)
        return jsonify({
            "since_ms": since_ms,
            "days": days,
            "strategy_filter": strategy,
            "rows": rows,
        })

    @app.route("/api/strategies")
    def api_strategies():
        if _engine is None:
            return jsonify([])
        result = []
        for s in getattr(_engine, "_strategies", []):
            result.append({
                "name": getattr(s, "name", "unknown"),
                "enabled": getattr(s, "enabled", True),
            })
            sub = getattr(s, "_strategies", None)
            if isinstance(sub, dict):
                for sub_s in sub.values():
                    result.append({
                        "name": getattr(sub_s, "name", "unknown"),
                        "enabled": getattr(sub_s, "enabled", True),
                    })
        return jsonify(result)

    @app.route("/api/strategy/<name>")
    def api_strategy_detail(name):
        """Drill-down endpoint for a single strategy (Task 5.3).

        Returns signals, win rate, PnL, and parameters.
        Searches both top-level and sub-strategies (e.g. within ensemble).
        """
        if _engine is None:
            return jsonify({"error": "Engine not running"}), 503

        def _search_strategies(strategies, target_name):
            for s in strategies:
                if getattr(s, "name", "") == target_name:
                    return s
                sub = getattr(s, "_strategies", None)
                if isinstance(sub, dict):
                    result = _search_strategies(sub.values(), target_name)
                    if result is not None:
                        return result
                elif isinstance(sub, (list, tuple)):
                    result = _search_strategies(sub, target_name)
                    if result is not None:
                        return result
            return None

        strategy = _search_strategies(getattr(_engine, "_strategies", []), name)
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

    @app.route("/api/trades")
    def api_trades():
        if _engine is None:
            return jsonify([])
        db = getattr(_engine, "_db", None)
        if db is None:
            return jsonify([])
        try:
            rows = db.get_trades(limit=100)
            return jsonify(rows)
        except Exception:
            return jsonify([])

    @app.route("/api/metrics")
    def api_metrics():
        db = _get_db()
        if db is None:
            return jsonify({"error": "Database not available"}), 503
        since_ms = int((time.time() - 30 * 86400) * 1000)
        strategy_metrics = db.get_metrics_by_strategy(since_ms)
        daily_pnl = db.get_daily_pnl_series(30)

        # Overall metrics
        total_trades = sum(m["total_trades"] for m in strategy_metrics.values())
        total_wins = sum(m["wins"] for m in strategy_metrics.values())
        total_pnl = sum(m["total_pnl"] for m in strategy_metrics.values())
        overall_win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0.0

        return jsonify(
            {
                "overall": {
                    "total_trades": total_trades,
                    "win_rate": round(overall_win_rate, 2),
                    "total_pnl": round(total_pnl, 2),
                },
                "strategies": strategy_metrics,
                "daily_pnl": daily_pnl,
            }
        )

    @app.route("/api/logs")
    def api_logs():
        # HIGH-010: Path validation - prevent directory traversal
        logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
        log_path = os.path.abspath(os.path.join(logs_dir, "bot.log"))
        # Ensure the resolved path is still inside the logs directory
        if not log_path.startswith(logs_dir + os.sep):
            abort(403)
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

    # Serve socket.io.min.js locally (avoids CDN / SRI issues in sandboxed browsers)
    @app.route("/socket.io.min.js")
    def serve_socket_io_js():
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        js_path = os.path.join(project_root, "socket.io.min.js")
        if os.path.exists(js_path):
            with open(js_path, "rb") as f:
                from flask import Response
                return Response(f.read(), mimetype="application/javascript")
        abort(404)

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
