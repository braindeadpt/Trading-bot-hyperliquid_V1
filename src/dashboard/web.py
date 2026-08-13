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
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable, Deque

from flask import Flask, jsonify, request, abort, render_template
from flask_socketio import SocketIO, emit

from src.dashboard.auth import (
    DashboardAuthConfig,
    resolve_dashboard_auth,
    validate_dashboard_token,
)
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)


class _PositionsCapitalView:
    """Minimal portfolio view for sync risk metric reads in the dashboard."""

    def __init__(self, positions: Dict[str, Any], capital: float) -> None:
        self.positions = positions
        self.current_capital = capital


# Globals set by main.py
_engine: Optional[Any] = None
_socketio: Optional[SocketIO] = None
_emit_fn: Optional[Callable] = None
_emitter: Optional["DashboardEmitter"] = None

# Shadow panel can be expensive (DB scans + optional outcome eval). Cache
# responses so the sync Flask thread does not stall the whole dashboard.
_shadow_panel_cache: Dict[str, Any] = {}
_shadow_panel_lock = threading.Lock()
_SHADOW_CACHE_TTL_LIGHT_S = 15.0
_SHADOW_CACHE_TTL_EVAL_S = 300.0


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


def _feed_silence_sparklines(feed_silence: Dict[str, Any]) -> Dict[str, List[List[float]]]:
    """Intraday max-pct series per feed (last 24h) for the sparkline panel.

    Reads ``feed_age_samples`` from the research DB — the same store the
    FeedAgeRecorder writes. Best-effort: a missing/broken research DB
    degrades to empty series, never an error.
    """
    out: Dict[str, List[List[float]]] = {}
    if not feed_silence:
        return out
    try:
        from src.data.research_database import ResearchDatabase

        rdb = ResearchDatabase()
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - 24 * 3_600_000
        for feed in feed_silence:
            series = rdb.load_feed_age_samples(feed, start_ms, now_ms)
            out[feed] = [[float(b), float(p)] for b, p, _ in series]
    except Exception as exc:  # noqa: BLE001
        logger.warning("feed_silence_sparklines failed: %s", exc)
    return out


def _socket_connect_auth(auth, enabled: bool, token: Optional[str]) -> Optional[bool]:
    """Token gate for the Socket.IO ``connect`` event.

    Returns ``False`` to reject the connection, ``None`` to accept.
    Module-level so tests can exercise the exact runtime path
    (flask-socketio's ``test_client`` cannot run against Flask 3.1).
    """
    if not enabled or not token:
        return None
    provided = None
    if isinstance(auth, dict):
        provided = auth.get("token")
    if not provided:
        provided = request.args.get("token")
    if not validate_dashboard_token(provided, token):
        logger.warning("Socket.IO connect rejected — invalid or missing token")
        return False
    return None


class _IPRateLimiter:
    """Sliding-window per-IP request limiter (thread-safe).

    Bounds brute-force attempts against the dashboard auth token: each client
    IP may issue at most ``limit_per_min`` requests per rolling 60s window.
    Memory is bounded — idle entries are pruned opportunistically.
    """

    _WINDOW_SEC = 60.0

    def __init__(self, limit_per_min: int) -> None:
        self.limit_per_min = max(1, int(limit_per_min))
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str, now: Optional[float] = None) -> bool:
        """Record one request from ``ip``; False when over the limit."""
        now = time.time() if now is None else now
        with self._lock:
            dq = self._hits.get(ip)
            if dq is None:
                self._hits[ip] = dq = deque()
            while dq and now - dq[0] >= self._WINDOW_SEC:
                dq.popleft()
            if len(dq) >= self.limit_per_min:
                return False
            dq.append(now)
            # Opportunistic prune of idle IPs to bound memory.
            if len(self._hits) > 4096:
                self._hits = {k: v for k, v in self._hits.items() if v}
            return True


# ═════════════════════════════════════════════════════════════════════════════
# Background emitter - pushes real-time data every second
# ═════════════════════════════════════════════════════════════════════════════

def build_positions_payload(engine: Any) -> List[Dict[str, Any]]:
    """Build open-positions list for Socket.IO and REST."""
    portfolio = getattr(engine, "portfolio", None)
    if portfolio is None:
        return []
    latest_prices = getattr(engine, "_latest_price", {}) or {}
    market_events = getattr(engine, "_last_market_events", {}) or {}
    mark_prices = getattr(engine, "get_mark_prices_sync", lambda: {})() or {}
    snap = getattr(portfolio, "get_dashboard_snapshot_sync", None)
    positions = snap().positions if snap else getattr(portfolio, "get_positions_sync", lambda: {})()
    result: List[Dict[str, Any]] = []
    for sym, pos in positions.items():
        entry = float(getattr(pos, "entry_price", 0) or 0)
        size = float(getattr(pos, "size", 0) or 0)
        side = getattr(pos, "side", "long")

        mid = float(mark_prices.get(sym, 0) or 0)
        if mid <= 0:
            tick = latest_prices.get(sym)
            mid = float(getattr(tick, "mid", 0) or 0) if tick is not None else 0.0
        if mid <= 0:
            evt = market_events.get(sym) or {}
            mid = float(evt.get("price", 0) or 0)
        if mid <= 0:
            mid = float(getattr(pos, "current_price", 0) or 0)
        if mid <= 0:
            mid = entry

        if side == "short":
            unrealized = (entry - mid) * size
        else:
            unrealized = (mid - entry) * size

        pnl_pct = (unrealized / (entry * size) * 100) if entry and size else 0
        result.append({
            "symbol": sym,
            "side": side,
            "size": size,
            "entry_price": entry,
            "current_price": mid,
            "unrealized_pnl": unrealized,
            "pnl_pct": pnl_pct,
            "stop_loss": getattr(pos, "stop_loss_price", None),
            "take_profit": getattr(pos, "take_profit_price", None),
            "strategy": (getattr(pos, "metadata", None) or {}).get("strategy", "unknown"),
        })
    return result


