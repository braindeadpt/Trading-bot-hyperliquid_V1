"""Funding-carry Phase 1 (historical) — notes/fundcarry-prereg-2026-11-21.md.

Stages:
  1. download (candles, meta, funding) -> data/backtests/fundcarry/raw/*.json
     (raw payloads cached; --skip-download uses only the cache)
  2. backtest + verdict -> data/backtests/fundcarry/phase1_result.json

Nothing here touches settings.yaml, the engine, bot.db, daemons or ledgers.
Operational definitions are fixed in DECISIONS (written to
data/backtests/fundcarry/DECISIONS_FROZEN.txt BEFORE the backtest stage runs).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.utils.helpers import safe_float, safe_json_loads   # noqa: E402
from src.research.fundcarry import core                      # noqa: E402
from src.resmom_shadow import spec as rspec                  # noqa: E402
from src.resmom_shadow.read import percentile, sharpe_annual  # noqa: E402
from src.resmom_shadow.signal import (                       # noqa: E402
    build_portfolio, compute_signals, pct_ranks)
from src.resmom_shadow.universe import select_universe       # noqa: E402

OUT_DIR = ROOT / "data" / "backtests" / "fundcarry"
RAW = OUT_DIR / "raw"
HL_INFO = "https://api.hyperliquid.xyz/info"
PAUSE_S = 0.6
NOTIONAL = rspec.NOTIONAL_USD
COST = rspec.COST_BPS_SIDE / 1e4
MS_D, MS_H = core.MS_D, core.MS_H
UNIVERSE_N = rspec.UNIVERSE_TOP_N

DECISIONS = """\
FUNDCARRY PHASE 1 - operational definitions (fixed BEFORE any result was computed)
1. Universe PIT: each Monday D, candidates = main-dex perps in meta (INCLUDING
   isDelisted=True). Eligible iff: not stable / no 'dex:' prefix; history
   (D - first 1d candle with trades n>0) >= 90 d; all 30 daily candles
   D-30..D-1 present (so a coin delisted before D drops out). Ranked by
   resmom_shadow.universe.select_universe (mean close*volume, 30 d), top 25.
   BTC ranks in, is never a leg. A week is VALID only if exactly 25 eligible
   names exist; first rebalance = first valid week.
2. Signal needs: >= 61 finite daily closes (60 returns for beta, vol30,
   resid15 via resmom_shadow.signal.compute_signals) AND >= 120 hourly funding
   observations in the 7 d before D. Names lacking either are not leg
   candidates that week. s = -mean(hourly funding, [D-7d, D)).
