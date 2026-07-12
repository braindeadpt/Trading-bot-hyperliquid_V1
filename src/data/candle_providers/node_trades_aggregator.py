"""Aggregate node_trades records into HL-wire OHLCV candles.

Reuses the existing dynamic-quantum / HL price formatting helpers in
``tick_meta.py`` so rebuilt candles are quantized exactly the way the
official ``hl_candleSnapshot`` feed and the GoldRush parity gate expect —
this module does not duplicate that rounding logic.

Bucket semantics mirror ``candle_rollup.py``: for a target interval of
*gap* ms, a trade at epoch ``time_ms`` belongs to the bucket whose open
time is ``(time_ms // gap) * gap`` (HL's ``t``), and whose close time is
``open_ms + gap - 1`` (HL's ``T``). A trade landing exactly on a bucket's
open boundary starts a new bucket; a trade on the last millisecond of a
bucket (``open_ms + gap - 1``) still belongs to that bucket. Buckets with
zero trades are never fabricated — they are simply absent from the output,
matching how official candle feeds behave when there is no trading activity
in a window (this is a deliberate difference from ``candle_rollup``'s
gap-aware *rollup of existing bars*, since here we are building bars from
raw trades rather than aggregating already-closed 1m bars).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.data.candle_providers.base import INTERVAL_MS
from src.data.candle_providers.node_trades_parser import NodeTradeRecord
from src.data.candle_providers.tick_meta import format_hl_price


def bucket_open_ms(time_ms: int, interval: str) -> int:
    """Floor *time_ms* to the start of its HL bucket for *interval*."""
    gap = INTERVAL_MS[interval]
    return (int(time_ms) // gap) * gap


def aggregate_trades_to_ohlcv(
    trades: List[NodeTradeRecord],
    *,
    symbol: str,
    interval: str = "1m",
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
    is_spot: bool = False,
) -> List[Dict[str, Any]]:
    """Aggregate raw trades (single symbol) into closed HL-wire candles.

    Trades for symbols other than *symbol* are ignored (defensive — callers
    should already have filtered per-coin, but archive objects may be mixed).
    Returned rows are sorted ascending by close time ``T`` and use the same
    dict shape (``s/i/t/T/o/h/l/c/v/n``) as other providers in this package.
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    sym = symbol.upper()

    buckets: Dict[int, List[NodeTradeRecord]] = {}
    for tr in trades:
        if tr.symbol != sym:
            continue
        bo = bucket_open_ms(tr.time_ms, interval)
        buckets.setdefault(bo, []).append(tr)

    gap = INTERVAL_MS[interval]
    out: List[Dict[str, Any]] = []
    for bo in sorted(buckets):
        chunk = sorted(buckets[bo], key=lambda t: t.time_ms)
        prices = [t.price for t in chunk]
        vol = sum(t.size for t in chunk)
        row = {
            "s": sym,
            "i": interval,
            "t": bo,
            "T": bo + gap - 1,
            "o": format_hl_price(chunk[0].price, sym, meta_cache, is_spot=is_spot),
            "h": format_hl_price(max(prices), sym, meta_cache, is_spot=is_spot),
            "l": format_hl_price(min(prices), sym, meta_cache, is_spot=is_spot),
            "c": format_hl_price(chunk[-1].price, sym, meta_cache, is_spot=is_spot),
            "v": str(vol),
            "n": len(chunk),
            "_source": "hl_node_trades_rebuild",
        }
        out.append(row)

    return sorted(out, key=lambda r: int(r["T"]))
