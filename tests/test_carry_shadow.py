"""Carry-shadow isolation + state-machine tests.

Mirrors the spirit of test_g_shadow_routing_mirror_never_touches_execution:
the module must never reach the execution surface. Here isolation is
structural — the package imports nothing from engine paths, so the guard is
a static import-graph assertion plus a full lifecycle driven through stub
fetchers proving all evidence lands in its own ledger.
"""
from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from src.research.carry_shadow import spec
from src.research.carry_shadow.daemon import CarryShadowDaemon
from src.research.carry_shadow.ledger import Ledger
from src.research.carry_shadow.sim import BookSnap, Episode, PendingLeg

pytestmark = pytest.mark.unit

PKG = Path(__file__).resolve().parents[1] / "src" / "research" / "carry_shadow"
FORBIDDEN_PREFIXES = (
    "src.core", "src.strategies", "src.dashboard", "src.backtest",
    "src.data.database", "src.utils.config", "main",
)


# ─── isolation guards ─────────────────────────────────────────────────────────

def _pkg_imports() -> list[tuple[str, str]]:
    """(file, imported-module-root) for every import in the package."""
    out = []
    for f in PKG.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    out.append((f.name, a.name))
            elif isinstance(node, ast.ImportFrom) and node.module:
                out.append((f.name, node.module))
    return out


def test_carry_shadow_never_imports_execution_surface() -> None:
    """Static guard: no engine/config/backtest/dashboard imports anywhere in
    the package — equivalent isolation to the shadow-routing mirror test."""
    bad = [(f, m) for f, m in _pkg_imports()
           if m.startswith(FORBIDDEN_PREFIXES)]
    assert bad == [], f"forbidden imports: {bad}"


def test_carry_shadow_imports_only_research_siblings() -> None:
    """Package may only touch stdlib, websockets, its own modules, and the
    public-history research helper."""
    allowed = ("src.research.carry_shadow", "scripts.research")
    stdlib_ok = ("__future__", "asyncio", "collections", "dataclasses",
                 "json", "logging", "pathlib", "sqlite3", "time", "typing")
    for fname, mod in _pkg_imports():
        assert (mod == allowed[1] or mod.startswith(allowed[1] + ".")
                or mod.startswith(allowed[0]) or mod in stdlib_ok
                or mod == "websockets"), f"{fname}: unexpected import {mod}"


def test_carry_shadow_config_hash_neutral() -> None:
    """The package must not read settings.yaml / .env — no config dependency
    means the config hash cannot change."""
    text = "\n".join(f.read_text(encoding="utf-8") for f in PKG.glob("*.py"))
    for needle in ("settings.yaml", ".env", "load_config", "config_hash"):
        assert needle not in text


# ─── state machine ────────────────────────────────────────────────────────────

def _ep(pm: bool = False) -> Episode:
    return Episode(pair="PURR/USDC", perp="PURR", entry_decision_ms=0,
                   pm_branch=pm, maint=0.1667, m_trigger=0.45, m0=0.60)


def test_pendingleg_fill_model_strict_crossing() -> None:
    buy = PendingLeg("buy", 100.0)
    # ask AT our bid is not a fill (queue position unknown — conservative)
    assert not buy.check(0, BookSnap(bid=99.0, ask=100.0))
    assert buy.check(1, BookSnap(bid=99.0, ask=99.9))
    sell = PendingLeg("sell", 100.0)
    assert not sell.check(0, BookSnap(bid=100.0, ask=100.5))
    assert sell.check(1, BookSnap(bid=100.1, ask=100.5))


