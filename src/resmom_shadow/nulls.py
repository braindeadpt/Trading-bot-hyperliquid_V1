"""Random-signal reference portfolios (prereg §Nulos — 200/semana).

Each week, N_NULLS portfolios are drawn: 2*n_legs distinct non-BTC names
from the eligible universe, first n_legs long / rest short, then the
SAME weight scheme (1/vol30 inside the leg + beta-neutral solve) and the
SAME costs (6.5bps/side on turnover vs the null's own previous week).

Nulls carry price PnL + costs only — funding is NOT accrued on nulls
(the funding term nets ~0 on random books and would need per-null
position ledgers; the real arm reports its funding separately so the
comparison stays honest).

resmom_nulls.weights_json stores {"w": {sym: signed_w}, "px": {sym:
entry_px}} — the entry fills are the same 1m-after-00:05 prices the real
arms used, so each row self-contains what its week PnL needs once the
exit (next week's fills) is known.

Seeds are deterministic: sha256("resmom-null|<week_key>|<i>") — any
rebuild reproduces the same 200 books.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

import numpy as np

from src.resmom_shadow import spec
from src.resmom_shadow.ledger import ResmomLedger
from src.resmom_shadow.portfolio import turnover_usd
from src.resmom_shadow.signal import (
    Portfolio, SymbolSignal, build_portfolio)


def null_seed(week_key_ms: int, null_id: int) -> str:
    return hashlib.sha256(
        f"resmom-null|{week_key_ms}|{null_id}".encode()).hexdigest()


def draw_null_weights(universe: List[str],
                      signals: Dict[str, SymbolSignal],
                      n_universe: int, seed_hex: str
                      ) -> Optional[Portfolio]:
    """One random book: random leg picks through the frozen weight math."""
    pool = [s for s in universe if s != spec.BTC and s in signals]
    n_legs = max(1, int(spec.QUINTILE_FRAC * n_universe))
    n_legs = min(n_legs, len(pool) // 2) or 1
    if len(pool) < 2 * n_legs:
        return None
    rng = np.random.default_rng(int(seed_hex[:16], 16))
    picks = rng.choice(pool, size=2 * n_legs, replace=False)
    return build_portfolio(list(picks), signals, n_universe)


def generate_week(ledger: ResmomLedger, rebalance_id: int, week_key_ms: int,
                  universe: List[str], signals: Dict[str, SymbolSignal],
                  prices: Dict[str, float],
                  prev_nulls: Dict[int, Dict[str, float]],
                  prev_navs: Dict[int, float]) -> int:
    """Write this week's 200 null books (entry px embedded). Returns count."""
    n_written = 0
    n_universe = len(universe)
    for i in range(spec.N_NULLS):
        seed = null_seed(week_key_ms, i)
        port = draw_null_weights(universe, signals, n_universe, seed)
        if port is None:
            continue
        px = {s: prices[s] for s in port.weights if s in prices}
        prev = prev_nulls.get(i, {})
        tover = turnover_usd(prev, port.weights)
        cost = tover * spec.COST_BPS_SIDE / 1e4
        nav = prev_navs.get(i, spec.NOTIONAL_USD) - cost
        ledger.null_insert({
            "week_key_ms": week_key_ms, "rebalance_id": rebalance_id,
            "null_id": i, "seed": seed,
            "weights_json": json.dumps(
                {"w": port.weights, "px": px}, sort_keys=True),
            "week_pnl_usd": None, "nav_usd": nav, "cost_usd": cost})
        n_written += 1
    return n_written


def week_pnl(weights: Dict[str, float], entry_px: Dict[str, float],
             exit_px: Dict[str, float]) -> Optional[float]:
    """Sum w * (exit/entry - 1) * NOTIONAL — None if any leg lacks px."""
    tot = 0.0
    for s, w in weights.items():
        e, x = entry_px.get(s), exit_px.get(s)
        if e is None or x is None or e <= 0:
            return None
        tot += float(w) * (x / e - 1.0) * spec.NOTIONAL_USD
    return tot


def score_week(ledger: ResmomLedger, week_key_ms: int,
               exit_px: Dict[str, float]) -> int:
    """Fill week_pnl_usd + running nav for a week's null rows."""
    scored = 0
    for row in ledger.nulls_open_week(week_key_ms):
        if row["week_pnl_usd"] is not None:
            continue
        payload = json.loads(row["weights_json"])
        pnl = week_pnl(payload["w"], payload.get("px") or {}, exit_px)
        nav = float(row["nav_usd"]) + (pnl or 0.0)
        ledger.null_score(int(row["id"]), pnl, nav)
        scored += 1
    return scored


def prev_weights(ledger: ResmomLedger, week_key_ms: int
                 ) -> Dict[int, Dict[str, float]]:
    """null_id -> this week's signed weights (for next week's turnover)."""
    out: Dict[int, Dict[str, float]] = {}
    for row in ledger.nulls_open_week(week_key_ms):
        out[int(row["null_id"])] = {
            s: float(w)
            for s, w in (json.loads(row["weights_json"]).get("w") or {})
            .items()}
    return out
