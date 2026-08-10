"""Order routing — market vs limit (maker/post-only) by sub-strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Set, Tuple

from src.strategies.base import Signal
from src.core.regime import regime_strategy_name
from src.utils.helpers import safe_float


@dataclass(frozen=True)
class OrderSpec:
    """Resolved execution parameters for one entry (and default exit fees)."""

    order_type: str  # market | limit_maker
    limit_price: Optional[float]
    entry_fee_pct: float
    exit_fee_pct: float
    entry_slippage_pct: float
    exit_slippage_pct: float
    post_only: bool = False


def _maker_strategies(config: Any) -> Set[str]:
    raw = config.get("execution.maker_orders.strategies", [])
    if not isinstance(raw, list):
        return set()
    return {str(s) for s in raw}


def _maker_cfg_enabled(config: Any) -> bool:
    return bool(config.get("execution.maker_orders.enabled", False))


def _best_limit_price(
    signal: Signal,
    orderbook: Any,
    offset_pct: float,
) -> Optional[float]:
    """Passive limit: join best bid (buy) or best ask (sell)."""
    if orderbook is None:
        return None
    bids = getattr(orderbook, "bids", None) or []
    asks = getattr(orderbook, "asks", None) or []
    if signal.side == "long":
        if not bids:
            return None
        px = safe_float(bids[0].price, 0.0)
        return px * (1.0 - offset_pct) if px > 0 else None
    if not asks:
        return None
    px = safe_float(asks[0].price, 0.0)
    return px * (1.0 + offset_pct) if px > 0 else None


def resolve_order_routing(
    signal: Signal,
    config: Any,
    orderbook: Any = None,
) -> Tuple[Signal, OrderSpec]:
    """Return signal enriched with order metadata and an OrderSpec for fees/slippage."""
    maker_cfg = config.get("execution.maker_orders", {}) or {}
    if isinstance(maker_cfg, dict):
        maker_enabled = bool(maker_cfg.get("enabled", False))
        maker_fee = safe_float(maker_cfg.get("maker_fee_pct", 0.015)) / 100.0
        offset_pct = safe_float(maker_cfg.get("limit_offset_pct", 0.0))
        exit_as_maker = bool(maker_cfg.get("exit_as_maker", False))
    else:
        maker_enabled = _maker_cfg_enabled(config)
        maker_fee = safe_float(config.get("execution.maker_orders.maker_fee_pct", 0.015)) / 100.0
        offset_pct = safe_float(config.get("execution.maker_orders.limit_offset_pct", 0.0))
        exit_as_maker = bool(config.get("execution.maker_orders.exit_as_maker", False))

    taker_fee = safe_float(config.get("risk.taker_fee_pct", 0.045)) / 100.0
    paper_slip = safe_float(config.get("risk.paper_slippage_pct", 0.05)) / 100.0

    sub = regime_strategy_name(signal)
    use_maker = maker_enabled and sub in _maker_strategies(config)

    if not use_maker:
        spec = OrderSpec(
            order_type="market",
            limit_price=None,
            entry_fee_pct=taker_fee,
            exit_fee_pct=taker_fee,
            entry_slippage_pct=paper_slip,
            exit_slippage_pct=paper_slip,
            post_only=False,
        )
        return signal, spec

    limit_px = _best_limit_price(signal, orderbook, offset_pct)
    if limit_px is None or limit_px <= 0:
        spec = OrderSpec(
            order_type="market",
            limit_price=None,
            entry_fee_pct=taker_fee,
            exit_fee_pct=taker_fee,
            entry_slippage_pct=paper_slip,
            exit_slippage_pct=paper_slip,
            post_only=False,
        )
        meta = {
            **(signal.metadata or {}),
            "order_type": "market",
            "order_routing_fallback": "no_book_for_maker",
        }
        return (
            Signal(
                strategy=signal.strategy,
                symbol=signal.symbol,
                side=signal.side,
                confidence=signal.confidence,
                size_pct=signal.size_pct,
                entry_price=signal.entry_price,
                stop_loss_pct=signal.stop_loss_pct,
                take_profit_pct=signal.take_profit_pct,
                reason=signal.reason,
                metadata=meta,
            ),
            spec,
        )

    exit_fee = maker_fee if exit_as_maker else taker_fee
    exit_slip = 0.0 if exit_as_maker else paper_slip

    spec = OrderSpec(
        order_type="limit_maker",
        limit_price=limit_px,
        entry_fee_pct=maker_fee,
        exit_fee_pct=exit_fee,
        entry_slippage_pct=0.0,
        exit_slippage_pct=exit_slip,
        post_only=True,
    )
    meta = {
        **(signal.metadata or {}),
        "order_type": "limit_maker",
        "limit_price": limit_px,
        "post_only": True,
        "sub_strategy": sub,
        "entry_fee_pct": maker_fee,
        "exit_fee_pct": exit_fee,
        "entry_slippage_pct": 0.0,
        "exit_slippage_pct": exit_slip,
    }
    enriched = Signal(
        strategy=signal.strategy,
        symbol=signal.symbol,
        side=signal.side,
        confidence=signal.confidence,
        size_pct=signal.size_pct,
        entry_price=limit_px,
        stop_loss_pct=signal.stop_loss_pct,
        take_profit_pct=signal.take_profit_pct,
        reason=f"{signal.reason}|maker_limit",
        metadata=meta,
    )
    return enriched, spec
