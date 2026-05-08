"""Realized correlation monitor for portfolio risk management.

Tracks rolling return histories per symbol and computes pairwise
Pearson correlation. Used by the RiskManager to reject entries that
would create overly-correlated positions.

Example:
    monitor = CorrelationMonitor(lookback=60)  # 60 periods
    monitor.add_return("BTC", 0.001)
    monitor.add_return("ETH", 0.0008)
    corr = monitor.get_correlation("BTC", "ETH")  # ~0.85
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class CorrelationMonitor:
    """Rolling realized correlation calculator.

    Maintains a ring-buffer of returns per symbol.  When a buffer has
    enough samples the pairwise Pearson coefficient is computed on
    demand.
    """

    def __init__(self, lookback: int = 60) -> None:
        self._lookback = lookback
        self._returns: Dict[str, deque[float]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_return(self, symbol: str, ret: float) -> None:
        """Append a single-period log-return for *symbol*."""
        if symbol not in self._returns:
            self._returns[symbol] = deque(maxlen=self._lookback)
        self._returns[symbol].append(ret)

    def add_candle_return(self, symbol: str, prev_close: float, curr_close: float) -> None:
        """Compute log-return from two closes and store it."""
        if prev_close <= 0 or curr_close <= 0:
            return
        ret = math.log(curr_close / prev_close)
        self.add_return(symbol, ret)

    def get_correlation(self, sym_a: str, sym_b: str) -> Optional[float]:
        """Return Pearson r between *sym_a* and *sym_b*, or None."""
        series_a = self._returns.get(sym_a)
        series_b = self._returns.get(sym_b)
        if series_a is None or series_b is None:
            return None
        # Need same-length overlapping window
        min_len = min(len(series_a), len(series_b))
        if min_len < 3:
            return None
        # Take last *min_len* points from both
        a = list(series_a)[-min_len:]
        b = list(series_b)[-min_len:]
        return _pearson_r(a, b)

    def would_violate(
        self,
        new_symbol: str,
        existing_symbols: List[str],
        threshold: float,
    ) -> Tuple[bool, Optional[str], Optional[float]]:
        """Check if adding *new_symbol* would breach correlation limit.

        Returns (violated: bool, conflicting_symbol: str|None, corr: float|None)
        """
        for sym in existing_symbols:
            corr = self.get_correlation(new_symbol, sym)
            if corr is not None and abs(corr) > threshold:
                return True, sym, corr
        return False, None, None

    def get_matrix(self, symbols: List[str]) -> Dict[Tuple[str, str], Optional[float]]:
        """Full pairwise matrix for diagnostics."""
        out: Dict[Tuple[str, str], Optional[float]] = {}
        for i, a in enumerate(symbols):
            for b in symbols[i + 1 :]:
                out[(a, b)] = self.get_correlation(a, b)
        return out

    def status(self) -> Dict[str, any]:
        """Quick health snapshot."""
        return {
            "symbols_tracked": list(self._returns.keys()),
            "lookback": self._lookback,
            "samples": {k: len(v) for k, v in self._returns.items()},
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _pearson_r(a: List[float], b: List[float]) -> Optional[float]:
    """Pearson correlation coefficient (sample)."""
    n = len(a)
    if n != len(b) or n < 3:
        return None

    mean_a = sum(a) / n
    mean_b = sum(b) / n

    num = 0.0
    den_a = 0.0
    den_b = 0.0
    for i in range(n):
        da = a[i] - mean_a
        db = b[i] - mean_b
        num += da * db
        den_a += da * da
        den_b += db * db

    if den_a <= 0.0 or den_b <= 0.0:
        return 0.0  # one series is constant

    return num / math.sqrt(den_a * den_b)
