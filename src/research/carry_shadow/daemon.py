"""Carry-shadow daemon — live L2 evidence collection for the A1 carry study.

Owns its own WebSocket + REST polling of PUBLIC Hyperliquid market data.
Isolation contract:
  * imports nothing from src.core / src.strategies / src.data engine paths;
  * writes only to its own SQLite ledger (data/research/carry_shadow.db);
  * places zero orders — every position is a hypothetical ledger row.

Kill-switch evaluation happens inline after every recorded event; a latched
death stops new entries but keeps measurement running (evidence preserved).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import websockets

from src.research.carry_shadow import spec
from src.research.carry_shadow.ledger import Ledger
from src.research.carry_shadow.sim import BookSnap, Episode, EpisodePool

logger = logging.getLogger("carry_shadow")

WS_URL = "wss://api.hyperliquid.xyz/ws"
MS_D = 86_400_000
MS_H = 3_600_000


def _now_ms() -> int:
    return int(time.time() * 1000)


class CarryShadowDaemon:
    """Orchestrates feeds, hypothetical episodes, and the kill switch."""

    def __init__(self, db_path: str = spec.DB_PATH,
                 ws_url: str = WS_URL,
                 fetch: Optional[Dict[str, Callable]] = None) -> None:
        self.ledger = Ledger(db_path)
        self.pool = EpisodePool(self._emit)
        self.ws_url = ws_url
        self.books: Dict[str, BookSnap] = {}
        self.funding_tail: Dict[str, Deque[Tuple[int, float]]] = {}
        self.neg_since: Dict[str, Optional[int]] = {}
        self.cands: List[Dict[str, Any]] = []
        self.pair_by_coin: Dict[str, Dict[str, Any]] = {}
        self.in_universe: Dict[str, bool] = {}
        self._prev_zone: Dict[str, str] = {}
        self._px_hist: Dict[str, Deque[Tuple[int, float]]] = {}
        self._shutdown = False
        self._ws: Any = None
        # Injectable fetchers keep the daemon testable without network.
        if fetch is None:
            from scripts.research import hl_public_history as hl
            fetch = {
                "post": hl.hl_post,
                "perp_universe": hl.fetch_perp_universe,
                "spot_pairs": hl.fetch_spot_pairs,
                "funding": hl.fetch_funding,
                "candles": hl.fetch_candles,
            }
        self.fetch = fetch

    # ─── boot ────────────────────────────────────────────────────────────

    async def boot(self) -> None:
        if self.ledger.meta_get("dead"):
            logger.error("experiment is DEAD (%s) — no new episodes",
                         self.ledger.meta_get("dead"))
            self.pool.dead = True
            self.pool.death_reason = self.ledger.meta_get("dead") or ""
        cands = await asyncio.to_thread(self._discover_candidates)
        self.cands = cands
        for c in cands:
            self.pair_by_coin[c["pair_name"]] = c
            self.pair_by_coin[c["perp"]] = c
            self.funding_tail[c["perp"]] = deque(maxlen=72)
            self.neg_since[c["pair_name"]] = None
            self._px_hist[c["pair_name"]] = deque(maxlen=240)
        await self._refresh_gates()
        await self._refresh_funding()
        self._reload_episodes()
        logger.info("boot: %d candidate pairs, %d episodes reloaded",
                    len(cands), len(self.pool.episodes))

    def _discover_candidates(self) -> List[Dict[str, Any]]:
        """A1 name-match rule: spot base == perp, or 'U'+perp (UBTC→BTC)."""
        pairs, _tok = self.fetch["spot_pairs"]()
        meta = self.fetch["post"]({"type": "meta"})
        max_lev = {u["name"]: float(u.get("maxLeverage", 1))
                   for u in meta["universe"]}
        perp_names = set(max_lev)
        out: List[Dict[str, Any]] = []
        for p in pairs:
            base = p["base_name"]
            perp = base if base in perp_names else (
                base[1:] if base.startswith("U") and base[1:] in perp_names
                else None)
            if not perp:
                continue
            lev = max_lev[perp]
            maint = spec.maint_for(lev)
            m, m0 = spec.mm_for(lev)
            out.append({
                "pair_name": p["pair_name"], "base": base, "perp": perp,
                "max_lev": lev, "maint": maint, "m": m, "m0": m0,
                "pm": perp in spec.PM_ELIGIBLE,
            })
        return out

    def _reload_episodes(self) -> None:
        for row in self.ledger.open_episodes():
            if row["state"] == "pending_entry":
                # a restart can't prove pending fills — mark aborted
                self.ledger.episode_update(
                    row["id"], state="aborted", close_reason="restart")
                continue
            ep = Episode(
                pair=row["pair"], perp=row["perp"],
                entry_decision_ms=row["entry_decision_ms"],
                pm_branch=bool(row["pm_branch"]),
                maint=row["maint"], m_trigger=row["m_trigger"],
                m0=row["m0"], id=row["id"],
                state=row["state"], opened_ms=row["opened_ms"] or 0,
                p0=row["p0"] or 0.0, s0=row["s0"] or 0.0,
                q=row["q"], collateral=row["margin"] or 0.0,
                cum_f=row["cum_f"], cost_bps=row["cost_bps"],
                last_funding_ms=row["last_funding_ms"] or 0,
            )
            self.pool.add(ep)

    # ─── REST pollers (run in threads; hl_post is blocking) ──────────────

    async def _refresh_gates(self) -> None:
        """PIT liquidity gates: rolling-30d median 2h notional per leg."""
        end = _now_ms()
        start = end - (spec.LIQ_LOOKBACK_D + 5) * MS_D
        for c in self.cands:
            ok = await asyncio.to_thread(self._gate_one, c, start, end)
            self.in_universe[c["pair_name"]] = ok

    def _gate_one(self, c: Dict[str, Any], start: int, end: int) -> bool:
        try:
            days = set()
            for coin, floor in ((c["pair_name"], spec.SPOT_MIN_DAILY_USD),
                                (c["perp"], spec.PERP_MIN_DAILY_USD)):
                bars = self.fetch["candles"](coin, "2h", start, end,
                                             60 * MS_D)
                if not bars:
                    return False
                daily: Dict[int, float] = {}
                for ts, _o, _h, _l, close, vol, _n in bars:
                    d = ts // MS_D
                    daily[d] = daily.get(d, 0.0) + vol * close
                    days.add(d)
                meds = sorted(v for d, v in daily.items()
                              if len(daily) > 1 and d != end // MS_D)
                if len(meds) < 7:
                    return False
                med = meds[len(meds) // 2]
                if med < floor:
                    return False
            span_d = max(days) - min(days) + 1
            return span_d >= spec.MIN_HISTORY_D
        except Exception as exc:  # noqa: BLE001 — gate failure = out of universe
            logger.warning("gate check failed %s: %s", c["pair_name"], exc)
            return False

    async def _refresh_funding(self) -> None:
        """Pull fresh hourly funding rows per candidate perp; decide signals."""
        end = _now_ms()
        start = end - 4 * MS_D  # 4d tail covers 24h f_ann + accrual catch-up
        for c in self.cands:
            perp = c["perp"]
            try:
                rows = await asyncio.to_thread(
                    self.fetch["funding"], perp, start, end)
            except Exception as exc:  # noqa: BLE001
                logger.warning("funding pull %s failed: %s", perp, exc)
                continue
            tail = self.funding_tail[perp]
            seen = {ts for ts, _ in tail}
            for ts, r in rows:
                if ts not in seen:
                    tail.append((ts, r))
            self._accrue(c, rows)
            f_ann = self._f_ann(perp)
            if f_ann is None:
                continue
            self._neg_clock(c["pair_name"], f_ann, end)
            await self._maybe_entry(c, f_ann, end)
            self._maybe_exit(c, f_ann, end)

    def _f_ann(self, perp: str) -> Optional[float]:
        tail = list(self.funding_tail[perp])
        if len(tail) < 24:
            return None
        last24 = [r for _, r in tail[-24:]]
        return (sum(last24) / len(last24)) * 24 * 365

    def _neg_clock(self, pair: str, f_ann: float, now_ms: int) -> None:
        if f_ann < 0:
            if self.neg_since.get(pair) is None:
                self.neg_since[pair] = now_ms
        else:
            self.neg_since[pair] = None

    def _accrue(self, c: Dict[str, Any], rows: List[Tuple[int, float]]) -> None:
        ep = self.pool.get(c["pair_name"])
        if not ep:
            return
        last = ep.last_funding_ms or ep.opened_ms or ep.entry_decision_ms
        for ts, rate in rows:
            if ts > last:
                acc = ep.on_funding_row(ts, rate)
                if acc:
                    self._emit("funding", {"pair": ep.pair, "rate": rate,
                                           "accrued": acc, "q": ep.q}, ep.id)
                self.ledger.episode_update(
                    ep.id, cum_f=ep.cum_f, last_funding_ms=ep.last_funding_ms)

    async def _maybe_entry(self, c: Dict[str, Any], f_ann: float,
                           now_ms: int) -> None:
        if self.pool.dead or self.pool.get(c["pair_name"]):
            return
        if f_ann < spec.ENTRY_F_ANN or not self.in_universe.get(c["pair_name"]):
            return
        spot, perp = self.books.get(c["pair_name"]), self.books.get(c["perp"])
        if not spot or not perp or spot.mid <= 0 or perp.mid <= 0:
            return  # no live book yet — wait for next poll
        ep = Episode(pair=c["pair_name"], perp=c["perp"],
                     entry_decision_ms=now_ms, pm_branch=c["pm"],
                     maint=c["maint"], m_trigger=c["m"], m0=c["m0"])
        ep.place_entry(spot, perp)
        ep.id = self.ledger.episode_open(ep.row())
        self.pool.add(ep)
        self._emit("entry_attempt", {
            "pair": ep.pair, "perp": ep.perp, "f_ann": f_ann,
            "spot_bid": ep.legs["spot"].price,
            "perp_ask": ep.legs["perp"].price,
            "pm_branch": ep.pm_branch, "maint": ep.maint,
            "m_trigger": ep.m_trigger, "m0": ep.m0}, ep.id, now_ms)

    def _maybe_exit(self, c: Dict[str, Any], f_ann: float, now_ms: int) -> None:
        ep = self.pool.get(c["pair_name"])
        if not ep or not ep.exit_signal(f_ann, self.neg_since.get(c["pair_name"]),
                                        now_ms):
            return
        spot, perp = self.books.get(c["pair_name"]), self.books.get(c["perp"])
        if not spot or not perp or spot.mid <= 0 or perp.mid <= 0:
            return
        ep.place_exit(now_ms, spot, perp)
        self.ledger.episode_update(ep.id, state="pending_exit")
        self._emit("exit_attempt", {"pair": ep.pair, "f_ann": f_ann,
                                    "spot_ask": ep.exit_legs["spot"].price,
                                    "perp_bid": ep.exit_legs["perp"].price},
                   ep.id, now_ms)

    # ─── websocket ───────────────────────────────────────────────────────

    async def ws_loop(self) -> None:
        backoff = 2.0
        while not self._shutdown:
            try:
                async with websockets.connect(
                        self.ws_url, ping_interval=20, ping_timeout=10,
                        close_timeout=5, open_timeout=10) as ws:
                    self._ws = ws
                    backoff = 2.0
                    for coin in self.pair_by_coin:
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "l2Book", "coin": coin}}))
                    async for raw in ws:
                        self._on_ws_message(raw)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 — reconnect is the contract
                logger.warning("carry-shadow WS error: %s", exc)
            self._ws = None
            if not self._shutdown:
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    def _on_ws_message(self, raw: Any) -> None:
        try:
            payload = json.loads(raw)
            if payload.get("channel") != "l2Book":
                return
            data = payload["data"]
            levels = data.get("levels") or [[], []]
            if not levels[0] or not levels[1]:
                return
            coin = str(data["coin"])
            self.books[coin] = BookSnap(bid=float(levels[0][0]["px"]),
                                        ask=float(levels[1][0]["px"]))
            ts = int(data.get("time") or _now_ms())
            c = self.pair_by_coin.get(coin)
            if c:
                self._on_pair_book(c, ts)
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("WS parse skip: %s", exc)

    def _on_pair_book(self, c: Dict[str, Any], ts: int) -> None:
        ep = self.pool.get(c["pair_name"])
        if not ep:
            return
        spot = self.books.get(c["pair_name"])
        perp = self.books.get(c["perp"])
        if not spot or not perp:
            return
        if ep.state == "pending_entry":
            ev = ep.on_book_entry(ts, spot, perp)
            self._handle_entry_event(ep, ev, ts)
        elif ep.state == "pending_exit":
            ev = ep.on_book_exit(ts, spot, perp)
            if ev:
                self._close_episode(ep, ev, ts, "exit")

    def _handle_entry_event(self, ep: Episode, ev: Optional[str], ts: int) -> None:
        if ev is None:
            return
        if ev == "fill":
            for leg in ("spot", "perp"):
                l = ep.legs[leg]
                self.ledger.record_fill(
                    leg, True, (l.filled_at_ms - ep.entry_decision_ms) / 1000.0,
                    l.filled_at_ms or ts)
            self.ledger.episode_update(
                ep.id, state="open", opened_ms=ep.opened_ms,
                p0=ep.p0, s0=ep.s0, cost_bps=ep.cost_bps)
            self._emit("fill", {"pair": ep.pair,
                                "unlegged_s": ep.max_unlegged_s}, ep.id, ts)
        else:
            for leg in ("spot", "perp"):
                l = ep.legs[leg]
                self.ledger.record_fill(leg, bool(l.filled_at_ms),
                                        None, ts)
            self.ledger.episode_update(
                ep.id, state="aborted", close_reason=ep.close_reason,
                cost_bps=ep.cost_bps)
            self._emit(ev, {"pair": ep.pair, "reason": ep.close_reason,
                            "spot_filled": bool(ep.legs["spot"].filled_at_ms),
                            "perp_filled": bool(ep.legs["perp"].filled_at_ms)},
                       ep.id, ts)
            self.pool.remove(ep.pair)
            self._check_interim_gates()

    # ─── margin loop ─────────────────────────────────────────────────────

    def margin_sweep(self, ts: Optional[int] = None) -> None:
        ts = ts or _now_ms()
        for ep in self.pool.live():
            c = self.pair_by_coin.get(ep.perp)
            if not c:
                continue
            perp = self.books.get(ep.perp)
            spot = self.books.get(ep.pair)
            if not perp or perp.mid <= 0:
                continue
            spot_mid = spot.mid if spot else 0.0
            hist = self._px_hist.get(ep.pair)
            if hist is not None:
                hist.append((ts, perp.mid))
            prev = self._prev_zone.get(ep.pair, "ok")
            ev = ep.on_margin_tick(ts, perp.mid, spot_mid)
            zone = ep.margin_zone(perp.mid) if ep.state == "open" else prev
            self._prev_zone[ep.pair] = "ok" if ep.state != "open" else zone
            if ev == "rebalance":
                self.ledger.episode_update(
                    ep.id, q=ep.q, margin=ep.collateral,
                    cost_bps=ep.cost_bps, cum_f=ep.cum_f)
                self._emit("rebalance", {
                    "pair": ep.pair, "q": ep.q, "deleverages": ep.deleverages,
                    "adverse_1h": self._max_adverse(ep, 1, ts),
                    "adverse_2h": self._max_adverse(ep, 2, ts),
                    "mid": perp.mid}, ep.id, ts)
            elif ev == "liquidated":
                self.ledger.episode_update(ep.id, state="liquidated",
                                           close_reason="liquidated")
                kind = "gap_kill" if prev == "ok" else "liquidation"
                self._emit(kind, {
                    "pair": ep.pair, "mid": perp.mid, "prev_zone": prev,
                    "adverse_1h": self._max_adverse(ep, 1, ts),
                    "adverse_2h": self._max_adverse(ep, 2, ts)}, ep.id, ts)
                self._die(kind if kind == "gap_kill"
                          else "liquidation")
            elif ev == "delev_floor":
                self._close_episode(ep, ev, ts, "delev_floor")

    def _max_adverse(self, ep: Episode, hours: int, now_ms: int) -> Optional[float]:
        hist = self._px_hist.get(ep.pair)
        if not hist or ep.p0 <= 0:
            return None
        cutoff = now_ms - hours * MS_H
        window = [p for ts, p in hist if ts >= cutoff]
        if not window:
            return None
        return (max(window) - ep.p0) / ep.p0  # adverse = perp up for the short

    def _close_episode(self, ep: Episode, ev: str, ts: int, kind: str) -> None:
        self.ledger.episode_update(
            ep.id, state=ep.state, closed_ms=ts,
            close_reason=ep.close_reason or kind,
            realized_pnl=ep.row()["realized_pnl"], cost_bps=ep.cost_bps,
            q=ep.q, margin=ep.collateral, cum_f=ep.cum_f)
        if kind == "exit":
            for leg in ("spot", "perp"):
                l = ep.exit_legs[leg]
                self.ledger.record_fill(leg, bool(l.filled_at_ms), None, ts)
        self._emit(kind, {
            "pair": ep.pair, "net_bps": ep.net_bps(
                (self.books.get(ep.perp) or BookSnap()).mid,
                (self.books.get(ep.pair) or BookSnap()).mid),
            "deleverages": ep.deleverages,
            "max_unlegged_s": ep.max_unlegged_s,
            "reason": ep.close_reason or kind}, ep.id, ts)
        self.pool.remove(ep.pair)
        self._check_interim_gates()

    # ─── kill switch / readout gates ─────────────────────────────────────

    def _check_interim_gates(self) -> None:
        if self.pool.dead:
            return
        for leg in ("spot", "perp"):
            n, rate = self.ledger.fill_rate(leg)
            if n >= spec.FILL_RATE_MIN_ATTEMPTS and rate < spec.MIN_FILL_RATE:
                self._die(f"fill_rate:{leg}:{rate:.2f}<{spec.MIN_FILL_RATE}")
                return
        closed = self.ledger.closed_episode_n()
        if closed >= spec.EDGE_CHECK_MIN_EPS:
            net, cost = self._closed_stats()
            if cost > 0 and net < spec.EDGE_MIN_X * cost:
                self._die(f"edge:{net:.1f}<3x cost:{cost:.1f}")
                return
        if closed >= spec.READ_EPS_TARGET:
            self._final_readout()

    def _closed_stats(self) -> Tuple[float, float]:
        rows = self.ledger.closed_rows()
        n = max(1, len(rows))
        net = sum(float(r["realized_pnl"] or 0) for r in rows) * 1e4 / n
        cost = sum(float(r["cost_bps"] or 0) for r in rows) / n
        return net, cost

    def _final_readout(self) -> None:
        """>=10 clustered closed eps → evaluate the 8%/yr hurdle, once."""
        if self.ledger.meta_get("final_readout"):
            return
        rows = self.ledger.closed_rows()
        eps = [dict(zip(
            ["entry", "opened", "closed", "pnl", "cost", "pm", "m0", "state"],
            (r["entry_decision_ms"], r["opened_ms"], r["closed_ms"],
             r["realized_pnl"], r["cost_bps"], r["pm_branch"], r["m0"],
             r["state"])))
            for r in rows]
        clusters = 1 + sum(
            1 for a, b in zip(eps, eps[1:])
            if (b["entry"] - a["entry"]) > spec.CLUSTER_GAP_H * MS_H)
        n_days = max(1.0, (time.time() - eps[0]["entry"] / 1000) / 86400)
        committed = sum(1.1 if e["pm"] else 1.0 + (e["m0"] or 0.6)
                        for e in eps) / max(1, len(eps))
        ann_ret = (sum(e["pnl"] for e in eps) / max(1, len(eps))) / committed \
            / n_days * 365
        verdict = "A" if ann_ret >= spec.HURDLE_APR else "C"
        out = {"clusters": clusters, "ann_ret_committed": round(ann_ret, 4),
               "hurdle": spec.HURDLE_APR, "verdict": verdict}
        self.ledger.meta_set("final_readout", json.dumps(out))
        self._emit("final_readout", out, None)
        if verdict == "C":
            self._die("hurdle_failed")

    def _die(self, reason: str) -> None:
        if self.pool.dead:
            return
        self.ledger.meta_set("dead", reason)
        self.pool.kill(reason)
        for ep in self.pool.live():
            self.ledger.episode_update(ep.id, state="closed",
                                       close_reason="forced_stop")
        self.pool.episodes.clear()
        logger.critical("CARRY-SHADOW DEATH: %s", reason)

    # ─── misc ────────────────────────────────────────────────────────────

    def _emit(self, kind: str, data: Dict[str, Any],
              episode_id: Optional[int], ts_ms: Optional[int] = None) -> None:
        self.ledger.event(kind, data, episode_id, ts_ms)

    async def _loop(self, period_s: float, fn: Callable[[], Any]) -> None:
        while not self._shutdown:
            try:
                await fn()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 — loops must not die silently
                logger.exception("carry-shadow loop iteration failed")
            await asyncio.sleep(period_s)

    async def run(self) -> None:
        await self.boot()
        await asyncio.gather(
            self.ws_loop(),
            self._loop(spec.FUNDING_POLL_S, self._refresh_funding),
            self._loop(spec.MARGIN_CHECK_S, self._margin_async),
            self._loop(spec.GATE_REFRESH_S, self._refresh_gates),
        )

    async def _margin_async(self) -> None:
        self.margin_sweep()

    def shutdown(self) -> None:
        self._shutdown = True
        self.ledger.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    d = CarryShadowDaemon()
    try:
        asyncio.run(d.run())
    finally:
        d.shutdown()


if __name__ == "__main__":
    main()
