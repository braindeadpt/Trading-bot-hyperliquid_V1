"""BorosLockedShadow daemon — hourly collector + paper lifecycle.

Pure REST + SQLite. At :05 UTC each hour (window :05-:09): fetch Boros
markets, fetch perp L2 books, evaluate + register entries; then Phase-2
— mark every open trade (every pass, descriptive included), ingest
funding, check hypothetical liquidations, close at maturity/delist.
Zero orders. All fetchers are injectable for tests.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from src.boros_shadow import spec
from src.boros_shadow.books import fetch_perp_book
from src.boros_shadow.boros_client import (
    fetch_boros_markets, parse_markets)
from src.boros_shadow.ledger import BorosLedger
from src.boros_shadow.marks import (
    liquidation_hits, mark_trade, split_flags, trade_legs)
from src.boros_shadow.opportunity import (
    Candidate, evaluate, needed_books, pick_winner)
from src.boros_shadow.settle import (
    CLOSE_DELISTED, CLOSE_LIQ_BOROS, CLOSE_LIQ_POOLED, CLOSE_MATURITY,
    close_trade, n5_kill)

logger = logging.getLogger("boros_shadow")


def _now_ms() -> int:
    return int(time.time() * 1000)


def hour_floor(ms: int) -> int:
    return ms - (ms % spec.MS_H)


def check_trade_fate(ledger: BorosLedger, trade: Dict[str, Any],
                     markets_by_id: Dict[int, Any], books: Dict,
                     now_ms: int, on_schedule: bool,
                     mark: Optional[Dict[str, Any]] = None) -> str:
    """Liquidations (any pass), delisted-market anomaly close (any
    pass), maturity close (on-schedule only). Returns the close reason
    or ''."""
    tid = trade["id"]
    hits = liquidation_hits(trade, mark, now_ms) if mark else []
    for leg in split_flags(trade, mark) if mark else []:
        known = {(f["trade_id"], json.loads(f["data_json"]).get("leg"))
                 for f in ledger.flag_get("split_liq")}
        if (tid, leg) not in known:
            ledger.flag_set("split_liq", tid, now_ms, {"leg": leg})
    if hits:
        reason = (CLOSE_LIQ_POOLED if "pooled" in hits
                  else CLOSE_LIQ_BOROS)
        ledger.flag_set("liquidation", tid, now_ms,
                        {"layer": reason})
        close_trade(ledger, trade, books, markets_by_id, now_ms,
                    reason, mark)
        logger.warning("boros-shadow HYPOTHETICAL LIQUIDATION "
                       "trade=%d layer=%s", tid, reason)
        return reason
    yu_legs = trade_legs(trade)["yu"]
    known = [yl["market_id"] for yl in yu_legs if yl.get("market_id")]
    yu_alive = all(
        markets_by_id.get(mid) is not None
        and markets_by_id[mid].state == "Normal" for mid in known)
    if known and not yu_alive and now_ms < int(trade["maturity_ms"]):
        close_trade(ledger, trade, books, markets_by_id, now_ms,
                    CLOSE_DELISTED, mark)
        return CLOSE_DELISTED
    if on_schedule and now_ms >= int(trade["maturity_ms"]):
        if close_trade(ledger, trade, books, markets_by_id, now_ms,
                       CLOSE_MATURITY, mark) is not None:
            return CLOSE_MATURITY
    return ""


class BorosShadowDaemon:
    def __init__(self, db_path: str = spec.DB_PATH,
                 fetch: Optional[Dict[str, Any]] = None) -> None:
        self.ledger = BorosLedger(db_path)
        if fetch is None:
            from src.boros_shadow.books import fetch_perp_book as _fpb
            from src.boros_shadow.funding import fetch_funding as _ff
            fetch = {"markets": fetch_boros_markets, "book": _fpb,
                     "funding": _ff}
        self.fetch = fetch
        self._shutdown = False

    # ─── snapshot pass (Phase 1.1-1.4) ───────────────────────────────────

    @staticmethod
    def _schedule(now_ms: int) -> Tuple[bool, int]:
        """(on_schedule, minute_utc). Entries only ever price off a book
        read inside the :05-:09 UTC window (loop-delay tolerance)."""
        minute = (now_ms % spec.MS_H) // 60_000
        on = spec.SNAPSHOT_UTC_MINUTE <= minute < spec.SNAPSHOT_WINDOW_END_MIN
        return on, minute

    def _snapshot_due(self, now_ms: int) -> bool:
        hour = hour_floor(now_ms)
        on_sched, _ = self._schedule(now_ms)
        last_snap = int(self.ledger.meta_get("last_snapshot_hour") or 0)
        if last_snap >= hour:
            return False                       # hour already consumed
        if on_sched:
            return True                        # the real :05-:09 pass
        # off-schedule: one descriptive pass per hour, never consumes it —
        # the window pass can still run and open this same hour
        last_pass = int(self.ledger.meta_get("last_pass_hour") or 0)
        return last_pass < hour

    def _snapshot_pass(self, now_ms: int) -> None:
        on_schedule, minute = self._schedule(now_ms)

        rows = self.fetch["markets"]()
        self.ledger.snapshot_write(now_ms, "markets", rows,
                                   n_rows=len(rows),
                                   meta={"on_schedule": on_schedule,
                                         "minute_utc": minute})
        markets = parse_markets(rows)

        # perp books for the pairs evaluate() consumes PLUS every pair
        # an open trade marks/closes on (a trade near maturity can drop
        # out of needed_books while still open)
        need = set(needed_books(markets, now_ms))
        for t in self.ledger.trades_by_state("open"):
            need |= {("hyperliquid", t["asset"]),
                     (t["venue_b"], t["asset"])}
        books: Dict[Tuple[str, str], Any] = {}
        unavailable: List[str] = []
        for venue, asset in sorted(need):
            book = self.fetch["book"](venue, asset)
            if book is None:
                unavailable.append(f"{venue}:{asset}")
            else:
                books[(venue, asset)] = book
        self.ledger.snapshot_write(
            now_ms, "books",
            {f"{v}:{a}": b for (v, a), b in books.items()},
            n_rows=len(books),
            meta={"venues_unavailable": unavailable,
                  "on_schedule": on_schedule,
                  "minute_utc": minute})

        cands = evaluate(markets, books, now_ms)
        self._register(cands, now_ms, on_schedule=on_schedule)
        self._mark_and_settle(markets, books, now_ms, on_schedule)
        # an off-schedule pass never consumes the hour — only an
        # on-schedule pass sets last_snapshot_hour; both stamp
        # last_pass_hour so descriptive passes run at most once/hour
        self.ledger.meta_set("last_pass_hour", str(hour_floor(now_ms)))
        if on_schedule:
            self.ledger.meta_set("last_snapshot_hour",
                                 str(hour_floor(now_ms)))
        n_eligible = sum(1 for c in cands if c.eligible)
        logger.info("boros snapshot: %d markets -> %d candidates "
                    "(%d eligible), books unavailable=%s",
                    len(markets), len(cands), n_eligible, unavailable)
        self.ledger.log("snapshot", {
            "markets": len(markets), "candidates": len(cands),
            "eligible": n_eligible, "books_unavailable": unavailable,
            "on_schedule": on_schedule, "minute_utc": minute},
            ts_ms=now_ms)

    def _register(self, cands: List[Candidate], now_ms: int,
                  on_schedule: bool = True) -> None:
        """Group by (asset, venue_b, maturity): the winner is the max
        gross-fixed candidate; it enters iff eligible and the dedup +
        daily-cap rules allow. The day's single slot per asset goes to
        the best eligible winner (net_apy desc), not the first parsed.
        Every candidate is recorded regardless."""
        groups: Dict[Tuple[str, str, int], List[Candidate]] = {}
        for c in cands:
            groups.setdefault((c.asset, c.venue_b, c.maturity_ms), []) \
                .append(c)
        open_triples = self.ledger.open_triples()
        day_ms = now_ms - (now_ms % spec.MS_D)
        winners = {k: pick_winner(g) for k, g in groups.items()}

        ranked = sorted(
            (k for k, w in winners.items()
             if w is not None and w.eligible and k not in open_triples),
            key=lambda k: -(winners[k].net_apy or 0))
        entered_keys = set()
        for key in ranked:
            winner = winners[key]
            if not on_schedule:
                winner.block_reason = "off_schedule"
                continue
            if (self.ledger.entries_today_for_asset(
                    key[0], day_ms) >= spec.MAX_NEW_PER_ASSET_PER_DAY):
                winner.block_reason = "daily_cap"
                continue
            tid = self.ledger.trade_insert({
                "asset": winner.asset, "venue_b": winner.venue_b,
                "maturity_ms": winner.maturity_ms,
                "structure": winner.structure,
                "direction": winner.direction, "state": "open",
                "entry_ms": now_ms,
                "fixed_bruto": winner.fixed_bruto,
                "net_apy": winner.net_apy,
                "capital_usd": winner.capital_usd,
                "costs_usd": winner.costs_usd,
                "prices_json": json.dumps(winner.legs, default=str)})
            if tid is not None:
                entered_keys.add(key)
                open_triples.add(key)
                self.ledger.log("entry", {
                    "asset": winner.asset, "venue_b": winner.venue_b,
                    "maturity_ms": winner.maturity_ms,
                    "structure": winner.structure,
                    "direction": winner.direction}, tid, now_ms)

        for key, winner in winners.items():
            if (winner is not None and winner.eligible
                    and key in open_triples and key not in entered_keys):
                winner.block_reason = "already_open"
        for key, group in groups.items():
            winner = winners[key]
            for c in group:
                self.ledger.opportunity_insert({
                    "ts_ms": now_ms, "asset": c.asset,
                    "venue_b": c.venue_b, "maturity_ms": c.maturity_ms,
                    "structure": c.structure, "direction": c.direction,
                    "fixed_bruto": c.fixed_bruto, "net_apy": c.net_apy,
                    "capital_usd": c.capital_usd, "costs_usd": c.costs_usd,
                    "ttm_days": c.ttm_days,
                    "eligible": int(c.eligible),
                    "entered": int(c is winner and key in entered_keys),
                    "block_reason": c.block_reason,
                    "legs_json": json.dumps(c.legs, default=str)})

    # ─── Phase 2: marks + funding + liquidation + close ────────────────

    def _mark_and_settle(self, markets: List[Any],
                         books: Dict[Tuple[str, str], Any],
                         now_ms: int, on_schedule: bool) -> None:
        """One mark per open trade per pass (prereg: every pass marks).
        Then fate checks — liquidation/delist on any pass, maturity
        close only inside the window."""
        by_id = {m.market_id: m for m in markets}
        for trade in self.ledger.trades_by_state("open"):
            mark = mark_trade(self.ledger, trade, by_id, books,
                              now_ms, on_schedule, self.fetch["funding"])
            reason = check_trade_fate(self.ledger, trade, by_id, books,
                                      now_ms, on_schedule, mark)
            if reason:
                logger.warning("boros-shadow close: trade=%d reason=%s",
                               trade["id"], reason)
                if n5_kill(self.ledger):
                    self.ledger.flag_set("n5_kill", None, now_ms)

    # ─── loop ────────────────────────────────────────────────────────────

    def tick(self, now_ms: Optional[int] = None) -> None:
        now_ms = now_ms or _now_ms()
        for name, fn in (
                ("snapshot", lambda: self._snapshot_if_due(now_ms)),):
            try:
                fn()
            except Exception:  # noqa: BLE001 — one bad pass never kills loop
                logger.exception("boros-shadow pass failed: %s", name)

    def _snapshot_if_due(self, now_ms: int) -> None:
        if self._snapshot_due(now_ms):
            self._snapshot_pass(now_ms)

    def _heartbeat(self) -> None:
        self.ledger.meta_set("hb_ts_ms", str(_now_ms()))
        self.ledger.meta_set("counts", json.dumps({
            "open": self.ledger.count(
                "boros_trades", "WHERE state='open'"),
            "entries": self.ledger.count("boros_trades"),
        }))

    async def boot(self) -> None:
        self.ledger.meta_incr("restart_count")
        self._heartbeat()
        # prereg §7: trades opened before this deploy get their marks
        # rebuilt from stored snapshots before live passes resume
        from src.boros_shadow.backfill import backfill
        stats = backfill(self.ledger, self.fetch["funding"])
        if stats["marks"]:
            logger.warning("boros-shadow backfill: %s", stats)

    async def run(self) -> None:
        await self.boot()
        last_hb = 0.0
        while not self._shutdown:
            try:
                # synchronous tick: the ledger's sqlite connection lives
                # on this thread; the loop is REST-polled
                self.tick()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("boros-shadow tick failed")
            if time.time() - last_hb >= spec.HEARTBEAT_S:
                self._heartbeat()
                last_hb = time.time()
            await asyncio.sleep(spec.TICK_S)

    def shutdown(self) -> None:
        self._shutdown = True
        self.ledger.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    d = BorosShadowDaemon()
    try:
        asyncio.run(d.run())
    finally:
        d.shutdown()


if __name__ == "__main__":
    main()
