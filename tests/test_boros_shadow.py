"""BorosLockedSpread tests — frozen-rule semantics + isolation guards.

Covers docs/PREREGISTER_BOROS_SPREAD.md Phase 1: market-universe
filtering, direction (receives the larger fixed), simple-YU vs spread
structures, the exact cost/capital formula, entry gates (8% APY,
14-90d, asset whitelist, dedup, daily cap), venue-unavailable
degradation, and the counts-only status board (no PnL leak).
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.boros_shadow import spec
from src.boros_shadow.books import book_mid, fetch_perp_book, vwap_slip_bps
from src.boros_shadow.boros_client import (
    fetch_boros_markets, parse_markets)
from src.boros_shadow.opportunity import (
    evaluate, needed_books, pick_winner)
from src.boros_shadow.ledger import BorosLedger
from src.boros_shadow.status import board
from src.boros_shadow.daemon import BorosShadowDaemon, hour_floor

pytestmark = pytest.mark.unit

PKG = Path(__file__).resolve().parents[1] / "src" / "boros_shadow"
FIX = Path(__file__).resolve().parents[0] / "fixtures" / "boros_shadow"
FORBIDDEN_PREFIXES = (
    "src.core", "src.strategies", "src.dashboard", "src.backtest",
    "src.data.database", "src.utils.config", "src.research", "main",
)
MS_D = spec.MS_D
MS_H = spec.MS_H


def ms(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc)
               .timestamp() * 1000)


NOW = ms(2026, 10, 15)
MAT = NOW + 30 * MS_D                       # 30d — inside [14,90]


def _market(market_id=1, name=None,
            venue="hyperliquid", asset="BTC", mat=MAT,
            bid=0.075, ask=0.077, spread=False, venue_b="",
            taker=5e14, settle=1e15, oi=100.0, state="Normal"):
    """BorosMarket-shaped row as parse_markets emits it."""
    from src.boros_shadow.boros_client import BorosMarket
    if name is None:  # unique per (venue) so _dedupe can't collapse rows
        name = f"{venue.title()} {asset} 14 Nov 2026"
    return BorosMarket(
        market_id=market_id, name=name, asset=asset, venue=venue,
        venue_b=venue_b, maturity_ms=mat, is_spread=spread,
        best_bid=bid, best_ask=ask, floating_apr=0.01,
        notional_oi=oi, taker_fee=taker / 1e18, settle_fee=settle / 1e18,
        state=state)


def _raw_row(market_id=1, name="Hyperliquid BTC 14 Nov 2026",
             venue="Hyperliquid", asset="BTC", mat_s=None,
             bid=0.075, ask=0.077, spread=False):
    return {
        "marketId": market_id,
        "imData": {"name": name,
                   "maturity": mat_s if mat_s is not None else MAT // 1000},
        "metadata": {"isSpreadMarket": spread, "platformName": venue,
                     "assetSymbol": asset},
        "data": {"bestBid": bid, "bestAsk": ask, "floatingApr": 0.01,
                 "notionalOI": 100.0},
        "config": {"takerFee": "500000000000000"},
        "extConfig": {"settleFeeRate": "1000000000000000"},
        "state": "Normal",
    }


def _deep_book(mid: float = 100.0, sz: float = 500.0):
    """Deep, tight book — VWAP slip for $10k ≈ 1 bp."""
    return ([(mid * 0.9999, sz)], [(mid * 1.0001, sz)])


def _books_for(*pairs, book=None):
    return {(v, a): (book or _deep_book()) for v, a in pairs}


# ─── isolation guards ─────────────────────────────────────────────────────────

def _pkg_imports() -> list:
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


def test_boros_shadow_never_imports_execution_surface() -> None:
    bad = [(f, m) for f, m in _pkg_imports()
           if m.startswith(FORBIDDEN_PREFIXES)]
    assert bad == [], f"forbidden imports: {bad}"


def test_boros_shadow_imports_only_boros_siblings() -> None:
    allowed = ("src.boros_shadow", "scripts.research")
    stdlib_ok = ("__future__", "asyncio", "collections", "dataclasses",
                 "datetime", "hashlib", "json", "logging", "pathlib",
                 "sqlite3", "statistics", "time", "typing", "urllib",
                 "zlib")
    for fname, mod in _pkg_imports():
        root = mod.split(".")[0]
        assert (mod.startswith(allowed[0]) or mod.startswith(allowed[1])
                or root in stdlib_ok), f"{fname}: unexpected import {mod}"


def test_boros_shadow_config_hash_neutral() -> None:
    text = "\n".join(f.read_text(encoding="utf-8") for f in PKG.glob("*.py"))
    for needle in ("settings.yaml", ".env", "load_config", "config_hash"):
        assert needle not in text


# ─── market parsing / universe ────────────────────────────────────────────────

class TestUniverse:
    def test_keeps_hl_leg_and_b_venue_yu(self) -> None:
        rows = [
            _raw_row(1, "Hyperliquid BTC 14 Nov 2026", "Hyperliquid", "BTC"),
            _raw_row(2, "Binance BTCUSDT 14 Nov 2026", "Binance", "BTC"),
            _raw_row(3, "Hyperliquid BTC - Gate BTCUSDT 14 Nov 2026",
                     "HL-GATE", "BTC", spread=True),
            _raw_row(4, "dYdX BTC-USD 14 Nov 2026", "dYdX", "BTC"),
            _raw_row(5, "Hyperliquid xyz:BRENTOIL 14 Nov 2026",
                     "Hyperliquid", "BRENTOIL"),
            _raw_row(6, "OKX ETHUSDT 14 Nov 2026", "OKX", "ETH"),
        ]
        out = parse_markets(rows)
        names = [m.name for m in out]
        assert "dYdX BTC-USD 14 Nov 2026" not in names      # no HL leg, not B YU
        assert len(out) == 5                               # 1-3 + BRENTOIL + OKX
        sp = [m for m in out if m.is_spread][0]
        assert sp.venue_b == "gate" and sp.asset == "BTC"
        fees = [m for m in out if m.market_id == 1][0]
        assert fees.taker_fee == pytest.approx(0.0005)     # 5e14/1e18
        assert fees.settle_fee == pytest.approx(0.001)

    def test_off_whitelist_assets_are_descriptive_only(self) -> None:
        """Prereg: assets outside {BTC,ETH,HYPE} are DESCRIPTIVE — priced
        and recorded with real math, never eligible to enter."""
        hl = _market(1, "Hyperliquid SOL 14 Nov 2026", "hyperliquid",
                     "SOL", bid=0.20, ask=0.21)
        b = _market(2, "Binance SOLUSDT 14 Nov 2026", "binance", "SOL",
                    bid=0.01, ask=0.02)
        books = _books_for(("hyperliquid", "SOL"), ("binance", "SOL"))
        out = evaluate([hl, b], books, NOW)
        assert len(out) == 2                       # recorded, not dropped
        assert all(not c.eligible for c in out)
        assert all(c.block_reason == "off_gate_asset" for c in out)
        assert all(c.net_apy is not None for c in out)   # priced for real

    def test_maturity_outside_window_skipped(self) -> None:
        for days in (5, 120):                       # outside [14,90]
            mat = NOW + days * MS_D
            hl = _market(1, mat=mat)
            b = _market(2, venue="binance", mat=mat)
            books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
            assert evaluate([hl, b], books, NOW) == []


# ─── direction + structure ────────────────────────────────────────────────────

class TestDirectionAndStructure:
    def test_direction_receives_larger_fixed(self) -> None:
        """HL fixed (0.20 bid) >> Binance fixed (0.02 ask): the winning
        side shorts the HL YU (receives fixed) and longs the cheap YU."""
        hl = _market(bid=0.20, ask=0.21)
        b = _market(2, venue="binance", bid=0.01, ask=0.02)
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        out = evaluate([hl, b], books, NOW)
        win = pick_winner(out)
        assert win.direction == "short_hl_long_b"
        assert win.fixed_bruto == pytest.approx(0.18)

    def test_reverse_direction_when_b_is_rich(self) -> None:
        hl = _market(bid=0.01, ask=0.02)
        b = _market(2, venue="binance", bid=0.20, ask=0.21)
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        win = pick_winner(evaluate([hl, b], books, NOW))
        assert win.direction == "short_b_long_hl"
        assert win.fixed_bruto == pytest.approx(0.18)

    def test_spread_beats_simple_when_fixed_higher(self) -> None:
        """Spread market bid 0.12 > simple capture (0.10 - 0.05) = 0.05."""
        hl = _market(bid=0.10, ask=0.11)
        b = _market(2, venue="gate", bid=0.04, ask=0.05)
        sp = _market(3, "Hyperliquid BTC - Gate BTCUSDT 14 Nov 2026",
                     venue="hl-gate", spread=True, venue_b="gate",
                     bid=0.12, ask=0.13)
        books = _books_for(("hyperliquid", "BTC"), ("gate", "BTC"))
        out = evaluate([hl, b, sp], books, NOW)
        win = pick_winner(out)
        assert (win.structure, win.direction) == ("spread", "short_spread")
        assert win.fixed_bruto == pytest.approx(0.12)
        # every candidate is still on the record
        assert {c.direction for c in out} == {
            "short_hl_long_b", "short_b_long_hl",
            "short_spread", "long_spread"}

    def test_long_spread_receives_when_ask_negative(self) -> None:
        sp = _market(3, venue="hl-gate", spread=True, venue_b="gate",
                     bid=-0.10, ask=-0.08)   # ask <0 -> buying RECEIVES 8%/yr
        books = _books_for(("hyperliquid", "BTC"), ("gate", "BTC"))
        out = evaluate([sp], books, NOW)
        win = pick_winner(out)
        assert win.direction == "long_spread"
        assert win.fixed_bruto == pytest.approx(0.08)


# ─── cost / capital math ─────────────────────────────────────────────────────

class TestCostAndCapital:
    def test_exact_formula_simple(self) -> None:
        """Hand-computed: N=10k, t=30/365y, slip~=1bp/side, fees 5e14+1e15."""
        hl = _market(bid=0.20, ask=0.21)
        b = _market(2, venue="binance", bid=0.01, ask=0.02)
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        c = [x for x in evaluate([hl, b], books, NOW)
             if x.direction == "short_hl_long_b"][0]
        t = 30 / 365.0
        slip = (abs(100 * 0.9999 - 100) / 100) * 1e4    # 1 bp
        perp = ((4.5 + slip) + (5.0 + slip)) / 1e4 * 10_000 * 2
        boros = 2 * (0.0005 + 0.001 + 0.0001) * 10_000 * t
        assert c.costs_usd == pytest.approx(perp + boros, abs=0.5)
        assert c.capital_usd == pytest.approx(0.2 * 10_000
                                            + 0.1 * 10_000 * 2)
        fixed_usd = 0.18 * 10_000 * t
        assert c.net_apy == pytest.approx(
            (fixed_usd - c.costs_usd) / c.capital_usd / t, rel=1e-6)
        assert c.eligible                              # ~38%/yr >= 8%

    def test_thin_spread_fails_apy_gate(self) -> None:
        """Realistic quotes (7.5% vs 4.6%) net well under 8%/yr."""
        hl = _market(bid=0.075, ask=0.077)
        b = _market(2, venue="binance", bid=0.042, ask=0.046)
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        c = pick_winner(evaluate([hl, b], books, NOW))
        assert c.net_apy < spec.NET_APY_MIN and not c.eligible
        assert "net_apy" in c.block_reason

    def test_spread_structure_pays_less_capital(self) -> None:
        sp = _market(3, venue="hl-gate", spread=True, venue_b="gate",
                     bid=0.20, ask=0.21)
        books = _books_for(("hyperliquid", "BTC"), ("gate", "BTC"))
        c = [x for x in evaluate([sp], books, NOW)
             if x.direction == "short_spread"][0]
        assert c.capital_usd == pytest.approx(0.2 * 10_000 + 0.1 * 10_000)


# ─── entry limits ─────────────────────────────────────────────────────────────

class TestEntryLimits:
    def _eligible_pair(self, venue="binance"):
        hl = _market(bid=0.20, ask=0.21)
        b = _market(2, venue=venue, bid=0.01, ask=0.02)
        return [hl, b]

    def test_one_open_per_triple(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        d = BorosShadowDaemon(db_path=str(tmp_path / "b.db"))
        d.ledger = led
        row = {"asset": "BTC", "venue_b": "binance", "maturity_ms": MAT,
               "structure": "simple", "direction": "short_hl_long_b",
               "state": "open", "entry_ms": NOW, "fixed_bruto": 0.18,
               "net_apy": 0.4, "capital_usd": 4000.0, "costs_usd": 20.0,
               "prices_json": "{}"}
        assert led.trade_insert(row) is not None
        assert led.trade_insert(dict(row)) is None    # partial unique idx
        led.close()

    def test_daily_cap_one_per_asset(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        day = NOW - NOW % MS_D
        for venue, mm in (("binance", MAT), ("gate", MAT)):
            led.trade_insert({"asset": "BTC", "venue_b": venue,
                              "maturity_ms": mm, "structure": "simple",
                              "direction": "x", "state": "open",
                              "entry_ms": NOW})
        assert led.entries_today_for_asset("BTC", day) == 2
        assert led.entries_today_for_asset("ETH", day) == 0
        led.close()

    def test_unfillable_book_blocked_thin(self) -> None:
        hl = _market(bid=0.20, ask=0.21)
        b = _market(2, venue="binance", bid=0.01, ask=0.02)
        thin = ([(99.0, 0.001)], [(101.0, 0.001)])    # $200 total depth
        books = {("hyperliquid", "BTC"): thin, ("binance", "BTC"): thin}
        out = evaluate([hl, b], books, NOW)
        # unpriced candidates are recorded blocked and can never win
        assert all(not c.eligible and c.block_reason == "book_thin"
                   for c in out)
        assert pick_winner(out) is None

    def test_venue_unavailable_marks_and_skips(self) -> None:
        """A venue without a book this snapshot is marked unavailable —
        its candidates are recorded blocked, others still evaluate."""
        hl = _market(bid=0.20, ask=0.21)
        bn = _market(2, venue="binance", bid=0.01, ask=0.02)
        ok = _market(3, venue="okx", bid=0.01, ask=0.02)
        books = {("hyperliquid", "BTC"): _deep_book(),
                 ("okx", "BTC"): _deep_book()}        # binance missing
        out = evaluate([hl, bn, ok], books, NOW)
        b_c = [c for c in out if c.venue_b == "binance"]
        o_c = [c for c in out if c.venue_b == "okx"]
        assert all(c.block_reason == "book_unavailable:binance"
                   and not c.eligible for c in b_c)
        assert pick_winner(o_c).eligible


# ─── book fetchers normalize ─────────────────────────────────────────────────

class TestBookFetchers:
    def _fixture(self, name):
        return json.loads((FIX / name).read_text(encoding="utf-8"))

    def _drive(self, venue, payload):
        calls = []
        def fake(url, body=None):
            calls.append(url)
            return payload
        return fetch_perp_book(venue, "BTC", fetch_fn=fake)

    def test_all_venues_normalize(self) -> None:
        for venue, fx in (("hyperliquid", "book_hyperliquid.json"),
                          ("binance", "book_binance.json"),
                          ("gate", "book_gate.json"),
                          ("okx", "book_okx.json"),
                          ("bybit", "book_bybit.json"),
                          ("kucoin", "book_kucoin.json"),
                          ("lighter", "book_lighter.json")):
            book = self._drive(venue, self._fixture(fx))
            assert book is not None, venue
            bids, asks = book
            assert bids[0][0] < asks[0][0]          # bid < ask
            assert all(isinstance(p, float) and isinstance(s, float)
                       for p, s in bids + asks)

    def test_failed_venue_returns_none(self) -> None:
        def boom(url, body=None):
            raise ConnectionError("down")
        assert fetch_perp_book("binance", "BTC", fetch_fn=boom) is None
        assert fetch_perp_book("nope", "BTC") is None

    def test_vwap_walk_and_mid(self) -> None:
        book = ([(99.9, 60.0), (99.8, 60.0)],
                [(100.1, 60.0), (100.2, 60.0)])
        mid = book_mid(book)
        assert mid == pytest.approx(100.0)
        s = vwap_slip_bps("sell", 10_000.0, book, mid)
        assert s == pytest.approx(14.0, abs=0.6)
        assert vwap_slip_bps("sell", 10_000_000.0, book, mid) is None

    def test_vwap_is_units_weighted(self) -> None:
        """Divergent levels: VWAP = Σusd/Σunits, not Σ(usd·px)/Σusd —
        asks 100x100 + 200x100, buy $15k -> 125 units -> vwap $120."""
        book = ([(99.0, 50.0)], [(100.0, 100.0), (200.0, 100.0)])
        s = vwap_slip_bps("buy", 15_000.0, book, 100.0)
        assert s == pytest.approx(2000.0)   # 20% above mid, not 33.3%


# ─── status board — counts only ───────────────────────────────────────────────

class TestStatusBoard:
    def test_counts_only_no_pnl_leak(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        led.trade_insert({"asset": "BTC", "venue_b": "binance",
                          "maturity_ms": MAT, "structure": "simple",
                          "direction": "short_hl_long_b", "state": "open",
                          "entry_ms": NOW, "fixed_bruto": 0.18,
                          "net_apy": 0.40})
        led.opportunity_insert({
            "ts_ms": NOW, "asset": "BTC", "venue_b": "gate",
            "maturity_ms": MAT, "structure": "simple",
            "direction": "short_hl_long_b", "fixed_bruto": 0.10,
            "net_apy": 0.06, "capital_usd": 4000.0, "costs_usd": 30.0,
            "ttm_days": 30.0, "eligible": 0, "entered": 0,
            "block_reason": "net_apy 0.0600 < 0.08", "legs_json": "{}"})
        b = board(led)
        blob = json.dumps(b).lower()
        for needle in ("pnl", "bps", "profit", "return_pct", "usd",
                       "apy", "fixed"):
            assert needle not in blob, f"status leaks {needle}"
        assert b["counts"]["entries"] == 1
        assert b["counts"]["open"] == 1
        assert b["counts"]["near_entries"] == 1      # 6% >= 5%, not entered
        assert b["read_gate"]["mode"] == "counts_only"
        led.close()


# ─── daemon end-to-end (stubbed fetchers) ────────────────────────────────────

class _StubFetch:
    """Two-venue world: HL BTC rich (20%), Binance BTC cheap (2%)."""

    def __init__(self, with_okx=True, binance_down=False):
        self.with_okx = with_okx
        self.binance_down = binance_down

    def markets(self):
        rows = [
            _raw_row(1, "Hyperliquid BTC 14 Nov 2026", "Hyperliquid",
                     "BTC", bid=0.20, ask=0.21),
            _raw_row(2, "Binance BTCUSDT 14 Nov 2026", "Binance",
                     "BTC", bid=0.01, ask=0.02),
        ]
        if self.with_okx:
            rows.append(_raw_row(3, "OKX BTCUSDT 14 Nov 2026", "OKX",
                                 "BTC", bid=0.01, ask=0.02))
        return rows  # fetch_boros_markets() returns the flat row list

    def book(self, venue, asset):
        if venue == "binance" and self.binance_down:
            return None
        return _deep_book()

    def funding(self, venue, asset, t0, t1):
        return []                                  # hermetic — no net

    def rates(self):
        return {}                                  # no Lighter sample


class TestDaemonLifecycle:
    def _daemon(self, tmp_path, stub=None):
        stub = stub or _StubFetch()
        return BorosShadowDaemon(
            db_path=str(tmp_path / "b.db"),
            fetch={"markets": stub.markets, "book": stub.book,
                   "funding": stub.funding, "rates": stub.rates})

    def test_snapshot_registers_entry(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path)
        snap_t = hour_floor(NOW) + 5 * 60_000       # :05 sharp
        d.tick(snap_t)
        trades = d.ledger.trades_by_state("open")
        # cap = 1 new entry per asset/day: two BTC pairs → exactly one
        # enters (equal quotes → binance, first venue in B_VENUES)
        assert len(trades) == 1
        t = trades[0]
        assert (t["asset"], t["venue_b"]) == ("BTC", "binance")
        assert t["direction"] == "short_hl_long_b"
        legs = json.loads(t["prices_json"])
        assert legs["hl_perp_side"] == "sell" and legs["b_perp_side"] == "buy"
        assert "hl_bid" in legs and "b_ask" in legs
        # prereg: "guardar o trade com todos os preços de entrada"
        assert legs["hl_perp_mid"] and legs["b_perp_mid"]
        assert legs["hl_perp_px"] < legs["hl_perp_mid"]   # sell below mid
        assert legs["b_perp_px"] > legs["b_perp_mid"]     # buy above mid
        opps = d.ledger.count("boros_opportunities")
        assert opps == 4                            # 2 venues x 2 directions
        capped = d.ledger.count(
            "boros_opportunities", "WHERE block_reason='daily_cap'")
        assert capped == 1                          # okx winner lost the slot
        d.shutdown()

    def test_second_snapshot_dedupes_open(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path)
        d.tick(hour_floor(NOW) + 5 * 60_000)
        d.tick(hour_floor(NOW) + spec.MS_H + 5 * 60_000)   # next hour
        assert len(d.ledger.trades_by_state("open")) == 1  # no doubles
        blocked = d.ledger.count(
            "boros_opportunities", "WHERE block_reason='already_open'")
        assert blocked == 1                         # the open binance pair
        d.shutdown()

    def test_daily_cap_blocks_second_asset_entry(self,
                                                 tmp_path: Path) -> None:
        """With today's BTC slot already consumed, the snapshot registers
        nothing new and marks winners daily_cap."""
        led = BorosLedger(tmp_path / "b.db")
        led.trade_insert({"asset": "BTC", "venue_b": "gate",
                          "maturity_ms": MAT, "structure": "simple",
                          "direction": "x", "state": "open",
                          "entry_ms": NOW})
        led.close()
        d = self._daemon(tmp_path)
        d.tick(hour_floor(NOW) + 5 * 60_000)
        assert len(d.ledger.trades_by_state("open")) == 1   # only the seed
        capped = d.ledger.count(
            "boros_opportunities", "WHERE block_reason='daily_cap'")
        assert capped == 2                                  # bn + okx winners
        d.shutdown()

    def test_maturity_closes_on_window_pass(self, tmp_path: Path) -> None:
        """Phase-2: at/after maturity the trade CLOSES on the first
        on-schedule pass (real VWAP exit + PnL row), not just a state
        flip. An off-schedule pass only marks it."""
        d = self._daemon(tmp_path)
        d.ledger.trade_insert({
            "asset": "BTC", "venue_b": "binance", "maturity_ms": MAT,
            "structure": "simple", "direction": "short_hl_long_b",
            "state": "open", "entry_ms": MAT - 30 * MS_D,
            "fixed_bruto": 0.18, "net_apy": 0.4, "capital_usd": 4000.0,
            "costs_usd": 20.0,
            "prices_json": json.dumps({
                "hl_yu": 1, "b_yu": 2,
                "hl_perp_side": "sell", "b_perp_side": "buy",
                "hl_perp_mid": 100.0, "b_perp_mid": 100.0,
                "hl_perp_px": 99.99, "b_perp_px": 100.01,
                "slip_hl_bps": 1.0, "slip_b_bps": 1.0,
                "hl_bid": 0.20, "hl_ask": 0.21,
                "b_bid": 0.01, "b_ask": 0.02})})
        d.tick(MAT + MS_H)                            # :00 descriptive
        assert d.ledger.trades_by_state("open")       # marked, not closed
        d.tick(hour_floor(MAT + MS_H) + 5 * 60_000)   # :05 on-schedule
        assert not d.ledger.trades_by_state("open")
        closed = d.ledger.trades_by_state("closed")
        assert len(closed) == 1
        close = d.ledger.closes()[0]
        assert close["reason"] == "maturity"
        assert close["realized_usd"] is not None
        d.shutdown()

    def test_off_schedule_snapshot_never_enters(self,
                                                tmp_path: Path) -> None:
        """Prereg: snapshots are at :05 UTC. A catch-up/restart pass at
        :23 records everything descriptively but can never open a trade."""
        d = self._daemon(tmp_path)
        d.tick(hour_floor(NOW) + 23 * 60_000)          # :23 — off-schedule
        assert not d.ledger.trades_by_state("open")    # zero entries
        off = d.ledger.count(
            "boros_opportunities", "WHERE block_reason='off_schedule'")
        assert off == 2                                # both venue winners
        meta = d.ledger._con.execute(
            "SELECT meta_json FROM boros_snapshots WHERE kind='markets'"
            " ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert json.loads(meta)["on_schedule"] is False
        # the off-schedule pass did NOT consume the hour
        assert d.ledger.meta_get("last_snapshot_hour") is None
        assert d.ledger.meta_get("last_pass_hour") is not None
        d.shutdown()

    def test_restart_at_02_descriptive_then_05_enters(self,
                                                      tmp_path: Path) -> None:
        """Restart at :02 runs a descriptive pass; the real :05 window
        pass of the SAME hour still runs and can open trades."""
        d = self._daemon(tmp_path)
        d.tick(hour_floor(NOW) + 2 * 60_000)         # :02 — descriptive
        assert not d.ledger.trades_by_state("open")
        assert d.ledger.count("boros_opportunities",
                              "WHERE block_reason='off_schedule'") == 2
        assert d.ledger.meta_get("last_snapshot_hour") is None
        d.tick(hour_floor(NOW) + 5 * 60_000)         # :05 — on-schedule
        assert len(d.ledger.trades_by_state("open")) == 1
        assert d.ledger.meta_get("last_snapshot_hour") is not None
        d.shutdown()

    def test_minute_07_is_on_schedule(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path)
        d.tick(hour_floor(NOW) + 7 * 60_000)         # :07 — inside window
        assert len(d.ledger.trades_by_state("open")) == 1
        meta = json.loads(d.ledger._con.execute(
            "SELECT meta_json FROM boros_snapshots WHERE kind='markets'"
            " ORDER BY id DESC LIMIT 1").fetchone()[0])
        assert meta["on_schedule"] is True and meta["minute_utc"] == 7
        d.shutdown()

    def test_minute_12_is_off_schedule(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path)
        d.tick(hour_floor(NOW) + 12 * 60_000)        # :12 — past window
        assert not d.ledger.trades_by_state("open")
        assert d.ledger.count("boros_opportunities",
                              "WHERE block_reason='off_schedule'") == 2
        d.shutdown()

    def test_two_window_ticks_count_once(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path)
        d.tick(hour_floor(NOW) + 5 * 60_000)
        d.tick(hour_floor(NOW) + 8 * 60_000)         # still in window
        # one pass only: 2 snapshot rows total (markets + books)
        assert d.ledger.count("boros_snapshots") == 2
        assert len(d.ledger.trades_by_state("open")) == 1
        d.shutdown()

    def test_venue_down_still_snapshots(self, tmp_path: Path) -> None:
        d = self._daemon(tmp_path, _StubFetch(binance_down=True))
        d.tick(hour_floor(NOW) + 5 * 60_000)
        trades = d.ledger.trades_by_state("open")
        assert {t["venue_b"] for t in trades} == {"okx"}
        blocked = d.ledger.count(
            "boros_opportunities",
            "WHERE block_reason='book_unavailable:binance'")
        assert blocked == 2
        d.shutdown()


# ─── real fixture ─────────────────────────────────────────────────────────────

class TestRealFixture:
    def test_today_payload_parses_and_evaluates(self) -> None:
        payload = json.loads((FIX / "markets.json")
                             .read_text(encoding="utf-8"))
        markets = parse_markets(payload["results"])
        assert len(markets) >= 30                     # universe kept
        assert all(m.name.startswith("Hyperliquid ")
                   or m.venue in spec.B_VENUES for m in markets)
        books = {}
        for venue, asset in needed_books(markets, ms(2026, 10, 15)):
            books[(venue, asset)] = _deep_book()
        out = evaluate(markets, books, ms(2026, 10, 15))
        assert out                                     # candidates produced
        for c in out:
            if c.asset not in spec.ASSETS:             # descriptive layer
                assert not c.eligible
                assert c.block_reason in ("off_gate_asset",
                                          "book_thin") or \
                    c.block_reason.startswith("book_unavailable")
        assert all(spec.MIN_MATURITY_D <= c.ttm_days
                   <= spec.MAX_MATURITY_D for c in out)

    def test_fetch_json_sends_json_content_type(self, monkeypatch) -> None:
        """Regression: HL /info rejects POSTs without application/json —
        the Mac smoke caught all-HL books unavailable for exactly this."""
        import src.boros_shadow.boros_client as bc
        seen = {}
        class _Resp:
            def read(self):
                return b"{}"
        def fake_urlopen(req, timeout=None):
            seen["ct"] = req.get_header("Content-type")
            return _Resp()
        monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
        bc.fetch_json("https://api.hyperliquid.xyz/info", body=b"{}")
        assert seen["ct"] == "application/json"

    def test_pagination_collects_all_pages(self, monkeypatch) -> None:
        import src.boros_shadow.boros_client as bc
        pages = iter([{"results": [{"x": i} for i in range(100)]},
                      {"results": [{"x": 200}]},
                      {"results": []}])
        monkeypatch.setattr(bc, "fetch_json", lambda url, **kw: next(pages))
        assert len(fetch_boros_markets()) == 101


# ─── Phase 2: marks / funding / liquidation / close / read ────────────────────

from src.boros_shadow.marks import (
    liquidation_hits, mark_trade, split_flags, trade_legs)
from src.boros_shadow.settle import (
    close_trade, closed_clusters, n5_kill)
from src.boros_shadow.read import run_read
from src.boros_shadow.backfill import backfill
from src.boros_shadow import risk


def _open_trade(led, venue="binance", direction="short_hl_long_b",
                structure="simple", entry=NOW, mat=MAT, fixed=0.18,
                net_apy=0.4, capital=4000.0, costs=23.0,
                entry_px=100.0, asset="BTC"):
    """Open paper trade with a full legs_json — every entry price."""
    legs = {
        "hl_yu": 1, "b_yu": 2,
        "hl_perp_side": "sell" if direction == "short_hl_long_b" else "buy",
        "b_perp_side": "buy" if direction == "short_hl_long_b" else "sell",
        "hl_perp_mid": entry_px, "b_perp_mid": entry_px,
        "hl_perp_px": entry_px - 0.01
        if direction == "short_hl_long_b" else entry_px + 0.01,
        "b_perp_px": entry_px + 0.01
        if direction == "short_hl_long_b" else entry_px - 0.01,
        "slip_hl_bps": 1.0, "slip_b_bps": 1.0,
        "hl_bid": 0.20, "hl_ask": 0.21, "b_bid": 0.01, "b_ask": 0.02,
        "sp_bid": 0.20, "sp_ask": 0.21,
    }
    if structure == "spread":
        legs["spread"] = 9
        legs["hl_perp_side"] = ("sell" if direction == "short_spread"
                                else "buy")
        legs["b_perp_side"] = ("buy" if direction == "short_spread"
                               else "sell")
    tid = led.trade_insert({
        "asset": asset, "venue_b": venue, "maturity_ms": mat,
        "structure": structure, "direction": direction, "state": "open",
        "entry_ms": entry, "fixed_bruto": fixed, "net_apy": net_apy,
        "capital_usd": capital, "costs_usd": costs,
        "prices_json": json.dumps(legs)})
    return led.trades_by_state("open")[-1]


def _markets_by_id(hl_apr=0.10, b_apr=0.02, sp_apr=0.05, state="Normal"):
    hl = _market(1, "Hyperliquid BTC 14 Nov 2026", "hyperliquid", "BTC",
                 bid=0.20, ask=0.21, state=state)
    b = _market(2, "Binance BTCUSDT 14 Nov 2026", "binance", "BTC",
                bid=0.01, ask=0.02, state=state)
    sp = _market(9, "Hyperliquid BTC - Binance BTCUSDT 14 Nov 2026",
                 "hl-binance", "BTC", spread=True, venue_b="binance",
                 bid=0.20, ask=0.21, state=state)
    hl.floating_apr, b.floating_apr, sp.floating_apr = hl_apr, b_apr, sp_apr
    return {1: hl, 2: b, 9: sp}


_NO_FUNDING = lambda *a, **k: []      # noqa: E731 — hermetic tests


class TestPhase2Marks:
    def test_mark_records_mids_vwap_funding(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        m = mark_trade(led, t, _markets_by_id(), books,
                       NOW + spec.MS_H, False, _NO_FUNDING)
        legs = json.loads(m["legs_json"])
        assert legs["hl"]["mid"] == pytest.approx(100.0)
        assert legs["hl"]["exit_px"]                      # close px priced
        assert legs["hl"]["stale"] == 0
        yu = json.loads(m["yu_json"])["legs"]
        assert {y["market_id"] for y in yu} == {1, 2}
        assert all(y["floating_source"] == "boros_floating_apr"
                   for y in yu)
        # floating: short HL pays 10%/yr, long B receives 2%/yr
        dt = spec.MS_H / spec.MS_YEAR
        assert m["floating_usd"] == pytest.approx(
            (-0.10 + 0.02) * 10_000 * dt, rel=1e-6)
        assert m["on_schedule"] == 0
        led.close()

    def test_stale_carry_and_data_gap(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        mark_trade(led, t, _markets_by_id(), books, NOW + spec.MS_H,
                   False, _NO_FUNDING)
        # next pass: books missing -> stale carry of last mid
        m = mark_trade(led, t, _markets_by_id(), {}, NOW + 2 * spec.MS_H,
                       False, _NO_FUNDING)
        legs = json.loads(m["legs_json"])
        assert legs["hl"]["stale"] == 1
        assert legs["hl"]["mid"] == pytest.approx(100.0)  # carried
        assert m["data_gap"] == 0
        # >6h stale -> data_gap
        m2 = mark_trade(led, t, _markets_by_id(), {},
                        NOW + 9 * spec.MS_H, False, _NO_FUNDING)
        assert m2["data_gap"] == 1
        led.close()


class TestPhase2Liquidation:
    def _pooled_equity_edge(self, tmp_path, mid_move_pct):
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)                          # hl short, b long
        books = {
            ("hyperliquid", "BTC"):
                ([(100 * (1 + mid_move_pct) * 0.9999, 500.0)],
                 [(100 * (1 + mid_move_pct) * 1.0001, 500.0)]),
            ("binance", "BTC"): _deep_book()}
        m = mark_trade(led, t, _markets_by_id(), books,
                       NOW + spec.MS_H, False, _NO_FUNDING)
        return led, t, m

    def test_pooled_death_at_threshold(self, tmp_path: Path) -> None:
        # hl short loses ~19% => equity 2000-1900=100 = floor -> dead
        led, t, m = self._pooled_equity_edge(tmp_path, 0.19)
        assert m["equity_pooled"] <= spec.MM_FLOOR_PER_LEG * 10_000 * 2 + 1
        assert "pooled" in liquidation_hits(t, m, NOW + spec.MS_H)
        led.close()

    def test_pooled_alive_just_above(self, tmp_path: Path) -> None:
        led, t, m = self._pooled_equity_edge(tmp_path, 0.15)
        assert m["equity_pooled"] > spec.MM_FLOOR_PER_LEG * 10_000 * 2
        assert liquidation_hits(t, m, NOW + spec.MS_H) == []
        led.close()

    def test_boros_leg_death(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)
        mkts = _markets_by_id()
        mkts[1].best_bid, mkts[1].best_ask = 1.40, 1.44   # mid 1.42 vs 0.20
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        m = mark_trade(led, t, mkts, books, NOW + spec.MS_H,
                       False, _NO_FUNDING)
        hits = liquidation_hits(t, m, NOW + spec.MS_H)
        assert "boros" in hits
        led.close()

    def test_spread_counts_one_boros_leg(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led, structure="spread",
                        direction="short_spread")
        legs = trade_legs(t)
        assert len(legs["yu"]) == 1
        assert legs["yu"][0]["market_id"] == 9
        led.close()

    def test_split_flag_at_30pct(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)                          # hl short
        books = {
            ("hyperliquid", "BTC"):
                ([(129.0, 500.0)], [(131.0, 500.0)]),  # mid ~130 (+30%)
            ("binance", "BTC"): _deep_book()}
        m = mark_trade(led, t, _markets_by_id(), books,
                       NOW + spec.MS_H, False, _NO_FUNDING)
        legs_hit = split_flags(t, m)
        assert "hl" in legs_hit and "b" not in legs_hit
        led.close()


class TestPhase2Funding:
    def test_funding_sign_per_leg(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)                          # hl short, b long
        evs = {"hyperliquid": [(NOW + 10, 0.0001)],
               "binance": [(NOW + 10, 0.0001)]}
        fund = lambda v, a, t0, t1: evs.get(v, [])
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        m = mark_trade(led, t, _markets_by_id(), books,
                       NOW + spec.MS_H, False, fund)
        rows = led._con.execute(
            "SELECT leg, cash_usd FROM boros_funding ORDER BY leg") \
            .fetchall()
        # short perp receives rate>0; long pays
        assert dict(rows)["b"] == pytest.approx(-1.0)
        assert dict(rows)["hl"] == pytest.approx(+1.0)
        assert m["funding_usd"] == pytest.approx(0.0)
        led.close()

    def test_venue_proxy_cancels_mismatch(self, tmp_path: Path) -> None:
        """Market row absent -> floating_source=venue_proxy and the
        interval's floating exactly cancels the leg's funding cash."""
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)
        evs = {"hyperliquid": [(NOW + 10, 0.0001)],
               "binance": [(NOW + 10, -0.0002)]}
        fund = lambda v, a, t0, t1: evs.get(v, [])
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        m = mark_trade(led, t, {}, books, NOW + spec.MS_H, False, fund)
        yu = json.loads(m["yu_json"])["legs"]
        assert all(y["floating_source"] == "venue_proxy" for y in yu)
        # perp cash: hl short +1.0 (recv), b long -(-0.0002)*N = +2.0;
        # floating proxy = -(hl cash) -(b cash) = -3.0 -> cancels to 0
        assert m["floating_usd"] == pytest.approx(-3.0)
        # mismatch = funding + floating = 0 exactly
        assert m["funding_usd"] + m["floating_usd"] == pytest.approx(0.0)
        led.close()

    def test_venue_failure_proxies_to_zero_mismatch(
            self, tmp_path: Path) -> None:
        """Venue history fails in an interval -> the leg's funding is
        proxied to cancel the measured floating (venue_proxy): mismatch
        contribution exactly 0, never funding=0 with floating counted."""
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)                          # hl short, b long
        evs = {"hyperliquid": [(NOW + 10, 0.0001)]}   # b fetch fails
        fund = lambda v, a, t0, t1: evs.get(v) if v == "hyperliquid" \
            else None
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        m = mark_trade(led, t, _markets_by_id(), books,
                       NOW + spec.MS_H, False, fund)
        legs = json.loads(m["legs_json"])
        assert legs["b"]["funding_source"] == "venue_proxy"
        assert legs["hl"]["funding_source"] == "venue"
        dt = spec.MS_H / spec.MS_YEAR
        fl_b = 0.02 * 10_000 * dt                     # long YU receives
        rows = led._con.execute(
            "SELECT leg, cash_usd, source FROM boros_funding"
            " WHERE leg='b'").fetchall()
        assert rows[0][2] == "venue_proxy"
        # proxy cash exactly cancels that leg's floating -> mismatch 0
        assert rows[0][1] == pytest.approx(-fl_b, rel=1e-6)
        led.close()

    def test_lighter_leg_uses_sampled_series(self, tmp_path: Path) -> None:
        """Lighter has no public history: the daemon samples the current
        rate per pass; the leg accumulates rate x N x dt_hours forward,
        source='lighter_sampled'."""
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led, venue="lighter")          # b leg = lighter
        books = _books_for(("hyperliquid", "BTC"), ("lighter", "BTC"))
        m = mark_trade(led, t, _markets_by_id(), books,
                       NOW + spec.MS_H, False, _NO_FUNDING,
                       {"BTC": 0.0001})
        legs = json.loads(m["legs_json"])
        assert legs["b"]["funding_source"] == "lighter_sampled"
        # long perp pays rate>0: -0.0001 x $10k x 1/8 of an 8h period
        # (endpoint rate is per-8h — pinned vs Boros floatingApr)
        rows = led._con.execute(
            "SELECT leg, cash_usd, source FROM boros_funding"
            " WHERE leg='b'").fetchall()
        assert rows[0][2] == "lighter_sampled"
        assert rows[0][1] == pytest.approx(-0.125)
        led.close()

    def test_lighter_no_sample_zero_mismatch(self,
                                             tmp_path: Path) -> None:
        """Interval without a Lighter sample -> leg funding proxied to
        the measured floating: the leg's mismatch is exactly 0."""
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led, venue="lighter")
        books = _books_for(("hyperliquid", "BTC"), ("lighter", "BTC"))
        m = mark_trade(led, t, _markets_by_id(), books,
                       NOW + spec.MS_H, False, _NO_FUNDING,
                       {})                            # no sample
        legs = json.loads(m["legs_json"])
        assert legs["b"]["funding_source"] == "venue_proxy"
        dt = spec.MS_H / spec.MS_YEAR
        fl_b = 0.02 * 10_000 * dt                     # b YU floating leg
        rows = led._con.execute(
            "SELECT cash_usd, source FROM boros_funding"
            " WHERE leg='b'").fetchall()
        assert rows[0][1] == "venue_proxy"
        assert rows[0][0] == pytest.approx(-fl_b, rel=1e-6)
        led.close()

    def test_rates_table_roundtrip(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        led.rate_insert(NOW, "BTC", 0.0001)
        led.rate_insert(NOW + spec.MS_H, "BTC", -0.0002)
        led.rate_insert(NOW + spec.MS_H, "ETH", 0.0003)
        assert led.rates_at(NOW + spec.MS_H // 2) == {"BTC": 0.0001}
        assert led.rates_at(NOW + spec.MS_H) == {
            "BTC": pytest.approx(-0.0002), "ETH": 0.0003}
        led.close()


class TestPhase2Close:
    def test_fallback_close_after_24h(self, tmp_path: Path) -> None:
        """Book still missing >24h after maturity -> closes at the last
        known mid with slip = 2x entry slip, close_fallback flag."""
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        mark_trade(led, t, _markets_by_id(), books, MAT - spec.MS_H,
                   False, _NO_FUNDING)               # fresh mids on record
        mark = mark_trade(led, t, _markets_by_id(), {}, MAT + spec.MS_H,
                          True, _NO_FUNDING)         # stale carry mid=100
        cid = close_trade(led, t, {}, _markets_by_id(),
                          MAT + 25 * spec.MS_H, "maturity", mark)
        assert cid is not None
        row = led.closes()[0]
        flags = json.loads(row["flags_json"])
        assert flags.get("close_fallback") is True
        assert flags["exit"]["hl"]["exit_slip_bps"] == pytest.approx(2.0)
        led.close()

    def test_maturity_close_waits_for_book(self, tmp_path: Path) -> None:
        """Inside the 24h grace, a missing book defers the close."""
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)
        mark = mark_trade(led, t, _markets_by_id(), {}, MAT + spec.MS_H,
                          True, _NO_FUNDING)
        assert close_trade(led, t, {}, _markets_by_id(),
                           MAT + spec.MS_H, "maturity", mark) is None
        assert led.trades_by_state("open")               # still open
        led.close()

    def test_pnl_components_hand_computed(self, tmp_path: Path) -> None:
        """Flat books at exit, no funding, floatingApr None -> proxy=0:
        realized = fixed + basis - costs, component by component."""
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)                          # entry ~100 both
        mkts = _markets_by_id(hl_apr=None, b_apr=None)
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        mark = mark_trade(led, t, mkts, books, MAT + spec.MS_H,
                          True, _NO_FUNDING)
        cid = close_trade(led, t, books, mkts, MAT + spec.MS_H,
                          "maturity", mark)
        row = [c for c in led.closes() if c["id"] == cid][0]
        t_held = (MAT - NOW) / spec.MS_YEAR
        assert row["fixed_usd"] == pytest.approx(0.18 * 10_000 * t_held)
        assert row["floating_usd"] == pytest.approx(0.0)
        # basis: both legs re-buy/re-sell ~2c worse than entry x qty~100
        assert row["basis_usd"] == pytest.approx(-4.0, abs=0.6)
        # costs: entry (4.5+1)+(5+1)=11.5, exit ~11.5, boros = 23-23 = 0
        assert row["costs_usd"] == pytest.approx(23.0, abs=1.0)
        assert row["realized_usd"] == pytest.approx(
            row["fixed_usd"] + row["basis_usd"] - row["costs_usd"],
            rel=1e-6)
        assert row["locked_usd"] == pytest.approx(0.4 * 4000 * t_held)
        assert row["ratio"] == pytest.approx(
            row["realized_usd"] / row["locked_usd"], rel=1e-6)
        led.close()

    def test_liquidated_close_loses_layer_margin(self,
                                                 tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led)
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        mark = mark_trade(led, t, _markets_by_id(), books,
                          NOW + spec.MS_H, False, _NO_FUNDING)
        close_trade(led, t, books, _markets_by_id(), NOW + spec.MS_H,
                    "liquidated_pooled", mark)
        row = led.closes()[0]
        assert row["realized_usd"] == pytest.approx(-2000.0)  # -0.2*N
        assert led.trades_by_state("liquidated")
        led.close()


