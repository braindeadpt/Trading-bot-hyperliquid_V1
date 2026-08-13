"""Liquidation flush shadow — LIVE mode (paper, 7-day window).

Monitors the real liquidation feed (okx + bybit, the sources the bot
ingests into `liquidation_events`) in real time, and paper-executes the
exact cell that passed the v2 simulation gate:

    ETH / p90 of dominant-minute notional / hold 30m / fade / no SL
    (v2 real-source baseline: n=46, WR 50.0%, PF 2.35, avg +7.0 bps)

Design (fidelity to scripts/liquidation_flush_shadow.py):

  * Flush: 1-minute bucket where dominant-side notional >= pinned p90
    threshold (computed from the full real sample 08-09..08-13, so the
    live cell is byte-identical to the simulated one — no re-fit).
  * Entry: OPEN of the next 1m candle after the flush minute closes
    (candle whose ts == flush_minute_ms + 60_000). Same as simulation's
    bisect_left(ts_list, minute_ms + 60_000).
  * Exit:  close of the candle at entry + 30 minutes. Fees 0.045% x 2.
  * No stop-loss: the v2 evidence showed SL 1%/2% is a no-op in this
    cell (never triggers before the 30m hold exit).

State persistence (survives restarts — Freebuff kills background jobs):

  * JSON state file (data/research/liquidation_flush_shadow_live_state.json)
    written atomically after every change. Relaunching the script resumes
    exactly where it stopped: already-scanned minutes are deduped, pending
    flushes and open positions are completed from the DB candles.
  * On startup the script backfills any minutes with candles that have
    not yet been scanned (so downtime between restarts loses nothing),
    tagged kind="backfill"; minutes scanned live are kind="live".

Known limitation (documented): fills use the candle OPEN, not the tick.
The bot writes candles_1m at minute close, so a "live" entry is
observable ~1 min after the real open — identical to the simulation's
fill assumption, but ~60s later than a tick-fill would be.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "live" / "bot.db"
STATE_DIR = ROOT / "data" / "research"
STATE_PATH = STATE_DIR / "liquidation_flush_shadow_live_state.json"
LOG_PATH = ROOT / "logs" / "liquidation_flush_shadow_live.log"

SYMBOL = "ETH"
HOLD_MIN = 30
FEES_RT_PCT = 0.045 * 2  # 0.090% round-trip, same as simulation

# Pinned from the real sample (okx+bybit, 08-09..08-13): dominant-minute
# notional p90 per symbol, in USD. ETH is the cell under test; the others
# are monitored for context only.
PINNED_P90: Dict[str, float] = {
    "BTC": 9_897_000.0,
    "ETH": 1_024_000.0,
    "SOL": 46_000.0,
    "HYPE": 61_000.0,
}

# v2 simulation baseline (real source) for the comparison report.
BASELINE = {
    "n": 46,
    "win_rate": 50.0,
    "profit_factor": 2.35,
    "avg_net_bps": 7.0,
    "note": "ETH p90 hold=30m fade, real source 08-09..08-13",
}

POLL_SECONDS = 15
STALL_WARN_SECONDS = 300  # no new liquidation events for 5 min -> warn


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def connect_ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            log("state file corrupt — starting fresh")
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_event_ts": 0,
        "scanned_minutes": {},        # symbol -> max minute scanned
        "flushes": {},                # flush minute -> pending flush info
        "open_positions": {},         # flush minute -> open position
        "trades": [],                 # closed trades
    }


def save_state(state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def get_candles(cur: sqlite3.Cursor, symbol: str) -> Dict[int, Tuple[float, float, float, float]]:
    cur.execute(
        "SELECT timestamp_ms, open, high, low, close FROM candles_1m "
        "WHERE symbol = ? ORDER BY timestamp_ms ASC",
        (symbol,),
    )
    return {int(ts): (float(o), float(h), float(l), float(c)) for ts, o, h, l, c in cur.fetchall()}


def get_new_events(cur: sqlite3.Cursor, after_ts: int, sources: Tuple[str, ...]) -> List[Tuple[str, int, float, str]]:
    ph = ",".join("?" * len(sources))
    cur.execute(
        f"SELECT symbol, timestamp_ms, notional_usd, side FROM liquidation_events "
        f"WHERE source IN ({ph}) AND timestamp_ms > ? ORDER BY timestamp_ms ASC",
        (*sources, after_ts),
    )
    return [(sym, int(ts), float(n), side) for sym, ts, n, side in cur.fetchall()]


def summarize(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(trades)
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    losses = [t["net_pct"] for t in trades if t["net_pct"] <= 0]
    gross_wins = sum(t["net_pct"] + FEES_RT_PCT for t in trades if t["net_pct"] > 0)
    net = sum(t["net_pct"] for t in trades)
    return {
        "n": n,
        "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
        "profit_factor": round(gross_wins / abs(sum(losses)), 3) if losses and sum(losses) else 0.0,
        "net_bps": round(net * 100, 1),
        "avg_net_bps": round(net / n * 100, 2) if n else 0.0,
    }


def report(state: Dict[str, Any], conn: sqlite3.Connection) -> None:
    trades = state["trades"]
    live = [t for t in trades if t["kind"] == "live"]
    backfill = [t for t in trades if t["kind"] == "backfill"]
    cur = conn.cursor()
    cur.execute("SELECT MAX(timestamp_ms) FROM liquidation_events WHERE source IN ('okx','bybit')")
    last_feed = cur.fetchone()[0] or 0
    now = int(time.time() * 1000)
    staleness = (now - last_feed) / 1000.0

    log("=" * 78)
    log("SHADOW-LIVE REPORT  (ETH p90 / hold 30m / fade — paper)")
    log(f"  baseline (v2 real): n={BASELINE['n']} WR={BASELINE['win_rate']}% "
        f"PF={BASELINE['profit_factor']} avg={BASELINE['avg_net_bps']}bps")
    for label, subset in (("live", live), ("backfill", backfill), ("total", trades)):
        if not subset:
            log(f"  {label:8}: 0 trades")
            continue
        s = summarize(subset)
        log(f"  {label:8}: n={s['n']:>3} WR={s['win_rate']:>5.1f}% PF={s['profit_factor']:>5.2f} "
            f"net={s['net_bps']:>7.0f}bps avg={s['avg_net_bps']:>6.2f}bps")
    if trades:
        s = summarize(trades)
        log(f"  vs baseline: n {s['n']}/{BASELINE['n']} | avg {s['avg_net_bps']:+.2f} vs "
            f"{BASELINE['avg_net_bps']:+.2f} bps | PF {s['profit_factor']:.2f} vs {BASELINE['profit_factor']:.2f}")
    log(f"  feed: last event {staleness:.0f}s ago | scanned minutes: "
        f"{sum(state['scanned_minutes'].values())} | open: {len(state['open_positions'])} | "
        f"pending flushes: {len(state['flushes'])}")
    log("=" * 78)


def process_forward(state: Dict[str, Any], conn: sqlite3.Connection, live: bool) -> int:
    """Scan one pass over the DB: close buckets, fill entries, close exits.

    Returns number of new liquidation events consumed.
    """
    cur = conn.cursor()
    sources = ("okx", "bybit")
    events = get_new_events(cur, state["last_event_ts"], sources)
    if events:
        state["last_event_ts"] = events[-1][1]

    # Bucket events into minutes (partial buckets for the live minute are fine —
    # a bucket is only finalized once its candle exists).
    buckets: Dict[int, Dict[str, float]] = {}
    for sym, ts, notional, side in events:
        if sym != SYMBOL:
            continue
        m = ts // 60_000
        b = buckets.setdefault(m, {"long": 0.0, "short": 0.0})
        b[side] = b.get(side, 0.0) + notional

    candles = get_candles(cur, SYMBOL)
    ts_list = sorted(candles)
    if not ts_list:
        return len(events)

    # Candle convention (verified in DB): ts = END of the minute
    # (ts % 60_000 == 59_999). Entry semantics mirror the simulation exactly:
    # entry_i = bisect_left(ts_list, flush_minute_ms + 60_000) — the first
    # candle whose minute OPENS after the flush minute closes (candle m+1).
    import bisect

    # 1) Finalize buckets for minutes whose own candle exists (minute closed).
    scanned = state["scanned_minutes"].get(SYMBOL, 0)
    cand_ts_set = set(candles)
    for m in sorted(buckets):
        if m <= scanned:
            continue
        if (m * 60_000 + 59_999) not in cand_ts_set:
            continue  # minute not closed yet — wait for its candle
        b = buckets[m]
        dominant = "long" if b["long"] >= b["short"] else "short"
        if b[dominant] >= PINNED_P90[SYMBOL]:
            state["flushes"][m] = {
                "minute_ms": m * 60_000,
                "dominant_side": dominant,
                "notional": round(b[dominant], 2),
                "kind": "live" if live else "backfill",
            }
            log(f"FLUSH minute {m} ({datetime.fromtimestamp(m*60, tz=timezone.utc).strftime('%m-%d %H:%M')}) "
                f"dominant={dominant} ${b[dominant]/1e3:.0f}K >= p90 ${PINNED_P90[SYMBOL]/1e3:.0f}K — scheduling fade")
        scanned = max(scanned, m)
    state["scanned_minutes"][SYMBOL] = scanned

    # 2) Fill entries: flush minute m enters at the OPEN of the first candle
    #    with ts >= m*60_000 + 60_000 (candle m+1), when it becomes available.
    for m in list(state["flushes"]):
        f = state["flushes"][m]
        entry_i = bisect.bisect_left(ts_list, m * 60_000 + 60_000)
        if entry_i >= len(ts_list):
            continue  # entry candle not available yet
        entry_ts = ts_list[entry_i]
        o, _, _, _ = candles[entry_ts]
        long_flush = f["dominant_side"] == "long"
        # Fade semantics match the simulation: liquidation side 'long' = forced
        # sells -> buy the dip; side 'short' = forced buys -> sell the bounce.
        side = "long" if long_flush else "short"
        state["open_positions"][m] = {
            "flush_minute": m,
            "entry_ts": entry_ts,
            "entry_i": entry_i,
            "side": side,
            "entry_price": o,
            "kind": f["kind"],
            "dominant_side": f["dominant_side"],
            "flush_notional": f["notional"],
        }
        del state["flushes"][m]
        log(f"ENTRY {side.upper()} @ {o:.2f} (candle {entry_ts}, flush m{m})")

    # 3) Close exits: entry index + HOLD_MIN candles, exit at close of that candle.
    for m in list(state["open_positions"]):
        pos = state["open_positions"][m]
        exit_i = pos["entry_i"] + HOLD_MIN
        if exit_i >= len(ts_list):
            continue
        exit_ts = ts_list[exit_i]
        close = candles[exit_ts][3]
        if pos["side"] == "long":
            ret = (close / pos["entry_price"]) - 1.0
        else:
            ret = (pos["entry_price"] / close) - 1.0
        net_pct = ret * 100.0 - FEES_RT_PCT
        trade = {
            "kind": pos["kind"],
            "flush_minute": m,
            "entry_ts": pos["entry_ts"],
            "exit_ts": exit_ts,
            "side": pos["side"],
            "entry_price": round(pos["entry_price"], 6),
            "exit_price": round(close, 6),
            "net_pct": round(net_pct, 4),
            "flush_notional": pos["flush_notional"],
            "dominant_side": pos["dominant_side"],
        }
        state["trades"].append(trade)
        del state["open_positions"][m]
        log(f"EXIT  {pos['side'].upper()} @ {close:.2f} net={net_pct*100:+.2f}bps "
            f"({'WIN' if net_pct > 0 else 'LOSS'})")

    return len(events)


def run_forever(state: Dict[str, Any], poll: int) -> None:
    # Initial backfill: scan all minutes with candles (kind=backfill) so a
    # restart loses nothing, then continue live.
    conn = connect_ro()
    n = process_forward(state, conn, live=False)
    save_state(state)
    log(f"startup pass consumed {n} new events — backfill done, entering live loop")
    report(state, conn)
    conn.close()

    last_report = time.time()
    while True:
        time.sleep(poll)
        try:
            conn = connect_ro()
            sig = (len(state["trades"]), len(state["open_positions"]), len(state["flushes"]))
            n = process_forward(state, conn, live=True)
            sig2 = (len(state["trades"]), len(state["open_positions"]), len(state["flushes"]))
            changed = sig != sig2
            if changed:  # persist every state mutation so a crash loses nothing
                save_state(state)
            if changed or time.time() - last_report > 600:
                report(state, conn)
                last_report = time.time()
            conn.close()
        except Exception as e:  # keep the monitor alive across transient DB errors
            log(f"pass error: {e!r}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true",
                    help="single pass (backfill only), then exit — for smoke tests")
    ap.add_argument("--poll", type=int, default=POLL_SECONDS, help="poll interval seconds")
    args = ap.parse_args()

    log("=== shadow-live flush harness starting "
        f"(ETH p90 {PINNED_P90['ETH']/1e3:.0f}K, hold {HOLD_MIN}m, fade, paper) ===")
    state = load_state()
    if args.once:
        conn = connect_ro()
        n = process_forward(state, conn, live=False)
        save_state(state)
        report(state, conn)
        log(f"once-pass done: consumed {n} events, {len(state['trades'])} total trades")
        return
    run_forever(state, args.poll)


if __name__ == "__main__":
    main()