def test_entry_both_legs_fill_opens_episode() -> None:
    ep = _ep()
    ep.place_entry(BookSnap(99.0, 100.0), BookSnap(99.0, 100.0))
    assert ep.legs["spot"].price == 99.0
    assert ep.legs["perp"].price == 100.0
    # spot fills when ask crosses below our 99.0 bid; perp still untouched
    ev = ep.on_book_entry(1_000, BookSnap(98.5, 98.9), BookSnap(99.0, 100.0))
    assert ev is None                      # only spot filled — still pending
    assert ep.unlegged_since_ms == 1_000
    # perp fills when bid crosses above our 100.0 ask
    ev = ep.on_book_entry(5_000, BookSnap(98.5, 98.9), BookSnap(100.1, 100.2))
    assert ev == "fill"
    assert ep.state == "open" and ep.p0 == 100.0 and ep.s0 == 99.0
    assert ep.max_unlegged_s == 4.0        # 4s naked exposure recorded


def test_entry_unfilled_aborts_and_unlegged_unwinds() -> None:
    ep = _ep()
    ep.place_entry(BookSnap(99.0, 100.0), BookSnap(99.0, 100.0))
    w = spec.MAKER_FILL_WINDOW_S * 1000 + 1
    # neither leg crossed within the window → abort, zero taker cost
    ev = ep.on_book_entry(w, BookSnap(99.0, 100.5), BookSnap(98.0, 100.0))
    assert ev == "abort_unfilled" and ep.state == "aborted"
    assert ep.cost_bps == 0.0
    # one leg filled → other leg unwound at taker
    ep2 = _ep()
    ep2.place_entry(BookSnap(99.0, 100.0), BookSnap(99.0, 100.0))
    ep2.on_book_entry(1_000, BookSnap(98.5, 98.9), BookSnap(99.0, 100.0))
    ev = ep2.on_book_entry(w, BookSnap(98.5, 98.9), BookSnap(98.0, 100.0))
    assert ev == "unlegged_unwind"
    assert ep2.cost_bps == spec.TAKER_FEE_BPS


def _open_ep(pm: bool = False) -> Episode:
    ep = _ep(pm)
    ep.place_entry(BookSnap(99.0, 100.0), BookSnap(99.0, 100.0))
    # cross both legs in one tick: spot ask<99, perp bid>100
    ep.on_book_entry(1, BookSnap(98.5, 98.9), BookSnap(100.1, 100.2))
    return ep


def test_margin_warn_deleverages_both_legs() -> None:
    ep = _open_ep()
    # perp +35% adverse: equity = .60-.35=.25 < .45*N(1.35)=.6075 → warn
    assert ep.margin_zone(135.0) == "warn"
    ev = ep.on_margin_tick(2, 135.0, 99.0)
    assert ev == "rebalance"
    assert ep.q == pytest.approx(0.25 / (0.6 * 1.35), rel=0.01)
    assert ep.deleverages == 1
    # equity is wealth-preserved (mod taker cost); ratio restored to ~m0
    assert ep.margin_zone(135.0) == "ok"
    eq = ep.equity(135.0)
    n_t = ep.q * 1.35
    assert abs(eq / n_t - ep.m0) < 0.01   # ~m0 minus the taker fee drift


def test_margin_gap_kill_and_liquidation_zones() -> None:
    ep = _open_ep()  # PURR maint .1667: liq when equity<=.1667*N → mid≈137.1
    assert ep.margin_zone(105.0) == "ok"       # eq=.5497 > .45*1.05=.4725
    assert ep.margin_zone(135.0) == "warn"     # eq=.2497 < .45*1.35=.6075
    assert ep.margin_zone(200.0) == "liq"      # eq=.60-1.0<0 → liq
    ev = ep.on_margin_tick(2, 200.0, 99.0)
    assert ev == "liquidated" and ep.state == "liquidated"


