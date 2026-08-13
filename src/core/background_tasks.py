"""Background asyncio loop bodies owned by ``TradingEngine`` (Fase 09 extraction).

Task *lifecycle* (creation in ``start()``, cancellation in ``stop()``, and the
task-handle attributes such as ``_funding_poll_task``) intentionally stays on
``TradingEngine`` — several tests build the engine via
``TradingEngine.__new__(TradingEngine)`` and stub instance attributes/methods
directly (e.g. ``engine._poll_funding_loop = _no_loop``), then assert on
``engine._funding_poll_task`` etc. Moving only the loop *bodies* here keeps
that stubbing surface intact: ``TradingEngine`` keeps thin ``async def``
delegators with the same names that call into this class.

Zero behavior change vs. the code previously inlined in engine.py — this is
a straight extraction with ``self`` renamed to ``engine`` throughout.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.engine import TradingEngine

logger = logging.getLogger(__name__)


class BackgroundTasks:
    """Holds the four long-running loop bodies for a ``TradingEngine``."""

    def __init__(self, engine: "TradingEngine") -> None:
        self._engine = engine

    async def reconcile_loop(self) -> None:
        """Reconcile local state with Hyperliquid user_state (Phase 03)."""
        engine = self._engine
        while engine._running:
            try:
                await asyncio.sleep(engine._reconciliation_interval_sec)
                if not engine._running:
                    return
                if engine._reconciler is None:
                    continue
                report = await engine._reconciler.reconcile_once(executor=engine._executor)
                if report.success:
                    logger.debug(
                        "Reconciliation OK — exchange=%d local=%d actions=%s",
                        len(report.exchange_positions),
                        len(report.local_symbols),
                        report.actions,
                    )
                else:
                    logger.warning(
                        "Reconciliation issues: errors=%s mismatches=%s orphans_ex=%s orphans_loc=%s",
                        report.errors,
                        report.mismatches,
                        report.orphan_exchange,
                        report.orphan_local,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Reconcile loop iteration failed: %s", exc)

    async def ws_health_loop(self) -> None:
        """Background task: check WS health every 30s."""
        engine = self._engine
        while engine._running:
            try:
                await asyncio.sleep(30)
                if engine._hl_ws_client is None:
                    continue
                healthy = getattr(engine._hl_ws_client, 'is_healthy', True)
                now = time.time()
                if not healthy:
                    if not engine._ws_health_warned:
                        engine._ws_disconnect_start = now
                        logger.warning("WS health check FAILED — no data for >90s")
                        engine._ws_health_warned = True
                    if engine._notifier is not None and engine._ws_disconnect_start is not None:
                        duration = now - engine._ws_disconnect_start
                        engine._notify(
                            lambda d=duration: engine._notifier.ws_disconnect(
                                exchange="Hyperliquid", duration_sec=d
                            )
                        )
                else:
                    if engine._ws_health_warned:
                        duration = (
                            now - engine._ws_disconnect_start
                            if engine._ws_disconnect_start is not None
                            else 0.0
                        )
                        logger.info("WS health RESTORED after %.0fs", duration)
                        engine._ws_health_warned = False
                        engine._ws_disconnect_start = None
                        if engine._notifier is not None:
                            engine._notify(
                                lambda: engine._notifier.send_alert("WS reconnected")
                            )
                    engine._last_ws_healthy_time = now
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("WS health check error")

    async def periodic_summary_loop(self) -> None:
        """Log a structured summary every 15 min: exposure, DD, PnL, active strategies."""
        engine = self._engine
        await asyncio.sleep(60)  # initial settle-in delay
        while engine._running:
            try:
                portfolio = engine._portfolio
                positions = await portfolio.positions
                capital = await portfolio.current_capital
                daily_pnl = await portfolio.daily_pnl
                max_dd = portfolio.sync_max_drawdown_pct()
                active_strategies = []
                for s in engine._strategies:
                    active_strategies.append(getattr(s, "name", "?"))
                    subs = getattr(s, "_strategies", None)
                    if isinstance(subs, dict):
                        active_strategies.extend(
                            getattr(sub, "name", n) for n, sub in subs.items()
                        )

                long_exposure = 0.0
                short_exposure = 0.0
                for pos in positions.values():
                    mid = getattr(pos, "current_price", 0) or 1
                    notional = getattr(pos, "size", 0) * mid
                    if getattr(pos, "side", "") == "long":
                        long_exposure += notional
                    else:
                        short_exposure += notional
                cb_tripped = engine._risk.is_circuit_breaker_tripped()
                daily_trades = await portfolio.daily_trades

                logger.info(
                    "=== PERIODIC SUMMARY === "
                    "capital=%.2f | daily_pnl=%.2f | max_dd=%.2f%% | "
                    "long_exposure=%.2f | short_exposure=%.2f | "
                    "open_pos=%d | daily_trades=%d | cb=%s | "
                    "strategies=%s",
                    capital, daily_pnl, max_dd,
                    long_exposure, short_exposure,
                    len(positions), daily_trades,
                    "TRIPPED" if cb_tripped else "ok",
                    ",".join(active_strategies),
                )

                if engine._notifier is not None:
                    pnl_emoji = "+" if daily_pnl >= 0 else ""
                    summary_msg = (
                        f"Summary: capital={capital:.0f} | daily_pnl={pnl_emoji}{daily_pnl:.2f} | "
                        f"DD={max_dd:.1f}% | positions={len(positions)} | "
                        f"cb={'TRIPPED' if cb_tripped else 'ok'}"
                    )
                    engine._notify(
                        lambda m=summary_msg: engine._notifier.send(m, level="info")
                    )

            except Exception:
                logger.exception("Periodic summary error")
            await asyncio.sleep(900)

    async def poll_funding_loop(self) -> None:
        """Background task: poll CEX funding/OI and HL predictedFundings."""
        engine = self._engine
        while engine._running:
            try:
                results = await engine._funding_aggregator.poll(engine._symbols)
                for sym, data in results.items():
                    if data:
                        engine._latest_agg_funding[sym] = data
                        if (
                            getattr(engine, "_feed_silence_enabled", False)
                            and not data.stale
                        ):
                            engine._feed_silence.beat(
                                "funding_cex",
                                int(getattr(data, "timestamp_ms", 0) or time.time() * 1000),
                            )
                        if data.stale:
                            logger.warning(
                                "CEX funding %s is STALE (age=%.0fs, exchanges=%s)",
                                sym,
                                data.age_sec,
                                ",".join(data.by_exchange.keys()),
                            )
                stale_count = sum(1 for d in results.values() if d.stale)
                logger.debug(
                    "FundingAggregator updated for %d symbols (exchanges=%s, stale=%d)",
                    len(results),
                    ", ".join(
                        sorted(
                            set(
                                ex
                                for d in results.values()
                                for ex in d.by_exchange.keys()
                            )
                        )
                    ) if results else "none",
                    stale_count,
                )
                now = time.time()
                if now - engine._last_hl_predicted_poll >= engine._hl_predicted_poll_sec:
                    hl_results = await engine._hl_predicted.poll(engine._symbols)
                    engine._last_hl_predicted_poll = now
                    for sym, snap in hl_results.items():
                        hl = snap.predicted_funding_hl
                        hl8 = snap.predicted_funding_hl_8h
                        level = logger.warning if snap.stale else logger.debug
                        level(
                            "HL predictedFundings %s%s: hl=%s hl_8h=%s venues=%s",
                            sym,
                            " STALE" if snap.stale else "",
                            f"{hl:.8f}" if hl is not None else "N/A",
                            f"{hl8:.8f}" if hl8 is not None else "N/A",
                            ",".join(sorted(snap.venues.keys())),
                        )
                        if (
                            getattr(engine, "_feed_silence_enabled", False)
                            and not snap.stale
                        ):
                            engine._feed_silence.beat(
                                "funding_hl",
                                int(getattr(snap, "timestamp_ms", 0) or time.time() * 1000),
                            )
                engine._persist_funding_oi_snapshot()
                engine._refresh_market_data_health()
                await engine._check_market_data_alerts()
            except Exception as exc:  # noqa: BLE001
                logger.warning("FundingAggregator poll failed: %s", exc)
            try:
                await asyncio.wait_for(
                    engine._shutdown_event.wait()
                    if engine._shutdown_event
                    else asyncio.sleep(engine._funding_poll_sec),
                    timeout=engine._funding_poll_sec,
                )
            except asyncio.TimeoutError:
                pass
