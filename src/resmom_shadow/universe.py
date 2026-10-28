"""Universe selection (prereg §Regra — top 25 HL perps).

  * eligible = main-dex perp (no 'dex:' prefix), not a stable, with
    >= MIN_HISTORY_D days of HL daily-candle history at the cutoff.
  * rank     = 30d mean notional volume, descending; top UNIVERSE_TOP_N.
  * BTC stays in the ranking; legs exclude it downstream.

Daily candles are `{"t","c","v"}`-shaped (t=open ms, c=close, v=coin vol).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.resmom_shadow import spec


@dataclass
class UniverseEntry:
    symbol: str
    vol30_notional: float      # mean daily notional (USD) over VOL_WINDOW_D
    history_d: int             # age in days of the first candle at cutoff


def is_perp_symbol(name: str) -> bool:
    """Main-dex perp names only — HIP-3 dex coins carry a 'dex:' prefix."""
    if not name or ":" in name:
        return False
    return name.upper() not in spec.STABLES


def mean_notional_30d(candles: List[dict]) -> Optional[float]:
    """Mean close*volume over the last VOL_WINDOW_D complete candles."""
    rows = [c for c in candles if c.get("c") and c.get("v") is not None]
    if len(rows) < spec.VOL_WINDOW_D:
        return None
    tail = rows[-spec.VOL_WINDOW_D:]
    vals = [float(c["c"]) * float(c["v"]) for c in tail]
    return sum(vals) / len(vals)


def history_days(candles: List[dict], cutoff_ms: int) -> int:
    if not candles:
        return 0
    first = min(int(c["t"]) for c in candles)
    return int((cutoff_ms - first) // spec.MS_D)


def select_universe(candles_by_sym: Dict[str, List[dict]],
                    cutoff_ms: int,
                    top_n: int = spec.UNIVERSE_TOP_N
                    ) -> Tuple[List[UniverseEntry], List[str]]:
    """Ranked eligible universe + symbols rejected for short history.

    candles_by_sym should cover every candidate perp (the daemon fetches
    VOL_WINDOW_D + a margin per name; MIN_HISTORY_D is checked against the
    first returned candle timestamp).
    """
    entries: List[UniverseEntry] = []
    too_young: List[str] = []
    for sym, candles in candles_by_sym.items():
        if not is_perp_symbol(sym):
            continue
        hd = history_days(candles, cutoff_ms)
        if hd < spec.MIN_HISTORY_D:
            too_young.append(sym)
            continue
        v = mean_notional_30d(candles)
        if v is None:
            too_young.append(sym)
            continue
        entries.append(UniverseEntry(sym, v, hd))
    entries.sort(key=lambda e: -e.vol30_notional)
    return entries[:top_n], too_young