def test_delev_floor_closes_episode() -> None:
    # BTC-like pair (maint 1.25%): floor reachable without crossing maint
    ep = Episode(pair="UBTC/USDC", perp="BTC", entry_decision_ms=0,
                 pm_branch=False, maint=0.0125, m_trigger=0.45, m0=0.60)
    ep.place_entry(BookSnap(99.0, 100.0), BookSnap(99.0, 100.0))
    ep.on_book_entry(1, BookSnap(98.5, 98.9), BookSnap(100.1, 100.2))
    ep.collateral = 0.47
    # eq = .47-.445=.025 > .0125*1.445=.018 → warn (not liq)
    assert ep.margin_zone(144.5) == "warn"
    ev = ep.on_margin_tick(3, 144.5, 99.0)
    # keep = .025/(.60*1.445)=.029 ≤ .05 → close at floor
    assert ev == "delev_floor" and ep.state == "closed" and ep.q == 0.0


def test_funding_accrues_on_q_and_scales_with_deleverage() -> None:
    ep = _open_ep()
    ep.q = 0.5
    acc = ep.on_funding_row(3_600_000, 0.0005)  # 5bp/h on remaining q
    assert acc == pytest.approx(0.00025)
    assert ep.cum_f == pytest.approx(0.00025)
    assert ep.on_funding_row(7_200_000, 0.0) == 0.0


def test_exit_requires_48h_negative_and_settles() -> None:
    ep = _open_ep()
    t0 = 10_000_000
    assert not ep.exit_signal(-0.05, t0, t0 + 47 * 3_600_000)
    assert ep.exit_signal(-0.05, t0, t0 + 48 * 3_600_000)
    assert not ep.exit_signal(0.01, t0, t0 + 999 * 3_600_000)
    ep.place_exit(t0 + 48 * 3_600_000,
                  BookSnap(98.0, 99.0), BookSnap(99.0, 100.0))
    assert ep.exit_legs["spot"].price == 99.0    # sell at ask
    assert ep.exit_legs["perp"].price == 99.0    # buy at bid
    ev = ep.on_book_exit(t0 + 49 * 3_600_000,
                         BookSnap(99.1, 99.2), BookSnap(98.9, 98.95))
    assert ev == "exit" and ep.state == "closed"
    assert ep.close_reason == "funding_exit"
    assert ep.q == 0.0


def test_pm_branch_skips_margin_checks() -> None:
    ep = _open_ep(pm=True)
    assert ep.margin_zone(300.0) == "ok"          # PM collateral = spot itself
    assert ep.on_margin_tick(2, 300.0, 99.0) is None


def test_spec_mm_rule_fixed_function() -> None:
    # BTC 40x → maint 1.25% → M stays .45 (trigger distance 43.75pp)
    assert spec.maint_for(40.0) == pytest.approx(0.0125)
    assert spec.mm_for(40.0) == (0.45, 0.60)
    # PURR 3x → maint 16.67% → distance 28.3pp ≥ 15 → M still .45
    assert spec.mm_for(3.0) == (0.45, 0.60)
    # hypothetical 1.5x asset → maint 33.3% → distance <15pp → M bumps
    m, m0 = spec.mm_for(1.5)
    assert m == pytest.approx(1 / 3.0 + 0.15)
    assert m0 == pytest.approx(m + 0.15)


# ─── daemon lifecycle through stub fetchers (no network) ─────────────────────

def _stub_fetch(now_ms: int):
    meta = {"universe": [{"name": "PURR", "maxLeverage": 3}]}
    candles = [(now_ms - i * 7_200_000, 100.0, 101.0, 99.0, 100.0,
                900_000.0, 10) for i in range(450)]  # ~37d of 2h bars, $180k/2h
    funding = [(now_ms - h * 3_600_000, 0.001) for h in range(48)]
    return {
        "post": lambda payload: meta,
        "perp_universe": lambda: [{"name": "PURR"}],
        "spot_pairs": lambda: (
            [{"pair_name": "PURR/USDC", "index": 1, "base_name": "PURR"}], {}),
        "funding": lambda coin, s, e: funding,
        "candles": lambda coin, iv, s, e, p: candles,
    }


