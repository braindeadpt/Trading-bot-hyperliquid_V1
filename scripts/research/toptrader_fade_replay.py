"""TopTraderFlow contrarian ("fade the herd") replay harness.

Backlog §"Candidate - TopTraderFlow inversion": the shadow scoreboard showed
TopTraderFlow net PF 0.002 / WR 1.3% (n=985) — a signal THAT wrong is a
candidate contrarian signal. This harness replays the fade side honestly,
fixing the three documented caveats:

  1. Funding is CHARGED hourly from live bot.db ``funding_history.current``
     (the original sim had funding_coverage ~0 — PnL was unreliable).
  2. Entry is at the FIRST 1m candle open strictly after the bias sample
     timestamp (no lookahead); intrabar SL checked BEFORE TP (conservative).
  3. ``min_wallets`` cell lets the thin-wallet caveat be tested rather than
     assumed (BTC/SOL often show n_wallets=1..2).

Data sources (preregistered):
  - bias:  research DB ``top_trader_bias_samples`` (E:) — 2026-08-11→
  - 1m candles: research DB ``candles_1m`` (same DB, same coverage)
  - funding: live ``data/live/bot.db`` ``funding_history`` (research DB
    funding table is empty)

Tier-0 taker fees both sides (0.045%). Size fixed $1000 notional per trade.
"""
from __future__ import annotations

import argparse
import bisect
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

RESEARCH_DB = r"E:\hyperliquid_research\hyperliquid.db"
LIVE_DB = "data/live/bot.db"

TAKER_FEE = 0.00045          # tier-0, per side
NOTIONAL_USD = 1_000.0
HOUR_MS = 3_600_000

DEFAULTS: Dict[str, Any] = {
    "bias_threshold": 0.55,   # |net_bias| to trigger
    "min_wallets": 0,         # n_long + n_short floor (0 = off)
    "sl_pct": 0.02,
    "tp_pct": 0.02,
    "max_hold_h": 24.0,
    "min_gap_h": 4.0,         # min spacing between entries per symbol
}


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def load_bias(db_path: str, symbol: str, s_ms: int, e_ms: int
              ) -> List[Tuple[int, float, int]]:
    """(timestamp_ms, net_bias, n_wallets) ordered by ts."""
    db = _ro(db_path)
    rows = db.execute(
        "SELECT timestamp_ms, net_bias, n_long + n_short "
        "FROM top_trader_bias_samples "
        "WHERE symbol=? AND timestamp_ms>=? AND timestamp_ms<=? "
        "ORDER BY timestamp_ms",
        (symbol, s_ms, e_ms),
    ).fetchall()
    db.close()
    return [(int(t), float(b or 0.0), int(w or 0)) for t, b, w in rows]