3. Ranking ties broken by symbol (asc). Quintile = floor(0.2*25)=5 per leg
   (resmom build_portfolio; min(n_legs, pool//2)). 1/vol30 weights, gross 0.5
   per leg through beta-neutral solve as in resmom portfolio.py (unsolvable ->
   0.5/0.5), THEN the 10% cap: |w|>0.10 (of GROSS_TOTAL=1.0) is cut and the
   excess goes to the other legs of the same side proportionally to their
   current weights, iterated until none exceeds. If every leg of a side is at
   the cap the leftover excess is DROPPED (side gross < pre-cap).
4. Fill fidelity: entry = open of the Monday 1d candle, exit = open of the next
   Monday (forward uses 1m open after 00:05; HL serves only the last 5000 1m
   candles). If a coin has no candle at that open (delisted mid-week) the last
   available close (<=10 d back) is used.
5. Accounting (resmom precedent): fixed gross NOTIONAL 10,000 USD; weekly
   return = (price pnl + funding pnl - cost) / running NAV; NAV compounds with
   pnl. Cost = 6.5 bps * sum|w_new - w_prev| * 10,000 charged at the weekly
   rebalance (first week = full entry). No exit cost after the last week.
6. Funding: real hourly HL fundingHistory events with entry <= t < exit; paid
   on notional qty*price(t), price(t) = linear interpolation of daily opens.
   Long pays rate>0, short receives. A held coin-week with no funding events
   is counted and reported (contributes 0).
7. Windows: 30-d non-overlapping from the first valid rebalance; week -> window
   floor((rebalance - first)/30d). Window COMPLETE iff its end <= exit of the
   last closed week. K = complete windows; K < 4 -> INCONCLUSIVE. Window
   return = compounded weekly net returns of its weeks. Last closed week:
   rebalance + 7d <= most recent Monday 00:00 UTC.
8. Sharpe = sqrt(52)*mean/std(ddof=1) of ALL closed weekly net returns.
   Nulls: 200 random-direction books per week (random 2*n_legs names from that
   week's pool, first half long; resmom nulls.py method) through the same
   weights/cap/costs/FUNDING, seed sha256('fundcarry-null|<week_ms>|<i>').
   PASS cond.2 needs Sharpe > 0.5 AND > p95 (linear interpolation, resmom
   percentile) of the 200 null Sharpes.
9. Sign-flip: one-sided on the K window returns, p = share of the 2^K sign
   patterns with sum >= observed (ties count; exact K<=12). K>12: 200,000
   seeded samples (seed 20261121), p=(count+1)/(N+1). Majority = positives >
   K/2. Cond.3 = majority AND p <= 0.10.
10. Ex-top-3 (resmom read.py): sum over symbols of (price pnl + funding pnl)
    minus the 3 largest symbol sums. Costs are not allocated (precedent);
    a cost-allocated variant is reported as a diagnostic only.
11. Survivorship: controlled iff the API returns candles AND funding for
    isDelisted coins and >=1 of them enters a weekly universe. If not
    controlled: FAIL stays FAIL, PASS becomes 'PASS-provisorio: sobrevivencia
    nao controlada'. Meta-completeness (coins removed from meta entirely)
    cannot be verified and is reported as a caveat.
12. Verdict: all 4 criteria with K>=4 -> PASS; any failure -> KILL (C);
    K<4 -> INCONCLUSIVO. No second round.
13. ResMom diagnostic: arm A (resmom signal.py: resid15/vol30, frozen
    build_portfolio, no cap) on the same history, fills, costs and funding.
"""


# ── HTTP (public Hyperliquid info only) ─────────────────────────────────────
def hl_post(payload: Dict[str, Any], tries: int = 9) -> Any:
    body = json.dumps(payload).encode()
    delay = 3.0
    for attempt in range(tries):
        time.sleep(PAUSE_S)
        req = urllib.request.Request(
            HL_INFO, data=body, headers={
                "Content-Type": "application/json",
                "User-Agent": "fundcarry-research"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                logging.warning("HL HTTP %d, backoff %.0fs", e.code, delay)
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logging.warning("HL net error %r, backoff %.0fs", e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 120)
    raise RuntimeError(f"HL request failed after {tries} tries: {payload}")


def _w(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    tmp.replace(path)


def _r(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def last_monday_ms(now_ms: int) -> int:
    d = now_ms // MS_D
    return (d - ((d + 3) % 7)) * MS_D       # epoch day 0 was a Thursday


# ── stage 1a: meta + candles ────────────────────────────────────────────────
def download_candles() -> None:
    mf = RAW / "manifest.json"
    if mf.exists():
        man = _r(mf)
    else:
        now = int(time.time() * 1000)
        man = {"as_of_ms": now, "last_monday_ms": last_monday_ms(now)}
        _w(mf, man)
    meta_p = RAW / "meta.json"
    if not meta_p.exists():
        _w(meta_p, hl_post({"type": "meta"}))
    meta = _r(meta_p)
    for u in meta["universe"]:
        name = u["name"]
        p = RAW / "candles" / f"{name}.json"
        if p.exists():
            continue
        print(f"candles {name}", flush=True)
        rows = hl_post({"type": "candleSnapshot", "req": {
            "coin": name, "interval": "1d", "startTime": 0,
            "endTime": man["as_of_ms"]}})
        _w(p, rows)


# ── data loading ────────────────────────────────────────────────────────────
class Data:
    def __init__(self) -> None:
        self.man = _r(RAW / "manifest.json")
        meta = _r(RAW / "meta.json")["universe"]
        self.delisted = {u["name"] for u in meta if u.get("isDelisted")}
        self.names = [u["name"] for u in meta if ":" not in u["name"]]
        self.last_monday = int(self.man["last_monday_ms"])
        raw: Dict[str, List[dict]] = {}
        d_min = None
        for n in self.names:
            p = RAW / "candles" / f"{n}.json"
            if not p.exists():
                continue
            rows = _r(p)
            if rows:
                raw[n] = rows
                m = min(int(r["t"]) for r in rows) // MS_D
                d_min = m if d_min is None else min(d_min, m)
        self.d0 = d_min
        self.d1 = self.last_monday // MS_D
        T = self.d1 - self.d0 + 1
        self.T = T
        self.C: Dict[str, np.ndarray] = {}
        self.O: Dict[str, np.ndarray] = {}
        self.V: Dict[str, np.ndarray] = {}
        self.first_active: Dict[str, int] = {}
        self.candle_count: Dict[str, int] = {}
        for n, rows in raw.items():
            c = np.full(T, np.nan)
            o = np.full(T, np.nan)
            v = np.full(T, np.nan)
            fa = None
            for r in rows:
                di = int(r["t"]) // MS_D - self.d0
                if di < 0 or di >= T:
                    continue
                c[di] = float(r["c"])
                o[di] = float(r["o"])
                v[di] = float(r["v"])
                if int(r.get("n", 1)) > 0 and (fa is None or di < fa):
                    fa = di
            self.C[n], self.O[n], self.V[n] = c, o, v
            self.candle_count[n] = len(rows)
            if fa is not None:
                self.first_active[n] = fa
        self.fund_t: Dict[str, np.ndarray] = {}
        self.fund_r: Dict[str, np.ndarray] = {}
        fdir = RAW / "funding"
        if fdir.exists():
            for p in fdir.glob("*.json"):
                d = _r(p)
                rows = sorted({(int(x["time"]), float(x["fundingRate"]))
                               for x in d["rows"]})
                self.fund_t[d["coin"]] = np.array([a for a, _ in rows],
                                                  dtype=np.int64)
                self.fund_r[d["coin"]] = np.array([b for _, b in rows])
        self._tab: Dict[Tuple[str, int], Tuple[float, float, int]] = {}
        self.fallback_px = 0
        self.no_funding_held = 0

    # price at the OPEN of day index d (fallback: last close <= d-1)
    def px(self, sym: str, day: int) -> Optional[float]:
        d = day - self.d0              # `day` is the absolute epoch day
        if d >= self.T:
            return None
        o = self.O[sym][d]
        if np.isfinite(o) and o > 0:
            return float(o)
        for k in range(d - 1, max(d - 11, -1), -1):
            c = self.C[sym][k]
            if np.isfinite(c) and c > 0:
                self.fallback_px += 1
                return float(c)
        return None

    def week_RF(self, sym: str, dw: int) -> Optional[Tuple[float, float]]:
        """(price return R, funding fraction F) over [dw, dw+7) in days."""
        key = (sym, dw)
        if key in self._tab:
            t = self._tab[key]
            return (t[0], t[1])
        e, x = self.px(sym, dw), self.px(sym, dw + 7)
        if e is None or x is None:
            return None
        R = x / e - 1.0
        F = 0.0
        ft = self.fund_t.get(sym)
        if ft is not None and len(ft):
            a = int(np.searchsorted(ft, dw * MS_D, "left"))
            b = int(np.searchsorted(ft, (dw + 7) * MS_D, "left"))
            if b > a:
                o = self.O[sym]
                ok = np.isfinite(o) & (o > 0)
                tt = (np.arange(self.T) + self.d0) * MS_D
                pxe = np.interp(ft[a:b].astype(float), tt[ok].astype(float),
                                o[ok])
                F = float((self.fund_r[sym][a:b] * pxe).sum() / e)
            else:
                self.no_funding_held += 1
        else:
            self.no_funding_held += 1
        self._tab[key] = (R, F, 0)
        return (R, F)


# ── universe ────────────────────────────────────────────────────────────────
def universe_at(D: Data, dw: int) -> List[Any]:
    cutoff = dw * MS_D
    cands: Dict[str, List[dict]] = {}
    for n, fa in D.first_active.items():
        if dw - (D.d0 + fa) < rspec.MIN_HISTORY_D:
            continue
        i = dw - D.d0
        c = D.C[n][i - 30:i]
        v = D.V[n][i - 30:i]
        if not (np.all(np.isfinite(c)) and np.all(np.isfinite(v))):
            continue
        rows = [{"t": (D.d0 + fa) * MS_D, "c": float(D.C[n][fa]),
                 "v": float(D.V[n][fa])}]
        rows += [{"t": (dw - 30 + j) * MS_D, "c": float(c[j]),
                  "v": float(v[j])} for j in range(30)]
        cands[n] = rows
    uni, _ = select_universe(cands, cutoff)
    return uni


def compute_universes(D: Data) -> Dict[int, List[str]]:
    out: Dict[int, List[str]] = {}
    for dw in range(D.d0 + 90, D.d1 - 6):
        if (dw + 3) % 7 != 0:               # Mondays only
            continue
        uni = universe_at(D, dw)
        if len(uni) == UNIVERSE_N:
            out[dw] = [e.symbol for e in uni]
    return out


# ── stage 1b: funding for coins that ever enter a universe ──────────────────
def download_funding(D: Data, uni: Dict[int, List[str]]) -> None:
    first: Dict[str, int] = {}
    for dw in sorted(uni):
        for s in uni[dw]:
            first.setdefault(s, dw)
    end_ms = D.last_monday
    for s in sorted(first):
        p = RAW / "funding" / f"{s}.json"
        if p.exists():
            continue
        start = (first[s] - 8) * MS_D
        print(f"funding {s} from day {first[s]}", flush=True)
        rows: List[dict] = []
        cur = start
        while True:
            d = hl_post({"type": "fundingHistory", "coin": s,
                         "startTime": cur, "endTime": end_ms})
            if not d:
                break
            rows.extend(d)
            if len(d) < 500:
                break
            nxt = int(d[-1]["time"]) + 1
            if nxt <= cur:
                break
            cur = nxt
        _w(p, {"coin": s, "start_ms": start, "end_ms": end_ms,
               "rows": rows})


# ── backtest ────────────────────────────────────────────────────────────────
def turnover(prev: Dict[str, float], new: Dict[str, float]) -> float:
    return sum(abs(new.get(s, 0.0) - prev.get(s, 0.0))
               for s in set(prev) | set(new))


def week_pnl(D: Data, w: Dict[str, float], dw: int
             ) -> Optional[Tuple[float, float, Dict[str, Tuple[float, float]]]]:
    price = fund = 0.0
    per: Dict[str, Tuple[float, float]] = {}
    for s, wt in w.items():
        rf = D.week_RF(s, dw)
        if rf is None:
            return None
        p = wt * rf[0] * NOTIONAL
        f = -wt * rf[1] * NOTIONAL
        price += p
        fund += f
        per[s] = (p, f)
    return price, fund, per


def run(D: Data, uni: Dict[int, List[str]]) -> Dict[str, Any]:
    weeks = sorted(uni)
    weeks = [dw for dw in weeks if (dw + 7) <= D.d1]
    if not weeks:
        # No completed week can be simulated (empty universe or all
        # beyond the candle end) — evaluate() maps this to INCONCLUSIVO.
        return {"rows": [], "nav": NOTIONAL, "nav_resmom": NOTIONAL,
                "sym_pnl": {}, "sym_cost": {}, "sym_weeks": {},
                "nulls_ret": [[] for _ in range(core.N_NULLS)],
                "corr_sig": [], "legs_overlap": [], "cap_bind": 0,
                "flags": {}, "skipped": [], "first_dw": None,
                "fund_split": None, "fund_split_weekly": []}
    first_dw = weeks[0]
    nav = NOTIONAL
    nav_r = NOTIONAL
    prev_w: Dict[str, float] = {}
    prev_wr: Dict[str, float] = {}
    nulls_prev = [dict() for _ in range(core.N_NULLS)]
    nulls_nav = [NOTIONAL] * core.N_NULLS
    nulls_ret: List[List[float]] = [[] for _ in range(core.N_NULLS)]
    rows: List[dict] = []
    sym_pnl: Dict[str, float] = {}
    sym_cost: Dict[str, float] = {}
    sym_weeks: Dict[str, int] = {}
    corr_sig: List[float] = []
    legs_overlap: List[float] = []
    cap_bind = 0
    gross_after: List[float] = []
    flags_ct: Dict[str, int] = {}
    skipped: List[dict] = []
    # Prereg line 62 diagnostic: funding received vs paid, by leg side.
    # Accumulation only — touches none of the existing result fields.
    fund_split: Dict[str, Dict[str, float]] = {
        "long": {"received": 0.0, "paid": 0.0},
        "short": {"received": 0.0, "paid": 0.0}}
    fund_split_weekly: List[dict] = []

    for dw in weeks:
        cutoff = dw * MS_D
        i = dw - D.d0
        syms = uni[dw]
        ret: Dict[str, np.ndarray] = {}
        for s in set(syms) | {rspec.BTC}:
            c = D.C[s][i - 61:i] if s in D.C else None
            if c is None or len(c) < 61 or not np.all(np.isfinite(c)) \
                    or np.any(c <= 0):
                continue
            ret[s] = np.diff(np.log(c))
        if rspec.BTC not in ret:
            skipped.append({"dw": dw, "why": "btc_returns"})
            continue
        sigs = compute_signals(ret, ret[rspec.BTC])
        # carry scores (funding must exist for the week's coin)
        carry: Dict[str, float] = {}
        for s in syms:
            if s == rspec.BTC or s not in sigs or s not in D.fund_t:
                continue
            ft = D.fund_t[s]
            a = int(np.searchsorted(ft, cutoff - 7 * MS_D, "left"))
            b = int(np.searchsorted(ft, cutoff, "left"))
            sc = core.carry_score(
                list(zip(ft[a:b].tolist(), D.fund_r[s][a:b].tolist())),
                cutoff)
            if sc is not None:
                carry[s] = sc
        pool = sorted(carry)
        if len(pool) < 10:
            skipped.append({"dw": dw, "why": "pool<10", "n": len(pool)})
            continue
        sub = {s: sigs[s] for s in pool}
        ranked = sorted(pool, key=lambda s: (-carry[s], s))
        port = build_portfolio(ranked, sub, UNIVERSE_N)
        for fl in port.flags:
            flags_ct[fl] = flags_ct.get(fl, 0) + 1
        pre = dict(port.weights)
        w = core.apply_cap(pre)
        if any(abs(pre[s]) > core.CAP_PER_COIN + 1e-12 for s in pre):
            cap_bind += 1
        gross_after.append(sum(abs(x) for x in w.values()))
        res = week_pnl(D, w, dw)
        if res is None:
            skipped.append({"dw": dw, "why": "no_px"})
            continue
        price, fund, per = res
        tover = turnover(prev_w, w)
        cost = tover * NOTIONAL * COST
        pnl = price + fund - cost
        r = pnl / nav
        nav += pnl
        wk_rec = wk_paid = 0.0
        for s, (p_, f_) in per.items():
            sym_pnl[s] = sym_pnl.get(s, 0.0) + p_ + f_
            sym_weeks[s] = sym_weeks.get(s, 0) + 1
            leg = "long" if w[s] > 0 else "short"
            if f_ >= 0.0:
                fund_split[leg]["received"] += f_
                wk_rec += f_
            else:
                fund_split[leg]["paid"] += -f_
                wk_paid += -f_
        tot_abs = sum(abs(x) for x in set(w) | set(prev_w)
                      for x in [w.get(x, 0.0) - prev_w.get(x, 0.0)]) or 1.0
        for s in set(w) | set(prev_w):
            d_ = abs(w.get(s, 0.0) - prev_w.get(s, 0.0))
            sym_cost[s] = sym_cost.get(s, 0.0) \
                + cost * d_ / tot_abs
        prev_w = w

        # ResMom arm A on the same data (no cap, frozen build)
        s_a = {s: sigs[s].s for s in sigs if s != rspec.BTC and s in sub}
        ranked_a = sorted(s_a, key=lambda s: (-s_a[s], s))
        port_a = build_portfolio(ranked_a, sub, UNIVERSE_N)
        res_a = week_pnl(D, port_a.weights, dw)
        r_a = None
        if res_a is not None:
            c_a = turnover(prev_wr, port_a.weights) * NOTIONAL * COST
            pnl_a = res_a[0] + res_a[1] - c_a
            r_a = pnl_a / nav_r
            nav_r += pnl_a
            prev_wr = dict(port_a.weights)
        # signal correlation (rank corr over common leg-candidates)
        common = [s for s in pool if s in s_a]
        sp = core.spearman([carry[s] for s in common],
                           [s_a[s] for s in common])
        if sp is not None:
            corr_sig.append(sp)
        lc = {s for s, x in w.items() if x > 0}
        la = {s for s, x in port_a.weights.items() if x > 0}
        sc_ = {s for s, x in w.items() if x < 0}
        sa_ = {s for s, x in port_a.weights.items() if x < 0}
        legs_overlap.append(
            (len(lc & la) + len(sc_ & sa_)) / max(1, len(lc) + len(sc_)))

        # 200 nulls (same pool / weights / cap / costs / funding)
        for k in range(core.N_NULLS):
            seed = core.null_seed(dw * MS_D, k)
            n_legs = min(max(1, int(rspec.QUINTILE_FRAC * UNIVERSE_N)),
                         len(pool) // 2) or 1
            rng = np.random.default_rng(int(seed[:16], 16))
            picks = [str(x) for x in rng.choice(pool, size=2 * n_legs,
                                                replace=False)]
            pn = core.apply_cap(build_portfolio(picks, sub,
                                                UNIVERSE_N).weights)
            rn = week_pnl(D, pn, dw)
            if rn is None:
                continue
            cn = turnover(nulls_prev[k], pn) * NOTIONAL * COST
            pl = rn[0] + rn[1] - cn
            nulls_ret[k].append(pl / nulls_nav[k])
            nulls_nav[k] += pl
            nulls_prev[k] = pn

        rows.append({
            "dw": dw, "week_ms": cutoff,
            "date": dt.datetime.fromtimestamp(
                cutoff / 1000, dt.timezone.utc).strftime("%Y-%m-%d"),
            "ret": r, "price_usd": price, "funding_usd": fund,
            "cost_usd": cost, "pnl_usd": pnl, "turnover_w": tover,
            "n_long": sum(1 for x in w.values() if x > 0),
            "n_short": sum(1 for x in w.values() if x < 0),
            "gross_after_cap": gross_after[-1],
            "longs": sorted(s for s, x in w.items() if x > 0),
            "shorts": sorted(s for s, x in w.items() if x < 0),
            "ret_resmom": r_a, "pool": len(pool),
            "universe": list(syms)})
        fund_split_weekly.append({
            "dw": dw,
            "date": dt.datetime.fromtimestamp(
                cutoff / 1000, dt.timezone.utc).strftime("%Y-%m-%d"),
            "received": wk_rec, "paid": wk_paid})
    return {"rows": rows, "nav": nav, "nav_resmom": nav_r,
            "sym_pnl": sym_pnl, "sym_cost": sym_cost, "sym_weeks": sym_weeks,
            "nulls_ret": nulls_ret, "corr_sig": corr_sig,
            "legs_overlap": legs_overlap, "cap_bind": cap_bind,
            "flags": flags_ct, "skipped": skipped, "first_dw": first_dw,
            "fund_split": fund_split,
            "fund_split_weekly": fund_split_weekly}


# ── evaluation ──────────────────────────────────────────────────────────────
def evaluate(D: Data, uni: Dict[int, List[str]], R: Dict[str, Any]
             ) -> Dict[str, Any]:
    rows = R["rows"]
    rets = [x["ret"] for x in rows]
    if not rows:
        # Frozen rule: K<4 complete windows -> INCONCLUSIVO. K=0 is the
        # degenerate case — emit the verdict (with the skip reasons) instead
        # of IndexError on rows[0]. An empty `uni` (cache coverage gap) is
        # distinguished from weeks that were eligible but all skipped.
        skipped = R.get("skipped") or []
        return {
            "verdict": "INCONCLUSIVO", "criteria": {}, "K": 0,
            "survivorship_controlled": False,
            "inconclusive_reason": (
                "no eligible weeks in universe (cache coverage gap)"
                if not uni else
                f"{len(uni)} eligible weeks, 0 simulated — "
                f"{len(skipped)} skipped"),
            "period": None, "universe": None,
            "cum_return": None, "sharpe": None, "n_weeks": 0,
            "weekly_mean": None, "weekly_std": None, "max_drawdown": None,
            "nulls": {"n": 0, "p": {}, "real_percentile": None,
                      "mean": None, "seed_tag": core.NULL_SEED_TAG},
            "windows": [],
            "sign_flip": {"p": None, "method": "none", "n_pos": 0,
                          "K": 0, "seed": core.SIGNFLIP_SEED,
                          "samples": core.SIGNFLIP_SAMPLES},
            "ex_top3": {"pnl_ex_top3_usd": 0.0, "top3": [],
                        "total_sym_pnl": 0.0,
                        "cost_allocated_variant_usd": 0.0},
            "top10": [], "bottom10": [],
            "decomposition_usd": {"price": 0.0, "funding": 0.0,
                                  "cost": 0.0, "net": 0.0},
            "turnover": None, "cap": None, "resmom_diag": None,
            "data_quality": {"skipped_weeks": skipped,
                             "beta_unsolvable_flags": R.get("flags") or {}},
            "weekly": [],
        }
    cum = R["nav"] / NOTIONAL - 1.0
    sh = sharpe_annual(rets)
    null_sh = [s for s in (sharpe_annual(x) for x in R["nulls_ret"])
               if s is not None]
    p = {q: percentile(null_sh, q) for q in (5, 25, 50, 75, 95, 99)}
    pct_rank = (100.0 * sum(1 for x in null_sh if x < (sh or -99))
                / max(1, len(null_sh)))
    week_ms = [x["week_ms"] for x in rows]
    first_ms = rows[0]["week_ms"]
    last_end = (rows[-1]["dw"] + 7) * MS_D
    wins = core.split_windows(week_ms, first_ms, last_end)
    wtab = []
    for wi in wins:
        idx = wi["idx"]
        wr = 1.0
        for j in idx:
            wr *= 1.0 + rows[j]["ret"]
        wtab.append({
            "k": wi["k"], "complete": wi["complete"], "n_weeks": len(idx),
            "start": dt.datetime.fromtimestamp(
                wi["start_ms"] / 1000, dt.timezone.utc).strftime("%Y-%m-%d"),
            "end": dt.datetime.fromtimestamp(
                wi["end_ms"] / 1000, dt.timezone.utc).strftime("%Y-%m-%d"),
            "ret_net": wr - 1.0,
            "funding_pct": sum(rows[j]["funding_usd"] for j in idx)
            / NOTIONAL,
            "price_pct": sum(rows[j]["price_usd"] for j in idx) / NOTIONAL,
            "cost_pct": sum(rows[j]["cost_usd"] for j in idx) / NOTIONAL})
    comp = [x for x in wtab if x["complete"]]
    K = len(comp)
    wr = [x["ret_net"] for x in comp]
    npos = sum(1 for x in wr if x > 0)
    sfp, sfm = (core.sign_flip_p(wr) if K else (None, "none"))
    spnl = R["sym_pnl"]
    top3 = sorted(spnl.values(), reverse=True)[:3]
    ex3 = sum(spnl.values()) - sum(top3)
    scost = R["sym_cost"]
    net_alloc = {s: spnl.get(s, 0.0) - scost.get(s, 0.0) for s in spnl}
    top3c = sorted(net_alloc.values(), reverse=True)[:3]
    ex3c = sum(net_alloc.values()) - sum(top3c)

    in_uni = set()
    uni_weeks: Dict[str, int] = {}
    for x in rows:
        for s in x["universe"]:
            in_uni.add(s)
            uni_weeks[s] = uni_weeks.get(s, 0) + 1
    delisted_in = sorted(s for s in in_uni if s in D.delisted)
    delisted_traded = sorted(s for s in R["sym_weeks"] if s in D.delisted)
    delisted_data_ok = [s for s in D.delisted
                        if s in D.C and D.candle_count.get(s, 0) > 0]
    surv_controlled = bool(delisted_data_ok) and bool(delisted_in) and all(
        s in D.fund_t and len(D.fund_t[s]) > 0 for s in delisted_in)

    crit = {
        "c1_cum_return>0": {"obs": cum, "thr": 0.0, "pass": cum > 0},
        "c2a_sharpe>0.5": {"obs": sh, "thr": 0.5,
                           "pass": sh is not None and sh > 0.5},
        "c2b_sharpe>p95_nulls": {"obs": sh, "thr": p[95],
                                 "pass": sh is not None and sh > p[95]},
        "c3a_majority_windows_pos": {"obs": f"{npos}/{K}", "thr": f">{K/2}",
                                     "pass": K > 0 and npos > K / 2},
        "c3b_signflip_p<=0.10": {"obs": sfp, "thr": 0.10,
                                 "pass": sfp is not None and sfp <= 0.10},
        "c4_pnl_ex_top3>0": {"obs": ex3, "thr": 0.0, "pass": ex3 > 0},
    }
    all_pass = all(c["pass"] for c in crit.values())
    if K < core.MIN_WINDOWS:
        verdict = "INCONCLUSIVO"
    elif all_pass:
        verdict = ("PASS" if surv_controlled else
                   "PASS-provisorio: sobrevivencia nao controlada")
    else:
        verdict = "KILL (C)"

    # equity / drawdown
    nav = NOTIONAL
    peak = nav
    mdd = 0.0
    for r in rets:
        nav *= 1 + r
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1)
    # ResMom comparison
    pairs = [(x["ret"], x["ret_resmom"]) for x in rows
             if x["ret_resmom"] is not None]
    a = np.array([p_[0] for p_ in pairs])
    b = np.array([p_[1] for p_ in pairs])
    pc = float(np.corrcoef(a, b)[0, 1]) if len(a) > 2 else None
    sc = core.spearman(a.tolist(), b.tolist())
    rm_rets = [p_[1] for p_ in pairs]
    srt = sorted(spnl.items(), key=lambda kv: -kv[1])
    return {
        "verdict": verdict, "criteria": crit, "K": K,
        "survivorship_controlled": surv_controlled,
        "period": {"first_rebalance": rows[0]["date"],
                   "last_rebalance": rows[-1]["date"],
                   "n_rebalances": len(rows),
                   "last_exit": dt.datetime.fromtimestamp(
                       last_end / 1000, dt.timezone.utc)
                   .strftime("%Y-%m-%d")},
        "universe": {"avg_size": float(np.mean(
            [len(x["universe"]) for x in rows])),
            "avg_pool": float(np.mean([x["pool"] for x in rows])),
            "n_distinct": len(in_uni),
            "delisted_in_universe": delisted_in,
            "delisted_traded": delisted_traded,
            "delisted_with_data_in_api": len(delisted_data_ok),
            "delisted_in_meta": len(D.delisted),
            "weeks_in_universe": uni_weeks},
        "cum_return": cum, "sharpe": sh, "n_weeks": len(rets),
        "weekly_mean": float(np.mean(rets)), "weekly_std": float(np.std(
            rets, ddof=1)),
        "max_drawdown": mdd,
        "nulls": {"n": len(null_sh), "p": p, "real_percentile": pct_rank,
                  "mean": float(np.mean(null_sh)),
                  "seed_tag": core.NULL_SEED_TAG},
        "windows": wtab, "sign_flip": {"p": sfp, "method": sfm,
                                       "n_pos": npos, "K": K,
                                       "seed": core.SIGNFLIP_SEED,
                                       "samples": core.SIGNFLIP_SAMPLES},
        "ex_top3": {"pnl_ex_top3_usd": ex3, "top3": top3,
                    "total_sym_pnl": sum(spnl.values()),
                    "cost_allocated_variant_usd": ex3c},
        "top10": srt[:10], "bottom10": srt[-10:][::-1],
        "decomposition_usd": {
            "price": sum(x["price_usd"] for x in rows),
            "funding": sum(x["funding_usd"] for x in rows),
            "cost": sum(x["cost_usd"] for x in rows),
            "net": sum(x["pnl_usd"] for x in rows)},
        "turnover": {"mean_weekly_sum_abs_dw": float(np.mean(
            [x["turnover_w"] for x in rows])),
            "mean_first_excluded": float(np.mean(
                [x["turnover_w"] for x in rows[1:]])) if len(rows) > 1
            else None,
            "total_cost_usd": sum(x["cost_usd"] for x in rows),
            "total_cost_pct_notional": sum(
                x["cost_usd"] for x in rows) / NOTIONAL},
        "cap": {"weeks_cap_bound": R["cap_bind"],
                "mean_gross_after_cap": float(np.mean(
                    [x["gross_after_cap"] for x in rows])),
                "min_gross_after_cap": float(min(
                    x["gross_after_cap"] for x in rows))},
        "resmom_diag": {
            "mean_weekly_spearman_signal": float(np.mean(R["corr_sig"]))
            if R["corr_sig"] else None,
            "median_weekly_spearman_signal": float(np.median(R["corr_sig"]))
            if R["corr_sig"] else None,
            "frac_weeks_signal_corr_gt_0.5": float(np.mean(
                [c > 0.5 for c in R["corr_sig"]])) if R["corr_sig"] else None,
            "frac_weeks_signal_corr_lt_-0.5": float(np.mean(
                [c < -0.5 for c in R["corr_sig"]])) if R["corr_sig"]
            else None,
            "pnl_pearson": pc, "pnl_spearman": sc,
            "leg_overlap_same_side_mean": float(np.mean(R["legs_overlap"])),
            "resmom_cum_return": R["nav_resmom"] / NOTIONAL - 1.0,
            "resmom_sharpe": sharpe_annual(rm_rets)},
        "data_quality": {
            "fallback_px_uses": D.fallback_px,
            "held_coin_weeks_without_funding": D.no_funding_held,
            "skipped_weeks": R["skipped"],
            "beta_unsolvable_flags": R["flags"],
            "d0": dt.datetime.fromtimestamp(
                D.d0 * MS_D / 1000, dt.timezone.utc).strftime("%Y-%m-%d"),
            "n_candidate_coins": len(D.first_active)},
        "weekly": [{k: v for k, v in x.items() if k not in ("universe",)}
                   for x in rows],
    }


def _git_blob_bytes(path: Path) -> bytes:
    """Git object representation of a file: b"blob <len>\\0<content>"
    with LF-canonical content — the id `git hash-object`/`cat-file` sees."""
    content = path.read_bytes().replace(b"\r\n", b"\n")
    return b"blob " + str(len(content)).encode() + b"\0" + content


def _git_head() -> Optional[str]:
    """HEAD sha without subprocess (handles worktree .git pointers and
    packed refs)."""
    try:
        gitdir = ROOT / ".git"
        if gitdir.is_file():
            txt = gitdir.read_text(encoding="utf-8").strip()
            gitdir = Path(txt.split(":", 1)[1].strip())
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(None, 1)[1]
            refp = gitdir / ref
            if refp.exists():
                return refp.read_text(encoding="utf-8").strip()
            for ln in (gitdir / "packed-refs").read_text(
                    encoding="utf-8").splitlines():
                if ln.endswith(" " + ref):
                    return ln.split()[0]
            return None
        return head[:40]
    except OSError:
        return None


def diag_funding_split() -> int:
    """Prereg line 62: funding received vs paid, by leg side.

    Diagnostic computed AFTER the verdict — reads the local cache only
    (no downloads), writes a separate artifact, and never rewrites the
    sealed pair (DECISIONS_FROZEN.txt / phase1_result.json).
    """
    res_p = OUT_DIR / "phase1_result.json"
    sealed = safe_json_loads(res_p.read_bytes(), default=None)
    if not isinstance(sealed, dict):
        logging.warning("phase1_result.json unreadable — cannot "
                        "verify the diagnostic against it")
        return 3
    sealed_sha = hashlib.sha256(res_p.read_bytes()).hexdigest()
    D = Data()
    uni = compute_universes(D)
    R = run(D, uni)
    fs = R["fund_split"]
    if fs is None or not R["rows"]:
        logging.warning("no simulated weeks — nothing to decompose")
        return 3
    rec = fs["long"]["received"] + fs["short"]["received"]
    paid = fs["long"]["paid"] + fs["short"]["paid"]
    net = rec - paid
    sealed_f = safe_float((sealed.get("decomposition_usd") or {})
                          .get("funding"), 0.0)
    # A diagnostic that simulated zero weeks must never produce a trivial
    # MATCH (e.g. against a hypothetical zero-funding sealed result).
    ok = abs(net - sealed_f) < 1e-6 and bool(R["rows"])
    out = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "diagnostic": ("prereg line 62 — funding received vs paid; "
                       "computed post-verdict, changes no criterion"),
        # Provenance: HEAD is only *where the run happened* — the tree
        # may be dirty. diag_script_* identify the exact code that ran:
        # sha256 of the LF-canonical bytes (matches `git show` output on
        # every platform) and the git blob id (matches `git cat-file`).
        "code_commit": _git_head(),
        "diag_script_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
            .replace(b"\r\n", b"\n")).hexdigest(),
        "diag_script_git_blob": hashlib.sha1(
            _git_blob_bytes(Path(__file__))).hexdigest(),
        "n_weeks_simulated": len(R["rows"]),
        "sealed_result_file": "phase1_result.json",
        "sealed_result_sha256": sealed_sha,
        "sealed_decisions_file_sha256": sealed.get(
            "decisions_file_sha256"),
        "funding_received_usd": rec,
        "funding_paid_usd": paid,
        "net_usd": net,
        "check_net_equals_sealed_funding": {
            "diag_net_usd": net, "sealed_funding_usd": sealed_f,
            "delta": net - sealed_f, "ok": ok},
        "by_leg": {
            side: {"received_usd": v["received"],
                   "paid_usd": v["paid"],
                   "net_usd": v["received"] - v["paid"]}
            for side, v in fs.items()},
        "weekly": R["fund_split_weekly"],
    }
    _w(OUT_DIR / "phase1_diag_funding_split.json", out)
    print(f"funding received {rec:,.2f} | paid {paid:,.2f} | "
          f"net {net:,.2f} vs sealed {sealed_f:,.2f} -> "
          f"{'MATCH' if ok else 'MISMATCH'}", flush=True)
    if not ok:
        logging.warning(
            "MISMATCH: received-paid != sealed funding — possible bug "
            "in the original result; investigate before trusting "
            "either file")
    return 0 if ok else 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-download", action="store_true",
                    help="use only the cache in data/backtests/fundcarry/raw")
    ap.add_argument("--download-only", action="store_true")
    ap.add_argument("--freeze-decisions", action="store_true",
                    help="write DECISIONS_FROZEN.txt (once) and exit")
    ap.add_argument("--diag-funding-split", action="store_true",
                    help="prereg line 62 diagnostic: funding received vs "
                    "paid -> phase1_diag_funding_split.json; cache only, "
                    "sealed result never rewritten")
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if a.freeze_decisions:
        dec = OUT_DIR / "DECISIONS_FROZEN.txt"
        if dec.exists():
            print("already frozen:", dec)
            return 1
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        # write_bytes: text-mode \n -> \r\n translation on Windows made the
        # frozen file CRLF there but LF on Linux checkouts, so the recorded
        # sha256 only verified on Windows. Freeze canonical LF bytes.
        dec.write_bytes(("frozen_at_utc: " + stamp + "\n" + DECISIONS)
                        .encode("utf-8"))
        print("frozen at", stamp)
        return 0
    if a.diag_funding_split:
        return diag_funding_split()
    if not a.skip_download:
        download_candles()
    D = Data()
    uni = compute_universes(D)
    print(f"valid weeks: {len(uni)}; coins ever in universe: "
          f"{len({s for v in uni.values() for s in v})}", flush=True)
    if not a.skip_download:
        download_funding(D, uni)
    if a.download_only:
        return 0
    D = Data()                       # reload with funding
    dec = OUT_DIR / "DECISIONS_FROZEN.txt"
    if not dec.exists():
        logging.error("DECISIONS_FROZEN.txt missing: write it BEFORE the backtest")
        return 2
    R = run(D, uni)
    if not R["rows"]:
        # Zero simulated weeks is never a publishable result — it means
        # the cache is insufficient, not that the strategy is INCONCLUSIVO.
        # Writing it would overwrite the sealed verdict with an empty
        # report (Devin Review PR#21 second pass). Refuse, keep the file.
        logging.warning(
            "refusing to publish: 0 weeks simulated — cache coverage "
            "insufficient (run without --skip-download). Sealed "
            "phase1_result.json left untouched.")
        return 2
    out = evaluate(D, uni, R)
    out["decisions_file_sha256"] = hashlib.sha256(
        dec.read_bytes()).hexdigest()
    out["decisions_file_mtime_utc"] = dt.datetime.fromtimestamp(
        dec.stat().st_mtime, dt.timezone.utc).isoformat()
    out["as_of_ms"] = D.man["as_of_ms"]
    out["last_monday_ms"] = D.last_monday
    _w(OUT_DIR / "phase1_result.json", out)
    print(json.dumps({k: out[k] for k in (
        "verdict", "K", "cum_return", "sharpe", "criteria")}, indent=1,
        default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