def test_spot_coin_uses_atindex_name_from_spotmeta(tmp_path) -> None:
    """P1: spot legs subscribe by spotMeta universe name ('@{index}'), never
    by base token name — only PURR/USDC carries a real name."""
    now = int(time.time() * 1000)
    f = _stub_fetch(now)
    f["spot_pairs"] = lambda: (
        [{"pair_name": "@107", "index": 107, "base_name": "HYPE"},
         {"pair_name": "@142", "index": 142, "base_name": "UBTC"}],
        {})
    f["post"] = lambda p: {"universe": [
        {"name": "HYPE", "maxLeverage": 10},
        {"name": "BTC", "maxLeverage": 40}]}
    d = CarryShadowDaemon(db_path=str(tmp_path / "cs.db"), fetch=f)
    cands = d._discover_candidates()
    assert {c["pair_name"] for c in cands} == {"@107", "@142"}
    for c in cands:
        d._register_candidate(c)
    # the WS coin map keys on pair_name (@N) AND perp — not the base token
    assert d.pair_by_coin["@107"]["perp"] == "HYPE"
    assert d.pair_by_coin["@142"]["perp"] == "BTC"   # U-prefix -> BTC
    assert d.pair_by_coin["HYPE"]["pair_name"] == "@107"
    assert d.pair_by_coin["@107"]["pm"] is True       # HYPE PM-eligible
    assert d.pair_by_coin["@142"]["pm"] is False      # UBTC unconfirmed
    d.shutdown()


@pytest.mark.asyncio
async def test_daemon_lifecycle_end_to_end(tmp_path) -> None:
    now = int(time.time() * 1000)
    d = CarryShadowDaemon(db_path=str(tmp_path / "cs.db"),
                          fetch=_stub_fetch(now))
    await d.boot()
    assert len(d.cands) == 1 and d.cands[0]["perp"] == "PURR"
    assert d.in_universe.get("PURR/USDC") is True
    # f_ann = .001*24*365 = 876%/yr ≥ 15% → entry attempt next poll
    d.books["PURR/USDC"] = BookSnap(99.0, 100.0)
    d.books["PURR"] = BookSnap(99.0, 100.0)
    await d._refresh_funding()
    ep = d.pool.get("PURR/USDC")
    assert ep is not None and ep.state == "pending_entry"
    # books cross → open; margin sweep healthy
    d._on_ws_message('{"channel":"l2Book","data":{"coin":"PURR/USDC","time":'
                     + str(now + 5_000) + ',"levels":[[{"px":"98.5"}],'
                     '[{"px":"98.9"}]]}}')
    d._on_ws_message('{"channel":"l2Book","data":{"coin":"PURR","time":'
                     + str(now + 6_000) + ',"levels":[[{"px":"100.2"}],'
                     '[{"px":"100.3"}]]}}')
    assert ep.state == "open"
    d.margin_sweep(now + 60_000)
    assert not d.pool.dead
    # liquidation kills the experiment
    d.books["PURR"] = BookSnap(400.0, 401.0)
    d.margin_sweep(now + 120_000)
    assert d.pool.dead
    d.shutdown()

    # audit trail: every transition landed in the append-only ledger
    led = Ledger(str(tmp_path / "cs.db"))
    kinds = [r[0] for r in led._con.execute("SELECT kind FROM events")]
    assert "entry_attempt" in kinds and "fill" in kinds
    assert any(k in kinds for k in ("liquidation", "gap_kill"))
    assert led.meta_get("dead") is not None
    led.close()


@pytest.mark.asyncio
async def test_daemon_dead_flag_blocks_new_entries(tmp_path) -> None:
    now = int(time.time() * 1000)
    d = CarryShadowDaemon(db_path=str(tmp_path / "cs.db"),
                          fetch=_stub_fetch(now))
    await d.boot()
    d._die("test_death")
    d.books["PURR/USDC"] = BookSnap(99.0, 100.0)
    d.books["PURR"] = BookSnap(99.0, 100.0)
    await d._refresh_funding()
    assert d.pool.live() == []
    d.shutdown()


