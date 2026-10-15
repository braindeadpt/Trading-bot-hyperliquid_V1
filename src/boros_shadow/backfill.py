"""Backfill (prereg Phase-2.7) — rebuild marks/liquidations for trades
that opened before the Phase-2 deploy, by replaying the compressed
snapshot pairs already stored in boros_snapshots.

Replays the SAME per-pass pipeline (marks -> liquidation checks ->
delisted/maturity closes) so backfilled history is indistinguishable
from live marks. Idempotent: a trade with a mark at/after a snapshot
ts is skipped for that pass.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.boros_shadow.boros_client import parse_markets
from src.boros_shadow.ledger import BorosLedger
from src.boros_shadow.marks import mark_trade
from src.boros_shadow.settle import n5_kill

logger = logging.getLogger("boros_shadow.backfill")


def _books_of(payload: Any) -> Dict:
    """books snapshot payload -> {(venue, asset): (bids, asks)}."""
    out = {}
    for key, book in (payload or {}).items():
        venue, _, asset = key.partition(":")
        try:
            out[(venue, asset)] = (
                [(float(p), float(s)) for p, s in book[0]],
                [(float(p), float(s)) for p, s in book[1]])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return out


def backfill(ledger: BorosLedger, funding_fetch=None,
             now_ms: Optional[int] = None) -> Dict[str, int]:
    """Replay stored snapshot passes for trades opened before Phase 2.
    Returns counters (marks written, liquidations, closes)."""
    from src.boros_shadow.daemon import check_trade_fate
    stats = {"marks": 0, "liquidations": 0, "closes": 0}
    trades = ledger.trades_by_state("open", "matured", "closed",
                                  "liquidated")
    if not trades:
        return stats
    for pair in ledger.snapshot_pairs():
        ts = int(pair["ts_ms"])
        rows = ledger.snapshot_read(pair["markets"])
        books = _books_of(ledger.snapshot_read(pair["books"]))
        markets = parse_markets(rows or [])
        by_id = {m.market_id: m for m in markets}
        meta = {}
        try:
            meta = ledger._con.execute(
                "SELECT meta_json FROM boros_snapshots WHERE id=?",
                (pair["markets"],)).fetchone()[0]
            import json as _json
            meta = _json.loads(meta or "{}")
        except Exception:  # noqa: BLE001
            meta = {}
        on_sched = bool(meta.get("on_schedule"))
        for trade in ledger.trades_by_state("open", "matured"):
            if ts <= int(trade["entry_ms"]):
                continue
            last = ledger.last_mark(trade["id"])
            if last and int(last["ts_ms"]) >= ts:
                continue                     # already marked this pass
            mark = mark_trade(ledger, trade, by_id, books, ts,
                              on_sched, funding_fetch,
                              ledger.rates_at(ts))
            stats["marks"] += 1
            closed = check_trade_fate(ledger, trade, by_id, books, ts,
                                      on_sched, mark)
            stats["liquidations"] += int(closed.startswith("liquidated"))
            stats["closes"] += int(bool(closed))
    if n5_kill(ledger):
        ledger.flag_set("n5_kill", None, now_ms or 0)
    logger.info("boros backfill: %s", stats)
    return stats
