"""Live virtual swing book for TopTrader aggregate bias (research only).

One open virtual position per symbol. Hybrid exits: bias flip, max hold, or SL/TP.
Never touches OMS / risk / execution.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.exchanges.top_trader_tracker import TopTraderSymbolSnapshot
from src.research.top_trader_store import (
    EXIT_BIAS_FLIP,
    EXIT_SL,
    EXIT_TIMEOUT,
    EXIT_TP,
    TopTraderStore,
)
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)


@dataclass
class VirtualPosition:
    symbol: str
    side: str
    entry_price: float
    entry_ts_ms: int
    stop_loss_pct: float
    take_profit_pct: float
    size_pct: float
    entry_bias: float
    row_id: Optional[int] = None
    last_mark: float = 0.0
    last_bias: float = 0.0


@dataclass
class TopTraderVirtualBook:
    """In-process mark-to-market book driven by bias snapshots + mid prices."""

    bias_threshold: float = 0.55
    min_wallets: int = 3
    min_notional_usd: float = 50_000.0
    max_hold_ms: int = 120 * 3_600_000
    stop_loss_pct: float = 0.04
    take_profit_pct: float = 0.10
    size_pct: float = 0.01
    signal_throttle_ms: int = 300_000
    store: Optional[TopTraderStore] = None

    def __post_init__(self) -> None:
        self._open: Dict[str, VirtualPosition] = {}
        self._last_entry_ms: Dict[str, int] = {}
        self._store = self.store or TopTraderStore()
        self._closed_cache: List[Dict[str, Any]] = []

    def open_positions(self) -> List[Dict[str, Any]]:
        now = int(time.time() * 1000)
        out: List[Dict[str, Any]] = []
        for pos in self._open.values():
            mark = pos.last_mark if pos.last_mark > 0 else pos.entry_price
            pnl = _pnl_pct(pos.side, pos.entry_price, mark)
            out.append(
                {
                    "symbol": pos.symbol,
                    "side": pos.side,
                    "entry_price": pos.entry_price,
                    "entry_ts_ms": pos.entry_ts_ms,
                    "mark_price": mark,
                    "unrealized_pnl_pct": pnl,
                    "age_ms": max(0, now - pos.entry_ts_ms),
                    "entry_bias": pos.entry_bias,
                    "last_bias": pos.last_bias,
                    "stop_loss_pct": pos.stop_loss_pct,
                    "take_profit_pct": pos.take_profit_pct,
                    "row_id": pos.row_id,
                }
            )
        return out

    def recent_closed(self, *, limit: int = 30) -> List[Dict[str, Any]]:
        if self._closed_cache:
            return self._closed_cache[:limit]
        return self._store.list_closed_trades(limit=limit)

    def on_snapshots(
        self,
        snaps: Dict[str, TopTraderSymbolSnapshot],
        *,
        prices: Optional[Dict[str, float]] = None,
    ) -> List[Dict[str, Any]]:
        """Apply bias update: maybe open, maybe flip-close. Returns closed events."""
        prices = prices or {}
        closed: List[Dict[str, Any]] = []
        now_ms = int(time.time() * 1000)

        for sym, snap in snaps.items():
            symbol = sym.upper()
            bias = float(snap.net_bias)
            px = safe_float(prices.get(symbol))
            pos = self._open.get(symbol)
            if pos is not None:
                pos.last_bias = bias
                if px > 0:
                    pos.last_mark = px
                event = self._try_exit(pos, bias=bias, price=pos.last_mark or px, now_ms=now_ms)
                if event is not None:
                    closed.append(event)
                continue

            # Entry gates
            total = snap.long_notional_usd + snap.short_notional_usd
            if total < self.min_notional_usd:
                continue
            if snap.n_wallets < self.min_wallets:
                continue
            if abs(bias) < self.bias_threshold:
                continue
            if px <= 0:
                continue
            last = self._last_entry_ms.get(symbol, 0)
            if now_ms - last < self.signal_throttle_ms:
                continue

            side = "long" if bias > 0 else "short"
            pos = VirtualPosition(
                symbol=symbol,
                side=side,
                entry_price=px,
                entry_ts_ms=now_ms,
                stop_loss_pct=self.stop_loss_pct,
                take_profit_pct=self.take_profit_pct,
                size_pct=self.size_pct,
                entry_bias=bias,
                last_mark=px,
                last_bias=bias,
            )
            row_id = self._store.insert_virtual_trade_open(
                {
                    "symbol": symbol,
                    "side": side,
                    "entry_price": px,
                    "entry_ts_ms": now_ms,
                    "stop_loss_pct": self.stop_loss_pct,
                    "take_profit_pct": self.take_profit_pct,
                    "size_pct": self.size_pct,
                    "entry_bias": bias,
                }
            )
            pos.row_id = row_id
            self._open[symbol] = pos
            self._last_entry_ms[symbol] = now_ms
            logger.info(
                "TopTraderVirtualBook OPEN %s %s @ %.4f bias=%.2f",
                side,
                symbol,
                px,
                bias,
            )

        # Also check open symbols missing from this poll (timeout / SL via price path)
        for symbol, pos in list(self._open.items()):
            if symbol in {s.upper() for s in snaps}:
                continue
            event = self._try_exit(
                pos,
                bias=pos.last_bias,
                price=pos.last_mark,
                now_ms=now_ms,
                allow_flip=False,
            )
            if event is not None:
                closed.append(event)
        return closed

    def on_price(self, symbol: str, price: float, timestamp_ms: int) -> Optional[Dict[str, Any]]:
        """Mark open position and check SL/TP / max-hold."""
        symbol = symbol.upper()
        pos = self._open.get(symbol)
        if pos is None:
            return None
        px = safe_float(price)
        if px <= 0:
            return None
        pos.last_mark = px
        return self._try_exit(
            pos,
            bias=pos.last_bias,
            price=px,
            now_ms=int(timestamp_ms),
            allow_flip=True,
        )

    def _try_exit(
        self,
        pos: VirtualPosition,
        *,
        bias: float,
        price: float,
        now_ms: int,
        allow_flip: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if price <= 0:
            # Still allow timeout without mark
            if now_ms - pos.entry_ts_ms >= self.max_hold_ms and pos.last_mark > 0:
                return self._close(pos, pos.last_mark, now_ms, EXIT_TIMEOUT, bias)
            return None

        # SL / TP
        if pos.side == "long":
            sl = pos.entry_price * (1.0 - pos.stop_loss_pct)
            tp = pos.entry_price * (1.0 + pos.take_profit_pct)
            if price <= sl:
                return self._close(pos, price, now_ms, EXIT_SL, bias)
            if price >= tp:
                return self._close(pos, price, now_ms, EXIT_TP, bias)
        else:
            sl = pos.entry_price * (1.0 + pos.stop_loss_pct)
            tp = pos.entry_price * (1.0 - pos.take_profit_pct)
            if price >= sl:
                return self._close(pos, price, now_ms, EXIT_SL, bias)
            if price <= tp:
                return self._close(pos, price, now_ms, EXIT_TP, bias)

        # Bias flip
        if allow_flip:
            if pos.side == "long" and bias <= -self.bias_threshold:
                return self._close(pos, price, now_ms, EXIT_BIAS_FLIP, bias)
            if pos.side == "short" and bias >= self.bias_threshold:
                return self._close(pos, price, now_ms, EXIT_BIAS_FLIP, bias)

        # Max hold
        if now_ms - pos.entry_ts_ms >= self.max_hold_ms:
            return self._close(pos, price, now_ms, EXIT_TIMEOUT, bias)
        return None

    def _close(
        self,
        pos: VirtualPosition,
        exit_price: float,
        exit_ts_ms: int,
        reason: str,
        exit_bias: float,
    ) -> Dict[str, Any]:
        pnl = _pnl_pct(pos.side, pos.entry_price, exit_price)
        if pos.row_id is not None:
            self._store.close_virtual_trade(
                pos.row_id,
                exit_price=exit_price,
                exit_ts_ms=exit_ts_ms,
                exit_reason=reason,
                exit_bias=exit_bias,
                pnl_pct=pnl,
            )
        event = {
            "symbol": pos.symbol,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "entry_ts_ms": pos.entry_ts_ms,
            "exit_price": exit_price,
            "exit_ts_ms": exit_ts_ms,
            "exit_reason": reason,
            "entry_bias": pos.entry_bias,
            "exit_bias": exit_bias,
            "pnl_pct": pnl,
            "status": "closed",
            "row_id": pos.row_id,
        }
        self._open.pop(pos.symbol, None)
        self._closed_cache.insert(0, event)
        self._closed_cache = self._closed_cache[:100]
        logger.info(
            "TopTraderVirtualBook CLOSE %s %s reason=%s pnl=%.3f%%",
            pos.side,
            pos.symbol,
            reason,
            pnl * 100.0,
        )
        return event


def _pnl_pct(side: str, entry: float, exit_px: float) -> float:
    if entry <= 0:
        return 0.0
    if side == "long":
        return (exit_px - entry) / entry
    return (entry - exit_px) / entry


def build_virtual_book_from_config(config: Any) -> TopTraderVirtualBook:
    """Build book from ``strategy.top_trader_flow`` section."""
    cfg: Dict[str, Any] = {}
    if hasattr(config, "get"):
        cfg = dict(config.get("strategy.top_trader_flow", {}) or {})
    max_hold_hours = float(cfg.get("max_hold_hours", 120.0))
    return TopTraderVirtualBook(
        bias_threshold=float(cfg.get("bias_threshold", 0.55)),
        min_wallets=int(cfg.get("min_wallets_with_position", 3)),
        min_notional_usd=float(cfg.get("min_aggregate_notional_usd", 50_000.0)),
        max_hold_ms=int(max_hold_hours * 3_600_000),
        stop_loss_pct=float(cfg.get("stop_loss_pct", 0.04)),
        take_profit_pct=float(cfg.get("take_profit_pct", 0.10)),
        size_pct=float(cfg.get("size_pct", 0.01)),
        signal_throttle_ms=int(cfg.get("signal_throttle_ms", 300_000)),
    )


_VIRTUAL_BOOK: Optional[TopTraderVirtualBook] = None


def get_virtual_book() -> Optional[TopTraderVirtualBook]:
    return _VIRTUAL_BOOK


def set_virtual_book(book: Optional[TopTraderVirtualBook]) -> None:
    global _VIRTUAL_BOOK
    _VIRTUAL_BOOK = book