@pytest.mark.asyncio
async def test_book_missing_recorded_not_silent(tmp_path) -> None:
    """P2: funding-eligible + in-universe but no live book -> book_missing
    event + meta counter; the expiry can't end in a silent 0-episode C."""
    now = int(time.time() * 1000)
    d = CarryShadowDaemon(db_path=str(tmp_path / "cs.db"),
                          fetch=_stub_fetch(now))
    await d.boot()
    assert d.in_universe.get("PURR/USDC") is True
    await d._refresh_funding()          # f_ann eligible, books missing
    await d._refresh_funding()
    assert d.pool.live() == []          # no episode (still fail-safe)
    assert d.ledger.meta_get("book_missing_count") == "3"  # boot + 2 polls
    kinds = [r[0] for r in d.ledger._con.execute("SELECT kind FROM events")]
    assert kinds.count("book_missing") == 3
    d.shutdown()


@pytest.mark.asyncio
async def test_ws_gap_catchup_liquidation_and_deleverage(tmp_path) -> None:
    """P3: on reconnect, missed-window extremes are replayed through the
    margin rule — past maintenance = death; past trigger = deleverage at
    the worst price."""
    now = int(time.time() * 1000)

    async def _open_btc_ep(d):
        d.books["@142"] = BookSnap(99.0, 100.0)
        d.books["BTC"] = BookSnap(99.0, 100.0)
        await d._refresh_funding()
        ep = d.pool.get("@142")
        assert ep is not None
        # force-fill both legs
        ep.on_book_entry(now, BookSnap(98.5, 98.9), BookSnap(100.1, 100.2))
        d._handle_entry_event(ep, "fill", now)
        return ep

    # case 1: gap high past maintenance (BTC maint 1.25%, M .45, M0 .60)
    # liq: eq=.6-adv <= .0125*(1+adv) -> adv >= .52 ; gap_hi=160 -> adv .60 liq
    d = CarryShadowDaemon(db_path=str(tmp_path / "a.db"),
                          fetch=_btc_fetch(160.0, now))
    await d.boot()
    ep = await _open_btc_ep(d)
    await d._catchup_gap(now - 120_000, now)
    assert ep.state == "liquidated" and d.pool.dead
    assert d.ledger.meta_get("dead") == "gap_kill"
    d.shutdown()

    # case 2: gap high past trigger but not maintenance: adv .30 < liq
    # eq=.6-.3=.3 < .45*1.3=.585 -> deleverage at the worst price
    d = CarryShadowDaemon(db_path=str(tmp_path / "b.db"),
                          fetch=_btc_fetch(130.0, now))
    await d.boot()
    ep = await _open_btc_ep(d)
    await d._catchup_gap(now - 120_000, now)
    assert ep.state == "open" and ep.q < 1.0 and ep.deleverages == 1
    kinds = [r[0] for r in d.ledger._con.execute("SELECT kind FROM events")]
    assert "gap_catchup" in kinds and "rebalance" in kinds
    assert not d.pool.dead
    d.shutdown()


