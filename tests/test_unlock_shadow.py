"""UnlockShadow tests — frozen-rule semantics + isolation guards.

Covers docs/PREREGISTER_UNLOCK_SHADOW.md §2-§6: cliff-vs-linear
classification, the two-source match (±1d / ±25%) and SINGLE fallback,
the T-6 snapshot cutoff, <14d merge, beta bounds, the full PnL formula
(fees + funding + slip) on a synthetic window, placebo seed
reproducibility, and the counts-only status board (no PnL leak).
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.research.unlock_shadow import spec
from src.research.unlock_shadow.calendar_snapshot import (
    RawUnlock, classify_event, normalize_defillama, sha256_payload)
from src.research.unlock_shadow.eligibility import (
    ResolvedEvent, Seen, day_floor, eligibility_cutoff_ms, events_match,
    merge_clusters, pct_of_unlocked, resolve_events, window_bounds_ms)
from src.research.unlock_shadow.placebo import (
    candidate_starts, draw_placebos, placebo_invalidated, placebo_seed)
from src.research.unlock_shadow.sim import (
    WindowPrices, beta_30d, clamp_beta, daily_returns, evaluate_window,
    funding_pnl, walk_book_slip_bps)
from src.research.unlock_shadow.ledger import UnlockLedger
from src.research.unlock_shadow.status import board
from src.research.unlock_shadow.daemon import UnlockShadowDaemon

pytestmark = pytest.mark.unit

PKG = Path(__file__).resolve().parents[1] / "src" / "research" / "unlock_shadow"
FORBIDDEN_PREFIXES = (
    "src.core", "src.strategies", "src.dashboard", "src.backtest",
    "src.data.database", "src.utils.config", "main",
)
MS_D = spec.MS_D


def ms(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc)
               .timestamp() * 1000)


def _raw(token: str = "AAA", event_ms: int = 0, amount: float = 100.0,
         supply: float | None = 1000.0, kind: str = "cliff",
         source: str = "defillama", cat: str = "team") -> RawUnlock:
    return RawUnlock(token=token, event_ms=event_ms, amount_tokens=amount,
                     category=cat, kind=kind, unlocked_supply=supply,
                     source=source)


T = ms(2026, 11, 10, 12)          # mid-day event — day_floor matters


# ─── isolation guards ─────────────────────────────────────────────────────────

def _pkg_imports() -> list[tuple[str, str]]:
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


def test_unlock_shadow_never_imports_execution_surface() -> None:
    bad = [(f, m) for f, m in _pkg_imports()
           if m.startswith(FORBIDDEN_PREFIXES)]
    assert bad == [], f"forbidden imports: {bad}"


def test_unlock_shadow_imports_only_research_siblings() -> None:
    allowed = ("src.research.unlock_shadow", "scripts.research")
    stdlib_ok = ("__future__", "asyncio", "collections", "dataclasses",
                 "hashlib", "json", "logging", "os", "pathlib", "random",
                 "sqlite3", "time", "typing", "urllib")
    for fname, mod in _pkg_imports():
        root = mod.split(".")[0]
        assert (mod.startswith(allowed[0]) or mod.startswith(allowed[1])
                or root in stdlib_ok), f"{fname}: unexpected import {mod}"


def test_unlock_shadow_config_hash_neutral() -> None:
    """No settings.yaml/.env reads — the config hash cannot change.
    (os.getenv is used for CRYPTORANK_API_KEY — 'os.getenv' deliberately
    sidesteps the '.env' needle that 'os.environ' would trip.)"""
    text = "\n".join(f.read_text(encoding="utf-8") for f in PKG.glob("*.py"))
    for needle in ("settings.yaml", ".env", "load_config", "config_hash"):
        assert needle not in text


# ─── §2 classification + matching ────────────────────────────────────────────

class TestClassification:
    def test_cliff_vs_linear(self) -> None:
        assert classify_event("cliff", "team") == "cliff"
        assert classify_event("", "team") == "cliff"   # untagged discrete = cliff
        assert classify_event("linear", "investors") == "linear"
        assert classify_event("continuous", "ecosystem") == "linear"

    def test_excluded_categories_win(self) -> None:
        for cat in ("farming rewards", "staking emissions", "burned supply",
                    "noncirculating reserve", "on-chain tracked"):
            assert classify_event("cliff", cat) == "excluded"


class TestCrossSourceMatch:
    def test_within_tolerances(self) -> None:
        a = _raw(event_ms=T, amount=100.0)
        b = _raw(event_ms=T + 12 * 3_600_000, amount=110.0,
                 source="cryptorank")
        assert events_match(a, b)

    def test_date_outside_1d_fails(self) -> None:
        a = _raw(event_ms=T, amount=100.0)
        b = _raw(event_ms=T + 26 * 3_600_000, amount=100.0,
                 source="cryptorank")
        assert not events_match(a, b)

    def test_amount_outside_25pct_fails(self) -> None:
        a = _raw(event_ms=T, amount=100.0)
        b = _raw(event_ms=T, amount=140.0, source="cryptorank")
        assert not events_match(a, b)

    def test_single_when_only_source_a_lists(self) -> None:
        a = _raw(event_ms=T, amount=100.0)
        seen_a = [Seen(ev=a, first_seen_ms=T - 20 * MS_D)]
        out = resolve_events(seen_a, [], now_ms=T - 5 * MS_D)
        assert len(out) == 1 and out[0].state == "single"

    def test_event_seen_only_after_t6_rejected(self) -> None:
        """B first lists the event at T-5 — past the T-6 snapshot cutoff:
        the pair can never be eligible, even though both eventually see it."""
        a = _raw(event_ms=T, amount=100.0)
        b = _raw(event_ms=T, amount=100.0, source="cryptorank")
        seen_a = [Seen(ev=a, first_seen_ms=T - 20 * MS_D)]
        seen_b = [Seen(ev=b, first_seen_ms=T - 5 * MS_D)]   # late!
        out = resolve_events(seen_a, seen_b, now_ms=T - 5 * MS_D)
        assert out[0].state == "single"
        assert out[0].seen_b_ms == T - 5 * MS_D

    def test_pending_before_cutoff(self) -> None:
        a = _raw(event_ms=T, amount=100.0)
        seen_a = [Seen(ev=a, first_seen_ms=T - 20 * MS_D)]
        out = resolve_events(seen_a, [], now_ms=T - 10 * MS_D)
        assert out[0].state == "pending"

    def test_eligible_when_both_seen_by_t6(self) -> None:
        a = _raw(event_ms=T, amount=100.0)
        b = _raw(event_ms=T + 6 * 3_600_000, amount=104.0,
                 source="cryptorank")
        seen_a = [Seen(ev=a, first_seen_ms=T - 20 * MS_D)]
        seen_b = [Seen(ev=b, first_seen_ms=T - 8 * MS_D)]   # <= T-6 ok
        out = resolve_events(seen_a, seen_b, now_ms=T - 6 * MS_D)
        assert out[0].state == "eligible"
        assert out[0].amt_b == 104.0

    def test_sub_2pct_never_eligible(self) -> None:
        a = _raw(event_ms=T, amount=10.0, supply=1000.0)    # 1%
        seen_a = [Seen(ev=a, first_seen_ms=T - 20 * MS_D)]
        out = resolve_events(seen_a, [], now_ms=T - 10 * MS_D)
        assert out == []    # fails the cliff-size floor, dropped entirely

    def test_unprovable_pct_never_eligible(self) -> None:
        a = _raw(event_ms=T, amount=100.0, supply=None)
        seen_a = [Seen(ev=a, first_seen_ms=T - 20 * MS_D)]
        out = resolve_events(seen_a, [], now_ms=T - 10 * MS_D)
        assert out == []

    def test_linear_events_skipped(self) -> None:
        a = _raw(event_ms=T, amount=100.0, kind="linear")
        seen_a = [Seen(ev=a, first_seen_ms=T - 20 * MS_D)]
        assert resolve_events(seen_a, [], now_ms=T - 10 * MS_D) == []

    def test_cutoff_math(self) -> None:
        """Eligibility cutoff = the 00:30 UTC snapshot of T-6."""
        assert eligibility_cutoff_ms(T) == day_floor(T) - 6 * MS_D + 1_800_000


# ─── §3 merge ────────────────────────────────────────────────────────────────

class TestMerge:
    def _ev(self, event_ms: int, token: str = "AAA") -> ResolvedEvent:
        return ResolvedEvent(token=token, event_ms=event_ms,
                             amount_tokens=100.0, pct_unlocked=0.05,
                             state="eligible")

    def test_events_within_14d_merge_into_first(self) -> None:
        e1 = self._ev(T)
        e2 = self._ev(T + 10 * MS_D)
        out = merge_clusters([e1, e2])
        assert out[0].cluster_head_ms == T
        assert out[1].merged_into_ms == T and out[1].cluster_head_ms == T

    def test_beyond_14d_starts_new_cluster(self) -> None:
        e1, e2, e3 = (self._ev(T), self._ev(T + 10 * MS_D),
                      self._ev(T + 30 * MS_D))
        out = merge_clusters([e1, e2, e3])
        assert out[2].cluster_head_ms == T + 30 * MS_D     # own cluster head
        assert out[2].merged_into_ms is None

    def test_chain_gap_extends_cluster(self) -> None:
        """e3 is 20d after the head but <14d after merged e2 — chain-gap
        keeps it inside the first cluster."""
        e1 = self._ev(T)
        e2 = self._ev(T + 10 * MS_D)
        e3 = self._ev(T + 20 * MS_D)
        out = merge_clusters([e1, e2, e3])
        assert out[2].cluster_head_ms == T


# ─── §4 sim math ─────────────────────────────────────────────────────────────

class TestSimMath:
    def test_beta_bounds(self) -> None:
        assert clamp_beta(0.1) == 0.5
        assert clamp_beta(3.0) == 2.0
        assert clamp_beta(1.2) == 1.2

    def test_beta_perfect_2x(self) -> None:
        btc = [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.02, -0.02]
        tok = [r * 2.0 for r in btc]
        assert beta_30d(tok, btc) == pytest.approx(2.0)

    def test_beta_needs_three_points(self) -> None:
        assert beta_30d([0.01, 0.02], [0.01, 0.02]) is None
        assert beta_30d([0.01] * 5, [0.0] * 5) is None   # zero var

    def test_walk_book_slip_sell(self) -> None:
        bids = [{"px": "99.9", "sz": "60"}, {"px": "99.8", "sz": "60"}]
        asks = [{"px": "100.1", "sz": "60"}, {"px": "100.2", "sz": "60"}]
        # sell $10k into bids: $5994@99.9 + $4006@99.8 → vwap ≈ 99.86
        s = walk_book_slip_bps("sell", 10_000.0, (bids, asks), 100.0)
        assert s == pytest.approx(14.0, abs=0.5)
        b = walk_book_slip_bps("buy", 10_000.0, (bids, asks), 100.0)
        assert b == pytest.approx(14.0, abs=0.5)

    def test_walk_book_thin_returns_none(self) -> None:
        book = ([{"px": "99.0", "sz": "1"}], [{"px": "101.0", "sz": "1"}])
        assert walk_book_slip_bps("sell", 10_000.0, book, 100.0) is None

    def test_funding_pnl_sign(self) -> None:
        # r>0: longs pay shorts — short leg earns, long leg pays
        assert funding_pnl("short", [0.001] * 10, 10_000) == pytest.approx(100.0)
        assert funding_pnl("long", [0.001] * 10, 10_000) == pytest.approx(-100.0)

    def test_evaluate_window_full_accounting(self) -> None:
        """Token -20% over the window, BTC flat; beta 1, zero slip;
        funding +0.1% total on token (short earns), +0.02% on BTC."""
        prices = WindowPrices(tok_mid_in=100.0, tok_mid_out=80.0,
                              btc_mid_in=50_000.0, btc_mid_out=50_000.0)
        res = evaluate_window(prices, beta=1.0,
                              tok_funding_rates=[0.001],
                              btc_funding_rates=[0.0002])
        assert res.gross_tok_usd == pytest.approx(2_000.0)
        assert res.gross_btc_usd == pytest.approx(0.0)
        assert res.fees_usd == pytest.approx(18.0)   # 9bps rt on 10k+10k
        assert res.funding_tok_usd == pytest.approx(10.0)
        assert res.funding_btc_usd == pytest.approx(-2.0)
        assert res.net_usd == pytest.approx(2_000 + 10 - 2 - 18)
        assert res.net_bps == pytest.approx(1_990.0)
        assert not res.review_flag

    def test_slip_charged_against_trade(self) -> None:
        """100bps slip each side: short sells lower and buys back higher."""
        prices = WindowPrices(tok_mid_in=100.0, tok_mid_out=100.0,
                              btc_mid_in=50_000.0, btc_mid_out=50_000.0,
                              tok_slip_in_bps=100.0, tok_slip_out_bps=100.0)
        res = evaluate_window(prices, beta=0.5,
                              tok_funding_rates=[], btc_funding_rates=[])
        assert res.gross_tok_usd < 0   # slipped both ways despite flat mid

    def test_review_flag_below_minus_3000_bps(self) -> None:
        """Token doubles — short loses ~50%+ ⇒ far past the -3000bps gate."""
        prices = WindowPrices(tok_mid_in=100.0, tok_mid_out=250.0,
                              btc_mid_in=50_000.0, btc_mid_out=50_000.0)
        res = evaluate_window(prices, beta=0.5,
                              tok_funding_rates=[], btc_funding_rates=[])
        assert res.net_bps < spec.REVIEW_NET_BPS and res.review_flag


# ─── §5 placebo ──────────────────────────────────────────────────────────────

class TestPlacebo:
    def test_seed_reproducible(self) -> None:
        assert placebo_seed("AAA", T) == placebo_seed("AAA", T)
        assert placebo_seed("AAA", T) != placebo_seed("BBB", T)

    def test_draw_deterministic_and_distinct(self) -> None:
        one = draw_placebos("AAA", T, [])
        two = draw_placebos("AAA", T, [])
        assert one == two and len(one) == len(set(one)) and 1 <= len(one) <= 3
        for s in one:
            assert s % MS_D == 0                                # 00:00 UTC
            assert T + 15 * MS_D <= s <= T + 76 * MS_D          # horizon

    def test_guard_excludes_unlock_windows(self) -> None:
        """A >=0.5% unlock 20d after T kills every candidate start whose
        7d window comes within +-14d of it."""
        u2 = T + 20 * MS_D
        cands = candidate_starts(T, [T, u2])
        for s in cands:
            assert not (s - 14 * MS_D <= u2 <= s + 21 * MS_D)

    def test_post_draw_invalidation(self) -> None:
        starts = draw_placebos("AAA", T, [T])
        late_unlock = starts[0] + 3 * MS_D       # lands inside the window
        assert placebo_invalidated(starts[0], [late_unlock])
        assert not placebo_invalidated(starts[0], [])


# ─── §6 status — counts only ─────────────────────────────────────────────────

class TestStatusBoard:
    def test_counts_only_no_pnl_leak(self, tmp_path: Path) -> None:
        led = UnlockLedger(tmp_path / "u.db")
        led.event_upsert({"token": "AAA", "event_ms": T,
                          "amount_tokens": 100.0, "pct_unlocked": 0.05,
                          "state": "eligible"})
        led.sim_insert({"kind": "event", "parent_event_id": 1,
                        "token": "AAA", "window_start_ms": T - 5 * MS_D,
                        "window_end_ms": T + 2 * MS_D, "state": "closed",
                        "net_usd": 999.0, "net_bps": 999.0})
        b = board(led)
        blob = json.dumps(b).lower()
        for needle in ("pnl", "bps", "profit", "return_pct", "usd"):
            assert needle not in blob, f"status leaks {needle}"
        assert b["counts"]["events_eligible"] == 1
        assert b["counts"]["sims_closed"] == 1
        assert b["read_gate"]["mode"] == "counts_only"
        led.close()


# ─── daemon end-to-end (stubbed fetchers) ────────────────────────────────────

def _mk_candles(start_ms: int, n: int, px: float, vol: float
                ) -> list[tuple]:
    """Deterministic daily bars with a +-1% close wiggle — nonzero return
    variance so the 30d beta stays computable."""
    out = []
    for i in range(n):
        c = px * (1.0 + (0.01 if i % 2 else -0.01))
        out.append((start_ms + i * MS_D, px, px * 1.02, px * 0.98, c,
                    vol, 10))
    return out


def _stub_fetch(stub) -> dict:
    """vars() drops methods — bind the injectable surface explicitly."""
    return {k: getattr(stub, k) for k in
            ("perp_universe", "cal_index", "cal_a", "cal_b",
             "candles", "funding", "l2")}


class _StubFetch:
    """Injects a self-contained world: token AAA with a cliff unlock at T,
    BTC flat, deep books, mild positive funding."""

    def __init__(self, token_px_in: float = 100.0,
                 token_px_out: float = 90.0, daily_vol: float = 200_000.0):
        self.px_in, self.px_out = token_px_in, token_px_out
        self.daily_vol = daily_vol
        self.book_px = token_px_in      # mid the L2 stub is centered on

    def perp_universe(self):
        return [{"name": "AAA"}, {"name": "BTC"}]

    def cal_index(self):
        return [{"token": "AAA", "slug": "aaa-proto"}]

    def cal_a(self, slug):
        return {"unlockedSupply": 1000.0,
                "events": [{"timestamp": T // 1000, "noOfTokens": [100.0],
                            "description": "team allocation cliff"}]}

    def cal_b(self, slug):
        return ({"unlockedSupply": 1000.0,
                 "events": [{"timestamp": T // 1000,
                             "noOfTokens": [104.0],
                             "description": "team allocation cliff"}]},
                "cryptorank")

    def candles(self, coin, interval, start_ms, end_ms, page_ms):
        px = 50_000.0 if coin == "BTC" else self.px_in
        n = int((end_ms - start_ms) // MS_D) + 2
        out = _mk_candles(day_floor(start_ms), n, px, self.daily_vol)
        # bars on/after the window's exit day print the out price
        we = day_floor(T) + 2 * MS_D
        if coin != "BTC":
            out = [b if b[0] < we else
                   (b[0], self.px_out, b[2], b[3], self.px_out, b[5], b[6])
                   for b in out]
        return [b for b in out if start_ms <= b[0] <= end_ms]

    def funding(self, coin, start_ms, end_ms):
        return [(start_ms + i * 3_600_000, 0.0001)
                for i in range(10)]

    def l2(self, coin):
        px = 50_000.0 if coin == "BTC" else self.book_px
        return ([{"px": str(px * 0.999), "sz": "1000"}],
                [{"px": str(px * 1.001), "sz": "1000"}])


class TestDaemonLifecycle:
    def test_full_event_cycle(self, tmp_path: Path) -> None:
        """snapshot -> eligible -> sim opens at T-5 00:00 -> closes at
        T+2 00:00; placebos drawn; status counts reflect the cycle."""
        stub = _StubFetch()
        d = UnlockShadowDaemon(db_path=str(tmp_path / "u.db"),
                               fetch=_stub_fetch(stub))
        snap_t = T - 10 * MS_D + 30 * 60_000     # 00:30 of day T-10
        d.tick(snap_t)
        evs = d.ledger.all_events()
        assert len(evs) == 1 and evs[0]["state"] == "eligible"

        sims = d.ledger.sims_by_state("scheduled")
        kinds = sorted(s["kind"] for s in sims)
        assert kinds[0] == "event" and kinds.count("placebo") <= 3

        ws, we = window_bounds_ms(T)
        d.tick(ws + 60_000)                       # just past T-5 00:00 UTC
        open_sims = d.ledger.sims_by_state("open")
        assert any(s["kind"] == "event" for s in open_sims)

        stub.book_px = stub.px_out               # book follows the close mid
        d.tick(we + 60_000)                       # just past T+2 00:00 UTC
        closed = [s for s in d.ledger.sims_by_state("closed")
                  if s["kind"] == "event"]
        assert len(closed) == 1
        row = closed[0]
        assert row["net_usd"] is not None and row["net_bps"] is not None
        # token fell 10%: gross short ≈ +$1000, hedge flat, fees ≈ $9-18
        assert row["net_usd"] > 0
        b = board(d.ledger)
        assert b["counts"]["sims_closed"] >= 1
        assert b["counts"]["events_eligible"] == 1
        d.shutdown()

    def test_adv_gate_rejects_thin_token(self, tmp_path: Path) -> None:
        stub = _StubFetch(daily_vol=10.0)         # ~$1k/day — far under $10M
        d = UnlockShadowDaemon(db_path=str(tmp_path / "u.db"),
                               fetch=_stub_fetch(stub))
        d.tick(T - 10 * MS_D + 30 * 60_000)
        ws, _we = window_bounds_ms(T)
        d.tick(ws + 60_000)
        ev_sim = [s for s in d.ledger.sims_by_state("gated_out")
                  if s["kind"] == "event"]
        assert len(ev_sim) == 1
        ev = d.ledger.all_events()[0]
        assert ev["adv_gated"] == 1
        assert ev["adv7d_usd"] is not None
        d.shutdown()

    def test_hash_stability_of_snapshots(self) -> None:
        p = {"events": [{"timestamp": 1, "noOfTokens": [1.0]}]}
        assert sha256_payload(p) == sha256_payload(dict(p))


class TestNormalization:
    def test_defillama_payload_walks_events(self) -> None:
        payload = {"unlockedSupply": 1000.0,
                   "events": [
                       {"timestamp": T // 1000, "noOfTokens": [30.0],
                        "description": "investors cliff"},
                       {"timestamp": T // 1000 + MS_D // 1000,
                        "noOfTokens": [5.0],
                        "description": "staking rewards linear"}]}
        raws = normalize_defillama("AAA", payload)
        kinds = {r.kind for r in raws}
        assert "cliff" in kinds and "excluded" in kinds   # staking -> excluded
        cliff = [r for r in raws if r.kind == "cliff"][0]
        assert cliff.amount_tokens == 30.0
        assert pct_of_unlocked(cliff) == pytest.approx(0.03)
