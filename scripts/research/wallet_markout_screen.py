#!/usr/bin/env python3
"""Q13 Phase A — wallet markout persistence screen ("Public Trader Identity" port).

Measures whether a persistent informed sub-cohort exists inside the
leaderboard-wallet universe, using the methodology of arXiv 2608.04373 and
Research Square rs-10147582:

- signed markout  M_i(h) = d_i * (m(t_i + h) - p_i) / p_i * 1e4   [bps]
  where d_i = +1 for buyer-initiated (side='B') taker fills, -1 for sellers,
  and m(t+h) is the 1m-candle close at/after t+h (our finest granularity —
  the papers used 2-10s L4 data; longer horizons are a weaker claim).
- per-wallet score = notional-weighted mean markout over taker fills.
- split-half by fill time; Spearman rank correlation of wallet scores.

Preregistered unlock rule (QUEUE.md Q13): Phase B wiring is justified only if
rho >= 0.30 at >= 1 horizon, with >= 20 wallets having >= 30 taker fills in
EACH half. Absent persistence, the wallet-flow question is dead.

Usable window (pre-registered 2026-10-07, docs/Q13_PHASE_A_RESULT_*):
per symbol, fills are restricted to
    [max(first_taker_fill, first_candle_ts),
     min(last_taker_fill,  last_candle_ts - 3_600_000)]
i.e. the intersection of candle coverage (clipped by the longest horizon)
and the available fill range. Fills inside the window but falling in a
candles_1m gap are still counted for the time-split boundary (the split is
"half the usable-window fills by time") but contribute no markout — they
are reported in the rejected-no-candle counter, per horizon.

Diagnostics (pre-registered, not decision rules):
- per-horizon permutation p-value: N_PERM within-set permutations of the
  half-2 wallet->score assignment; p = fraction with rho_perm >= rho_obs.
- family diagnostic: N_PERM draws of ONE shared wallet permutation over the
  union of the three ok-sets; for each draw rho_perm(h) is recomputed per
  horizon over pairs whose permuted partner stays in that horizon's ok-set
  (approximation, documented). Reports P(max_h rho_perm >= RHO_UNLOCK) —
  the chance of the 0.30 threshold being hit at >= 1 of 3 correlated
  horizons under the null. The permissive threshold plus three horizons
  makes a chance pass possible; this diagnostic quantifies it, it does
  not gate anything.

Usage:
    python -X utf8 scripts/research/wallet_markout_screen.py
    python -X utf8 scripts/research/wallet_markout_screen.py --symbols BTC,ETH,SOL,HYPE
"""

from __future__ import annotations

import argparse
import bisect
import collections
import sqlite3
import sys
from array import array
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.data.research_database import ResearchDatabase  # noqa: E402

HORIZONS_MS = (300_000, 900_000, 3_600_000)  # 5m, 15m, 1h
HORIZON_LABELS = {300_000: "5m", 900_000: "15m", 3_600_000: "1h"}
MIN_FILLS_PER_HALF = 30
MIN_WALLETS = 20
RHO_UNLOCK = 0.30
N_PERM = 10_000
PERM_SEED = 20261007
GAP_MS = 5 * 60_000


def load_candles(db: sqlite3.Connection, symbol: str) -> tuple[list[int], list[float]]:
    rows = db.execute(
        "select timestamp_ms, close from candles_1m where symbol=? order by timestamp_ms",
        (symbol,),
    ).fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def usable_window(
    fill_lo: int | None,
    fill_hi: int | None,
    candle_first: int | None,
    candle_last: int | None,
    max_horizon_ms: int = 3_600_000,
) -> tuple[int | None, int | None]:
    """Intersection of candle coverage (clipped by the longest horizon) and
    the available fill range. Returns (lo, hi) or (None, None) if empty."""
    if fill_lo is None or candle_first is None:
        return None, None
    lo = max(fill_lo, candle_first)
    hi = min(fill_hi, candle_last - max_horizon_ms)
    return (lo, hi) if lo <= hi else (None, None)


def candle_gaps(ts: list[int], gap_ms: int = GAP_MS) -> list[tuple[int, int]]:
    """(prev_ts, next_ts) pairs where consecutive candles are > gap_ms apart."""
    return [(a, b) for a, b in zip(ts, ts[1:]) if b - a > gap_ms]


