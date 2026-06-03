"""Intraday volatility circuit breaker.

Tracks the rolling 1h-ATR per symbol and blocks new entries when current
volatility spikes above a configurable multiple of the recent baseline
("3-sigma vol" guardrail).

Heuristic:
  baseline = mean ATR(1h) over the last ``baseline_window_bars`` (default 168
             = 7 days of hourly bars)
  current  = ATR(1h) of the most recent closed 1h candle
  if current > multiplier * baseline  →  block entries for
                                          ``block_duration_min`` minutes

This is a **soft** circuit breaker — it only blocks *new* entries, never
exits existing positions. The hard drawdown / daily-loss breakers still
own flatten behavior.

State is per-symbol; the breaker is independent of the daily UTC reset.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class VolCircuitConfig:
    """Tunable parameters for the intraday vol circuit breaker."""
    enabled: bool = True
    multiplier: float = 3.0
    baseline_window_bars: int = 168  # 7d * 24 hourly bars
    block_duration_min: float = 30.0
    min_samples: int = 24  # require ~1d of history before evaluating


@dataclass
class _SymbolState:
    samples: Deque[float] = field(default_factory=lambda: deque(maxlen=512))
    block_until_ms: int = 0
    last_breach_at: float = 0.0
    last_breach_ratio: float = 0.0


class VolatilityCircuitBreaker:
    """Per-symbol intraday volatility guardrail.

    Usage:
        cb = VolatilityCircuitBreaker(VolCircuitConfig())
        cb.update("BTC", atr_pct=0.012, ts_ms=now_ms)  # on each closed 1h candle
        if cb.is_blocked("BTC", ts_ms=now_ms):
            ...  # reject entry
    """

    def __init__(self, config: Optional[VolCircuitConfig] = None) -> None:
        self._cfg = config or VolCircuitConfig()
        self._state: Dict[str, _SymbolState] = {}

    @classmethod
    def from_config_dict(cls, cfg: Dict[str, object]) -> "VolatilityCircuitBreaker":
        return cls(VolCircuitConfig(
            enabled=bool(cfg.get("enabled", True)),
            multiplier=float(cfg.get("multiplier", 3.0)),
            baseline_window_bars=int(cfg.get("baseline_window_bars", 168)),
            block_duration_min=float(cfg.get("block_duration_min", 30.0)),
            min_samples=int(cfg.get("min_samples", 24)),
        ))

    def _get(self, symbol: str) -> _SymbolState:
        st = self._state.get(symbol)
        if st is None:
            st = _SymbolState(samples=deque(maxlen=max(64, self._cfg.baseline_window_bars * 2)))
            self._state[symbol] = st
        return st

    def update(self, symbol: str, atr_pct: float, ts_ms: int) -> bool:
        """Record a new ATR sample and re-evaluate the block.

        Returns True if the update tripped the breaker (i.e. a new block
        was just activated). Returns False otherwise.
        """
        if not self._cfg.enabled:
            return False
        st = self._get(symbol)
        st.samples.append(float(atr_pct))

        if len(st.samples) < self._cfg.min_samples:
            return False

        window = list(st.samples)[-self._cfg.baseline_window_bars:]
        baseline = sum(window) / len(window)
        if baseline <= 0.0:
            return False
        ratio = float(atr_pct) / baseline
        st.last_breach_ratio = ratio

        if ratio > self._cfg.multiplier:
            new_block = ts_ms + int(self._cfg.block_duration_min * 60_000)
            # Only extend; never shorten an existing block
            if new_block > st.block_until_ms:
                st.block_until_ms = new_block
                st.last_breach_at = time.time()
                logger.warning(
                    "VOLATILITY CIRCUIT %s: ATR=%.4f / baseline=%.4f = %.2fx > %.2fx — "
                    "blocking entries for %.0f min",
                    symbol, atr_pct, baseline, ratio, self._cfg.multiplier,
                    self._cfg.block_duration_min,
                )
                return True
        return False

    def is_blocked(self, symbol: str, ts_ms: int) -> bool:
        """Return True if entries for *symbol* are currently blocked."""
        if not self._cfg.enabled:
            return False
        st = self._state.get(symbol)
        if st is None:
            return False
        return ts_ms < st.block_until_ms

    def block_remaining_sec(self, symbol: str, ts_ms: int) -> int:
        """Return seconds remaining on the active block (0 if none)."""
        st = self._state.get(symbol)
        if st is None or ts_ms >= st.block_until_ms:
            return 0
        return int((st.block_until_ms - ts_ms) / 1000)

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        """Return a dict of per-symbol state for the dashboard."""
        out: Dict[str, Dict[str, object]] = {}
        for sym, st in self._state.items():
            window = list(st.samples)[-self._cfg.baseline_window_bars:]
            baseline = (sum(window) / len(window)) if window else 0.0
            out[sym] = {
                "samples": len(st.samples),
                "baseline_atr": round(baseline, 6),
                "last_ratio": round(st.last_breach_ratio, 3),
                "block_until_ms": st.block_until_ms,
            }
        return out

    def reset(self, symbol: Optional[str] = None) -> None:
        """Reset state for one symbol (or all)."""
        if symbol is None:
            self._state.clear()
        else:
            self._state.pop(symbol, None)