def build_portfolio_payload(engine: Any) -> Dict[str, Any]:
    """Unified portfolio KPI payload (live marks + cached counters)."""
    portfolio = getattr(engine, "portfolio", None)
    if portfolio is None:
        return {}
    mark_prices = getattr(engine, "get_mark_prices_sync", lambda: {})() or {}
    build_live = getattr(portfolio, "build_live_dashboard_metrics", None)
    if build_live is None:
        return {}
    try:
        metrics = build_live(mark_prices)
        return {
            "capital": metrics["capital"],
            "daily_pnl": metrics["daily_pnl"],
            "daily_pnl_pct": metrics["daily_pnl_pct"],
            "daily_equity_pnl": metrics["daily_equity_pnl"],
            "daily_unrealized_pnl": metrics["daily_unrealized_pnl"],
            "daily_realized_pnl": metrics["daily_realized_pnl"],
            "day_start_equity": metrics["day_start_equity"],
            "max_drawdown_pct": metrics["max_drawdown_pct"],
            "daily_max_drawdown_pct": metrics["daily_max_drawdown_pct"],
            "open_positions": int(metrics["open_positions"]),
            "daily_trades": int(metrics["daily_trades"]),
            "total_trades": int(metrics["total_trades"]),
            "timestamp_ms": int(metrics["timestamp_ms"]),
            "unrealized_pnl": metrics["unrealized_pnl"],
        }
    except Exception as exc:
        logger.warning("build_portfolio_payload failed: %s", exc)
        return {}


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

    _FAST_EVENTS: tuple = ("live_data", "engine_monitor", "positions", "portfolio")
    _SLOW_EVENTS: tuple = (
        "status",
        "candles",
        "strategies",
        "signals",
        "decisions",
        "portfolio",
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
        # Legacy emitters call _safe_emit internally and return None.
        if payload is None:
            return
        try:
            blob = json.dumps(payload, sort_keys=True, default=str)
        except (TypeError, ValueError):
            blob = repr(payload)
        h = hash(blob)
        if event != "positions" and self._last_payload_hash.get(event) == h:
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
        hl_ws = getattr(_engine, "_hl_ws_client", None)
        ws_healthy = bool(getattr(hl_ws, "is_healthy", True)) if hl_ws is not None else True
        self._safe_emit("status", {
            "mode": getattr(_engine, "_config", {}).get("mode", "paper") if hasattr(_engine, "_config") else "paper",
            "uptime_sec": getattr(_engine, "uptime_sec", 0),
            "memory_mb": getattr(_engine, "memory_mb", 0),
            "circuit_breaker": "ON" if getattr(getattr(_engine, "_risk", None), "circuit_breaker_tripped", False) else "OFF",
            "running": getattr(_engine, "_running", False),
            "ws_healthy": ws_healthy,
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

            price_mid = getattr(price, "mid", None) if price else None
            oi_coins = getattr(ctx, "open_interest", None) if ctx else None
            oi_usd = None
            if oi_coins is not None and price_mid:
                oi_usd = float(oi_coins) * float(price_mid)

            rows.append({
                "symbol": sym,
                "price": price_mid,
                "bid": getattr(price, "bid", None) if price else None,
                "ask": getattr(price, "ask", None) if price else None,
                "spread_pct": round((getattr(price, "ask", 0) - getattr(price, "bid", 0)) / getattr(price, "mid", 1) * 100, 4) if price else None,
                "funding": getattr(ctx, "funding_rate", None) if ctx else None,
                "predicted": getattr(ctx, "predicted_funding", None) if ctx else None,
                "oi": oi_coins,
                "oi_usd": oi_usd,
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
                # v3.1.15: volume observability (pure read-out, no trading logic)
                "obv_slope_5m": evt.get("obv_slope_5m"),
                "mfi_5m": evt.get("mfi_5m"),
                "vwap_1m_rolling": evt.get("vwap_1m_rolling"),
                "vwap_5m_rolling": evt.get("vwap_5m_rolling"),
                "vwap_15m_rolling": evt.get("vwap_15m_rolling"),
                "vwap_1h_rolling": evt.get("vwap_1h_rolling"),
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
        """Engine health - ticks/sec, total ticks, last error, regime, reconciliation."""
        stats = getattr(_engine, "_tick_stats", {})
        last_err = getattr(_engine, "_last_error", None)
        last_events = getattr(_engine, "_last_market_events", {})

        vol_cb = getattr(_engine, "_vol_circuit", None)
        vol_snapshot = vol_cb.snapshot() if vol_cb is not None else {}
        vol_blocked = {
            sym: int(st.get("block_until_ms", 0)) > time.time() * 1000
            for sym, st in vol_snapshot.items()
        } if vol_snapshot else {}

        recent = []
        for sym, evt in sorted(last_events.items(), key=lambda x: x[1].get("processed_at", 0), reverse=True)[:3]:
            recent.append({
                "symbol": sym,
                "price": evt.get("price"),
                "age_ms": int((time.time() - evt.get("processed_at", 0)) * 1000),
            })

        all_strategy_names = []
        for s in getattr(_engine, "_strategies", []):
            all_strategy_names.append(getattr(s, "name", "unknown"))
            sub_strategies = getattr(s, "_strategies", None)
            if sub_strategies and isinstance(sub_strategies, dict):
                for sub_name, sub in sub_strategies.items():
                    all_strategy_names.append(getattr(sub, "name", sub_name))

        regime_per_symbol: Dict[str, str] = {}
        adx_per_symbol: Dict[str, Optional[float]] = {}
        for sym, evt in last_events.items():
            adx = evt.get("adx_14")
            if adx is not None and isinstance(adx, (int, float)):
                adx_per_symbol[sym] = float(adx)
                if adx >= 25.0:
                    regime_per_symbol[sym] = "trend"
                elif adx <= 20.0:
                    regime_per_symbol[sym] = "range"
                else:
                    regime_per_symbol[sym] = "transition"
            else:
                regime_per_symbol[sym] = "unknown"

        recon_task = getattr(_engine, "_reconcile_task", None)
        ws_task = getattr(_engine, "_ws_health_check_task", None)
        reconcile_status = {
            "running": recon_task is not None and not recon_task.done(),
            "enabled": recon_task is not None,
        }
        ws_health_status = {
            "running": ws_task is not None and not ws_task.done(),
            "enabled": ws_task is not None,
        }

        governor = getattr(_engine, "_strategy_governor", None)
        governor_info = {
            "disabled": sorted(governor.disabled_strategies) if governor else [],
            "active_count": sum(
                1 for s in all_strategy_names
                if governor is None or s not in governor.disabled_strategies
            ) if governor else len(all_strategy_names),
        }

        risk = getattr(_engine, "_risk", None)
        portfolio = getattr(_engine, "portfolio", None)
        portfolio_leverage = 0.0
        total_notional = 0.0
        long_exposure = 0.0
        short_exposure = 0.0
        leverage_max = 0.0
        open_trade_risk = 0.0
        daily_drawdown = 0.0
        max_drawdown_pct = 0.0
        mark_prices = getattr(_engine, "get_mark_prices_sync", lambda: {})() or {}
        live_metrics: Dict[str, Any] = {}
        if portfolio is not None:
            build_live = getattr(portfolio, "build_live_dashboard_metrics", None)
            if build_live is not None:
                try:
                    live_metrics = build_live(mark_prices)
                except Exception as exc:
                    logger.warning("engine_monitor live metrics failed: %s", exc)

        if risk is not None and portfolio is not None:
            snap = getattr(portfolio, "get_dashboard_snapshot_sync", lambda: None)()
            capital = live_metrics.get("capital") or (
                snap.total_equity if snap else getattr(portfolio, "sync_capital", lambda: 0)()
            )
            positions = snap.positions if snap else getattr(portfolio, "get_positions_sync", lambda: {})()
            if capital > 0:
                portfolio_leverage = risk.get_portfolio_leverage(positions, capital, mark_prices)
                total_notional = risk.get_portfolio_total_notional(positions, mark_prices)
                long_exposure, short_exposure = risk.get_directional_exposure(
                    _PositionsCapitalView(positions, capital)
                )
                open_trade_risk = risk.get_open_risk_pct(positions, capital)
            leverage_max = getattr(risk, "_leverage_max", 0.0)
            if snap is not None:
                daily_drawdown = live_metrics.get(
                    "daily_max_drawdown_pct",
                    snap.daily_max_drawdown_pct,
                )
                max_drawdown_pct = live_metrics.get(
                    "max_drawdown_pct",
                    snap.max_drawdown_pct,
                )
            else:
                daily_drawdown = getattr(portfolio, "sync_daily_max_drawdown_pct", lambda: 0)()
                max_drawdown_pct = getattr(portfolio, "sync_max_drawdown_pct", lambda: 0)()

        fb = getattr(_engine, "_funding_blackout", None)
        funding_blackout_active = bool(
            fb.is_blocked(int(time.time() * 1000)) if fb is not None else False
        )
        circuit_breaker = bool(getattr(risk, "circuit_breaker_tripped", False)) if risk else False

        self._safe_emit("engine_monitor", {
            "ticks_per_second": stats.get("per_second", 0),
            "total_ticks": stats.get("total", 0),
            "last_error": last_err,
            "recent_events": recent,
            "symbols": getattr(_engine, "_symbols", []),
            "strategies": all_strategy_names,
            "regime_per_symbol": regime_per_symbol,
            "adx_per_symbol": adx_per_symbol,
            "reconcile": reconcile_status,
            "ws_health": ws_health_status,
            "governor": governor_info,
            "vol_circuit_blocked": vol_blocked,
            "vol_circuit": vol_snapshot,
            "portfolio_leverage": portfolio_leverage,
            "leverage_max": leverage_max,
            "total_notional_usd": total_notional,
            "directional_exposure": long_exposure + short_exposure,
            "long_exposure_pct": long_exposure,
            "short_exposure_pct": short_exposure,
            "open_trade_risk": open_trade_risk,
            "daily_drawdown": daily_drawdown,
            "max_drawdown_pct": max_drawdown_pct,
            "circuit_breaker": circuit_breaker,
            "funding_blackout_active": funding_blackout_active,
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
        from src.strategies.ensemble import STRATEGY_CLASS
        strategies = getattr(_engine, "_strategies", [])
        sig_hist = getattr(_engine, "_signal_history", [])
        governor = getattr(_engine, "_strategy_governor", None)
        governor_metrics = governor.last_metrics if governor else {}
        governor_disabled = governor.disabled_strategies if governor else set()
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        def _build_strategy_info(s):
            name = getattr(s, "name", "unknown")
            today_count = sum(
                1 for sig in sig_hist
                if sig.get("strategy") == name and sig.get("time", "").startswith(today_str)
            )
            last = next((sig for sig in sig_hist if sig.get("strategy") == name), None)
            strat_class = STRATEGY_CLASS.get(name, "other")
            is_governed = name in governor_disabled
            metrics = governor_metrics.get(name, {})
            return {
                "name": name,
                "enabled": getattr(s, "enabled", True) and not is_governed,
                "governor_disabled": is_governed,
                "strategy_class": strat_class,
                "description": (getattr(s, "__doc__", "") or "No description").split("\n")[0][:80],
                "params": str(getattr(s, "params", {})),
                "last_signal_time": last.get("time") if last else None,
                "last_signal_side": last.get("side") if last else None,
                "last_signal_confidence": last.get("confidence") if last else None,
                "last_signal_status": last.get("status") if last else None,
                "signals_today": today_count,
                "sharpe_30d": safe_float(metrics.get("sharpe"), 0.0),
                "trades_30d": int(safe_float(metrics.get("trades"), 0.0)),
            }

        result = []
        for s in strategies:
            result.append(_build_strategy_info(s))
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

    def _emit_portfolio(self) -> Optional[Dict[str, Any]]:
        if _engine is None:
            return None
        payload = build_portfolio_payload(_engine)
        if not payload:
            logger.warning("portfolio emit skipped — empty payload")
            return None
        return payload

    def _build_positions_payload(self) -> List[Dict[str, Any]]:
        if _engine is None:
            return []
        return build_positions_payload(_engine)

    def _emit_positions(self) -> List[Dict[str, Any]]:
        try:
            return self._build_positions_payload()
        except Exception:
            return []

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
                    "funding_paid": safe_float(r.get("funding_paid"), 0.0),
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

    # CRIT-004: per-IP rate limit on REST endpoints (brute-force mitigation).
    # Resolved hash-neutrally: dashboard.rate_limit_per_min (tests) →
    # DASHBOARD_RATE_LIMIT_PER_MIN env → 100/min default. Registered BEFORE
    # the auth guard so failed token attempts are counted too. Socket.IO
    # transport + static assets are exempt (realtime stream / page assets).
    _dash_rl_cfg = (config or {}).get("dashboard", {}) or {}
    try:
        _rl_limit = int(_dash_rl_cfg.get("rate_limit_per_min") or 0)
    except (TypeError, ValueError):
        _rl_limit = 0
    if not _rl_limit:
        try:
            _rl_limit = int(os.environ.get("DASHBOARD_RATE_LIMIT_PER_MIN", "") or 0)
        except ValueError:
            _rl_limit = 0
    if not _rl_limit:
        _rl_limit = 100
    _rate_limiter = _IPRateLimiter(_rl_limit)

    @app.before_request
    def _rate_limit():
        path = request.path
        if path.startswith(("/static/", "/socket.io/")) or path == "/socket.io.min.js":
            return None
        if not _rate_limiter.allow(request.remote_addr or "unknown"):
            resp = jsonify({"error": "rate limit exceeded", "retry_after_sec": 60})
            resp.status_code = 429
            resp.headers["Retry-After"] = "60"
            return resp
        return None

    # CRIT-003: Dashboard auth - token-based guard (REST + Socket.IO)
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

    # Start background emitter (B9) — honor configured cadence
    _dash_cfg = (config or {}).get("dashboard", {}) or {}
    _push_sec = float(_dash_cfg.get("push_interval_sec", 2.0) or 2.0)
    global _emitter
    emitter = DashboardEmitter(
        socketio,
        fast_interval=min(0.5, max(0.1, _push_sec / 4.0)),
        slow_interval=max(1.0, _push_sec * 2.5),
    )
    _emitter = emitter
    emitter.start()


    @app.route("/")
    def index():
        symbols = sorted(_allowed_symbols()) if _allowed_symbols() else ["BTC", "ETH", "SOL"]
        return render_template(
            "index.html",
            auth_required=_auth_enabled and bool(_dashboard_token),
            symbols=symbols,
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
                try:
                    data_bus_lag = bus.last_publish_age_sec("price:BTC")
                except TypeError:
                    data_bus_lag = None
            vol_cb = getattr(_engine, "_vol_circuit", None)
            fb = getattr(_engine, "_funding_blackout", None)
            vol_snapshot = vol_cb.snapshot() if vol_cb is not None else {}
            vol_blocked = {
                sym: int(st.get("block_until_ms", 0)) > time.time() * 1000
                for sym, st in vol_snapshot.items()
            } if vol_snapshot else {}
            reconcile_task = getattr(_engine, "_reconcile_task", None)
            ws_task = getattr(_engine, "_ws_health_check_task", None)
            governor = getattr(_engine, "_strategy_governor", None)
            body.update({
                "running": running,
                "uptime_sec": getattr(_engine, "uptime_sec", 0),
                "last_tick_age_sec": last_tick,
                "data_bus_lag_sec": data_bus_lag,
                "circuit_breaker": bool(cb),
                "vol_circuit": vol_snapshot,
                "vol_circuit_blocked": vol_blocked,
                "funding_blackout_active": bool(
                    fb.is_blocked() if fb is not None else False
                ),
                "ws_healthy": bool(
                    getattr(getattr(_engine, "_hl_ws_client", None), "is_healthy", True)
                ),
                "reconcile_running": reconcile_task is not None and not reconcile_task.done(),
                "ws_health_loop_running": ws_task is not None and not ws_task.done(),
                "governor_disabled": sorted(governor.disabled_strategies) if governor else [],
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
            silence = getattr(_engine, "_feed_silence", None)
            if silence is not None:
                body["feed_silence"] = silence.snapshot()
                body["feed_silence_degraded"] = bool(silence.any_degraded)
            body["feed_silence_spark"] = _feed_silence_sparklines(
                body.get("feed_silence", {})
            )
            return jsonify(body)
        health = getattr(_engine, "_market_data_health", {}) or {}
        rows = [h.to_dict() for h in health.values()]
        overall = "green"
        if any(r.get("status") == "red" for r in rows):
            overall = "red"
        elif any(r.get("status") == "yellow" for r in rows):
            overall = "yellow"
        body = {"feeds": rows, "overall": overall, "symbols": {r["symbol"]: r for r in rows}}
        silence = getattr(_engine, "_feed_silence", None)
        if silence is not None:
            body["feed_silence"] = silence.snapshot()
            body["feed_silence_degraded"] = bool(silence.any_degraded)
        body["feed_silence_spark"] = _feed_silence_sparklines(
            body.get("feed_silence", {})
        )
        return jsonify(body)

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
        """List all strategies with v3.1.23 fields: class, governor, sharpe."""
        from src.strategies.ensemble import STRATEGY_CLASS
        if _engine is None:
            return jsonify([])
        governor = getattr(_engine, "_strategy_governor", None)
        gov_metrics = governor.last_metrics if governor else {}
        gov_disabled = governor.disabled_strategies if governor else set()
        sig_hist = getattr(_engine, "_signal_history", [])
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        def _info(s):
            name = getattr(s, "name", "unknown")
            today_count = sum(
                1 for sig in sig_hist
                if sig.get("strategy") == name
                and sig.get("time", "").startswith(today_str)
            )
            metrics = gov_metrics.get(name, {})
            return {
                "name": name,
                "enabled": getattr(s, "enabled", True) and name not in gov_disabled,
                "governor_disabled": name in gov_disabled,
                "strategy_class": STRATEGY_CLASS.get(name, "other"),
                "signals_today": today_count,
                "trades_30d": int(safe_float(metrics.get("trades"), 0.0)),
                "sharpe_30d": safe_float(metrics.get("sharpe"), 0.0),
            }

        result = []
        for s in getattr(_engine, "_strategies", []):
            result.append(_info(s))
            sub = getattr(s, "_strategies", None)
            if isinstance(sub, dict):
                for sub_s in sub.values():
                    result.append(_info(sub_s))
        return jsonify(result)

    @app.route("/api/shadow_panel")
    def api_shadow_panel():
        """Phase08 shadow scoreboard + baseline-gate progress (read-only).

        Idealized fills disclaimer included. Does not affect trading.
        Query: evaluate=0 to skip expensive outcome simulation (signals only).
        Results are TTL-cached to keep the dashboard responsive.
        """
        from src.research.shadow_panel import build_shadow_panel_payload
        from src.utils.config import get_strategy_section, load_config

        evaluate = str(request.args.get("evaluate", "0")).strip() not in (
            "0",
            "false",
            "False",
            "no",
        )
        cache_key = "eval" if evaluate else "light"
        ttl = _SHADOW_CACHE_TTL_EVAL_S if evaluate else _SHADOW_CACHE_TTL_LIGHT_S
        now = time.time()
        with _shadow_panel_lock:
            cached = _shadow_panel_cache.get(cache_key)
            if cached and (now - float(cached.get("ts", 0))) < ttl:
                return jsonify(cached["payload"])

        cfg = None
        shadow_names: list = []
        try:
            if _engine is not None and getattr(_engine, "_config", None) is not None:
                cfg = _engine._config
            else:
                from pathlib import Path
                cfg = load_config(Path("config/settings.yaml"))
            p08 = get_strategy_section(cfg, "phase08")
            shadow_names = [str(s) for s in (p08.get("shadow_strategies") or [])]
            # Also surface live shadow instances if wired on the engine
            live_shadow = getattr(_engine, "_shadow_strategies", None) if _engine else None
            if live_shadow:
                for s in live_shadow:
                    n = getattr(s, "name", None)
                    if n and n not in shadow_names:
                        shadow_names.append(n)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc), "rows": []}), 500

        try:
            payload = build_shadow_panel_payload(
                shadow_names=shadow_names,
                config=cfg,
                evaluate=evaluate,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("shadow_panel build failed evaluate=%s: %s", evaluate, exc)
            with _shadow_panel_lock:
                stale = _shadow_panel_cache.get(cache_key)
            if stale:
                out = dict(stale["payload"])
                out["stale"] = True
                out["error"] = str(exc)
                return jsonify(out)
            return jsonify({"error": str(exc), "rows": []}), 500

        with _shadow_panel_lock:
            _shadow_panel_cache[cache_key] = {"ts": now, "payload": payload}
        return jsonify(payload)

    @app.route("/api/top_traders")
    def api_top_traders():
        """Top-wallet aggregate bias + virtual swing book (research only)."""
        from src.research.top_trader_panel import build_top_traders_panel_payload
        from src.utils.config import load_config

        try:
            cfg = None
            if _engine is not None and getattr(_engine, "_config", None) is not None:
                cfg = _engine._config
            else:
                from pathlib import Path

                cfg = load_config(Path("config/settings.yaml"))
            payload = build_top_traders_panel_payload(config=cfg, engine=_engine)
            return jsonify(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("top_traders panel failed: %s", exc)
            return jsonify({"error": str(exc), "snapshots": [], "open_positions": []}), 500

    @app.route("/api/research_watchdogs")
    def api_research_watchdogs():
        """Read-only status of the auto-rerun research watchdogs.

        bias screening (≥20 datas) + liquidation flush recheck (≥30 days).
        """
        from src.research.research_watchdog_status import build_research_watchdogs_payload

        try:
            return jsonify(build_research_watchdogs_payload())
        except Exception as exc:  # noqa: BLE001
            logger.warning("research_watchdogs failed: %s", exc)
            return jsonify({"error": str(exc), "watchdogs": []}), 500

    @app.route("/api/dvol")
    def api_dvol():
        """DVOL daily series + trailing-30d percentile per symbol (IV gate).

        Read-only research-feed data — never touches execution. BTC/ETH use
        their own Deribit index; SOL/HYPE classify against BTC (global proxy),
        mirroring the backtest evidence (docs/IV_HIGH_ONLY_AB_SPLIT.md).
        """
        try:
            from src.data.dvol_feed import (
                DVOL_WINDOW_DAYS,
                IV_HIGH_PCT,
                build_iv_percentile,
                classify_iv,
                dvol_currency_for,
                iv_pct_at,
            )
            from src.data.research_database import ResearchDatabase

            rdb = ResearchDatabase()
            try:
                symbols = sorted(_allowed_symbols()) or ["BTC", "ETH", "SOL", "HYPE"]
                now_ms = int(time.time() * 1000)
                lookback = int(max(2 * DVOL_WINDOW_DAYS + 5, 45)) * 86_400_000

                series: Dict[str, Any] = {}
                current_pct: Dict[str, Optional[float]] = {}
                for currency in ("BTC", "ETH"):
                    closes = rdb.load_dvol_daily(currency, now_ms - lookback, now_ms)
                    pct_series = build_iv_percentile(closes, DVOL_WINDOW_DAYS)
                    pct_by_ts = {ts: p for ts, p in pct_series}
                    series[currency] = [
                        {
                            "ts": ts,
                            "close": round(float(c), 2),
                            "pct": (
                                None if pct_by_ts.get(ts) is None
                                else round(float(pct_by_ts[ts]), 1)
                            ),
                        }
                        for ts, c in closes
                    ]
                    current_pct[currency] = iv_pct_at(pct_series, now_ms)

                current: Dict[str, Any] = {}
                for sym in symbols:
                    currency = dvol_currency_for(sym)
                    pct = current_pct.get(currency)
                    current[sym] = {
                        "pct": None if pct is None else round(float(pct), 1),
                        "cls": classify_iv(pct),
                        "currency": currency,
                    }
                return jsonify({
                    "series": series,
                    "current": current,
                    "threshold": IV_HIGH_PCT,
                    "window_days": DVOL_WINDOW_DAYS,
                    "asof_ms": now_ms,
                })
            finally:
                rdb.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("dvol endpoint failed: %s", exc)
            return jsonify({"error": str(exc), "series": {}, "current": {}}), 500

    @app.route("/api/iv_gate_shadow")
    def api_iv_gate_shadow():
        """Shadow IV sample distribution — n per class + avg percentile.

        Uses the exact same join/slices as ``scripts/iv_gate_shadow_vs_pnl.py``
        (the single source of truth), so the dashboard and the recheck watchdog
        can never disagree about what counts as a matched IV decision. Read-only
        research data — the gate stays shadow, never touches execution.
        """
        try:
            from scripts.iv_gate_shadow_vs_pnl import (
                BACKTEST_EVIDENCE,
                build_report,
            )
            from scripts.iv_gate_shadow_recheck import TARGET_CLOSED

            report = build_report()
            if report.get("error"):
                return jsonify({"error": report["error"], "by_class": {}, "total": 0}), 200
            by_class = {}
            for cls in ("high_iv", "low_iv", "unknown"):
                s = report["slices"][cls]
                by_class[cls] = {
                    "n": s["n"],
                    "n_closed": s["n_closed"],
                    "n_open": s["n_open"],
                    "n_pct": s.get("n_pct", 0),
                    "avg_pct": s.get("avg_pct"),
                    # Accumulated PnL of closed trades in the slice — the
                    # live-vs-backtest evidence the gate will be decided on.
                    "net_pnl_usd": s.get("net_pnl_usd"),
                    "win_rate": s.get("win_rate"),
                    "avg_pnl_usd": s.get("avg_pnl_usd"),
                    "median_pnl_usd": s.get("median_pnl_usd"),
                    "best_usd": s.get("best_usd"),
                    "worst_usd": s.get("worst_usd"),
                }
            n_dec = max(1, report["n_decisions"])
            n_total = max(1, report["n_trades"])
            return jsonify({
                "by_class": by_class,
                "total": report["n_trades"],
                "n_decisions": report["n_decisions"],
                "matched": report["matched_decisions"],
                "trades_with_decision": report["trades_with_decision"],
                # Join coverage — how much of the decision stream matched an
                # executed trade, and how much of the trade stream carries an
                # IV class. A gap here is a wiring problem, not an edge signal.
                "join_coverage_pct": round(report["matched_decisions"] / n_dec * 100, 1),
                "trade_coverage_pct": round(report["trades_with_decision"] / n_total * 100, 1),
                "verdict": report["verdict"],
                "threshold": report["iv_high_pct"],
                "target_closed": TARGET_CLOSED,
                "backtest": BACKTEST_EVIDENCE,
                "asof_ms": int(time.time() * 1000),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("iv_gate_shadow endpoint failed: %s", exc)
            return jsonify({"error": str(exc), "by_class": {}, "total": 0}), 500

    @app.route("/api/strategy/<name>")
    def api_strategy_detail(name):
        """Drill-down endpoint for a single strategy (Task 5.3).

        Returns signals, win rate, PnL, and parameters.
        Searches both top-level and sub-strategies (e.g. within ensemble).
        Prefers live DB trades/signals over empty in-memory ``_strategy_stats``.
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

        # Params: prefer YAML section / strategy._cfg over empty ``.params``
        params: Dict[str, Any] = {}
        raw_params = getattr(strategy, "params", None)
        if isinstance(raw_params, dict) and raw_params:
            params = dict(raw_params)
        else:
            cfg_obj = getattr(_engine, "_config", None)
            section_key = None
            # Map class names → settings.yaml keys
            name_to_section = {
                "VWAPDeviation": "vwap_deviation",
                "ChecklistMeta": "checklist_meta",
                "VolatilityBreakout": "volatility_breakout",
                "OrderBookScalper": "orderbook_scalper",
                "CVDOrderFlow": "cvd_orderflow",
                "FundingArbitrage": "funding_arbitrage",
                "FundingMomentum": "funding_momentum",
                "SpotPerpCarry": "spot_perp_carry",
                "LeadLag": "lead_lag",
                "LiquidationCatcher": "liquidation_catcher",
                "TopTraderFlow": "top_trader_flow",
            }
            section_key = name_to_section.get(name)
            if cfg_obj is not None and section_key and hasattr(cfg_obj, "get"):
                section = cfg_obj.get(f"strategy.{section_key}", {}) or {}
                if isinstance(section, dict):
                    # Keep a short readable subset for the modal
                    keep = (
                        "enabled", "z_threshold", "volume_surge", "min_adx", "max_adx",
                        "min_confidence", "use_session_filter", "session_start_utc_h",
                        "session_end_utc_h", "max_hold_hours", "bias_threshold",
                        "stop_loss_pct", "take_profit_pct",
                    )
                    params = {k: section[k] for k in keep if k in section}
            if not params:
                # Fall back to a few public UPPER attrs on the strategy instance
                for attr in (
                    "Z_THRESHOLD", "VOLUME_SURGE", "MIN_ADX", "MAX_ADX",
                    "MIN_CONFIDENCE", "USE_SESSION_FILTER", "BIAS_THRESHOLD",
                ):
                    if hasattr(strategy, attr):
                        params[attr.lower()] = getattr(strategy, attr)

        mem_stats = getattr(_engine, "_strategy_stats", {}).get(name, {})
        db = getattr(_engine, "_db", None)
        total_signals = int(mem_stats.get("total_signals", 0) or 0)
        approved = int(mem_stats.get("approved_signals", 0) or 0)
        rejected = int(mem_stats.get("rejected_signals", 0) or 0)
        winning = int(mem_stats.get("winning_trades", 0) or 0)
        losing = int(mem_stats.get("losing_trades", 0) or 0)
        total_pnl = float(mem_stats.get("total_pnl", 0.0) or 0.0)
        avg_pnl = float(mem_stats.get("avg_pnl", 0.0) or 0.0)
        signal_history = list(mem_stats.get("signal_history", []) or [])[:20]

        if db is not None:
            try:
                trades = db.get_trades(limit=500, strategy=name) or []
                closed = [t for t in trades if str(t.get("status") or "") == "closed"]
                if closed:
                    pnls = []
                    for t in closed:
                        p = t.get("pnl_pct")
                        if p is None and t.get("pnl_usd") is not None and t.get("entry_price"):
                            # best-effort; prefer stored pct
                            p = t.get("pnl_pct")
                        if p is not None:
                            pnls.append(float(p))
                    winning = sum(1 for p in pnls if p > 0)
                    losing = sum(1 for p in pnls if p <= 0)
                    if pnls:
                        avg_pnl = sum(pnls) / len(pnls)
                        # pnl_pct in DB is often already fraction (0.01 = 1%)
                        total_pnl = sum(pnls)
                if hasattr(db, "get_signals"):
                    sigs = db.get_signals(limit=500, strategy=name) or []
                    if sigs:
                        total_signals = max(total_signals, len(sigs))
                        signal_history = [
                            {
                                "time": (
                                    datetime.fromtimestamp(
                                        int(s.get("timestamp") or 0) / 1000.0,
                                        tz=timezone.utc,
                                    ).strftime("%Y-%m-%d %H:%M")
                                    if s.get("timestamp")
                                    else "--"
                                ),
                                "symbol": s.get("symbol"),
                                "side": s.get("side"),
                                "confidence": s.get("confidence"),
                                "status": "logged",
                                "reason": s.get("reason"),
                            }
                            for s in sigs[:20]
                        ]
            except Exception as exc:  # noqa: BLE001
                logger.debug("strategy detail DB enrich failed: %s", exc)

        n_closed = winning + losing
        win_rate = (winning / n_closed) if n_closed else float(mem_stats.get("win_rate", 0.0) or 0.0)

        return jsonify({
            "name": name,
            "enabled": getattr(strategy, "enabled", True),
            "description": (getattr(strategy, "__doc__", "") or "No description").split("\n")[0][:200],
            "params": params,
            "stats": {
                "total_signals": total_signals,
                "approved_signals": approved,
                "rejected_signals": rejected,
                "winning_trades": winning,
                "losing_trades": losing,
                "win_rate": win_rate,
                "total_pnl_pct": total_pnl * 100.0,
                "avg_pnl_pct": avg_pnl * 100.0,
            },
            "signal_history": signal_history,
            "note": (
                "Evaluating live — 0 today often means volume_surge/session filters skipped entries "
                "(need vol ≥1.5×). Not broken."
                if name == "VWAPDeviation"
                else None
            ),
        })

    @app.route("/api/portfolio")
    def api_portfolio():
        if _engine is None:
            return jsonify({})
        payload = build_portfolio_payload(_engine)
        return jsonify(payload or {})

    @app.route("/api/positions")
    def api_positions():
        if _engine is None:
            return jsonify([])
        return jsonify(build_positions_payload(_engine))

    @app.route("/api/trades")
    def api_trades():
        if _engine is None:
            return jsonify([])
        db = getattr(_engine, "_db", None)
        if db is None:
            return jsonify([])
        try:
            rows = db.get_trades(limit=100)
            # Enrich each executed trade with its shadow IV decision
            # (percentile + high/low class) via the same join the recheck
            # watchdog and CLI use — single source of truth, read-only.
            try:
                from scripts.iv_gate_shadow_vs_pnl import (
                    join_decisions_to_trades,
                    load_shadow_decisions,
                    resolve_db_paths,
                )

                _research = resolve_db_paths()[1]
                _decisions = load_shadow_decisions(_research)
                rows, _matched = join_decisions_to_trades(
                    _decisions, rows, tolerance_ms=60_000
                )
            except Exception:  # noqa: BLE001 — IV enrichment is best-effort
                pass
            return jsonify(rows)
        except Exception:
            return jsonify([])

    # ── Chart endpoints ──

    VALID_TFS = frozenset(["1m", "5m", "15m", "1h"])

    def _allowed_symbols() -> frozenset:
        """Return the set of symbols configured in the engine."""
        symbols = getattr(_engine, "_symbols", None)
        if symbols:
            return frozenset(symbols)
        return frozenset()

    @app.route("/api/candles")
    def api_candles():
        if _engine is None:
            return jsonify([]), 503
        db = _get_db()
        if db is None:
            return jsonify([]), 503

        symbol = (request.args.get("symbol", "") or "").strip().upper()
        tf = (request.args.get("tf", "") or "").strip()
        limit_str = (request.args.get("limit", "") or "").strip()

        # Whitelist symbol
        allowed = _allowed_symbols()
        if not allowed:
            return jsonify({"error": "No symbols configured"}), 400
        if symbol not in allowed:
            return jsonify({"error": f"Invalid symbol '{symbol}'. Allowed: {sorted(allowed)}"}), 400

        # Whitelist timeframe
        if tf not in VALID_TFS:
            return jsonify({"error": f"Invalid tf '{tf}'. Allowed: {sorted(VALID_TFS)}"}), 400

        # Parse limit (safe int)
        try:
            limit = max(1, min(int(limit_str), 1000)) if limit_str else 200
        except (ValueError, TypeError):
            limit = 200

        try:
            candles = db.get_candles(symbol, tf, limit=limit)
            result = [
                {
                    "time": int(c.timestamp_ms / 1000),
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for c in candles
            ]
            return jsonify(result)
        except Exception as exc:
            logger.error("api_candles error: %s", exc)
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/trades_chart")
    def api_trades_chart():
        """Trades for chart markers — filtered by symbol when provided."""
        if _engine is None:
            return jsonify([])
        db = _get_db()
        if db is None:
            return jsonify([])

        symbol = (request.args.get("symbol", "") or "").strip().upper()
        limit_str = (request.args.get("limit", "") or "").strip()

        allowed = _allowed_symbols()
        if symbol and symbol not in allowed:
            return jsonify({"error": f"Invalid symbol '{symbol}'"}), 400

        try:
            limit = max(1, min(int(limit_str), 500)) if limit_str else 200
        except (ValueError, TypeError):
            limit = 200

        try:
            if symbol:
                sql = "SELECT * FROM trades WHERE symbol = ? ORDER BY entry_time DESC LIMIT ?"
                params = (symbol, limit)
            else:
                sql = "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?"
                params = (limit,)
            with db._conn():
                cur = db._conn().execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
            return jsonify(rows)
        except Exception as exc:
            logger.error("api_trades_chart error: %s", exc)
            return jsonify([]), 500

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
        """Socket.IO connect gate: auth-enabled deployments require a token.

        Single registration point (CRIT: the auth check MUST live here — a
        second @socketio.on("connect") on the same namespace silently
        overwrites the first).
        """
        if _socket_connect_auth(auth, _auth_enabled, _dashboard_token) is False:
            return False
        logger.info("Dashboard client connected")
        # Push everything immediately
        emitter._emit_all()

    @socketio.on("disconnect")
    def on_disconnect(reason=None):
        logger.info("Dashboard client disconnected: %s", reason)

    return app, socketio, emitter._emit_all