def spearman_rho(score1: dict, score2: dict, wallets: list[str]) -> float:
    """Spearman rho over paired scores via rank positions (no tie correction,
    same convention as the original screen). Ties break by wallet order —
    callers pass wallets sorted by address for determinism."""
    n = len(wallets)
    if n < 2:
        return float("nan")
    r1 = {w: i for i, w in enumerate(sorted(wallets, key=lambda w: score1[w]))}
    r2 = {w: i for i, w in enumerate(sorted(wallets, key=lambda w: score2[w]))}
    d2 = sum((r1[w] - r2[w]) ** 2 for w in wallets)
    return 1.0 - 6.0 * d2 / (n * (n * n - 1))


def _ranks(scores: np.ndarray) -> np.ndarray:
    order = np.argsort(scores, kind="stable")
    ranks = np.empty(len(scores), dtype=np.float64)
    ranks[order] = np.arange(len(scores), dtype=np.float64)
    return ranks


def permutation_pvalue(
    score1: dict,
    score2: dict,
    wallets: list[str],
    n_perm: int = N_PERM,
    rng: np.random.Generator | None = None,
) -> tuple[float, float]:
    """Marginal permutation diagnostic for one horizon.

    Null: half-2 scores are exchangeable across wallets. Each draw applies a
    uniform permutation to the half-2 score vector and recomputes rho.
    Returns (rho_obs, p) with p = fraction of draws where rho_perm >= rho_obs.
    """
    rng = rng if rng is not None else np.random.default_rng(PERM_SEED)
    n = len(wallets)
    s1 = np.array([score1[w] for w in wallets], dtype=np.float64)
    s2 = np.array([score2[w] for w in wallets], dtype=np.float64)
    r1 = _ranks(s1)
    r2 = _ranks(s2)
    denom = n * (n * n - 1)
    rho_obs = 1.0 - 6.0 * float(((r1 - r2) ** 2).sum()) / denom
    hits = 0
    for _ in range(n_perm):
        d2 = float(((r1 - r2[rng.permutation(n)]) ** 2).sum())
        if 1.0 - 6.0 * d2 / denom >= rho_obs:
            hits += 1
    return rho_obs, hits / n_perm


def family_threshold_pvalue(
    ok_by_horizon: dict[int, dict[str, tuple[float, float]]],
    n_perm: int = N_PERM,
    rng: np.random.Generator | None = None,
    threshold: float = RHO_UNLOCK,
) -> float:
    """Joint diagnostic: P(at least one horizon reaches rho >= threshold).

    One shared permutation sigma over the UNION of ok-sets induces the
    permuted half-2 pairing at every horizon simultaneously, preserving
    whatever cross-horizon dependence the null creates (same relabeling,
    same real score vectors). For each horizon, pairs whose permuted
    partner falls outside that horizon's ok-set are dropped — an
    approximation, documented in the run report.
    """
    rng = rng if rng is not None else np.random.default_rng(PERM_SEED)
    union = sorted({w for ok in ok_by_horizon.values() for w in ok})
    if not union:
        return float("nan")
    pos = {w: i for i, w in enumerate(union)}
    per_h = {}
    for h, ok in ok_by_horizon.items():
        idx = np.array([pos[w] for w in sorted(ok)], dtype=np.int64)
        s1 = np.array([ok[w][0] for w in sorted(ok)], dtype=np.float64)
        s2 = np.array([ok[w][1] for w in sorted(ok)], dtype=np.float64)
        # union position -> position inside this horizon's ok arrays (-1 outside)
        inv = np.full(len(union), -1, dtype=np.int64)
        inv[idx] = np.arange(len(idx), dtype=np.int64)
        per_h[h] = (idx, s1, s2, inv)
    hits = 0
    for _ in range(n_perm):
        sigma = rng.permutation(len(union))
        for idx, s1, s2, inv in per_h.values():
            partner_pos = inv[sigma[idx]]
            keep = partner_pos >= 0
            if keep.sum() < 4:
                continue
            rho = spearman_rho_array(s1[keep], s2[partner_pos[keep]])
            if rho >= threshold:
                hits += 1
                break
    return hits / n_perm


