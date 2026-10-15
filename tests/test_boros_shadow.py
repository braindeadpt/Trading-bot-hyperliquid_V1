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
                 "hashlib", "json", "logging", "pathlib", "sqlite3",
                 "time", "typing", "urllib", "zlib")
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

    def test_off_whitelist_assets_never_evaluated(self) -> None:
        hl = _market(1, "Hyperliquid SOL 14 Nov 2026", "hyperliquid",
                     "SOL", bid=0.20, ask=0.21)
        b = _market(2, "Binance SOLUSDT 14 Nov 2026", "binance", "SOL",
                    bid=0.01, ask=0.02)
        books = _books_for(("hyperliquid", "SOL"), ("binance", "SOL"))
        assert evaluate([hl, b], books, NOW) == []

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
        c = pick_winner(evaluate([hl, b], books, NOW))
        assert not c.eligible and c.block_reason == "book_thin"

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


class TestDaemonLifecycle:
    def _daemon(self, tmp_path, stub=None):
        stub = stub or _StubFetch()
        return BorosShadowDaemon(
            db_path=str(tmp_path / "b.db"),
            fetch={"markets": stub.markets, "book": stub.book})

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

    def test_maturity_flips_state(self, tmp_path: Path) -> None:
        led = BorosLedger(tmp_path / "b.db")
        d = self._daemon(tmp_path)
        led.trade_insert({"asset": "BTC", "venue_b": "binance",
                          "maturity_ms": NOW - MS_D, "structure": "simple",
                          "direction": "x", "state": "open",
                          "entry_ms": NOW - 40 * MS_D})
        d.tick(NOW)
        assert d.ledger.trades_by_state("matured")
        assert not d.ledger.trades_by_state("open")
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
        assert all(c.asset in spec.ASSETS for c in out)
        assert all(spec.MIN_MATURITY_D <= c.ttm_days
                   <= spec.MAX_MATURITY_D for c in out)

    def test_pagination_collects_all_pages(self, monkeypatch) -> None:
        import src.boros_shadow.boros_client as bc
        pages = iter([{"results": [{"x": i} for i in range(100)]},
                      {"results": [{"x": 200}]},
                      {"results": []}])
        monkeypatch.setattr(bc, "fetch_json", lambda url, **kw: next(pages))
        assert len(fetch_boros_markets()) == 101