def _btc_fetch(gap_hi: float, now: int = 0):
    """BTC-only stub: @142 spot pair, 40x perp (maint 1.25%), funding 0.1%/h,
    gap candles replay a perp high of `gap_hi`; 2h series keeps the pair
    in-universe. 1m returns [] when gap_hi is None (no-data path)."""
    now = now or int(time.time() * 1000)
    f = _stub_fetch(now)
    f["spot_pairs"] = lambda: (
        [{"pair_name": "@142", "index": 142, "base_name": "UBTC"}], {})
    f["post"] = lambda p: {"universe": [{"name": "BTC", "maxLeverage": 40}]}
    f["funding"] = lambda c, s, e: [(now - h * 3_600_000, 0.001)
                                    for h in range(48)]
    def candles(coin, iv, s, e, p):
        # 2h liquidity series only when the request spans a real history
        # window — a ~minutes-long gap query must fall through to the
        # interval-specific data (or [] = "no data for the missed window")
        if iv == "2h" and e - s >= 86_400_000:
            return [(now - i * 7_200_000, 100.0, 101.0, 99.0, 100.0,
                     900_000.0, 10) for i in range(450)]
        if iv == "1m" and gap_hi is not None:
            return [(s + 60_000, 100.0, gap_hi, 99.0, gap_hi, 500.0, 5)]
        return []
    f["candles"] = candles
    return f


@pytest.mark.asyncio
async def test_gap_catchup_missing_marks_gap_unverified(tmp_path) -> None:
    """P3.5: no candle data for the missed window -> episode flagged
    gap_unverified=1 and counted separately at the readout, not dropped."""
    now = int(time.time() * 1000)
    d = CarryShadowDaemon(db_path=str(tmp_path / "g.db"),
                          fetch=_btc_fetch(None, now))
    await d.boot()
    d.books["@142"] = BookSnap(99.0, 100.0)
    d.books["BTC"] = BookSnap(99.0, 100.0)
    await d._refresh_funding()
    ep = d.pool.get("@142")
    ep.on_book_entry(now, BookSnap(98.5, 98.9), BookSnap(100.1, 100.2))
    d._handle_entry_event(ep, "fill", now)
    await d._catchup_gap(now - 120_000, now)
    gu = d.ledger._con.execute(
        "SELECT gap_unverified FROM episodes WHERE id=?", (ep.id,)).fetchone()
    assert gu[0] == 1 and ep.state == "open"
    kinds = [r[0] for r in d.ledger._con.execute("SELECT kind FROM events")]
    assert "gap_catchup_missing" in kinds
    d.shutdown()


@pytest.mark.asyncio
async def test_fill_proxy_uses_trade_volume_at_our_price(tmp_path) -> None:
    """P4: trades feed accumulates aggressor USD at-or-better than our
    resting price; proxy recorded on fill_stats next to the strict gate."""
    now = int(time.time() * 1000)
    d = CarryShadowDaemon(db_path=str(tmp_path / "cs.db"),
                          fetch=_stub_fetch(now))
    await d.boot()
    d.books["PURR/USDC"] = BookSnap(99.0, 100.0)
    d.books["PURR"] = BookSnap(99.0, 100.0)
    await d._refresh_funding()
    ep = d.pool.get("PURR/USDC")
    assert ep is not None and ep.state == "pending_entry"
    # sell-aggressor prints below our 99.0 spot bid -> proxy evidence
    for _ in range(3):
        d._on_trade({"coin": "PURR/USDC", "px": "98.9", "sz": "40",
                     "side": "A", "time": now + 60_000})
    acc = d._proxy[(ep.id, "spot")]
    assert acc["vol_usd"] == pytest.approx(3 * 98.9 * 40)
    # resolve legs unfilled at window end -> proxy row still written
    # (books must NOT cross our resting prices or the strict model fills)
    d.books["PURR/USDC"] = BookSnap(99.0, 100.5)
    d.books["PURR"] = BookSnap(98.0, 100.5)
    ev = ep.on_book_entry(now + 901_000, d.books["PURR/USDC"], d.books["PURR"])
    d._handle_entry_event(ep, ev, now + 901_000)
    row = d.ledger._con.execute(
        "SELECT leg, filled, proxy_filled, proxy_vol_usd FROM fill_stats"
        " WHERE leg='spot'").fetchone()
    assert row is not None and row[1] == 0
    need = spec.SHADOW_NOTIONAL_USD
    assert row[2] == (1 if row[3] >= need else 0)
    assert row[3] == pytest.approx(3 * 98.9 * 40)
    d.shutdown()


