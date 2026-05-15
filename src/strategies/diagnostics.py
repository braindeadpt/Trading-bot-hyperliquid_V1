"""
Strategy diagnostics utilities for Hyperliquid Trading Bot.

Provides a reusable @diagnostic decorator that logs why a strategy
did NOT generate a signal, making it easy to debug silent strategies.
"""

import logging
import time
from functools import wraps
from typing import Optional, Callable

logger = logging.getLogger(__name__)


def diagnostic(strategy_name: str, log_every_n: int = 10):
    """Decorator that logs detailed diagnostics when a strategy returns None.

    Usage:
        @diagnostic("TrendFollow")
        def on_market_event(self, event):
            ...
            return signal or None
    """
    def decorator(func: Callable) -> Callable:
        # Counter per strategy instance (using closure)
        counter = {"n": 0}
        last_log = {"ts": 0.0}

        @wraps(func)
        def wrapper(self, event, *args, **kwargs) -> Optional:
            result = func(self, event, *args, **kwargs)
            counter["n"] += 1

            if result is None:
                # Log diagnostics periodically (not every single call)
                if counter["n"] % log_every_n == 0:
                    now = time.time()
                    # Throttle to max once per 30 seconds per strategy
                    if now - last_log["ts"] >= 30.0:
                        last_log["ts"] = now
                        _log_strategy_state(strategy_name, self, event)
            else:
                # Always log when we DO get a signal
                logger.info(
                    "DIAGNOSTIC %s — SIGNAL GENERATED: side=%s conf=%.2f reason=%s",
                    strategy_name, result.side, result.confidence, result.reason[:80]
                )

            return result
        return wrapper
    return decorator


def _log_strategy_state(strategy_name: str, strategy_instance, event) -> None:
    """Log the internal state of a strategy for debugging."""
    symbol = getattr(event, "symbol", "unknown")
    price = getattr(event, "price", 0.0)

    # Build state dict from common strategy attributes
    state = {"symbol": symbol, "price": price}

    # Common attributes across strategies
    for attr in [
        "ema_fast", "ema_slow", "atr", "rsi", "adx", "vwap",
        "prev_funding", "current_funding", "oi_delta", "last_funding_avg",
        "z_score", "volume_surge", "candles_1h", "candles_5m",
    ]:
        val = getattr(strategy_instance, attr, None)
        if val is not None:
            if isinstance(val, (list, tuple)):
                state[attr] = len(val)
            elif isinstance(val, float):
                state[attr] = round(val, 4)
            else:
                state[attr] = val

    logger.info(
        "DIAGNOSTIC %s — NO SIGNAL for %s @ %.2f | state=%s",
        strategy_name, symbol, price, state
    )