def spearman_rho_array(s1: np.ndarray, s2: np.ndarray) -> float:
    n = len(s1)
    if n < 2:
        return float("nan")
    d2 = float(((_ranks(s1) - _ranks(s2)) ** 2).sum())
    return 1.0 - 6.0 * d2 / (n * (n * n - 1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--min-fills", type=int, default=MIN_FILLS_PER_HALF)
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    rdb_path = ResearchDatabase.open().db_path
    rdb = sqlite3.connect(f"file:{rdb_path}?mode=ro", uri=True)
    print(f"markout_screen: db={rdb_path} symbols={symbols}")

    ts: dict[str, list[int]] = {}
    cl: dict[str, list[float]] = {}
    windows: dict[str, tuple[int | None, int | None]] = {}
    for s in symbols:
        ts[s], cl[s] = load_candles(rdb, s)
        fill_lo, fill_hi = rdb.execute(
            "select min(time_ms), max(time_ms) from top_trader_fills "
            "where coin=? and crossed=1",
            (s,),
        ).fetchone()
        windows[s] = usable_window(
            fill_lo, fill_hi,
            ts[s][0] if ts[s] else None, ts[s][-1] if ts[s] else None,
        )
        gaps = candle_gaps(ts[s])
        print(
            f"  candles_1m[{s}]: {len(ts[s])} rows "
            f"first={ts[s][0] if ts[s] else '-'} last={ts[s][-1] if ts[s] else '-'} "
            f"gaps>5m={len(gaps)}"
        )
        for a, b in gaps[:5]:
            print(f"    gap {a} -> {b} ({(b - a) / 3_600_000:.1f}h)")
        print(f"    usable_window[{s}]: {windows[s]}")

    def markout(coin: str, t0: int, p0: float, sign: float, h_ms: int) -> float | None:
        # bisect lands on the last candle close <= t0+h. Two reject cases,
        # both observed in the data (candles_1m has gaps — e.g. 08-20..08-25):
        #   * ts[i] <= t0: the "reference" close predates the fill (gap after
        #     fill) -> the markout would compare against a stale price.
        #   * ts[i] < t0+h-2min: candle exists but is far short of the
        #     horizon -> silently measuring a shorter horizon.
        i = bisect.bisect_right(ts[coin], t0 + h_ms) - 1
        if i < 0 or ts[coin][i] <= t0 or ts[coin][i] < t0 + h_ms - 120_000:
            return None
        return sign * (cl[coin][i] / p0 - 1.0) * 1e4

    # PASS 1 — full-sample notional-weighted markout + usable-window fills
    W = collections.defaultdict(lambda: [[0.0, 0.0, 0] for _ in HORIZONS_MS])
    taker_times: array = array("q")
    rejected_full = [0] * len(HORIZONS_MS)
    in_window_total = 0
    for s in symbols:
        lo, hi = windows[s]
        if lo is None:
            continue
        for w, t, side, p0, sz in rdb.execute(
            "select wallet, time_ms, side, px, sz from top_trader_fills "
            "where coin=? and crossed=1 and time_ms>=? and time_ms<=? "
            "order by time_ms",
            (s, lo, hi),
        ):
            sign = 1.0 if side == "B" else -1.0
            notional = p0 * sz
            taker_times.append(t)
            in_window_total += 1
            for hi_, h in enumerate(HORIZONS_MS):
                m = markout(s, t, p0, sign, h)
                if m is None:
                    rejected_full[hi_] += 1
                    continue
                W[w][hi_][0] += notional * m
                W[w][hi_][1] += notional
                W[w][hi_][2] += 1

    n_taker = len(taker_times)
    print(f"\nmarkout_screen: {n_taker} usable-window taker fills across "
          f"{len(W)} wallets")
    for hi_, h in enumerate(HORIZONS_MS):
        print(f"  h={HORIZON_LABELS[h]}: rejected_no_candle={rejected_full[hi_]}")

    # top/bottom wallets by 15m markout (paper's canonical horizon)
    hi_mid = HORIZONS_MS.index(900_000)
    ranked = sorted(
        W.items(),
        key=lambda kv: -(kv[1][hi_mid][0] / kv[1][hi_mid][1] if kv[1][hi_mid][1] else -1e9),
    )
    print(f"\n{'wallet':<14}{'n_tk':>6}{'mk5m':>8}{'mk15m':>8}{'mk1h':>8}")
    for w, acc in ranked[:10]:
        vals = [acc[i][0] / acc[i][1] if acc[i][1] else float("nan") for i in range(3)]
        print(f"{w[:12]:<14}{acc[hi_mid][2]:>6}{vals[0]:>8.2f}{vals[1]:>8.2f}{vals[2]:>8.2f}")
    print("  ...")
    for w, acc in ranked[-5:]:
        vals = [acc[i][0] / acc[i][1] if acc[i][1] else float("nan") for i in range(3)]
        print(f"{w[:12]:<14}{acc[hi_mid][2]:>6}{vals[0]:>8.2f}{vals[1]:>8.2f}{vals[2]:>8.2f}")

    if not taker_times:
        print("\nmarkout_screen: no taker fills -> BLOCKED")
        return 2
    taker_times = sorted(taker_times)
    half = taker_times[len(taker_times) // 2]
    print(f"\nsplit-half boundary t={half} (median of usable-window taker fills)")

    # PASS 2 — split halves, all horizons in one scan
    H = [
        collections.defaultdict(lambda: [[0.0, 0.0, 0], [0.0, 0.0, 0]])
        for _ in HORIZONS_MS
    ]
    rejected_half = [0] * len(HORIZONS_MS)
    for s in symbols:
        lo, hi = windows[s]
        if lo is None:
            continue
        for w, t, side, p0, sz in rdb.execute(
            "select wallet, time_ms, side, px, sz from top_trader_fills "
            "where coin=? and crossed=1 and time_ms>=? and time_ms<=?",
            (s, lo, hi),
        ):
            sign = 1.0 if side == "B" else -1.0
            notional = p0 * sz
            k = 0 if t <= half else 1
            for hi_, h in enumerate(HORIZONS_MS):
                m = markout(s, t, p0, sign, h)
                if m is None:
                    rejected_half[hi_] += 1
                    continue
                H[hi_][w][k][0] += notional * m
                H[hi_][w][k][1] += notional
                H[hi_][w][k][2] += 1

    rng = np.random.default_rng(PERM_SEED)
    ok_by_horizon: dict[int, dict[str, tuple[float, float]]] = {}
    unlocked = False
    for hi_, h in enumerate(HORIZONS_MS):
        acc = H[hi_]
        ok = {
            w: (v[0][0] / v[0][1], v[1][0] / v[1][1])
            for w, v in acc.items()
            if v[0][2] >= args.min_fills and v[1][2] >= args.min_fills
        }
        ok_by_horizon[h] = ok
        label = HORIZON_LABELS[h]
        n = len(ok)
        fills_h1 = sum(v[0][2] for v in acc.values())
        fills_h2 = sum(v[1][2] for v in acc.values())
        if n < 4:
            print(f"  h={label}: n_wallets={n} (<4) -> cannot rank "
                  f"(fills h1={fills_h1} h2={fills_h2} "
                  f"rejected={rejected_half[hi_]})")
            continue
        ws = sorted(ok)
        score1 = {w: ok[w][0] for w in ws}
        score2 = {w: ok[w][1] for w in ws}
        rho, p_perm = permutation_pvalue(score1, score2, ws, n_perm=N_PERM, rng=rng)
        sufficient = n >= MIN_WALLETS
        passed = rho >= RHO_UNLOCK and sufficient
        verdict = "PASS" if passed else ("FAIL" if sufficient else "NOT MET")
        print(
            f"  h={label}: n_wallets={n} rho={rho:+.3f} p_perm={p_perm:.4f} "
            f"fills h1={fills_h1} h2={fills_h2} "
            f"rejected={rejected_half[hi_]} -> {verdict}"
        )
        unlocked = unlocked or passed

    fam_p = family_threshold_pvalue(ok_by_horizon, n_perm=N_PERM, rng=rng)
    print(
        f"\n  diagnostic: P(any horizon rho>={RHO_UNLOCK} under null | "
        f"observed n, {N_PERM} shared perms) = {fam_p:.4f}"
    )

    print(f"\nmarkout_screen: Phase B unlock rule -> "
          f"{'MET' if unlocked else 'NOT MET'}")
    return 0 if unlocked else 1


if __name__ == "__main__":
    sys.exit(main())