def test_ledger_fill_rate_and_reload(tmp_path) -> None:
    led = Ledger(str(tmp_path / "cs.db"))
    for i in range(20):
        led.record_fill("spot", i < 9, 60.0 if i < 9 else None, i)
    n, rate = led.fill_rate("spot")
    assert n == 20 and rate == pytest.approx(0.45)
    ep = _ep()
    ep.state, ep.opened_ms, ep.p0, ep.s0 = "open", 1, 100.0, 99.0
    ep.id = led.episode_open(ep.row())
    led.close()
    # a fresh daemon reloads open episodes
    d = CarryShadowDaemon(db_path=str(tmp_path / "cs.db"),
                          fetch=_stub_fetch(int(time.time() * 1000)))
    d._reload_episodes()
    assert d.pool.get("PURR/USDC") is not None
    d.shutdown()


# ── unlegged-risk instrumentation (amendment 2026-10-09, measurement only) ──

def test_unlegged_adverse_excursion_tracked_and_latched() -> None:
    """Spot leg fills; the reference is the MISSING leg's (perp) own mid at
    that instant, so adverse excursion is basis-free. Perp mid then falls
    and recovers: worst excursion must latch the max."""
    ep = _ep()
    ep.place_entry(BookSnap(99.0, 100.0), BookSnap(99.0, 100.0))
    # spot fills at 99.0 bid; perp still pending, perp mid = 99.5 -> ref
    ep.on_book_entry(1_000, BookSnap(98.5, 98.9), BookSnap(99.0, 100.0))
    assert ep.unlegged_missing_leg == "perp"
    assert ep.unlegged_ref_price == 99.5
    # basis context: (perp_mid - spot_mid)/spot_mid at first fill
    assert ep.unlegged_basis_bps == pytest.approx(
        (99.5 - 98.7) / 98.7 * 1e4, rel=0.01)
    # perp mid drops to 98.0 (bid 97.5/ask 98.5): adverse for a pending
    # SELL = (99.5 - 98.0)/99.5 * 1e4 ~= 150.8 bps
    ep.on_book_entry(2_000, BookSnap(98.5, 98.9), BookSnap(97.5, 98.5))
    assert ep.unlegged_max_adverse_bps == pytest.approx(150.75, rel=0.01)
    # smaller adverse move doesn't lower the latch; favourable move ignored
    ep.on_book_entry(3_000, BookSnap(98.5, 98.9), BookSnap(98.0, 98.8))
    ep.on_book_entry(4_000, BookSnap(98.5, 98.9), BookSnap(99.5, 100.5))
    assert ep.unlegged_max_adverse_bps == pytest.approx(150.75, rel=0.01)
    # perp fills -> unlegged closed, duration + excursion kept
    ev = ep.on_book_entry(5_000, BookSnap(98.5, 98.9), BookSnap(100.1, 100.2))
    assert ev == "fill"
    assert ep.max_unlegged_s == 4.0
    assert ep.unlegged_max_adverse_bps == pytest.approx(150.75, rel=0.01)


def test_unlegged_adverse_pending_buy_leg() -> None:
    """Mirror image: perp sells first; the missing spot BUY leg suffers
    when the spot mid rises above its first-fill mid reference."""
    ep = _ep()
    ep.place_entry(BookSnap(99.0, 100.0), BookSnap(99.0, 100.0))
    # perp fills first (bid crosses our 100.0 ask); spot pending, ref = 99.5
    ep.on_book_entry(1_000, BookSnap(99.0, 100.0), BookSnap(100.1, 100.5))
    assert ep.unlegged_missing_leg == "spot"
    assert ep.unlegged_ref_price == 99.5
    # spot mid rises to 101.0: adverse for pending BUY ~= 150.8 bps
    ep.on_book_entry(2_000, BookSnap(100.5, 101.5), BookSnap(100.1, 100.5))
    assert ep.unlegged_max_adverse_bps == pytest.approx(150.75, rel=0.01)