def load_1m(db_path: str, symbol: str, s_ms: int, e_ms: int
            ) -> Tuple[List[int], List[Tuple[float, float, float, float]]]:
    """(ts[], (o,h,l,c)[]) — 1m bars."""
    db = _ro(db_path)
    rows = db.execute(
        "SELECT timestamp_ms, open, high, low, close FROM candles_1m "
        "WHERE symbol=? AND timestamp_ms>=? AND timestamp_ms<=? "
        "ORDER BY timestamp_ms",
        (symbol, s_ms, e_ms),
    ).fetchall()
    db.close()
    ts = [int(r[0]) for r in rows]
    ohlc = [(float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]
    return ts, ohlc


def load_funding(db_path: str, symbol: str) -> Tuple[List[int], List[float]]:
    """Hourly funding epochs: (ts[], current-rate[]) from live bot.db."""
    db = _ro(db_path)
    rows = db.execute(
        "SELECT timestamp, current FROM funding_history "
        "WHERE symbol=? ORDER BY timestamp", (symbol,),
    ).fetchall()
    db.close()
    # `current` is the rate for the hour the sample sits in; dedupe to one
    # sample per epoch hour (samples poll ~1/min).
    ep_ts: List[int] = []
    ep_rate: List[float] = []
    last_epoch = -1
    for t, cur in rows:
        epoch = int(t) // HOUR_MS
        if epoch != last_epoch:
            last_epoch = epoch
            ep_ts.append(epoch * HOUR_MS)
            ep_rate.append(float(cur or 0.0))
    return ep_ts, ep_rate


def _funding_cost(side: str, notional: float, entry_ms: int, exit_ms: int,
                  ep_ts: List[int], ep_rate: List[float]) -> float:
    """Hourly funding over the hold. Long pays when rate>0; short receives."""
    i = bisect.bisect_right(ep_ts, entry_ms)
    j = bisect.bisect_left(ep_ts, exit_ms)
    cost = 0.0
    for k in range(i, j):
        r = ep_rate[k]
        cost += r * notional if side == "long" else -r * notional
    return cost


def replay_fade(
    bias: List[Tuple[int, float, int]],
    c_ts: List[int],
    c_ohlc: List[Tuple[float, float, float, float]],
    funding: Tuple[List[int], List[float]],
    symbol: str,
    params: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Replay fade entries over the window. Returns per-trade dicts."""
    p = {**DEFAULTS, **(params or {})}
    thr = float(p["bias_threshold"])
    sl, tp = float(p["sl_pct"]), float(p["tp_pct"])
    max_hold = int(float(p["max_hold_h"]) * HOUR_MS)
    min_gap = int(float(p["min_gap_h"]) * HOUR_MS)
    min_w = int(p["min_wallets"])
    ep_ts, ep_rate = funding

    trades: List[Dict[str, Any]] = []
    open_pos: Optional[Dict[str, Any]] = None
    last_entry_ms = -10**15

    for bts, b, wallets in bias:
        # --- manage open position on candles after the last sample ---
        if open_pos is not None:
            i = bisect.bisect_right(c_ts, open_pos["checked_until_ms"])
            while i < len(c_ts) and c_ts[i] <= bts:
                cts, (o, h, l, c) = c_ts[i], c_ohlc[i]
                s = open_pos["side"]
                sl_px = open_pos["sl"]
                tp_px = open_pos["tp"]
                hit_sl = (l <= sl_px) if s == "long" else (h >= sl_px)
                hit_tp = (h >= tp_px) if s == "long" else (l <= tp_px)
                # conservative: SL first when both levels inside the bar
                if hit_sl or hit_tp:
                    if hit_sl:
                        exit_px, reason = sl_px, "sl"
                    else:
                        exit_px, reason = tp_px, "tp"
                    trades.append(_settle(open_pos, exit_px, cts, reason,
                                          ep_ts, ep_rate, symbol))
                    open_pos = None
                    break
                if cts - open_pos["entry_ts_ms"] >= max_hold:
                    trades.append(_settle(open_pos, c, cts, "max_hold",
                                          ep_ts, ep_rate, symbol))
                    open_pos = None
                    break
                open_pos["checked_until_ms"] = cts
                i += 1
            if open_pos is None:
                continue

        # --- entry: fade extreme consensus ---
        if (open_pos is None and abs(b) >= thr and wallets >= min_w
                and bts - last_entry_ms >= min_gap):
            # first 1m bar opening strictly after the sample
            i = bisect.bisect_right(c_ts, bts)
            if i >= len(c_ts):
                continue
            entry_px = c_ohlc[i][0]
            side = "short" if b > 0 else "long"
            sl_px = entry_px * (1 - sl) if side == "long" else entry_px * (1 + sl)
            tp_px = entry_px * (1 + tp) if side == "long" else entry_px * (1 - tp)
            open_pos = {
                "symbol": symbol, "side": side, "entry_ts_ms": c_ts[i],
                "entry_price": entry_px, "sl": sl_px, "tp": tp_px,
                "entry_bias": b, "n_wallets": wallets,
                "checked_until_ms": c_ts[i],
            }
            last_entry_ms = c_ts[i]

    # settle anything still open at last candle close
    if open_pos is not None and c_ts:
        open_pos = _settle(open_pos, c_ohlc[-1][3], c_ts[-1], "eod",
                           ep_ts, ep_rate, symbol)
        trades.append(open_pos)
    return trades


def _settle(pos: Dict[str, Any], exit_px: float, exit_ms: int,
            reason: str, ep_ts: List[int], ep_rate: List[float],
            symbol: str) -> Dict[str, Any]:
    """Apply exit + tier-0 fees + hourly funding. Returns the trade dict."""
    s = pos["side"]
    entry = float(pos["entry_price"])
    gross = ((exit_px - entry) if s == "long" else (entry - exit_px)) \
        / entry * NOTIONAL_USD
    fees = NOTIONAL_USD * TAKER_FEE * 2
    funding = _funding_cost(s, NOTIONAL_USD, pos["entry_ts_ms"], exit_ms,
                            ep_ts, ep_rate)
    pos.update({
        "exit_ts_ms": exit_ms, "exit_price": exit_px, "exit_reason": reason,
        "pnl_usd": gross - fees - funding,
        "funding_usd": funding, "fees_usd": fees,
    })
    pos.pop("checked_until_ms", None)
    pos.pop("sl", None)
    pos.pop("tp", None)
    return pos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="HYPE")
    ap.add_argument("--start", default="2026-08-12")
    ap.add_argument("--end", default="2026-09-08")
    ap.add_argument("--research-db", default=RESEARCH_DB)
    ap.add_argument("--live-db", default=LIVE_DB)
    ap.add_argument("--bias-threshold", type=float, default=None)
    args = ap.parse_args()

    from datetime import datetime, timezone
    s_ms = int(datetime.strptime(args.start, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)
    e_ms = int(datetime.strptime(args.end, "%Y-%m-%d")
               .replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
               .timestamp() * 1000)

    params = dict(DEFAULTS)
    if args.bias_threshold is not None:
        params["bias_threshold"] = args.bias_threshold

    bias = load_bias(args.research_db, args.symbol, s_ms, e_ms)
    c_ts, c_ohlc = load_1m(args.research_db, args.symbol, s_ms, e_ms)
    funding = load_funding(args.live_db, args.symbol)
    trades = replay_fade(bias, c_ts, c_ohlc, funding, args.symbol, params)

    pnls = [t["pnl_usd"] for t in trades]
    gw = sum(x for x in pnls if x > 0)
    gl = abs(sum(x for x in pnls if x < 0))
    print(f"{args.symbol} {args.start}..{args.end} | n={len(pnls)} "
          f"net={sum(pnls):.2f} PF={(gw/gl if gl else float('inf')):.3f} "
          f"bias_samples={len(bias)} candles={len(c_ts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
