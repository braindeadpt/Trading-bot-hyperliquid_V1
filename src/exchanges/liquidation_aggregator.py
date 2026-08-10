"""Multi-venue real liquidation aggregator (WS-first).

Mirrors :class:`FundingOIAggregator` in spirit: multiple public venues → one
normalized event stream on DataBus topic ``liquidation:{symbol}``.

Architecture
------------
* **Hyperliquid** — *not available market-wide on the public WS* (confirmed
  2026-08-09: public ``trades`` payloads are ``{coin,side,px,sz,tid,time,hash,users}``
  with no ``liquidation`` field; official docs put ``liquidation`` on
  *per-user* ``WsFill`` / ``userEvents`` only). GoldRush documents an
  equivalent ``liquidationFills`` channel as **GoldRush-native** with no
  ``wss://api.hyperliquid.xyz/ws`` counterpart. We still defensively inspect
  public trades in case HL adds the field later, but do **not** enable a
  ``liquidation_hl`` silence contract (it would false-alarm forever).
* **OKX** — public WS ``liquidation-orders`` (``instType=SWAP``) + REST
  ``/api/v5/public/liquidation-orders`` for calibration / backfill helpers.
* **Bybit** — public WS ``allLiquidation.{SYMBOL}`` on
  ``wss://stream.bybit.com/v5/public/linear`` (v5; replaces deprecated
  ``liquidation.{symbol}``).
* **Coinalyze** — REST ``/liquidation-history`` at **low frequency** only.
  Coinalyze already aggregates Binance/OKX/Bybit (and more). We use it as a
  **coverage cross-check**, never summed into strategy notional (would
  double-count). See request budget below.

Aggregation semantics
---------------------
* Each DataBus event keeps ``source ∈ {hl,okx,bybit,binance}``.
* The engine's 5m window **sums notional across real venues** →
  "cross-venue market liquidation pressure" (not a single-venue figure).
* Coinalyze is **verify-only**: compared to our okx+bybit coverage; never
  published on ``liquidation:{symbol}``.
* Proxy synthesis remains a separate engine path gated by
  ``market_data.liquidation_source``.

Coinalyze request budget (free tier ≈ 40 req/min)
-------------------------------------------------
FundingOIAggregator already consumes ~3 REST calls × N symbols per
``funding_poll_sec`` (default 30s). With N=4 that is ~12 req / 30s ≈ 24/min
when Coinalyze is enabled — leaving ~16/min headroom.

This module defaults to **1 call per symbol every ``coinalyze_poll_sec``
(900s = 15 min)** → 4 req / 15 min ≈ **0.27 req/min**. Documented ceiling:
keep ``coinalyze_poll_sec ≥ 600`` so liq checks stay under ~0.4 req/min.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from src.exchanges.hyperliquid_ws import DataBus
from src.exchanges.liquidation_event import LiquidationEvent

logger = logging.getLogger(__name__)

OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"
OKX_REST = "https://www.okx.com"
BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"
COINALYZE_BASE = "https://api.coinalyze.net/v1"

INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 30.0

# Base asset → venue instrument ids.
# HYPE: present on OKX (HYPE-USDT-SWAP) and Bybit (HYPEUSDT) as of 2026-08;
# absent instruments are logged once and skipped (never silently dropped mid-stream).
SYMBOL_MAP: Dict[str, Dict[str, str]] = {
    "BTC": {
        "okx": "BTC-USDT-SWAP",
        "okx_uly": "BTC-USDT",
        "bybit": "BTCUSDT",
        "coinalyze": "BTCUSDT_PERP.A",
    },
    "ETH": {
        "okx": "ETH-USDT-SWAP",
        "okx_uly": "ETH-USDT",
        "bybit": "ETHUSDT",
        "coinalyze": "ETHUSDT_PERP.A",
    },
    "SOL": {
        "okx": "SOL-USDT-SWAP",
        "okx_uly": "SOL-USDT",
        "bybit": "SOLUSDT",
        "coinalyze": "SOLUSDT_PERP.A",
    },
    "HYPE": {
        "okx": "HYPE-USDT-SWAP",
        "okx_uly": "HYPE-USDT",
        "bybit": "HYPEUSDT",
        "coinalyze": "HYPEUSDT_PERP.A",
    },
}


@dataclass
class CoinalyzeCoverageSnapshot:
    """Per-symbol Coinalyze 5m bucket totals (verify-only, not strategy input)."""

    symbol: str
    timestamp_ms: int
    long_usd: float
    short_usd: float
    # Coinalyze history values empirically look like USD *millions*;
    # we store both raw and scaled for operators.
    scale: str = "usd_millions_assumed"
    raw_long: float = 0.0
    raw_short: float = 0.0


@dataclass
class VenueAvailability:
    venue: str
    status: str  # live | unsupported | missing_symbols | disabled
    detail: str
    symbols: List[str] = field(default_factory=list)


def _okx_side(detail: Dict[str, Any]) -> Optional[str]:
    """Map OKX liquidation detail → long|short (position that was liquidated)."""
    pos = str(detail.get("posSide") or "").lower()
    if pos in ("long", "short"):
        return pos
    # net mode: side of the closing order — sell closes long, buy closes short
    side = str(detail.get("side") or "").lower()
    if side == "sell":
        return "long"
    if side == "buy":
        return "short"
    return None


def _bybit_side(side: str) -> Optional[str]:
    """Bybit allLiquidation: S=Buy → long liquidated; S=Sell → short liquidated."""
    s = (side or "").strip().lower()
    if s == "buy":
        return "long"
    if s == "sell":
        return "short"
    return None


def _hl_side_from_trade(trade: Dict[str, Any]) -> Optional[str]:
    """Infer liquidated side from HL fill-like trade if ``dir`` / side present."""
    d = str(trade.get("dir") or "").strip().lower()
    if d == "close long":
        return "long"
    if d == "close short":
        return "short"
    # Public trades only expose B/A — sell into book ≈ long liq, buy ≈ short liq
    side = str(trade.get("side") or "").upper()
    if side in ("A", "S", "SELL"):
        return "long"
    if side in ("B", "BUY"):
        return "short"
    return None


def parse_hl_trade_liquidation(
    trade: Dict[str, Any],
    *,
    allowed_symbols: Optional[Set[str]] = None,
) -> Optional[LiquidationEvent]:
    """Build an event if a (possibly future) public trade carries liquidation.

    Returns None for ordinary prints. Today the official public schema has no
    ``liquidation`` key — this exists so a venue upgrade wires itself.
    """
    liq = trade.get("liquidation")
    if not isinstance(liq, dict):
        return None
    symbol = str(trade.get("coin") or trade.get("asset") or "").upper()
    if not symbol:
        return None
    if allowed_symbols is not None and symbol not in allowed_symbols:
        return None
    px = float(trade.get("px") or liq.get("markPx") or 0.0)
    sz = abs(float(trade.get("sz") or 0.0))
    if px <= 0 or sz <= 0:
        return None
    side = _hl_side_from_trade(trade)
    if side is None:
        return None
    ts = int(trade.get("time") or time.time() * 1000)
    return LiquidationEvent(
        symbol=symbol,
        timestamp_ms=ts,
        notional_usd=px * sz,
        side=side,
        source="hl",
    )


class MultiVenueLiquidationAggregator:
    """WS-first OKX + Bybit liquidations → DataBus; Coinalyze verify-only."""

    def __init__(
        self,
        bus: DataBus,
        symbols: Optional[List[str]] = None,
        *,
        enable_okx: bool = True,
        enable_bybit: bool = True,
        enable_hl_hook: bool = True,
        enable_coinalyze_check: bool = True,
        coinalyze_api_key: Optional[str] = None,
        coinalyze_poll_sec: float = 900.0,
        on_event: Optional[Any] = None,
        on_coinalyze_check: Optional[Any] = None,
    ) -> None:
        self._bus = bus
        self._symbols = [s.upper() for s in (symbols or ["BTC", "ETH", "SOL", "HYPE"])]
        self._enable_okx = enable_okx
        self._enable_bybit = enable_bybit
        self._enable_hl_hook = enable_hl_hook
        self._enable_coinalyze = enable_coinalyze_check
        self._coinalyze_key = (
            coinalyze_api_key
            or os.environ.get("COINALYZE_API_KEY")
            or ""
        ).strip()
        self._coinalyze_poll_sec = max(600.0, float(coinalyze_poll_sec))
        self._on_event = on_event  # optional sync callback(LiquidationEvent)
        self._on_coinalyze_check = on_coinalyze_check  # optional sync callback(ts_ms)

        self._shutdown = False
        self._tasks: List[asyncio.Task] = []
        self._session: Optional[aiohttp.ClientSession] = None

        self._okx_inst_to_base: Dict[str, str] = {}
        self._bybit_sym_to_base: Dict[str, str] = {}
        self._missing_logged: Set[str] = set()
        self._availability: List[VenueAvailability] = []
        self._last_coinalyze: Dict[str, CoinalyzeCoverageSnapshot] = {}
        self._event_counts: Dict[str, int] = {"okx": 0, "bybit": 0, "hl": 0}
        self._allowed = set(self._symbols)

        self._resolve_symbol_maps()

    # ── lifecycle ──────────────────────────────────────────────────────

    def _resolve_symbol_maps(self) -> None:
        okx_syms: List[str] = []
        bybit_syms: List[str] = []
        for base in self._symbols:
            m = SYMBOL_MAP.get(base)
            if m is None:
                key = f"map:{base}"
                if key not in self._missing_logged:
                    self._missing_logged.add(key)
                    logger.warning(
                        "LiquidationAggregator: no SYMBOL_MAP entry for %s — skipped",
                        base,
                    )
                continue
            if "okx" in m:
                self._okx_inst_to_base[m["okx"]] = base
                okx_syms.append(base)
            else:
                logger.warning("LiquidationAggregator: %s missing OKX instrument", base)
            if "bybit" in m:
                self._bybit_sym_to_base[m["bybit"]] = base
                bybit_syms.append(base)
            else:
                logger.warning(
                    "LiquidationAggregator: %s missing Bybit instrument", base
                )

        self._availability = [
            VenueAvailability(
                venue="hyperliquid",
                status="unsupported",
                detail=(
                    "No market-wide liquidation channel on public WS "
                    "(trades have no liquidation field; user fills only). "
                    "Defensive trade-hook enabled; silence monitor not contracted."
                ),
                symbols=list(self._symbols),
            ),
            VenueAvailability(
                venue="okx",
                status="live" if self._enable_okx and okx_syms else "disabled",
                detail="WS channel liquidation-orders (instType=SWAP)",
                symbols=okx_syms,
            ),
            VenueAvailability(
                venue="bybit",
                status="live" if self._enable_bybit and bybit_syms else "disabled",
                detail="WS topic allLiquidation.{SYMBOL} (linear v5)",
                symbols=bybit_syms,
            ),
            VenueAvailability(
                venue="coinalyze",
                status=(
                    "live"
                    if self._enable_coinalyze and self._coinalyze_key
                    else "disabled"
                ),
                detail=(
                    f"verify-only REST /liquidation-history every "
                    f"{self._coinalyze_poll_sec:.0f}s — NEVER summed"
                ),
                symbols=list(self._symbols),
            ),
            VenueAvailability(
                venue="binance",
                status="blocked_on_network",
                detail=(
                    "fstream @forceOrder remains in BinanceFuturesFeed; "
                    "this network returns 0 msgs — not started here"
                ),
                symbols=list(self._symbols),
            ),
        ]

    async def start(self) -> None:
        if self._tasks:
            return
        self._shutdown = False
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"Accept": "application/json"},
        )
        if self._enable_okx and self._okx_inst_to_base:
            self._tasks.append(asyncio.create_task(self._okx_ws_loop(), name="liq_okx"))
            self._tasks.append(
                asyncio.create_task(self._okx_rest_bootstrap(), name="liq_okx_boot")
            )
        if self._enable_bybit and self._bybit_sym_to_base:
            self._tasks.append(
                asyncio.create_task(self._bybit_ws_loop(), name="liq_bybit")
            )
        if self._enable_coinalyze and self._coinalyze_key:
            self._tasks.append(
                asyncio.create_task(self._coinalyze_loop(), name="liq_coinalyze")
            )
        elif self._enable_coinalyze and not self._coinalyze_key:
            logger.warning(
                "LiquidationAggregator: Coinalyze check enabled but no API key "
                "(COINALYZE_API_KEY / market_data.coinalyze_api_key)"
            )

        for av in self._availability:
            logger.info(
                "LiquidationAggregator venue=%s status=%s symbols=%s — %s",
                av.venue,
                av.status,
                av.symbols,
                av.detail,
            )
        logger.info(
            "LiquidationAggregator started (okx=%s bybit=%s coinalyze_check=%s "
            "poll=%ss symbols=%s)",
            self._enable_okx,
            self._enable_bybit,
            bool(self._coinalyze_key and self._enable_coinalyze),
            int(self._coinalyze_poll_sec),
            self._symbols,
        )

    async def stop(self) -> None:
        self._shutdown = True
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        if self._session is not None:
            await self._session.close()
            self._session = None
        logger.info(
            "LiquidationAggregator stopped (counts okx=%d bybit=%d hl=%d)",
            self._event_counts["okx"],
            self._event_counts["bybit"],
            self._event_counts["hl"],
        )

    def availability(self) -> List[Dict[str, Any]]:
        return [
            {
                "venue": a.venue,
                "status": a.status,
                "detail": a.detail,
                "symbols": list(a.symbols),
            }
            for a in self._availability
        ]

    def stats(self) -> Dict[str, Any]:
        return {
            "event_counts": dict(self._event_counts),
            "coinalyze_last": {
                k: {
                    "timestamp_ms": v.timestamp_ms,
                    "long_usd": v.long_usd,
                    "short_usd": v.short_usd,
                    "scale": v.scale,
                }
                for k, v in self._last_coinalyze.items()
            },
            "availability": self.availability(),
            "coinalyze_poll_sec": self._coinalyze_poll_sec,
            "coinalyze_budget_note": (
                f"~{len(self._symbols)} req / {self._coinalyze_poll_sec:.0f}s "
                f"(funding aggregator separately uses ~3×N / funding_poll_sec)"
            ),
        }

    # ── publish ────────────────────────────────────────────────────────

    def publish_event(self, event: LiquidationEvent) -> None:
        """Publish a normalized event (also used by HL WS hook)."""
        if event.symbol not in self._allowed:
            return
        if event.notional_usd <= 0:
            return
        src = event.source.lower()
        if src in self._event_counts:
            self._event_counts[src] += 1
        self._bus.publish(f"liquidation:{event.symbol}", event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001
                logger.exception("liquidation on_event callback failed")

    def on_hl_raw_trade(self, trade_dict: Dict[str, Any]) -> None:
        """Defensive HL hook — no-op until public trades carry liquidation."""
        if not self._enable_hl_hook:
            return
        ev = parse_hl_trade_liquidation(trade_dict, allowed_symbols=self._allowed)
        if ev is not None:
            self.publish_event(ev)

    # ── OKX ────────────────────────────────────────────────────────────

    async def _okx_rest_bootstrap(self) -> None:
        """One-shot REST fill so quiet majors aren't empty until the next cascade."""
        if self._session is None:
            return
        try:
            await asyncio.sleep(1.0)
            total = 0
            for inst_id, base in list(self._okx_inst_to_base.items()):
                m = SYMBOL_MAP.get(base) or {}
                uly = m.get("okx_uly")
                if not uly:
                    logger.debug(
                        "OKX bootstrap skip %s (inst=%s): no okx_uly", base, inst_id
                    )
                    continue
                try:
                    evs = await fetch_okx_recent_liquidations(self._session, uly=uly)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("OKX REST bootstrap %s failed: %s", base, exc)
                    continue
                # REST history lags WS; keep a multi-hour window so majors
                # still seed the DB. Engine 5m window prunes older prints.
                cutoff = int(time.time() * 1000) - 6 * 3600_000
                recent = [e for e in evs if e.timestamp_ms >= cutoff]
                for ev in recent:
                    self.publish_event(ev)
                    total += 1
                logger.info(
                    "OKX REST bootstrap %s: published %d/%d recent (15m window)",
                    base,
                    len(recent),
                    len(evs),
                )
            logger.info("OKX REST bootstrap done — %d events published", total)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("OKX REST bootstrap error: %s", exc)

    async def _okx_ws_loop(self) -> None:
        backoff = INITIAL_BACKOFF
        while not self._shutdown:
            try:
                async with websockets.connect(
                    OKX_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    sub = {
                        "op": "subscribe",
                        "args": [{"channel": "liquidation-orders", "instType": "SWAP"}],
                    }
                    await ws.send(json.dumps(sub))
                    backoff = INITIAL_BACKOFF
                    logger.info("OKX liquidation WS subscribed (SWAP)")
                    async for raw in ws:
                        if self._shutdown:
                            break
                        self._on_okx_message(raw)
            except ConnectionClosed as exc:
                logger.warning("OKX liquidation WS closed: %s", exc)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("OKX liquidation WS error: %s", exc)
            if self._shutdown:
                return
            await asyncio.sleep(min(backoff, MAX_BACKOFF))
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _on_okx_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if payload.get("event") in ("subscribe", "error"):
            if payload.get("event") == "error":
                logger.warning("OKX liquidation WS error event: %s", payload)
            return
        if payload.get("arg", {}).get("channel") != "liquidation-orders":
            return
        for block in payload.get("data") or []:
            if not isinstance(block, dict):
                continue
            inst = str(block.get("instId") or "")
            base = self._okx_inst_to_base.get(inst)
            if base is None:
                continue
            details = block.get("details") or []
            if isinstance(details, dict):
                details = [details]
            for det in details:
                if not isinstance(det, dict):
                    continue
                side = _okx_side(det)
                if side is None:
                    continue
                try:
                    px = float(det.get("bkPx") or 0.0)
                    sz = abs(float(det.get("sz") or 0.0))
                    ts = int(det.get("ts") or det.get("time") or time.time() * 1000)
                except (TypeError, ValueError):
                    continue
                if px <= 0 or sz <= 0:
                    continue
                self.publish_event(
                    LiquidationEvent(
                        symbol=base,
                        timestamp_ms=ts,
                        notional_usd=px * sz,
                        side=side,
                        source="okx",
                    )
                )

    # ── Bybit ──────────────────────────────────────────────────────────

    async def _bybit_ws_loop(self) -> None:
        backoff = INITIAL_BACKOFF
        args = [f"allLiquidation.{sym}" for sym in sorted(self._bybit_sym_to_base)]
        while not self._shutdown:
            try:
                async with websockets.connect(
                    BYBIT_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    backoff = INITIAL_BACKOFF
                    logger.info("Bybit liquidation WS subscribed: %s", args)
                    async for raw in ws:
                        if self._shutdown:
                            break
                        self._on_bybit_message(raw)
            except ConnectionClosed as exc:
                logger.warning("Bybit liquidation WS closed: %s", exc)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bybit liquidation WS error: %s", exc)
            if self._shutdown:
                return
            await asyncio.sleep(min(backoff, MAX_BACKOFF))
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _on_bybit_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if payload.get("op") == "subscribe":
            if not payload.get("success", True):
                logger.warning("Bybit liquidation subscribe failed: %s", payload)
            return
        topic = str(payload.get("topic") or "")
        if not topic.startswith("allLiquidation."):
            return
        data = payload.get("data")
        rows: List[Dict[str, Any]]
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            rows = [data]
        else:
            return
        for row in rows:
            sym = str(row.get("s") or topic.split(".", 1)[-1])
            base = self._bybit_sym_to_base.get(sym)
            if base is None:
                continue
            side = _bybit_side(str(row.get("S") or ""))
            if side is None:
                continue
            try:
                # Bybit allLiquidation: p = price, v = size (base coin)
                px = float(row.get("p") or 0.0)
                qty = abs(float(row.get("v") or row.get("q") or 0.0))
                ts = int(row.get("T") or time.time() * 1000)
            except (TypeError, ValueError):
                continue
            if px <= 0 or qty <= 0:
                continue
            self.publish_event(
                LiquidationEvent(
                    symbol=base,
                    timestamp_ms=ts,
                    notional_usd=px * qty,
                    side=side,
                    source="bybit",
                )
            )

    # ── Coinalyze verify-only ──────────────────────────────────────────

    async def _coinalyze_loop(self) -> None:
        # Stagger first poll so we don't collide with funding aggregator's burst
        await asyncio.sleep(45.0)
        while not self._shutdown:
            try:
                await self._poll_coinalyze_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("Coinalyze liquidation check failed: %s", exc)
            try:
                await asyncio.sleep(self._coinalyze_poll_sec)
            except asyncio.CancelledError:
                return

    async def _poll_coinalyze_once(self) -> None:
        if self._session is None or not self._coinalyze_key:
            return
        now = int(time.time())
        # One request per symbol (Coinalyze bills per symbol in `symbols` param
        # when batched; we still issue one-at-a-time to stay predictable).
        for base in self._symbols:
            m = SYMBOL_MAP.get(base) or {}
            cz_sym = m.get("coinalyze")
            if not cz_sym:
                key = f"cz:{base}"
                if key not in self._missing_logged:
                    self._missing_logged.add(key)
                    logger.warning(
                        "LiquidationAggregator: %s has no Coinalyze symbol — skipped",
                        base,
                    )
                continue
            params = {
                "symbols": cz_sym,
                "interval": "5min",
                "from": now - 3600,
                "to": now,
            }
            try:
                async with self._session.get(
                    f"{COINALYZE_BASE}/liquidation-history",
                    params=params,
                    headers={"api-key": self._coinalyze_key},
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Coinalyze liquidation-history HTTP %s for %s",
                            resp.status,
                            base,
                        )
                        continue
                    data = await resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Coinalyze liquidation-history %s: %s", base, exc)
                continue

            history: List[Dict[str, Any]] = []
            if isinstance(data, list) and data:
                history = list(data[0].get("history") or [])
            elif isinstance(data, dict):
                history = list(data.get("history") or [])
            if not history:
                continue
            last = history[-1]
            raw_l = float(last.get("l") or 0.0)
            raw_s = float(last.get("s") or 0.0)
            # Empirical: Coinalyze liq history prints look like USD millions.
            scale = 1_000_000.0
            snap = CoinalyzeCoverageSnapshot(
                symbol=base,
                timestamp_ms=int(last.get("t") or now) * 1000,
                long_usd=raw_l * scale,
                short_usd=raw_s * scale,
                raw_long=raw_l,
                raw_short=raw_s,
            )
            self._last_coinalyze[base] = snap
            if self._on_coinalyze_check is not None:
                try:
                    self._on_coinalyze_check(snap.timestamp_ms)
                except Exception:  # noqa: BLE001
                    logger.exception("coinalyze check callback failed")
            logger.info(
                "Coinalyze liq check %s last5m raw_l=%.4f raw_s=%.4f "
                "(assumed USD_m → $%.0f long / $%.0f short) — verify-only, not published",
                base,
                raw_l,
                raw_s,
                snap.long_usd,
                snap.short_usd,
            )


# ── REST helpers (calibration / offline) ───────────────────────────────


async def fetch_okx_recent_liquidations(
    session: aiohttp.ClientSession,
    *,
    uly: str,
    state: str = "filled",
) -> List[LiquidationEvent]:
    """Pull recent OKX SWAP liquidations for one underlying (e.g. BTC-USDT)."""
    base = None
    for b, m in SYMBOL_MAP.items():
        if m.get("okx_uly") == uly or m.get("okx") == uly:
            base = b
            break
    if base is None:
        # allow raw base like BTC
        base = uly.split("-")[0].upper()
    url = f"{OKX_REST}/api/v5/public/liquidation-orders"
    params = {"instType": "SWAP", "uly": uly if "-" in uly else f"{uly}-USDT", "state": state}
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    out: List[LiquidationEvent] = []
    for block in payload.get("data") or []:
        for det in block.get("details") or []:
            side = _okx_side(det)
            if side is None:
                continue
            try:
                px = float(det.get("bkPx") or 0.0)
                sz = abs(float(det.get("sz") or 0.0))
                ts = int(det.get("ts") or det.get("time") or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0 or sz <= 0 or ts <= 0:
                continue
            out.append(
                LiquidationEvent(
                    symbol=base,
                    timestamp_ms=ts,
                    notional_usd=px * sz,
                    side=side,
                    source="okx",
                )
            )
    return out
