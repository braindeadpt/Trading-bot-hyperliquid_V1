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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.data.research_database import ResearchDatabase  # noqa: E402

HORIZONS_MS = (300_000, 900_000, 3_600_000)  # 5m, 15m, 1h
MIN_FILLS_PER_HALF = 30
MIN_WALLETS = 20
RHO_UNLOCK = 0.30


def load_candles(db: sqlite3.Connection, symbol: str) -> tuple[list[int], list[float]]:
    rows = db.execute(
        "select timestamp_ms, close from candles_1m where symbol=? order by timestamp_ms",
        (symbol,),
    ).fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


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
    for s in symbols:
        ts[s], cl[s] = load_candles(rdb, s)
        print(f"  candles_1m[{s}]: {len(ts[s])} rows")

    def markout(coin: str, t0: int, p0: float, sign: float, h_ms: int) -> float | None:
        i = bisect.bisect_right(ts[coin], t0 + h_ms) - 1
        if i < 0:
            return None
        return sign * (cl[coin][i] / p0 - 1.0) * 1e4

    # per-wallet, per-horizon notional-weighted markout (full sample)
    W = collections.defaultdict(lambda: [[0.0, 0.0, 0] for _ in HORIZONS_MS])
    taker_times: list[int] = []
    for s in symbols:
        for w, t, side, p0, sz in rdb.execute(
            "select wallet, time_ms, side, px, sz from top_trader_fills "
            "where coin=? and crossed=1 order by time_ms",
            (s,),
        ):
            sign = 1.0 if side == "B" else -1.0
            notional = p0 * sz
            taker_times.append(t)
            for hi, h in enumerate(HORIZONS_MS):
                m = markout(s, t, p0, sign, h)
                if m is None:
                    continue
                W[w][hi][0] += notional * m
                W[w][hi][1] += notional
                W[w][hi][2] += 1

    taker_times.sort()
    n_taker = len(taker_times)
    print(f"\nmarkout_screen: {n_taker} taker fills across {len(W)} wallets")

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

    # split-half persistence per horizon
    if not taker_times:
        print("\nmarkout_screen: no taker fills -> BLOCKED")
        return 2
    half = taker_times[len(taker_times) // 2]
    print(f"\nsplit-half boundary t={half}")
    unlocked = False
    for hi, h in enumerate(HORIZONS_MS):
        H = collections.defaultdict(lambda: [[0.0, 0.0, 0], [0.0, 0.0, 0]])
        for s in symbols:
            for w, t, side, p0, sz in rdb.execute(
                "select wallet, time_ms, side, px, sz from top_trader_fills "
                "where coin=? and crossed=1",
                (s,),
            ):
                m = markout(s, t, p0, 1.0 if side == "B" else -1.0, h)
                if m is None:
                    continue
                k = 0 if t <= half else 1
                H[w][k][0] += p0 * sz * m
                H[w][k][1] += p0 * sz
                H[w][k][2] += 1
        ok = {
            w: (v[0][0] / v[0][1], v[1][0] / v[1][1])
            for w, v in H.items()
            if v[0][2] >= args.min_fills and v[1][2] >= args.min_fills
        }
        label = {300_000: "5m", 900_000: "15m", 3_600_000: "1h"}[h]
        if len(ok) < 4:
            print(f"  h={label}: n_wallets={len(ok)} (<4) -> cannot rank")
            continue
        r1 = sorted(ok, key=lambda w: ok[w][0])
        r2 = sorted(ok, key=lambda w: ok[w][1])
        rk1 = {w: i for i, w in enumerate(r1)}
        rk2 = {w: i for i, w in enumerate(r2)}
        n = len(ok)
        d2 = sum((rk1[w] - rk2[w]) ** 2 for w in ok)
        rho = 1 - 6 * d2 / (n * (n * n - 1))
        verdict = "UNLOCK" if (rho >= RHO_UNLOCK and n >= MIN_WALLETS) else "no"
        print(f"  h={label}: n_wallets={n} rho={rho:+.3f} -> {verdict}")
        unlocked = unlocked or (rho >= RHO_UNLOCK and n >= MIN_WALLETS)

    print(f"\nmarkout_screen: Phase B unlock rule -> {'MET' if unlocked else 'NOT MET'}")
    return 0 if unlocked else 1


if __name__ == "__main__":
    sys.exit(main())