def test_no_unlegged_means_no_instrumentation() -> None:
    """Both legs fill in one tick — never unlegged, metrics stay zero."""
    ep = _open_ep()
    assert ep.max_unlegged_s == 0.0
    assert ep.unlegged_max_adverse_bps == 0.0


def test_ledger_persists_unlegged_columns(tmp_path) -> None:
    """New columns land on the episode row at open/abort; NULL pre-existed."""
    led = Ledger(str(tmp_path / "cs.db"))
    cols = {r[1] for r in led._con.execute("PRAGMA table_info(episodes)")}
    assert {"unlegged_s", "unlegged_max_adverse_bps",
            "unlegged_basis_bps"} <= cols
    ep = _ep()
    ep.state = "pending_entry"
    ep.id = led.episode_open(ep.row())
    led.episode_update(ep.id, unlegged_s=12.5,
                       unlegged_max_adverse_bps=33.3,
                       unlegged_basis_bps=81.0)
    row = led._con.execute(
        "SELECT unlegged_s, unlegged_max_adverse_bps, unlegged_basis_bps"
        " FROM episodes WHERE id=?", (ep.id,)).fetchone()
    assert row == (12.5, pytest.approx(33.3), pytest.approx(81.0))
    led.close()


def test_daemon_emits_leg_fill_and_bbo(tmp_path) -> None:
    """Each leg fill emits a `leg_fill` event carrying both legs' BBO."""
    now = int(time.time() * 1000)
    d = CarryShadowDaemon(db_path=str(tmp_path / "cs.db"),
                          fetch=_stub_fetch(now))
    d.cands = [{"pair_name": "PURR/USDC", "base": "PURR", "perp": "PURR",
                "max_lev": 5.0, "maint": 0.1, "m": 0.45, "m0": 0.6,
                "pm": True}]
    for c in d.cands:
        d._register_candidate(c)
    d.in_universe["PURR/USDC"] = True
    d.books["PURR/USDC"] = BookSnap(99.0, 100.0)
    d.books["PURR"] = BookSnap(99.0, 100.0)
    import asyncio as _a
    _a.run(d._maybe_entry(d.cands[0], 0.20, now))
    ep = d.pool.get("PURR/USDC")
    assert ep is not None and ep.state == "pending_entry"
    # tick: spot fills only -> leg_fill event with bbo
    d.books["PURR/USDC"] = BookSnap(98.5, 98.9)
    d._on_pair_book(d.cands[0], now + 1_000)
    evs = [json.loads(r[0]) for r in d.ledger._con.execute(
        "SELECT data FROM events WHERE kind='leg_fill'")]
    assert len(evs) == 1 and evs[0]["leg"] == "spot"
    assert evs[0]["bbo"]["spot"]["mid"] == pytest.approx(98.7)
    assert evs[0]["bbo"]["perp"]["mid"] == pytest.approx(99.5)
    assert evs[0]["unlegged_basis_bps"] == pytest.approx(
        (99.5 - 98.7) / 98.7 * 1e4, rel=0.01)
    # unwind past the window -> event carries bbo + unlegged metrics
    d._handle_entry_event(
        ep, ep.on_book_entry(now + 901_000 + 60_000,
                             d.books["PURR/USDC"], d.books["PURR"]),
        now + 901_000 + 60_000)
    row = d.ledger._con.execute(
        "SELECT data FROM events WHERE kind='unlegged_unwind'").fetchone()
    assert row is not None
    blob = json.loads(row[0])
    assert blob["bbo"]["spot"] is not None
    assert blob["unlegged_s"] is not None
    erow = d.ledger._con.execute(
        "SELECT unlegged_s, unlegged_max_adverse_bps FROM episodes").fetchone()
    assert erow[0] is not None
    d.shutdown()
