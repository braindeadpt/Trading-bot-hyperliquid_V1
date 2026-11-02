"""ResmomShadow daemon — weekly paper rebalance + daily marks.

Schedule (all UTC, no DST issues — everything is UTC):
  * Mon 00:05: rebalance pass — universe + signals from daily closes up
    to Sunday's close, fills at the first 1m candle open with t >= 00:05.
  * daily 00:10: NAV mark on daily closes for held names.
  * hourly  :10: funding accrual on held names (real HL fundingHistory).

Arm 'BIN' is the prereg's descriptive Binance USDT-M reference: the same
rule on Binance candles — its own top-25 (prefiltered by 24h quote
volume, ranked by true 30d mean) and beta vs BTCUSDT. Descriptive only:
excluded from the PASS/KILL gates, funding accrued from Binance too.

Zero orders. All fetchers injectable (constructor `fetch` dict).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from src.resmom_shadow import spec
from src.resmom_shadow.ledger import ResmomLedger
from src.resmom_shadow import nulls as nullmod
from src.resmom_shadow import portfolio as pf
from src.resmom_shadow import prices
from src.resmom_shadow.signal import (
    arm_b_scores, build_portfolio, compute_signals, log_returns)
from src.resmom_shadow.universe import (
    is_perp_symbol, select_universe)

logger = logging.getLogger("resmom_shadow")

MS_MIN = 60_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def week_key(now_ms: int) -> int:
    """Monday 00:00 UTC of the week containing now_ms."""
    # 1970-01-01 was a Thursday (weekday 3): days since epoch -> weekday
    days = now_ms // spec.MS_D
    dow = (days + 3) % 7                 # 0=Mon ... 6=Sun
    return (days - dow) * spec.MS_D


def default_fetch() -> Dict[str, Callable[..., Any]]:
    return {"meta": prices.hl_meta_universe,
            "c1d": prices.hl_candles_1d,
            "fill": prices.first_1m_open,
            "funding": prices.hl_funding_events,
            "mids": prices.hl_all_mids,
            "bn_klines": prices.binance_klines_1d,
            "bn_meta": None,                # set lazily (heavy)
            "bn_funding": None}


class ResmomShadowDaemon:
    LOOKBACK_D = 105                     # >= 90d history + 60d beta margin

    def __init__(self, db_path: str = spec.DB_PATH,
                 fetch: Optional[Dict[str, Any]] = None) -> None:
        self.ledger = ResmomLedger(db_path)
        self.fetch = fetch or default_fetch()
        self._shutdown = False
        # Binance universe fetcher for arm BIN — lazy default
        if self.fetch.get("bn_meta") is None:
            self.fetch["bn_meta"] = self._bn_meta
        if self.fetch.get("bn_funding") is None:
            self.fetch["bn_funding"] = self._bn_funding

    # ── scheduling ────────────────────────────────────────────────────

    def _rebalance_due(self, now_ms: int) -> bool:
        wk = week_key(now_ms)
        if wk < spec.READ_FIRST_MS:
            return False         # Amendment 1: no rebalance before READ_FIRST
        if now_ms < wk + spec.REBALANCE_MIN_UTC * MS_MIN:
            return False
        # A/B missing -> due now; BIN (descriptive) retries hourly so a
        # persistently failing Binance leg doesn't hammer the API
        if self.ledger.rebalance_get(wk, "A") is None:
            return True
        if self.ledger.rebalance_get(wk, "B") is None:
            return True
        if self.ledger.rebalance_get(wk, "BIN") is None:
            last_try = int(self.ledger.meta_get("bin_retry_hour") or 0)
            return last_try < now_ms // spec.MS_H
        return False

    def _mark_due(self, now_ms: int) -> bool:
        day = now_ms // spec.MS_D
        if (now_ms % spec.MS_D) < spec.MARK_MIN_UTC * MS_MIN:
            return False
        if self._rebalance_due(now_ms):
            return False                 # rebalance marks at entry
        last = int(self.ledger.meta_get("last_mark_day") or 0)
        return last < day

    def _funding_due(self, now_ms: int) -> bool:
        hour = now_ms // spec.MS_H
        if (now_ms % spec.MS_H) < spec.FUNDING_MIN_UTC * MS_MIN:
            return False
        last = int(self.ledger.meta_get("last_funding_hour") or 0)
        return last < hour

    # ── Binance helpers (descriptive arm) ─────────────────────────────

    def _bn_meta(self) -> List[str]:
        """USDT-M perp symbols, prefiltered by 24h quote volume top-60."""
        d = prices.fetch_json(
            "https://fapi.binance.com/fapi/v1/exchangeInfo")
        syms = [s["symbol"] for s in (d.get("symbols") or [])
                if s.get("contractType") == "PERPETUAL"
                and s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"]
        tick = prices.fetch_json(
            "https://fapi.binance.com/fapi/v1/ticker/24hr")
        vol = {t["symbol"]: float(t.get("quoteVolume") or 0)
               for t in (tick or [])}
        syms.sort(key=lambda s: -vol.get(s, 0.0))
        return syms[:60]

    def _bn_funding(self, symbol: str, t0: int, t1: int) -> List[tuple]:
        from src.boros_shadow.funding import fetch_funding
        return fetch_funding("binance", symbol[:-4] if symbol.endswith("USDT")
                             else symbol, t0, t1) or []

    # ── data assembly ─────────────────────────────────────────────────

    def _hl_candles_all(self, cutoff_ms: int) -> Dict[str, List[dict]]:
        """One 1d-candle fetch per main-dex perp (universe + signals)."""
        names = [n for n in self.fetch["meta"]() if is_perp_symbol(n)]
        out: Dict[str, List[dict]] = {}
        for n in names:
            try:
                out[n] = self.fetch["c1d"](n, self.LOOKBACK_D, cutoff_ms)
            except Exception:  # noqa: BLE001
                logger.warning("resmom: candle fetch failed for %s", n)
        return out

    def _bn_candles_all(self, cutoff_ms: int) -> Dict[str, List[dict]]:
        out: Dict[str, List[dict]] = {}
        for n in self.fetch["bn_meta"]():
            try:
                out[n] = self.fetch["bn_klines"](n, self.LOOKBACK_D,
                                                cutoff_ms)
            except Exception:  # noqa: BLE001
                logger.warning("resmom: binance kline failed for %s", n)
        return out

    # ── passes ────────────────────────────────────────────────────────

    def _fills(self, symbols: List[str], week_key_ms: int
               ) -> Dict[str, float]:
        t_fill = week_key_ms + spec.FILL_LAG_S * 1000
        out: Dict[str, float] = {}
        for s in symbols:
            try:
                px = self.fetch["fill"](s, t_fill)
            except Exception:  # noqa: BLE001
                px = None
            if px is not None:
                out[s] = px
        return out

    def _arm_pass(self, week_key_ms: int, arm: str,
                  universe_syms: List[str],
                  signals: Dict[str, Any],
                  score: Dict[str, float],
                  prices_map: Dict[str, float],
                  universe_meta: Any,
                  btc_sym: str = spec.BTC) -> Dict[str, Any]:
        """Rank -> portfolio -> row -> close/open legs. Returns stats."""
        ranked = sorted((s for s in universe_syms if s in score),
                        key=lambda s: -score[s])
        leg_pool = [s for s in ranked if s != btc_sym]
        port = build_portfolio(leg_pool, signals, len(universe_syms))
        targets = dict(port.weights)

        entry_ms = week_key_ms + spec.FILL_LAG_S * 1000
        inputs = {"universe": universe_syms, "signals":
                  {s: {"beta": sg.beta, "r15": sg.resid15, "v": sg.vol30,
                       "s": sg.s} for s, sg in signals.items()},
                  "weights": targets, "fills": prices_map}
        sha = hashlib.sha256(json.dumps(
            inputs, sort_keys=True, default=str).encode()).hexdigest()
        rid = self.ledger.rebalance_insert({
            "week_key_ms": week_key_ms, "arm": arm,
            "created_at_ms": _now_ms(),
            "universe_json": json.dumps(universe_meta, default=str),
            "signals_json": json.dumps(inputs["signals"], sort_keys=True),
            "weights_json": json.dumps(targets, sort_keys=True),
            "turnover_usd": 0.0, "cost_usd": 0.0,
            "flags_json": json.dumps(port.flags), "inputs_sha256": sha})
        prev = self.ledger.positions_open(arm)
        st = pf.apply_rebalance(self.ledger, arm, week_key_ms, rid, prev,
                                targets, prices_map, entry_ms)
        self.ledger.conn.execute(
            "UPDATE resmom_rebalances SET turnover_usd=?, cost_usd=? WHERE id=?",
            (st["turnover_usd"], st["cost_usd"], rid))
        self.ledger.conn.commit()
        self.ledger.log("rebalance", {"arm": arm, "n_targets": len(targets),
                                      "skipped": st["skipped_no_fill"],
                                      "turnover": st["turnover_usd"],
                                      "cost": st["cost_usd"],
                                      "flags": port.flags}, rid, _now_ms())
        return {"rebalance_id": rid, **st, "flags": port.flags}

    def rebalance_pass(self, now_ms: Optional[int] = None) -> Dict[str, Any]:
        now_ms = now_ms or _now_ms()
        wk = week_key(now_ms)
        cutoff = wk                # data ends at Sunday's close = Mon 00:00
        logger.info("resmom: rebalance pass week=%s", wk)

        candles = self._hl_candles_all(cutoff)
        universe, too_young = select_universe(candles, cutoff)
        uni_syms = [e.symbol for e in universe]
        returns = {s: log_returns([c["c"] for c in candles.get(s, [])])
                   for s in set(uni_syms) | {spec.BTC}}
        r_btc = log_returns([c["c"] for c in candles.get(spec.BTC, [])])
        signals = compute_signals(returns, r_btc)
        s_a = {k: v.s for k, v in signals.items()}
        s_b = arm_b_scores(signals)

        # fills for every symbol any arm/null can touch
        fill_syms = list(set(uni_syms) |
                         {p["symbol"] for a in pf.ARMS
                          for p in self.ledger.positions_open(a)
                          if a in ("A", "B")})
        fills = self._fills(fill_syms, wk)
        umeta = [{"sym": e.symbol, "vol30": e.vol30_notional,
                  "hist_d": e.history_d} for e in universe]
        stats = {"week": wk, "universe_n": len(uni_syms),
                 "too_young": too_young, "arms": {}}
        for arm, score in (("A", s_a), ("B", s_b)):
            if self.ledger.rebalance_get(wk, arm) is not None:
                continue
            st = self._arm_pass(wk, arm, uni_syms, signals, score,
                                fills, umeta)
            stats["arms"][arm] = st
            pf.mark_nav(self.ledger, arm, wk + spec.FILL_LAG_S * 1000,
                        fills)

        # nulls: close last week's books on these fills, open new ones
        prev_wk = wk - spec.MS_WEEK
        scored = nullmod.score_week(self.ledger, prev_wk, fills)
        prev_navs = self.ledger.nulls_latest_navs()
        prev_w = nullmod.prev_weights(self.ledger, prev_wk)
        rid_a = (self.ledger.rebalance_get(wk, "A") or {}).get("id", 0)
        if self.ledger.count("resmom_nulls",
                             f"WHERE week_key_ms={wk}") == 0:
            n_null = nullmod.generate_week(
                self.ledger, rid_a, wk, uni_syms, signals, fills,
                prev_w, prev_navs)
        else:
            n_null = 0
        stats["nulls"] = {"scored": scored, "written": n_null}

        # descriptive Binance arm — same rule on Binance candles
        try:
            bstats = self._binance_pass(wk, cutoff)
            stats["arms"]["BIN"] = bstats
        except Exception:  # noqa: BLE001
            logger.exception("resmom: binance arm failed")

        self.ledger.meta_set("last_mark_day", str(wk // spec.MS_D))
        self.ledger.log("rebalance_pass", stats)
        logger.info("resmom: rebalance done %s", {
            k: v for k, v in stats.items() if k != "arms"})
        return stats

    def _binance_pass(self, wk: int, cutoff: int) -> Dict[str, Any]:
        candles = self._bn_candles_all(cutoff)
        universe, too_young = select_universe(
            {s: c for s, c in candles.items()
             if s.endswith("USDT")}, cutoff)
        uni_syms = [e.symbol for e in universe]
        if not uni_syms:
            return {"skipped": "empty_universe"}
        btc_sym = "BTCUSDT"
        returns = {s: log_returns([c["c"] for c in candles.get(s, [])])
                   for s in set(uni_syms) | {btc_sym}}
        r_btc = log_returns([c["c"] for c in candles.get(btc_sym, [])])
        signals = compute_signals(returns, r_btc)
        s_a = {k: v.s for k, v in signals.items()}

        # fills cover the fresh universe AND already-open BIN legs
        t_fill = wk + spec.FILL_LAG_S * 1000
        held = {p["symbol"] for p in self.ledger.positions_open("BIN")}
        fills = self._bn_fills(sorted(set(uni_syms) | held), t_fill)

        umeta = [{"sym": e.symbol, "vol30": e.vol30_notional,
                  "hist_d": e.history_d} for e in universe]
        if self.ledger.rebalance_get(wk, "BIN") is None:
            st = self._arm_pass(wk, "BIN", uni_syms, signals, s_a,
                                fills, umeta, btc_sym=btc_sym)
            pf.mark_nav(self.ledger, "BIN",
                        wk + spec.FILL_LAG_S * 1000, fills)
            st["too_young"] = too_young
            return st
        return {"skipped": "already rebalanced"}

    def _bn_fills(self, syms: List[str], t_fill: int) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for s in syms:
            try:
                rows = prices.fetch_json(
                    f"{spec.BINANCE_KLINES}?symbol={s}&interval=1m"
                    f"&startTime={t_fill}&endTime={t_fill + 10 * MS_MIN}"
                    "&limit=10")
                rows = [r for r in rows or [] if int(r[0]) >= t_fill]
                if rows:
                    out[s] = float(rows[0][1])
            except Exception:  # noqa: BLE001
                continue
        return out

    # ── daily mark + hourly funding ───────────────────────────────────

    def daily_mark(self, now_ms: int) -> None:
        day = now_ms // spec.MS_D
        cutoff = day * spec.MS_D
        for arm in pf.ARMS:
            syms = {p["symbol"] for p in self.ledger.positions_open(arm)}
            if not syms:
                continue
            fetch = self.fetch["bn_klines"] if arm == "BIN" \
                else self.fetch["c1d"]
            mids: Dict[str, float] = {}
            for s in sorted(syms):
                try:
                    rows = fetch(s, 3, cutoff)
                except Exception:  # noqa: BLE001
                    continue
                rows = [r for r in rows if r["t"] < cutoff]
                if rows:
                    mids[s] = rows[-1]["c"]      # close AT 00:00 UTC
            pf.mark_nav(self.ledger, arm, now_ms, mids)
            k = pf.check_kill(self.ledger, arm, now_ms)
            if k:
                logger.warning("resmom: KILL flag arm=%s dd=%.3f", arm,
                               k["dd"])
        self.ledger.meta_set("last_mark_day", str(day))

    def funding_pass(self, now_ms: int) -> None:
        t1 = now_ms
        for arm in pf.ARMS:
            open_syms = {p["symbol"] for p in self.ledger.positions_open(arm)}
            if not open_syms:
                continue
            last_ms = int(self.ledger.meta_get(
                f"funding_from_{arm}") or (now_ms - spec.MS_D))
            if arm == "BIN":
                mids = {}
                try:
                    tick = prices.fetch_json(
                        "https://fapi.binance.com/fapi/v1/ticker/price")
                    px = {t["symbol"]: float(t["price"]) for t in tick or []}
                    mids = {s: px[s] for s in open_syms if s in px}
                except Exception:  # noqa: BLE001
                    pass
                ev = {}
                for s in open_syms:
                    try:
                        ev[s] = self.fetch["bn_funding"](s, last_ms, t1)
                    except Exception:  # noqa: BLE001
                        ev[s] = []
            else:
                mids = self.fetch["mids"]()
                ev = {}
                for s in open_syms:
                    try:
                        ev[s] = self.fetch["funding"](s, last_ms, t1)
                    except Exception:  # noqa: BLE001
                        ev[s] = []
            applied = pf.accrue_funding(self.ledger, arm, now_ms, ev, mids)
            if applied:
                logger.info("resmom: funding arm=%s %s", arm, applied)
            self.ledger.meta_set(f"funding_from_{arm}", str(t1))
        self.ledger.meta_set("last_funding_hour", str(now_ms // spec.MS_H))

    # ── loop ──────────────────────────────────────────────────────────

    def tick(self, now_ms: Optional[int] = None) -> None:
        now_ms = now_ms or _now_ms()
        try:
            if self._rebalance_due(now_ms):
                self.rebalance_pass(now_ms)
            elif self._mark_due(now_ms):
                self.daily_mark(now_ms)
            if self._funding_due(now_ms):
                self.funding_pass(now_ms)
        except Exception:  # noqa: BLE001 — a failed pass never kills loop
            logger.exception("resmom: pass failed")

    async def boot(self) -> None:
        self.ledger.meta_incr("restart_count")
        self._heartbeat()

    def _heartbeat(self) -> None:
        self.ledger.meta_set("hb_ts_ms", str(_now_ms()))
        self.ledger.meta_set("counts", json.dumps({
            "rebalances": self.ledger.count("resmom_rebalances"),
            "open_positions": self.ledger.count(
                "resmom_positions", "WHERE status='open'")}))

    async def run(self) -> None:
        import asyncio
        await self.boot()
        last_hb = 0.0
        while not self._shutdown:
            try:
                self.tick()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("resmom: tick failed")
            if time.time() - last_hb >= spec.HEARTBEAT_S:
                self._heartbeat()
                last_hb = time.time()
            await asyncio.sleep(spec.TICK_S)

    def shutdown(self) -> None:
        self._shutdown = True
        self.ledger.close()
