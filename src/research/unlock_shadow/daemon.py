"""UnlockShadow daemon — daily PIT calendar snapshots + hypothetical
beta-hedged short windows around eligible cliff unlocks.

Isolation contract:
  * imports nothing from src.core / src.strategies / src.data engine paths;
  * writes only data/research/unlock_shadow.db (unlock_* tables);
  * places zero orders — every position is a hypothetical ledger row.

REST-polled only (no WS): the decision boundaries are daily (00:30 UTC
snapshot; window opens/closes on 00:00 UTC day marks), so a slow aligned
loop is both sufficient and the most replayable surface.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from src.research.unlock_shadow import spec
from src.research.unlock_shadow.calendar_snapshot import (
    RawUnlock, build_symbol_map, fetch_defillama_emissions,
    fetch_defillama_index, fetch_source_b, normalize_defillama,
    normalize_source_b, sha256_payload)
from src.research.unlock_shadow.eligibility import (
    ResolvedEvent, Seen, day_floor, merge_clusters, resolve_events,
    window_bounds_ms)
from src.research.unlock_shadow.ledger import UnlockLedger
from src.research.unlock_shadow.placebo import (
    draw_placebos, placebo_invalidated)
from src.research.unlock_shadow.sim import (
    WindowPrices, beta_30d, clamp_beta, daily_returns, evaluate_window,
    walk_book_slip_bps)

logger = logging.getLogger("unlock_shadow")

SLIP_LIVE_WINDOW_MS = 15 * 60_000   # live L2 counts only near the boundary


def _now_ms() -> int:
    return int(time.time() * 1000)


class UnlockShadowDaemon:
    """Orchestrates snapshots, eligibility, and the hypothetical ledger."""

    def __init__(self, db_path: str = spec.DB_PATH,
                 fetch: Optional[Dict[str, Callable]] = None) -> None:
        self.ledger = UnlockLedger(db_path)
        if fetch is None:
            from scripts.research import hl_public_history as hl
            fetch = {
                "perp_universe": hl.fetch_perp_universe,
                "candles": hl.fetch_candles,
                "funding": hl.fetch_funding,
                "l2": lambda coin: (hl.hl_post(
                    {"type": "l2Book", "coin": coin}) or {}).get("levels"),
                "cal_index": fetch_defillama_index,
                "cal_a": fetch_defillama_emissions,
                "cal_b": fetch_source_b,
            }
        self.fetch = fetch
        self._shutdown = False

    # ─── market-data helpers (all injectable) ────────────────────────────

    def _daily_closes(self, coin: str, start_ms: int, end_ms: int
                      ) -> List[float]:
        bars = self.fetch["candles"](coin, "1d", start_ms, end_ms,
                                     spec.MS_D * 40)
        return [float(b[4]) for b in bars]

    def _day_open(self, coin: str, day_ms: int) -> Optional[float]:
        """The deterministic 'mid at 00:00 UTC': the open of the 1d candle
        covering that day boundary (candleSnapshot floor is 2h; the daily
        open is the replayable boundary mark)."""
        bars = self.fetch["candles"](coin, "1d", day_ms, day_ms
                                     + spec.MS_D, spec.MS_D * 40)
        for b in bars:
            if int(b[0]) == day_ms:
                return float(b[1])
        return float(bars[0][1]) if bars else None

    def _adv7d_usd(self, coin: str, boundary_ms: int) -> Optional[float]:
        """Mean daily notional (sum v*close) over the 7 completed days
        before `boundary_ms` — the §3 ADV gate."""
        bars = self.fetch["candles"](
            coin, "1d", boundary_ms - 7 * spec.MS_D, boundary_ms - 1,
            spec.MS_D * 40)
        if not bars:
            return None
        vols = [float(b[5]) * float(b[4]) for b in bars]
        return sum(vols) / len(vols) if vols else None

    def _beta(self, coin: str, boundary_ms: int) -> Optional[float]:
        """30d daily-return beta vs BTC ending at the boundary."""
        t = self._daily_closes(coin, boundary_ms - 31 * spec.MS_D,
                               boundary_ms)
        b = self._daily_closes(spec.HEDGE_COIN, boundary_ms
                               - 31 * spec.MS_D, boundary_ms)
        return beta_30d(daily_returns(t), daily_returns(b))

    def _slip_bps(self, coin: str, side: str, notional: float,
                  mid: float, boundary_ms: int, now_ms: int) -> Optional[float]:
        """L2-walk slip estimate — only meaningful when the boundary is
        ~live; historical boundaries get None (recorded as unverified,
        never backfilled with today's book)."""
        if abs(now_ms - boundary_ms) > SLIP_LIVE_WINDOW_MS:
            return None
        try:
            levels = self.fetch["l2"](coin)
        except Exception:  # noqa: BLE001
            return None
        return walk_book_slip_bps(side, notional, levels, mid)

    # ─── snapshot pass (§1) ──────────────────────────────────────────────

    def _snapshot_due(self, now_ms: int) -> bool:
        today = day_floor(now_ms)
        snap = today + (spec.SNAPSHOT_UTC_HHMM[0] * 60
                        + spec.SNAPSHOT_UTC_HHMM[1]) * 60_000
        last = int(self.ledger.meta_get("last_snapshot_day") or 0)
        return now_ms >= snap and last < today

    def _snapshot_pass(self, now_ms: int) -> None:
        index = self.fetch["cal_index"]() or []
        universe = [str(u.get("name"))
                    for u in (self.fetch["perp_universe"]() or [])]
        smap, misses = build_symbol_map(index, universe)
        self.ledger.snapshot_write(
            now_ms, "defillama_map", "*", sha256_payload(smap),
            json.dumps(smap, sort_keys=True), 0)
        for m in misses:
            self.ledger.log("map_miss", m)
        tracked: List[tuple] = sorted(smap.items())
        logger.info("snapshot: %d tracked unlock tokens", len(tracked))
        self.ledger.log("snapshot", {"tracked": len(tracked)})
        for sym, slug in tracked:
            try:
                payload_a = self.fetch["cal_a"](slug)
                if payload_a is not None:
                    self.ledger.snapshot_write(
                        now_ms, "defillama", sym, sha256_payload(payload_a),
                        json.dumps(payload_a, default=str), 0)
                    for r in normalize_defillama(sym, payload_a):
                        self._seen(r, now_ms)
            except Exception as e:  # noqa: BLE001 — one bad feed != down
                self.ledger.log("error", {"src": "defillama", "token": sym,
                                          "err": str(e)})
            try:
                payload_b, src_b = self.fetch["cal_b"](slug)
                if payload_b is not None:
                    self.ledger.snapshot_write(
                        now_ms, src_b, sym, sha256_payload(payload_b),
                        json.dumps(payload_b, default=str), 0)
                    for r in normalize_source_b(sym, payload_b, src_b):
                        self._seen(r, now_ms)
            except Exception as e:  # noqa: BLE001
                self.ledger.log("error", {"src": "source_b", "token": sym,
                                          "err": str(e)})
        self.ledger.meta_set("last_snapshot_day", str(day_floor(now_ms)))

    def _seen(self, raw: RawUnlock, seen_ms: int) -> None:
        from src.research.unlock_shadow.eligibility import pct_of_unlocked
        p = pct_of_unlocked(raw)
        self.ledger.calendar_seen(
            raw.token, raw.event_ms, raw.amount_tokens,
            p, raw.category, raw.kind, raw.source, seen_ms)
        if raw.kind == "cliff" and p is None:
            self.ledger.log("pct_missing", {"token": raw.token,
                                            "event_ms": raw.event_ms})

    def _calendar_seen_sets(self) -> tuple:
        seen_a: List[Seen] = []
        seen_b: List[Seen] = []
        for r in self.ledger.calendar_rows():
            ev = RawUnlock(token=r["token"], event_ms=r["event_ms"],
                           amount_tokens=r["amount_tokens"],
                           category=r["category"] or "", kind=r["kind"],
                           unlocked_supply=None, source=r["source"])
            s = Seen(ev=ev, first_seen_ms=r["first_seen_ms"],
                     last_seen_ms=r["last_seen_ms"], pct=r["pct_unlocked"])
            (seen_a if r["source"] == "defillama" else seen_b).append(s)
        return seen_a, seen_b

    # ─── resolve + register (§2, §3, §5) ─────────────────────────────────

    def _resolve_pass(self, now_ms: int) -> None:
        seen_a, seen_b = self._calendar_seen_sets()
        resolved = resolve_events(seen_a, seen_b, now_ms)
        resolved = merge_clusters(resolved)
        prev_state = { (e["token"], e["event_ms"]): e["state"]
                       for e in self.ledger.all_events() }
        for e in resolved:
            newly = prev_state.get((e.token, e.event_ms)) != e.state
            eid = self.ledger.event_upsert({
                "token": e.token, "event_ms": e.event_ms,
                "amount_tokens": e.amount_tokens,
                "pct_unlocked": e.pct_unlocked, "state": e.state,
                "seen_a_ms": e.seen_a_ms, "seen_b_ms": e.seen_b_ms,
                "amt_b": e.amt_b, "dt_b_ms": e.dt_b_ms,
                "b_confirmed": e.b_confirmed,
                "cluster_head_ms": e.cluster_head_ms,
                "registered_ms": now_ms})
            if newly:
                self.ledger.log(e.state, {"token": e.token,
                                          "event_ms": e.event_ms,
                                          "pct": e.pct_unlocked}, eid)
            if newly and e.state == "eligible" and \
                    e.cluster_head_ms == e.event_ms:
                self._register_sims(eid, e, now_ms)

    def _register_sims(self, event_id: int, e: ResolvedEvent,
                       now_ms: int) -> None:
        ws, we = window_bounds_ms(e.event_ms)
        self.ledger.sim_insert({
            "kind": "event", "parent_event_id": event_id, "token": e.token,
            "window_start_ms": ws, "window_end_ms": we,
            "state": "scheduled"})
        unlocks = self._guard_unlocks(e.token)
        for s in draw_placebos(e.token, e.event_ms, unlocks):
            self.ledger.sim_insert({
                "kind": "placebo", "parent_event_id": event_id,
                "token": e.token, "window_start_ms": s,
                "window_end_ms": s + 7 * spec.MS_D, "state": "scheduled"})
        self.ledger.log("placebo_draw", {"token": e.token,
                                         "event_ms": e.event_ms}, event_id)

    def _guard_unlocks(self, token: str) -> List[int]:
        """All known same-token unlocks >=0.5% — the placebo guard set.
        Any recorded kind counts: the prereg says 'no unlock >=0.5%',
        not 'no eligible unlock'."""
        return [int(r["event_ms"]) for r in self.ledger.calendar_rows()
                if r["token"] == token
                and (r["pct_unlocked"] or 0) >= spec.PLACEBO_UNLOCK_PCT]

    def _invalidate_placebos(self) -> None:
        for sim in self.ledger.sims_by_state("scheduled"):
            if sim["kind"] != "placebo":
                continue
            if placebo_invalidated(sim["window_start_ms"],
                                   self._guard_unlocks(sim["token"])):
                self.ledger.sim_update(sim["id"], state="invalidated")
                self.ledger.log("invalidate", {"token": sim["token"],
                                               "ws": sim["window_start_ms"]},
                                sim["id"])

    # ─── sim lifecycle (§4) ──────────────────────────────────────────────

    def _open_due(self, now_ms: int) -> None:
        for sim in self.ledger.sims_by_state("scheduled"):
            if sim["window_start_ms"] > now_ms:
                continue
            ws = int(sim["window_start_ms"])
            adv = self._adv7d_usd(sim["token"], ws)
            if adv is None:
                self.ledger.sim_update(sim["id"], notes="adv_missing")
                continue  # ADV unmeasurable — retry next tick, never guess
            if sim["kind"] == "event":
                ev = self.ledger.event_row(sim["parent_event_id"])
                if ev is not None:
                    self.ledger.event_upsert({
                        "token": ev["token"], "event_ms": ev["event_ms"],
                        "adv7d_usd": adv,
                        "adv_gated": int(adv < spec.ADV_MIN_USD)})
            if adv < spec.ADV_MIN_USD:
                self.ledger.sim_update(sim["id"], state="gated_out",
                                       notes=f"adv7d ${adv:,.0f} < min")
                self.ledger.log("adv_gate", {"token": sim["token"],
                                             "adv7d": adv}, sim["id"])
                continue
            beta = self._beta(sim["token"], ws)
            if beta is None:
                self.ledger.sim_update(sim["id"], state="invalidated",
                                       notes="beta_uncomputable")
                continue
            beta_c = clamp_beta(beta)
            tok_in = self._day_open(sim["token"], ws)
            btc_in = self._day_open(spec.HEDGE_COIN, ws)
            if tok_in is None or btc_in is None:
                self.ledger.sim_update(sim["id"], notes="open_px_missing")
                continue  # retry next tick
            n_btc = beta_c * spec.NOTIONAL_USD
            ts_in = self._slip_bps(sim["token"], "sell",
                                   spec.NOTIONAL_USD, tok_in, ws, now_ms)
            bs_in = self._slip_bps(spec.HEDGE_COIN, "buy", n_btc,
                                   btc_in, ws, now_ms)
            unver = int(ts_in is None or bs_in is None)
            self.ledger.sim_update(
                sim["id"], state="open", beta=beta_c,
                hedge_notional_usd=n_btc, tok_px_in=tok_in, btc_px_in=btc_in,
                tok_slip_in_bps=ts_in or 0.0, btc_slip_in_bps=bs_in or 0.0,
                slip_unverified=unver, priced_in_ms=now_ms)
            self.ledger.log("open", {"token": sim["token"],
                                     "beta": beta_c,
                                     "slip_unverified": bool(unver)},
                            sim["id"])

    def _close_due(self, now_ms: int) -> None:
        for sim in self.ledger.sims_by_state("open"):
            we = int(sim["window_end_ms"])
            if we > now_ms:
                continue
            tok_out = self._day_open(sim["token"], we)
            btc_out = self._day_open(spec.HEDGE_COIN, we)
            if tok_out is None or btc_out is None:
                continue  # retry next tick
            n_tok = spec.NOTIONAL_USD
            n_btc = float(sim["hedge_notional_usd"] or 0.0)
            ts_out = self._slip_bps(sim["token"], "buy", n_tok,
                                    tok_out, we, now_ms)
            bs_out = self._slip_bps(spec.HEDGE_COIN, "sell", n_btc,
                                    btc_out, we, now_ms)
            fund_tok = [r for _ts, r in self.fetch["funding"](
                sim["token"], int(sim["window_start_ms"]), we) or []]
            fund_btc = [r for _ts, r in self.fetch["funding"](
                spec.HEDGE_COIN, int(sim["window_start_ms"]), we) or []]
            res = evaluate_window(
                WindowPrices(
                    tok_mid_in=float(sim["tok_px_in"]), tok_mid_out=tok_out,
                    btc_mid_in=float(sim["btc_px_in"]), btc_mid_out=btc_out,
                    tok_slip_in_bps=float(sim["tok_slip_in_bps"] or 0.0),
                    tok_slip_out_bps=ts_out or 0.0,
                    btc_slip_in_bps=float(sim["btc_slip_in_bps"] or 0.0),
                    btc_slip_out_bps=bs_out or 0.0),
                float(sim["beta"] or 1.0), fund_tok, fund_btc, n_tok)
            self.ledger.sim_update(
                sim["id"], state="closed",
                tok_px_out=tok_out, btc_px_out=btc_out,
                tok_slip_out_bps=ts_out or 0.0,
                btc_slip_out_bps=bs_out or 0.0,
                funding_tok_usd=res.funding_tok_usd,
                funding_btc_usd=res.funding_btc_usd,
                gross_usd=res.gross_tok_usd + res.gross_btc_usd,
                fees_usd=res.fees_usd, net_usd=res.net_usd,
                net_bps=res.net_bps, review_flag=int(res.review_flag),
                priced_out_ms=now_ms)
            self.ledger.log("close", {"token": sim["token"],
                                      "kind": sim["kind"]}, sim["id"])
            if res.review_flag:
                self.ledger.log("manual_review",
                                {"token": sim["token"],
                                 "net_bps": res.net_bps}, sim["id"])
                logger.warning("manual review: %s sim %d net %.0f bps",
                               sim["token"], sim["id"], res.net_bps)

    # ─── loop ────────────────────────────────────────────────────────────

    def tick(self, now_ms: Optional[int] = None) -> None:
        now_ms = now_ms or _now_ms()
        for name, fn in (
                ("snapshot", lambda: self._snapshot_if_due(now_ms)),
                ("resolve", lambda: (self._resolve_pass(now_ms),
                                     self._invalidate_placebos())),
                ("open", lambda: self._open_due(now_ms)),
                ("close", lambda: self._close_due(now_ms))):
            try:
                fn()
            except Exception:  # noqa: BLE001 — one bad pass never kills loop
                logger.exception("unlock-shadow pass failed: %s", name)

    def _snapshot_if_due(self, now_ms: int) -> None:
        if self._snapshot_due(now_ms):
            self._snapshot_pass(now_ms)

    def _heartbeat(self) -> None:
        self.ledger.meta_set("hb_ts_ms", str(_now_ms()))
        self.ledger.meta_set("counts", json.dumps({
            "sims_open": self.ledger.count(
                "unlock_sims", "WHERE state='open'"),
            "eligible": self.ledger.count(
                "unlock_events", "WHERE state='eligible'"),
        }))

    async def boot(self) -> None:
        self.ledger.meta_incr("restart_count")
        self._heartbeat()

    async def run(self) -> None:
        await self.boot()
        last_hb = 0.0
        while not self._shutdown:
            try:
                # synchronous tick: the ledger's sqlite connection lives
                # on this thread (to_thread would trip check_same_thread);
                # the loop is REST-polled, nothing else needs the loop
                self.tick()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("unlock-shadow tick failed")
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
    d = UnlockShadowDaemon()
    try:
        asyncio.run(d.run())
    finally:
        d.shutdown()


if __name__ == "__main__":
    main()
