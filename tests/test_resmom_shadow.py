"""resmom-shadow tests — prereg-frozen math, ledger, daemon lifecycle.

All fetchers are injected; nothing touches the network. Synthetic
candles build a universe where the signal is known by construction.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.resmom_shadow import spec  # noqa: E402
from src.resmom_shadow.daemon import ResmomShadowDaemon, week_key  # noqa: E402
from src.resmom_shadow.ledger import ResmomLedger  # noqa: E402
from src.resmom_shadow import nulls as nullmod  # noqa: E402
from src.resmom_shadow import portfolio as pf  # noqa: E402
from src.resmom_shadow import prices  # noqa: E402
from src.resmom_shadow.read import evaluate  # noqa: E402
from src.resmom_shadow.signal import (  # noqa: E402
    arm_b_scores, build_portfolio, compute_signals, log_returns,
    ols_beta, pct_ranks, resid15, vol30, weights_equal)
from src.resmom_shadow.universe import (  # noqa: E402
    is_perp_symbol, select_universe)

pytestmark = pytest.mark.unit

DAY = spec.MS_D
WEEK = spec.MS_WEEK


# ── fixtures ──────────────────────────────────────────────────────────

def mk_candles(closes, end_t):
    """Daily candles [{t,o,h,l,c,v}] ending at close `end_t`."""
    n = len(closes)
    return [{"t": end_t - (n - i) * DAY, "o": closes[i], "h": closes[i],
             "l": closes[i], "c": closes[i], "v": 1_000_000.0}
            for i in range(n)]


def synth_returns(seed, n=120):
    rng = np.random.default_rng(seed)
    return np.cumsum(rng.normal(0, 0.02, n))


def fake_daemon(tmp_path, universe, closes_by_sym, fills, btc_closes):
    """Daemon with all network fetchers stubbed."""
    def meta():
        return universe

    def c1d(coin, days, end_ms):
        rows = mk_candles(closes_by_sym.get(coin, btc_closes), end_ms)
        return [r for r in rows if r["t"] < end_ms][-days:]

    def fill(coin, not_before_ms):
        return fills.get(coin)

    def funding(coin, t0, t1):
        return []

    def mids():
        return fills

    def bn_meta():
        return []

    def bn_klines(sym, days, end_ms):
        return []

    def bn_funding(sym, t0, t1):
        return []

    fetch = {"meta": meta, "c1d": c1d, "fill": fill, "funding": funding,
             "mids": mids, "bn_meta": bn_meta, "bn_klines": bn_klines,
             "bn_funding": bn_funding}
    return ResmomShadowDaemon(db_path=str(tmp_path / "r.db"), fetch=fetch)


# ── signal math ───────────────────────────────────────────────────────

class TestSignalMath:
    def test_log_returns(self):
        r = log_returns(np.array([1.0, np.e, np.e ** 2]))
        assert np.allclose(r, [1.0, 1.0])

    def test_ols_beta_exact(self):
        rng = np.random.default_rng(1)
        rb = rng.normal(0, 0.02, 80)
        ri = 2.0 * rb + 0.001
        assert abs(ols_beta(ri, rb) - 2.0) < 1e-9

    def test_ols_beta_needs_window(self):
        assert ols_beta(np.zeros(30), np.zeros(30)) is None

    def test_resid15_strips_beta(self):
        rb = np.linspace(-0.01, 0.01, 80)
        ri = 1.5 * rb + 0.01
        b = ols_beta(ri, rb)
        r15 = resid15(ri, rb, b)
        assert abs(r15 - 0.15) < 1e-9     # 15 days x +1%/d idio

    def test_vol30_ddof1(self):
        r = np.zeros(40)
        r[-30:] = np.concatenate([np.full(15, 0.02), np.full(15, -0.02)])
        v = vol30(r)
        assert abs(v - np.std(r[-30:], ddof=1)) < 1e-12

    def test_pct_ranks_bounds(self):
        pr = pct_ranks({"a": 1.0, "b": 2.0, "c": 3.0})
        assert pr["a"] == 0.0 and pr["c"] == 1.0 and pr["b"] == 0.5


class TestPortfolioBuild:
    def _signals(self, n=25):
        # s ranks 1..n descending; positive betas, varied vols
        return {f"S{i:02d}": _sig_for(i, n) for i in range(n)}

    def test_leg_count_floor_quintile(self):
        sigs = self._signals()
        ranked = sorted(sigs, key=lambda s: -sigs[s].s)
        port = build_portfolio(ranked, sigs, 25)
        n_legs = int(0.2 * 25)
        longs = [s for s, w in port.weights.items() if w > 0]
        shorts = [s for s, w in port.weights.items() if w < 0]
        assert len(longs) == n_legs and len(shorts) == n_legs

    def test_gross_total_one(self):
        sigs = self._signals()
        ranked = sorted(sigs, key=lambda s: -sigs[s].s)
        port = build_portfolio(ranked, sigs, 25)
        assert abs(sum(abs(w) for w in port.weights.values()) - 1.0) < 1e-9

    def test_beta_neutral_when_solvable(self):
        sigs = self._signals()
        ranked = sorted(sigs, key=lambda s: -sigs[s].s)
        port = build_portfolio(ranked, sigs, 25)
        pbeta = sum(w * sigs[s].beta for s, w in port.weights.items())
        assert abs(pbeta) < 1e-9
        assert port.flags == []

    def test_fallback_when_unsolvable(self):
        # mixed signs: long leg beta>0, short leg beta<0 — the short leg
        # ADDS positive portfolio beta (shorting negative-beta names);
        # beta=0 is unreachable -> fallback split + flag
        sigs = {f"S{i}": _Sig(-1.0 if i >= 8 else 1.0, 0.05, 10 - i)
                for i in range(10)}
        ranked = sorted(sigs, key=lambda s: -sigs[s].s)
        port = build_portfolio(ranked, sigs, 10)
        assert "beta_neutral_unsolvable" in port.flags
        assert abs(port.gross_long - 0.5) < 1e-9

    def test_both_negative_beta_still_solvable(self):
        # BL<0 & BS<0 share a sign -> g_L=BS/(BL+BS) is a valid split
        sigs = {f"S{i}": _Sig(-0.8 - 0.01 * i, 0.05, 10 - i)
                for i in range(10)}
        ranked = sorted(sigs, key=lambda s: -sigs[s].s)
        port = build_portfolio(ranked, sigs, 10)
        assert port.flags == []
        pbeta = sum(w * sigs[s].beta for s, w in port.weights.items())
        assert abs(pbeta) < 1e-9

    def test_inverse_vol_weights(self):
        sigs = self._signals()
        ranked = sorted(sigs, key=lambda s: -sigs[s].s)
        port = build_portfolio(ranked, sigs, 25)
        longs = {s: w for s, w in port.weights.items() if w > 0}
        # higher vol -> smaller weight inside the leg
        vols = {s: sigs[s].vol30 for s in longs}
        hi = max(vols, key=vols.get)
        lo = min(vols, key=vols.get)
        assert longs[lo] > longs[hi]


class _Sig:
    def __init__(self, beta, vol30_, s):
        self.beta, self.vol30, self.s = beta, vol30_, s
        self.resid15 = s * vol30_
        self.symbol = "x"


def _sig_for(i, n):
    # deterministic: s descending with i, beta ~1.0±0.3, vol varied
    return _Sig(1.0 + 0.05 * ((i * 7) % 11 - 5),
                0.02 + 0.001 * (i % 7), (n - i) * 0.1)


# ── universe ──────────────────────────────────────────────────────────

class TestUniverse:
    def test_perp_filter(self):
        assert is_perp_symbol("ETH")
        assert not is_perp_symbol("xyz:TSLA")
        assert not is_perp_symbol("USDC")

    def test_top_n_and_history(self):
        end = int(time.time() * 1000)
        cands = {}
        for i in range(30):
            rows = mk_candles([10.0 + i] * 100, end)
            for r in rows:
                r["v"] = (i + 1) * 1e5           # rank by notional
            cands[f"C{i:02d}"] = rows
        cands["YOUNG"] = mk_candles([5.0] * 20, end)   # <90d
        uni, young = select_universe(cands, end, top_n=25)
        assert len(uni) == 25
        assert "YOUNG" in young
        assert uni[0].symbol == "C29"              # biggest notional

    def test_btc_stays_in_universe(self):
        end = int(time.time() * 1000)
        cands = {spec.BTC: mk_candles([100.0] * 100, end)}
        for r in cands[spec.BTC]:
            r["v"] = 1e9
        uni, _ = select_universe(cands, end)
        assert uni and uni[0].symbol == spec.BTC


# ── nulls ─────────────────────────────────────────────────────────────

class TestNulls:
    def test_seed_deterministic(self):
        assert nullmod.null_seed(123, 7) == nullmod.null_seed(123, 7)
        assert nullmod.null_seed(123, 7) != nullmod.null_seed(124, 7)

    def test_draw_disjoint_legs(self):
        uni = [f"S{i}" for i in range(25)]
        sigs = {s: _Sig(1.0, 0.02 + 0.001 * i, 0.1 * i)
                for i, s in enumerate(uni)}
        port = nullmod.draw_null_weights(uni, sigs, 25,
                                         nullmod.null_seed(1, 0))
        longs = {s for s, w in port.weights.items() if w > 0}
        shorts = {s for s, w in port.weights.items() if w < 0}
        assert not (longs & shorts)
        assert abs(sum(abs(w) for w in port.weights.values()) - 1.0) < 1e-9

    def test_week_pnl_math(self):
        pnl = nullmod.week_pnl(
            {"A": 0.5, "B": -0.5}, {"A": 100.0, "B": 50.0},
            {"A": 110.0, "B": 45.0})
        # +0.5*10%*10k + (-0.5)*(-10%)*10k = 500+500
        assert abs(pnl - 1000.0) < 1e-6


# ── ledger + daemon lifecycle ─────────────────────────────────────────

class TestLedger:
    def test_schema_and_meta(self, tmp_path):
        ld = ResmomLedger(str(tmp_path / "l.db"))
        ld.meta_set("k", "v")
        assert ld.meta_get("k") == "v"
        assert ld.meta_incr("n") == 1 and ld.meta_incr("n") == 2
        for t in ("resmom_rebalances", "resmom_positions", "resmom_nav",
                  "resmom_nulls", "resmom_marks", "resmom_funding",
                  "resmom_meta", "resmom_log"):
            ld.conn.execute(f"SELECT 1 FROM {t} LIMIT 1")
        ld.close()


def _full_universe():
    return [spec.BTC] + [f"S{i:02d}" for i in range(30)]


def _daemon(tmp_path):
    uni = _full_universe()
    end = week_key(int(time.time() * 1000))
    closes = {s: list(100 + np.cumsum(np.linspace(-1, 1, 100)
                                    * (1 + i % 3)))
              for i, s in enumerate(uni)}
    closes[spec.BTC] = list(500 + np.cumsum(np.linspace(-2, 2, 100)))
    fills = {s: 100.0 + i for i, s in enumerate(uni)}
    return fake_daemon(tmp_path, uni, closes, fills, closes[spec.BTC])


class TestDaemon:
    def test_week_key_monday(self):
        # 2026-11-02 is a Monday
        import datetime as dt
        mon = int(dt.datetime(2026, 11, 2, tzinfo=dt.timezone.utc)
                  .timestamp() * 1000)
        assert week_key(mon + 3 * DAY) == mon
        assert week_key(mon) == mon
        assert week_key(mon - 1) == mon - WEEK

    def test_rebalance_pass_writes_everything(self, tmp_path):
        d = _daemon(tmp_path)
        now = week_key(int(time.time() * 1000)) + 6 * 60_000  # Mon 00:06
        stats = d.rebalance_pass(now)
        ld = d.ledger
        ra = ld.rebalance_get(week_key(now), "A")
        rb = ld.rebalance_get(week_key(now), "B")
        assert ra and rb
        w = json.loads(ra["weights_json"])
        assert abs(sum(abs(v) for v in w.values()) - 1.0) < 1e-9
        assert spec.BTC not in w
        open_a = ld.positions_open("A")
        assert len(open_a) == len(w)
        assert ld.count("resmom_nav") >= 2          # A + B entry marks
        assert ld.count("resmom_nulls") == spec.N_NULLS
        assert stats["nulls"]["written"] == spec.N_NULLS
        d.shutdown()

    def test_rebalance_idempotent(self, tmp_path):
        d = _daemon(tmp_path)
        now = week_key(int(time.time() * 1000)) + 6 * 60_000
        d.rebalance_pass(now)
        d.rebalance_pass(now)                     # rows exist -> skip
        assert d.ledger.count("resmom_rebalances") == 2   # A+B only
        assert d.ledger.count("resmom_nulls") == spec.N_NULLS
        d.shutdown()

    def test_positions_close_next_week(self, tmp_path):
        d = _daemon(tmp_path)
        wk = week_key(int(time.time() * 1000))
        d.rebalance_pass(wk + 6 * 60_000)
        d.rebalance_pass(wk + WEEK + 6 * 60_000)
        closed = d.ledger.count("resmom_positions",
                                "WHERE status='closed'")
        assert closed > 0
        # prev week's nulls scored on this week's fills
        scored = d.ledger.conn.execute(
            "SELECT COUNT(*) c FROM resmom_nulls"
            " WHERE week_pnl_usd IS NOT NULL").fetchone()["c"]
        assert scored == spec.N_NULLS
        d.shutdown()

    def test_funding_accrual(self, tmp_path):
        uni = _full_universe()
        wk = week_key(int(time.time() * 1000))
        # varying prices -> nonzero vol30 so legs actually open
        closes = {s: [100.0 + 0.1 * i + 0.01 * j for i in range(100)]
                  for j, s in enumerate(uni)}
        fills = {s: 100.0 for s in uni}
        d = fake_daemon(tmp_path, uni, closes, fills, closes[spec.BTC])
        # one funding event for every open symbol, +0.01% each
        def funding(coin, t0, t1):
            return [(wk + 3600_000, 0.0001)]
        d.fetch["funding"] = funding
        d.rebalance_pass(wk + 6 * 60_000)
        d.funding_pass(wk + 3700_000)
        # longs pay, shorts receive on rate>0
        fr = list(d.ledger.conn.execute(
            "SELECT symbol, pnl_usd FROM resmom_funding"))
        assert len(fr) > 0
        for sym, p in fr:
            pos = [x for x in d.ledger.positions_open("A")
                   if x["symbol"] == sym]
            if pos:
                exp_sign = -1 if pos[0]["side"] == "long" else 1
                assert (p > 0) == (exp_sign > 0)
        d.shutdown()

    def test_kill_flag_reports_only(self, tmp_path):
        d = _daemon(tmp_path)
        ld = d.ledger
        ld.nav_insert(1, "A", 10_000.0, 10_000.0, 10, 0.0)
        ld.nav_insert(2, "A", 6_000.0, 10_000.0, 10, -0.4)
        hit = pf.check_kill(ld, "A", 3)
        assert hit and hit["dd"] == -0.4
        assert ld.meta_get("kill_triggered") is not None
        # second call doesn't re-flag
        assert pf.check_kill(ld, "A", 4) is None
        d.shutdown()


# ── read gate ─────────────────────────────────────────────────────────

class TestReadGate:
    def _db(self, tmp_path, weekly_rets, null_rets=None, pos_pnls=None):
        ld = ResmomLedger(str(tmp_path / "r.db"))
        wk = week_key(int(time.time() * 1000)) - \
            (len(weekly_rets) + 1) * WEEK
        nav = spec.NOTIONAL_USD
        for i, r in enumerate(weekly_rets):
            w = wk + i * WEEK
            ld.nav_insert(w + 60_000, "A", nav, 10_000, 10, 0.0)
            nav *= 1 + r
            ld.nav_insert(w + WEEK + 30_000, "A", nav, 10_000, 10, 0.0)
        reb_id = ld.conn.execute(
            "INSERT INTO resmom_rebalances(week_key_ms, arm, created_at_ms,"
            " universe_json, signals_json, weights_json, turnover_usd,"
            " cost_usd, inputs_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
            (wk, "A", wk, "[]", "{}", "{}", 0.0, 0.0, "x")).lastrowid
        for j, pnl in enumerate(pos_pnls or []):
            pid = ld.position_insert({
                "rebalance_id": reb_id, "arm": "A",
                "symbol": f"Z{j}", "side": "long",
                "weight": 0.1, "qty": 1.0, "entry_px": 100.0,
                "entry_ms": wk})
            ld.position_close(pid, 100.0, wk + 60_000, pnl, "rebalance")
        ld.conn.commit()
        if null_rets:
            for nid, rets in enumerate(null_rets):
                nav = spec.NOTIONAL_USD
                for i, r in enumerate(rets):
                    nav *= 1 + r
                    ld.null_insert({
                        "week_key_ms": wk + i * WEEK, "rebalance_id": 1,
                        "null_id": nid, "seed": "s",
                        "weights_json": "{}", "week_pnl_usd": nav - 10000,
                        "nav_usd": nav})
        ld.close()
        return str(tmp_path / "r.db")

    def test_in_progress_below_26(self, tmp_path):
        db = self._db(tmp_path, [0.01] * 10)
        res = evaluate(db)
        assert res["verdict"] == "IN_PROGRESS"
        assert res["closed_weeks"] == 10

    def test_pass_all_checks(self, tmp_path):
        # varying returns -> nonzero std -> defined Sharpe
        rets = [0.02 + (0.006 if i % 2 else -0.004) for i in range(26)]
        nulls = [[0.004 + (0.005 if i % 2 else -0.004)
                  for i in range(26)] for _ in range(20)]
        # >3 symbols all positive -> pnl ex-top-3 stays > 0
        db = self._db(tmp_path, rets, null_rets=nulls,
                      pos_pnls=[40.0, 35.0, 30.0, 25.0, 20.0])
        res = evaluate(db)
        assert res["verdict"] == "PASS"
        assert res["checks"]["cum_return>0"]
        assert res["checks"]["sharpe>0.5"]
        assert res["checks"]["sharpe>p75_nulls"]

    def test_c_when_negative(self, tmp_path):
        db = self._db(tmp_path, [-0.01] * 26)
        res = evaluate(db)
        assert res["verdict"] == "C"
        assert not res["checks"]["cum_return>0"]

    def test_kill_verdict(self, tmp_path):
        db = self._db(tmp_path, [-0.10] * 6)
        res = evaluate(db)
        assert res["verdict"] == "KILL"
        assert res["kill"]["breached"]

    def test_pnl_ex_top3(self, tmp_path):
        ld = ResmomLedger(str(tmp_path / "r.db"))
        rid = ld.rebalance_insert({
            "week_key_ms": 1, "arm": "A", "created_at_ms": 1,
            "universe_json": "[]", "signals_json": "{}",
            "weights_json": "{}", "turnover_usd": 0, "cost_usd": 0,
            "flags_json": "[]", "inputs_sha256": "x"})
        for i, (sym, pnl) in enumerate(
                [("A", 500), ("B", 400), ("C", 300), ("D", -50)]):
            pid = ld.position_insert({
                "rebalance_id": rid, "arm": "A", "symbol": sym,
                "side": "long", "weight": 0.1, "qty": 1.0,
                "entry_px": 1.0, "entry_ms": 1})
            ld.position_close(pid, 2.0, 2, pnl, "rebalance")
        ld.nav_insert(1, "A", 10000, 10000, 4, 0.0)
        ld.close()
        res = evaluate(str(tmp_path / "r.db"))
        # total = 1150; ex-top3 = 1150-1200 = -50
        assert abs(res["pnl_ex_top3"] - (-50.0)) < 1e-6


# ── parity harness (self-check, historical ref pending) ───────────────

class TestParityHarness:
    def test_weights_equal_helper(self):
        a = {"A": 0.5, "B": -0.5}
        assert weights_equal(a, dict(a))
        assert not weights_equal(a, {"A": 0.5, "B": -0.49})
        assert not weights_equal(a, {"A": 0.5})

    def test_signal_deterministic_on_same_input(self):
        """Same code, same data -> same weights (parity floor)."""
        uni = [f"S{i}" for i in range(25)] + [spec.BTC]
        rng = np.random.default_rng(42)
        closes = {s: 100 + np.cumsum(rng.normal(0, 1, 100))
                  for s in uni}
        rets = {s: log_returns(np.maximum(closes[s], 1)) for s in uni}
        rb = rets[spec.BTC]
        sigs = compute_signals(rets, rb)
        ranked = sorted((s for s in uni if s in sigs and s != spec.BTC),
                        key=lambda s: -sigs[s].s)
        p1 = build_portfolio(ranked, sigs, len(uni))
        p2 = build_portfolio(ranked, sigs, len(uni))
        assert weights_equal(p1.weights, p2.weights)