class TestPhase2ClustersAndGates:
    def _closed(self, led, asset="BTC", entry=NOW, close=MAT):
        t = _open_trade(led, asset=asset, entry=entry, mat=close)
        books = _books_for(("hyperliquid", asset), ("binance", asset))
        mark = mark_trade(led, t, _markets_by_id(), books,
                          close + spec.MS_H, True, _NO_FUNDING)
        close_trade(led, t, books, _markets_by_id(),
                    close + spec.MS_H, "maturity", mark)

    def test_overlapping_windows_share_cluster(self,
                                               tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        self._closed(led, entry=NOW, close=MAT)
        self._closed(led, entry=NOW + 5 * MS_D, close=MAT + 5 * MS_D)
        self._closed(led, entry=NOW, close=MAT + 20 * MS_D)
        ids = [c["cluster_id"] for c in led.closes()]
        assert len(set(ids)) == 1                     # chained overlap
        led.close()

    def test_disjoint_windows_split_clusters(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        self._closed(led, entry=NOW, close=NOW + 10 * MS_D)
        self._closed(led, entry=NOW + 60 * MS_D, close=NOW + 90 * MS_D)
        self._closed(led, asset="ETH", entry=NOW, close=NOW + 10 * MS_D)
        ids = [c["cluster_id"] for c in led.closes()]
        assert len(set(ids)) == 3                     # gap + diff asset
        assert closed_clusters(led) == 3
        led.close()

    def test_n5_flag_on_bad_median(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        for i in range(5):
            t = _open_trade(led, entry=NOW + i, mat=MAT + i)
            led.close_insert({
                "trade_id": t["id"], "ts_ms": MAT + spec.MS_H,
                "reason": "maturity", "flags_json": "{}",
                "fixed_usd": 10, "floating_usd": 0, "basis_usd": -50,
                "costs_usd": 20, "realized_usd": -60,
                "realized_apy_cap": -0.5, "locked_usd": 130,
                "ratio": -0.46, "cluster_id": t["id"]})
        assert n5_kill(led) is True                   # median < 0.5
        led.close()

    def test_n5_not_triggered_on_good_median(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        for i in range(5):
            t = _open_trade(led, entry=NOW + i, mat=MAT + i)
            led.close_insert({
                "trade_id": t["id"], "ts_ms": MAT + spec.MS_H,
                "reason": "maturity", "flags_json": "{}",
                "fixed_usd": 130, "floating_usd": 0, "basis_usd": -10,
                "costs_usd": 20, "realized_usd": 100,
                "realized_apy_cap": 0.30, "locked_usd": 130,
                "ratio": 0.77, "cluster_id": t["id"]})
        assert n5_kill(led) is False
        led.close()

    def test_read_refuses_before_gate(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        self._closed(led)
        out = run_read(led, NOW)
        assert out["refused"] is True
        assert out["closed_clusters"] == 1
        led.close()

    def test_read_reports_after_expiry(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        self._closed(led)
        out = run_read(led, ms(2027, 7, 1))           # past expiry
        assert out["refused"] is False
        assert out["n_closed_trades"] == 1
        assert out["verdict"] in ("PASS", "FAIL")
        assert "criteria" in out and "profit_factor" in out
        led.close()

    def test_status_counts_only_with_kill_flags(self,
                                                tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        self._closed(led)
        led.flag_set("liquidation", 1, NOW, {"layer": "x"})
        b = board(led)
        assert b["counts"]["closed"] == 1
        assert b["counts"]["closed_clusters"] == 1
        assert b["kill_flags"]["liquidation"] is True
        assert b["kill_flags"]["n5_kill"] is False
        blob = json.dumps(b).lower()
        for needle in ("pnl", "bps", "profit", "return_pct", "usd",
                       "apy", "fixed", "ratio"):
            assert needle not in blob, f"status leaks {needle}"
        led.close()

    def test_backfill_replays_stored_snapshots(self,
                                               tmp_path: Path) -> None:
        """A trade opened before deploy gets marks rebuilt from the
        compressed snapshot pairs, pass by pass, idempotently."""
        led = BorosLedger(tmp_path / "b.db")
        rows = [_raw_row(1, "Hyperliquid BTC 14 Nov 2026", "Hyperliquid",
                         "BTC", bid=0.20, ask=0.21),
                _raw_row(2, "Binance BTCUSDT 14 Nov 2026", "Binance",
                         "BTC", bid=0.01, ask=0.02)]
        for ts in (NOW + spec.MS_H, NOW + 2 * spec.MS_H):
            led.snapshot_write(ts, "markets", rows, n_rows=2,
                               meta={"on_schedule": True})
            led.snapshot_write(ts, "books",
                               {"hyperliquid:BTC": _deep_book(),
                                "binance:BTC": _deep_book()},
                               n_rows=2, meta={"on_schedule": True})
        t = _open_trade(led, entry=NOW)
        stats = backfill(led, _NO_FUNDING)
        assert stats["marks"] == 2
        last = led.last_mark(t["id"])
        assert last["ts_ms"] == NOW + 2 * spec.MS_H
        stats2 = backfill(led, _NO_FUNDING)          # idempotent
        assert stats2["marks"] == 0
        led.close()


# ─── spread-market conformance (2026-10-19) ──────────────────────────────────

# Trimmed verbatim live row — marketId 232 fetched from
# open-api/v1/markets on 2026-10-19 (ETH HL-Gate spread, 27 Nov 2026).
_SPREAD_232_ROW = {
    "marketId": 232,
    "imData": {"name": "Hyperliquid ETH - Gate ETHUSDT 27 Nov 2026",
               "symbol": "HYPERLIQUID-ETH-GATE-ETHUSDT-27NOV2026",
               "maturity": 1795737600},
    "metadata": {"name": "ETH Spread", "assetSymbol": "ETH",
                 "platformName": "HL-GATE", "isSpreadMarket": True},
    "config": {"takerFee": "1000000000000000"},
    "extConfig": {"settleFeeRate": "2000000000000000",
                  "paymentPeriod": 28800},
    "data": {"bestBid": 0.03354968659088388,
             "bestAsk": 0.03448027674701263,
             "floatingApr": 0.02847000000000001,
             "notionalOI": 1172.6353400355586},
    "state": "Normal",
}


class TestSpreadConformance:
    def test_real_spread_row_parses(self) -> None:
        """The verbatim 232 payload must decode to a spread market on
        the gate venue with its own fee regime (0.1%/yr taker,
        0.2%/yr settle — not the YU 0.05%/0.1%)."""
        out = parse_markets([_SPREAD_232_ROW])
        assert len(out) == 1
        m = out[0]
        assert m.is_spread and m.asset == "ETH" and m.venue_b == "gate"
        assert m.market_id == 232
        assert m.maturity_ms == 1795737600 * 1000
        assert m.best_bid == pytest.approx(0.03354968659088388)
        assert m.floating_apr == pytest.approx(0.02847)
        assert m.taker_fee == pytest.approx(0.001)
        assert m.settle_fee == pytest.approx(0.002)

    def test_winner_is_highest_net_not_highest_fixed(self) -> None:
        """2026-10-19 binding: 'usa-se o melhor' = largest NET on
        capital. A spread quoting slightly below the simple gross
        still wins — one Boros leg of fees on 0.3N."""
        hl = _market(bid=0.10, ask=0.11)
        b = _market(2, venue="gate", bid=0.04, ask=0.05)
        sp = _market(9, "Hyperliquid BTC - Gate BTCUSDT 14 Nov 2026",
                     "hl-gate", "BTC", spread=True, venue_b="gate",
                     bid=0.049, ask=0.051,     # < 0.05 simple gross
                     taker=1e15, settle=2e15)
        books = _books_for(("hyperliquid", "BTC"), ("gate", "BTC"))
        out = evaluate([hl, b, sp], books, NOW)
        simple = [c for c in out if c.structure == "simple"
                  and c.direction == "short_hl_long_b"][0]
        spr = [c for c in out if c.direction == "short_spread"][0]
        assert simple.fixed_bruto > spr.fixed_bruto      # 0.05 > 0.049
        assert spr.net_apy > simple.net_apy              # but cheaper
        win = pick_winner(out)
        assert win.structure == "spread"
        # sanity: the numbers really do diverge
        assert spr.capital_usd == pytest.approx(0.3 * spec.NOTIONAL_USD)
        assert simple.capital_usd == pytest.approx(0.4 * spec.NOTIONAL_USD)

    def test_winner_ignores_unpriced(self) -> None:
        """A candidate that could not price (no book) must never win,
        even with the highest fixed."""
        hl = _market(bid=0.10, ask=0.11)
        b = _market(2, venue="gate", bid=0.04, ask=0.05)
        sp = _market(9, "Hyperliquid BTC - Gate BTCUSDT 14 Nov 2026",
                     "hl-gate", "BTC", spread=True, venue_b="gate",
                     bid=0.50, ask=0.51)
        out = evaluate([hl, b, sp], {}, NOW)    # no books at all
        assert all(c.net_apy is None for c in out)
        assert pick_winner(out) is None
        # spread priced, simple unpriced: only spread in contention
        books = _books_for(("hyperliquid", "BTC"), ("gate", "BTC"))
        out2 = evaluate([hl, b, sp], books, NOW)
        assert pick_winner(out2).structure == "spread"

    def test_spread_boros_costs_one_leg(self) -> None:
        """Fees: Boros taker+settle+slip once — not x2 like simple."""
        t = 30 / 365.0
        sp = _market(9, "Hyperliquid BTC - Gate BTCUSDT 14 Nov 2026",
                     "hl-gate", "BTC", spread=True, venue_b="gate",
                     bid=0.20, ask=0.21, taker=1e15, settle=2e15)
        books = _books_for(("hyperliquid", "BTC"), ("gate", "BTC"))
        c = [x for x in evaluate([sp], books, NOW)
             if x.direction == "short_spread"][0]
        slip = (abs(100 * 0.9999 - 100) / 100) * 1e4    # ~1bp
        perp = ((4.5 + slip) + (5.0 + slip)) / 1e4 * 10_000 * 2
        boros_1leg = (0.001 + 0.002 + 0.0001) * 10_000 * t
        assert c.costs_usd == pytest.approx(perp + boros_1leg, abs=0.5)

    def test_spread_mark_floating_differential(self,
                                               tmp_path: Path) -> None:
        """Spread trade: the single Boros leg floats the market's own
        floatingApr (the HL - B differential); when the market row is
        gone the proxy cancels BOTH perp fundings (sum, not one leg)."""
        led = BorosLedger(tmp_path / "b.db")
        t = _open_trade(led, structure="spread", direction="short_spread")
        books = _books_for(("hyperliquid", "BTC"), ("binance", "BTC"))
        mkts = _markets_by_id(sp_apr=0.03)
        fund = lambda v, a, t0, t1: [(NOW + 1, 0.0001)]   # noqa: E731
        m = mark_trade(led, t, mkts, books, NOW + spec.MS_H, False, fund)
        yu = json.loads(m["yu_json"])["legs"]
        assert len(yu) == 1 and yu[0]["market_id"] == 9
        assert yu[0]["floating_source"] == "boros_floating_apr"
        dt = spec.MS_H / spec.MS_YEAR
        # short spread pays the differential: -0.03 * N * dt
        assert m["floating_usd"] == pytest.approx(-0.03 * 10_000 * dt,
                                                rel=1e-6)
        # market row gone -> floating proxies BOTH perp fundings
        m2 = mark_trade(led, t, {}, books, NOW + 2 * spec.MS_H,
                        False, fund)
        yu2 = json.loads(m2["yu_json"])["legs"]
        assert yu2[0]["floating_source"] == "venue_proxy"
        led.close()

    def test_status_counts_split_by_structure(self,
                                              tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        led.opportunity_insert({
            "ts_ms": NOW, "asset": "BTC", "venue_b": "gate",
            "maturity_ms": MAT, "structure": "simple",
            "direction": "x", "fixed_bruto": 0.1, "net_apy": 0.06,
            "capital_usd": 4000, "costs_usd": 10, "ttm_days": 30,
            "eligible": 0, "entered": 0, "block_reason": "",
            "legs_json": "{}"})
        led.opportunity_insert({
            "ts_ms": NOW, "asset": "BTC", "venue_b": "gate",
            "maturity_ms": MAT, "structure": "spread",
            "direction": "x", "fixed_bruto": 0.09, "net_apy": 0.07,
            "capital_usd": 3000, "costs_usd": 8, "ttm_days": 30,
            "eligible": 0, "entered": 0, "block_reason": "",
            "legs_json": "{}"})
        b = board(led)
        c = b["counts"]
        assert (c["opportunities_spread"], c["opportunities_simple"]) \
            == (1, 1)
        assert (c["near_entries_spread"], c["near_entries_simple"]) \
            == (1, 1)
        assert (c["entries_spread"], c["entries_simple"]) == (0, 0)
        led.close()
