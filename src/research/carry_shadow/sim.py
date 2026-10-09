"""Hypothetical episode state machine — pure logic, no I/O, no network.

States per docs/PREREGISTER_CARRY_SHADOW.md:

    pending_entry ──both legs filled──> open ──exit signal──> pending_exit ──> closed
        │                                │                       │
        ├─ leg unfilled 15min ──> abort  ├─ equity<=maint ──> liquidated/gap_kill
        └─ unlegged unwind ────> aborted ├─ equity<M      ──> deleverage (stay open)
                                         └─ delev keep<=floor ──> closed

Accounting model (all quantities in entry-notional fractions):
  * `collateral` = cash posted to the perp margin. Realized losses, taker
    deleverage costs, and order fees are debited from it; funding accrues
    into it. `equity(mid) = collateral - q*adverse` is the live margin
    equity backing the position.
  * Deleveraging closes a fraction of BOTH legs at taker mid. Closing does
    not change wealth (it converts unrealized PnL into realized); the
    notional shrinks while equity stays, so the margin ratio recovers.
    Selling the spot fraction frees cash, tracked in `cash_freed`.
  * Wealth = spot open value (q*spot_mid/s0) + cash_freed + equity(mid).
    net_bps = (wealth - committed_at_entry) * 1e4. `cost_bps` is a
    report-only accumulator for the fill/edge gates.

Maker-fill model (conservative): a resting maker BUY at P fills only when
best_ask < P; a resting maker SELL at P fills only when best_bid > P — the
market must cross strictly through our level. Queue position is unobservable,
so this is a lower bound on the true fill rate (disclosed in the prereg).

ADL note: Hyperliquid does not expose ADL events on public WS/REST feeds as
of this writing; the 'adl_obs' event kind exists in the ledger schema for
when/if a source is identified (prereg §5 records it as observational, not a
death criterion).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from src.research.carry_shadow import spec

MS_H = 3_600_000


@dataclass
class BookSnap:
    """Top-of-book snapshot for one market."""
    bid: float = 0.0
    ask: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.bid and self.ask else 0.0


@dataclass
class PendingLeg:
    """One resting maker order on a leg. buy fills when ask<price; sell when
    bid>price (strictly-through model)."""
    side: str                     # 'buy' | 'sell'
    price: float = 0.0
    filled_at_ms: Optional[int] = None

    def check(self, ts_ms: int, book: BookSnap) -> bool:
        if self.filled_at_ms is not None:
            return False
        crossed = (book.ask < self.price if self.side == "buy"
                   else book.bid > self.price)
        if self.price > 0 and book.bid > 0 and book.ask > 0 and crossed:
            self.filled_at_ms = ts_ms
            return True
        return False

    @property
    def filled(self) -> bool:
        return self.filled_at_ms is not None


@dataclass
class Episode:
    """One hypothetical carry position on 1 unit notional at entry."""
    pair: str                     # spot pair name ('@142' or 'PURR/USDC')
    perp: str                     # perp symbol
    entry_decision_ms: int
    pm_branch: bool
    maint: float
    m_trigger: float
    m0: float
    state: str = "pending_entry"  # pending_entry|open|pending_exit|closed|aborted|liquidated
    id: int = 0
    legs: dict = field(default_factory=lambda: {
        "spot": PendingLeg("buy"), "perp": PendingLeg("sell")})
    exit_legs: dict = field(default_factory=dict)
    opened_ms: int = 0
    exit_decision_ms: int = 0
    s0: float = 0.0
    p0: float = 0.0
    q: float = 1.0
    collateral: float = 0.0       # cash posted to perp margin (see docstring)
    cash_freed: float = 0.0       # spot-leg proceeds released by deleverages
    cum_f: float = 0.0            # report-only: funding accrued so far
    cost_bps: float = 0.0         # report-only: fees for edge/fill gates
    last_funding_ms: int = 0
    deleverages: int = 0
    unlegged_since_ms: int = 0    # set when exactly one leg filled
    max_unlegged_s: float = 0.0   # longest naked exposure observed
    close_reason: str = ""

    # ─── equity / margin zones ───────────────────────────────────────────

    def equity(self, perp_mid: float) -> float:
        """Live margin equity = collateral - q*adverse (fractions of N0)."""
        if self.p0 <= 0 or perp_mid <= 0:
            return self.collateral
        return self.collateral - self.q * (perp_mid - self.p0) / self.p0

    def margin_zone(self, perp_mid: float) -> str:
        """'ok' | 'warn' (below trigger) | 'liq' (below maintenance)."""
        if self.state != "open" or perp_mid <= 0 or self.pm_branch:
            return "ok"
        n_t = self.q * perp_mid / self.p0
        eq = self.equity(perp_mid)
        if eq <= self.maint * n_t:
            return "liq"
        if eq < self.m_trigger * n_t:
            return "warn"
        return "ok"

    # ─── entry phase ─────────────────────────────────────────────────────

    def place_entry(self, spot: BookSnap, perp: BookSnap) -> None:
        """Rest maker at touch: buy spot at best_bid, sell perp at best_ask."""
        self.legs["spot"].price = spot.bid
        self.legs["perp"].price = perp.ask
        self.collateral = self.m0

    def on_book_entry(self, ts_ms: int, spot: BookSnap,
                      perp: BookSnap) -> Optional[str]:
        """Feed a book tick during pending_entry. Returns event kind or None."""
        if self.state != "pending_entry":
            return None
        before = self._n_filled(self.legs)
        self.legs["spot"].check(ts_ms, spot)
        self.legs["perp"].check(ts_ms, perp)
        after = self._n_filled(self.legs)
        if before == 0 and after == 1:
            self.unlegged_since_ms = ts_ms
        if after == 2:
            self._close_unlegged(ts_ms)
            self.state = "open"
            self.opened_ms = max(self.legs["spot"].filled_at_ms or 0,
                                 self.legs["perp"].filled_at_ms or 0)
            self.s0 = self.legs["spot"].price
            self.p0 = self.legs["perp"].price
            self._charge(2 * spec.MAKER_FEE_BPS)
            return "fill"
        if ts_ms - self.entry_decision_ms < spec.MAKER_FILL_WINDOW_S * 1000:
            return None
        self._close_unlegged(ts_ms)
        if after == 1:
            self._charge(spec.TAKER_FEE_BPS)
            self.state = "aborted"
            self.close_reason = "unlegged_unwind"
            return "unlegged_unwind"
        self.state = "aborted"
        self.close_reason = "abort_unfilled"
        return "abort_unfilled"

    # ─── open phase ──────────────────────────────────────────────────────

    def on_margin_tick(self, ts_ms: int, perp_mid: float,
                       spot_mid: float) -> Optional[str]:
        """Evaluate margin state. Returns event kind or None."""
        if self.state != "open" or perp_mid <= 0 or self.pm_branch:
            return None
        zone = self.margin_zone(perp_mid)
        if zone == "liq":
            self.state = "liquidated"
            self.close_reason = "liquidated"
            return "liquidated"
        if zone != "warn":
            return None
        n_t = self.q * perp_mid / self.p0
        keep = self.equity(perp_mid) / (self.m0 * n_t)
        if keep <= spec.DELEV_FLOOR:
            self._deleverage(1.0, perp_mid, spot_mid)
            self.state = "closed"
            self.close_reason = "delev_floor"
            return "delev_floor"
        if keep < 1.0:
            self._deleverage(1.0 - keep, perp_mid, spot_mid)
            self.deleverages += 1
            return "rebalance"
        return None

    def _deleverage(self, frac: float, perp_mid: float,
                    spot_mid: float) -> None:
        """Close frac of BOTH legs at taker mid; equity is wealth-preserved
        (minus taker cost); the spot fraction's sale frees cash."""
        closed_q = self.q * frac
        # perp: realized loss debited from collateral
        self.collateral -= closed_q * (perp_mid - self.p0) / self.p0
        # spot: sale proceeds freed as cash
        self.cash_freed += closed_q * spot_mid / self.s0
        # taker cost on the closed notional, both legs
        self._charge(spec.DELEV_TAKER_RT_BPS * frac * (self.q * perp_mid
                                                       / self.p0))
        self.q -= closed_q

    def on_funding_row(self, ts_ms: int, rate: float) -> float:
        """Accrue one hourly funding row on remaining q (shorts receive when
        rate>0). Funding lands in margin collateral."""
        if self.state not in ("open", "pending_exit"):
            return 0.0
        acc = rate * self.q
        self.collateral += acc
        self.cum_f += acc
        self.last_funding_ms = ts_ms
        return acc

    def exit_signal(self, f_ann_now: float, neg_since_ms: Optional[int],
                    ts_ms: int) -> bool:
        """A1 exit: 24h-mean funding negative for 48h consecutive."""
        return (self.state == "open" and f_ann_now < 0
                and neg_since_ms is not None
                and ts_ms - neg_since_ms >= spec.EXIT_NEG_H * MS_H)

    # ─── exit phase ──────────────────────────────────────────────────────

    def place_exit(self, ts_ms: int, spot: BookSnap, perp: BookSnap) -> None:
        """Rest maker exit legs: sell spot at best_ask, buy perp at best_bid."""
        self.exit_legs = {
            "spot": PendingLeg("sell", spot.ask),
            "perp": PendingLeg("buy", perp.bid),
        }
        self.exit_decision_ms = ts_ms
        self.state = "pending_exit"

    def on_book_exit(self, ts_ms: int, spot: BookSnap,
                     perp: BookSnap) -> Optional[str]:
        """Feed book ticks during pending_exit. Returns event kind or None."""
        if self.state != "pending_exit":
            return None
        before = self._n_filled(self.exit_legs)
        self.exit_legs["spot"].check(ts_ms, spot)
        self.exit_legs["perp"].check(ts_ms, perp)
        after = self._n_filled(self.exit_legs)
        if before == 0 and after == 1:
            self.unlegged_since_ms = ts_ms
        if after == 2:
            self._close_unlegged(ts_ms)
            self._settle(spot.mid, perp.mid)
            self._charge(2 * spec.MAKER_FEE_BPS)
            self.state = "closed"
            self.close_reason = "funding_exit"
            return "exit"
        if ts_ms - self.exit_decision_ms < spec.MAKER_FILL_WINDOW_S * 1000:
            return None
        self._close_unlegged(ts_ms)
        self._settle(spot.mid, perp.mid)
        self._charge(after * spec.MAKER_FEE_BPS + (2 - after) * spec.TAKER_FEE_BPS)
        self.state = "closed"
        self.close_reason = ("funding_exit" if after == 2
                             else "funding_exit_taker")
        return "exit"

    def _settle(self, spot_mid: float, perp_mid: float) -> None:
        """Realize remaining q: perp loss to collateral, spot sale to cash."""
        self.collateral -= self.q * (perp_mid - self.p0) / self.p0
        self.cash_freed += self.q * spot_mid / self.s0
        self.q = 0.0

    # ─── helpers / reporting ─────────────────────────────────────────────

    def _charge(self, bps: float) -> None:
        self.cost_bps += bps
        self.collateral -= bps / 1e4

    @staticmethod
    def _n_filled(legs: dict) -> int:
        return sum(1 for leg in legs.values() if leg.filled)

    def _close_unlegged(self, ts_ms: int) -> None:
        if self.unlegged_since_ms:
            self.max_unlegged_s = max(
                self.max_unlegged_s,
                (ts_ms - self.unlegged_since_ms) / 1000.0)
            self.unlegged_since_ms = 0

    def wealth(self, perp_mid: float, spot_mid: float) -> float:
        """Total episode wealth = open spot value + freed cash + margin equity."""
        spot_val = self.q * spot_mid / self.s0 if self.s0 > 0 else 0.0
        return spot_val + self.cash_freed + self.equity(perp_mid)

    def net_bps(self, perp_mid: float, spot_mid: float) -> float:
        """Net bps on entry notional vs capital committed at entry."""
        return (self.wealth(perp_mid, spot_mid)
                - self.committed_capital()) * 1e4

    def committed_capital(self) -> float:
        """Capital committed on entry notional (hurdle denominator)."""
        if self.pm_branch:
            return spec.SPOT_CAPITAL + spec.PM_USDC_BUFFER
        return spec.SPOT_CAPITAL + self.m0

    def row(self) -> dict:
        return {
            "pair": self.pair, "perp": self.perp,
            "pm_branch": int(self.pm_branch),
            "state": self.state,
            "entry_decision_ms": self.entry_decision_ms,
            "opened_ms": self.opened_ms or None,
            "p0": self.p0 or None, "s0": self.s0 or None,
            "q": self.q, "margin": self.collateral,
            "cum_f": self.cum_f,
            "realized_pnl": (self.wealth(0.0, 0.0) - self.committed_capital())
                if self.state == "closed" else self.collateral,
            "cost_bps": self.cost_bps,
            "maint": self.maint, "m_trigger": self.m_trigger,
            "m0": self.m0,
            "last_funding_ms": self.last_funding_ms or None,
        }


class EpisodePool:
    """Owns live episodes; one per pair max; emits ledger events via callback."""

    def __init__(self, emit: Callable[[str, dict, Optional[int]], None]) -> None:
        self.emit = emit
        self.episodes: dict[str, Episode] = {}
        self.dead = False
        self.death_reason = ""

    def get(self, pair: str) -> Optional[Episode]:
        return self.episodes.get(pair)

    def add(self, ep: Episode) -> None:
        self.episodes[ep.pair] = ep

    def remove(self, pair: str) -> Optional[Episode]:
        return self.episodes.pop(pair, None)

    def kill(self, reason: str) -> None:
        self.dead = True
        self.death_reason = reason
        self.emit("death", {"reason": reason}, None)

    def live(self) -> list[Episode]:
        return list(self.episodes.values())
