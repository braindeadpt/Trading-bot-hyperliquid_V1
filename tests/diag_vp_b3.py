#!/usr/bin/env python3
"""
tests/diag_vp_b3.py
Trail-to-BE investigation:
  1. Code analysis of trail implementation
  2. Simulate 86 SL trades with trail correctly re-applied
  3. Timing of trail-fire and SL-hit for the 36 high-MFE trades
"""
import sys, logging, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from validate_vp_strategies import (
    download, precompute,
    Position, _record,
    _is_rejection_long, _is_rejection_short,
    TIME_EXIT_C, TRAIL_PCT, RT_COST,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("diag3")

W = 72
def hr(c="-"): print(c * W)
def section(t): print(); hr("="); print(f"  {t}"); hr("="); print()


# ==============================================================================
# Instrumented runner — captures per-candle data during hold
# ==============================================================================

def run_b_full_trace(candles):
    """
    Same signal logic as Strategy B.
    Per trade extra fields:
      hold_candles   : list of (idx, high, low, open, close) for every hold candle
      trail_fire_hold: hold-candle-index when trail first fired (None if never)
      mfe_pct, tp_dist, orig_sl_dist, atr_entry
    """
    trades, pos = [], None
    mfe = 0.0
    trail_fire_hold = None
    hold_candles = []
    orig_sl_px = 0.0

    for i, c in enumerate(candles):
        if pos is not None:
            hold_c = i - pos.entry_idx

            # Track MFE before check()
            fav = ((c["high"] - pos.entry) / pos.entry if pos.side == "long"
                   else (pos.entry - c["low"]) / pos.entry)
            mfe = max(mfe, fav)

            # Track which candle first triggers the trail condition
            if trail_fire_hold is None:
                if pos.side == "long" and c["high"] >= pos.trail_triggered:
                    trail_fire_hold = hold_c
                elif pos.side == "short" and c["low"] <= pos.trail_triggered:
                    trail_fire_hold = hold_c

            hold_candles.append({
                "hold_c": hold_c,
                "idx":    i,
                "open":   c["open"], "high": c["high"],
                "low":    c["low"],  "close": c["close"],
            })

            result = pos.check(c, hold_c)
            if result:
                reason, exit_px = result
                t = _record(pos, c, exit_px, reason)
                t["mfe_pct"]        = mfe
                t["tp_dist"]        = abs(pos.tp_px - pos.entry) / pos.entry
                t["orig_sl_dist"]   = orig_sl_px / pos.entry  # saved before any BE move
                t["atr_entry"]      = pos.setup.get("atr", 0)
                t["trail_fire_hold"]= trail_fire_hold
                t["exit_hold"]      = hold_c
                t["hold_candles"]   = hold_candles
                t["entry_px"]       = pos.entry
                t["orig_sl_px"]     = orig_sl_px
                t["tp_px"]          = pos.tp_px
                trades.append(t)
                pos = None
                mfe = 0.0
                trail_fire_hold = None
                hold_candles = []
                orig_sl_px = 0.0

        if pos is not None or i < 2 or i + 1 >= len(candles):
            continue

        touch  = candles[i - 1]
        reject = c
        atr    = reject.get("atr", 0)
        if atr <= 0:
            continue

        sups = touch.get("avwap_sup", [])
        ress = touch.get("avwap_res", [])

        for av_sup in sups:
            if touch["low"] <= av_sup and _is_rejection_long(reject, av_sup):
                nxt   = candles[i + 1]
                entry = nxt["open"]
                sl    = reject["low"] - 0.5 * atr
                if entry <= sl:
                    continue
                res_above = [r for r in ress if r > entry]
                tp = min(res_above) if res_above else entry + 2 * (entry - sl)
                if (tp - entry) < (entry - sl) or tp <= entry:
                    continue
                orig_sl_px = sl
                pos = Position("B", "long", entry, sl, tp, nxt["ts"], i + 1,
                               {"atr": atr, "regime": reject.get("regime", "unknown")})
                break

        if pos is not None:
            continue

        for av_res in ress:
            if touch["high"] >= av_res and _is_rejection_short(reject, av_res):
                nxt   = candles[i + 1]
                entry = nxt["open"]
                sl    = reject["high"] + 0.5 * atr
                if entry >= sl:
                    continue
                sup_below = [s for s in sups if s < entry]
                tp = max(sup_below) if sup_below else entry - 2 * (sl - entry)
                if (entry - tp) < (sl - entry) or tp >= entry:
                    continue
                orig_sl_px = sl
                pos = Position("B", "short", entry, sl, tp, nxt["ts"], i + 1,
                               {"atr": atr, "regime": reject.get("regime", "unknown")})
                break

    return trades


# ==============================================================================
# Candle-level re-simulator (independent of Position.check())
# Used to verify trail logic and test close-based trail alternative.
# ==============================================================================

def simulate_exit(hold_candles, entry, orig_sl, tp, side,
                  use_close_trail=False):
    """
    Replay hold candles with given trail config.
    Returns: (reason, exit_px, be_triggered, trail_fire_hold, exit_hold_c)
    """
    trail_level = (entry + TRAIL_PCT * (tp - entry) if side == "long"
                   else entry - TRAIL_PCT * (entry - tp))
    sl_current  = orig_sl
    be_triggered = False
    trail_hold   = None

    for hc in hold_candles:
        hold_c = hc["hold_c"]
        op, hi, lo, cl = hc["open"], hc["high"], hc["low"], hc["close"]

        if hold_c >= TIME_EXIT_C:
            return "TIME", op, be_triggered, trail_hold, hold_c

        if side == "long":
            # Trail trigger: HIGH-based (current) or CLOSE-based (alternative)
            trig = hi if not use_close_trail else cl
            if not be_triggered and trig >= trail_level:
                be_triggered = True
                sl_current   = entry
                trail_hold   = hold_c

            if op <= sl_current: return "SL",  op,         be_triggered, trail_hold, hold_c
            if op >= tp:         return "TP",  op,         be_triggered, trail_hold, hold_c
            if lo <= sl_current: return "SL",  sl_current, be_triggered, trail_hold, hold_c
            if hi >= tp:         return "TP",  tp,         be_triggered, trail_hold, hold_c
        else:
            trig = lo if not use_close_trail else cl
            if not be_triggered and trig <= trail_level:
                be_triggered = True
                sl_current   = entry
                trail_hold   = hold_c

            if op >= sl_current: return "SL",  op,         be_triggered, trail_hold, hold_c
            if op <= tp:         return "TP",  op,         be_triggered, trail_hold, hold_c
            if hi >= sl_current: return "SL",  sl_current, be_triggered, trail_hold, hold_c
            if lo <= tp:         return "TP",  tp,         be_triggered, trail_hold, hold_c

    return "END", (hold_candles[-1]["close"] if hold_candles else entry), be_triggered, trail_hold, -1


def pnl_raw(entry, exit_px, side):
    return (exit_px - entry) / entry if side == "long" else (entry - exit_px) / entry


# ==============================================================================
# ANALYSIS 1 — Trail implementation audit
# ==============================================================================

def analysis1_trail_code():
    section("ANALYSIS 1 -- Trail Implementation Audit (Position.check)")

    print("  Relevant code in Position.check() [validate_vp_strategies.py:384]:")
    print()
    print("  LONG path:")
    print("    [1] if not self.be and hi >= self.trail_triggered:")
    print("            self.be = True; self.sl_px = self.entry         <- trail fires")
    print("    [2] if op <= self.sl_px: return 'SL', op               <- gap-open check")
    print("    [3] if op >= self.tp_px: return 'TP', op               <- gap-open check")
    print("    [4] if lo <= self.sl_px: return 'SL', self.sl_px       <- intra-candle")
    print("    [5] if hi >= self.tp_px: return 'TP', self.tp_px       <- intra-candle")
    print()
    print("  SHORT path: symmetric (lo <= trail_triggered)")
    print()
    print("  EXECUTION ORDER ISSUES:")
    print()
    print("  Issue A — Same-candle fire-and-hit:")
    print("    Trail fires at [1] because hi >= trail_trigger.")
    print("    sl_px becomes entry. Then [4]: if lo <= entry -> SL at entry.")
    print("    -> Trade exits as SL with exit_price = entry (BE, -0.11% net).")
    print("    -> be=True, exit_reason='SL'. Appears as a loss but IS the BE behaviour.")
    print()
    print("  Issue B — Gap-open below entry AFTER trail fires (same candle):")
    print("    Trail fires at [1] (hi >= trail_trigger). sl_px = entry.")
    print("    Then [2]: if op <= entry -> SL at op (BELOW entry).")
    print("    -> Requires: candle opened below entry, rallied past trail, reversed.")
    print("    -> be=True, exit at op < entry, raw_pct < 0 (worse than BE).")
    print()
    print("  Issue C — Trail fires, then gap-open below entry on NEXT candle:")
    print("    Candle K: hi >= trail_trigger, lo > entry -> no exit, SL=entry.")
    print("    Candle K+1: op < entry -> SL at op < entry.")
    print("    -> be=True, exit at op < entry, raw_pct < 0.")
    print()
    print("  Summary: trail fires correctly (hi/lo based, order is right).")
    print("  The 36 'MFE>=50% TP' SL trades WILL have be=True (trail fired).")
    print("  The question is whether they exited exactly AT entry (BE)")
    print("  or BELOW entry (Issues B/C -- gap through the BE stop).")


# ==============================================================================
# ANALYSIS 2 — Classify the 36 high-MFE SL trades + simulation
# ==============================================================================

def analysis2_simulate(trades):
    section("ANALYSIS 2 -- Classify 36 High-MFE SL Trades + Corrected Simulation")

    sl_trades = [t for t in trades if t["exit_reason"] == "SL"]
    tp_trades = [t for t in trades if t["exit_reason"] == "TP"]

    mfe_ratio = lambda t: (t["mfe_pct"] / t["tp_dist"] if t["tp_dist"] > 0 else 0)
    high_mfe  = [t for t in sl_trades if mfe_ratio(t) >= 0.50]
    low_mfe   = [t for t in sl_trades if mfe_ratio(t) <  0.50]

    print(f"  SL trades total: {len(sl_trades)}")
    print(f"    MFE >= 50% TP:  {len(high_mfe)}  (trail should have fired)")
    print(f"    MFE <  50% TP:  {len(low_mfe)}")
    print()

    # Classify high-MFE by be status
    be_true  = [t for t in high_mfe if t["be"]]
    be_false = [t for t in high_mfe if not t["be"]]
    print(f"  Of {len(high_mfe)} high-MFE SL trades:")
    print(f"    be=True  (trail fired):  {len(be_true)}")
    print(f"    be=False (trail MISSED): {len(be_false)}")
    print()

    if be_false:
        print("  *** UNEXPECTED: be=False with MFE>=50% TP ***")
        print("  These trades had price reach trail trigger but be was not set.")
        for t in be_false[:5]:
            print(f"    {t['entry_dt']}  {t['side'].upper():<5}"
                  f"  entry={t['entry_px']:.1f}  mfe={t['mfe_pct']*100:.3f}%"
                  f"  tp_dist={t['tp_dist']*100:.3f}%  trail_fire_hold={t['trail_fire_hold']}")

    # For be=True: how close is exit_price to entry?
    print(f"  For {len(be_true)} be=True SL trades — exit_price vs entry_price:")
    if be_true:
        diffs = [(t["exit_price"] - t["entry_px"]) / t["entry_px"] * 100
                 for t in be_true]
        # For short: entry > exit should be positive
        signed = []
        for t in be_true:
            d = (t["exit_price"] - t["entry_px"]) / t["entry_px"] * 100
            if t["side"] == "short": d = -d  # positive = favorable for short
            signed.append(d)
        at_entry    = sum(1 for d in signed if abs(d) < 0.01)
        below_entry = sum(1 for d in signed if d < -0.01)
        above_entry = sum(1 for d in signed if d > 0.01)
        print(f"    Exit AT entry (|diff|<0.01%): {at_entry}")
        print(f"    Exit BELOW entry (worse):     {below_entry}  <- gap-through-BE")
        print(f"    Exit ABOVE entry:             {above_entry}  <- should not occur for SL")
        if signed:
            s_sorted = sorted(signed)
            print(f"    dist distribution: min={s_sorted[0]:.3f}%  "
                  f"med={s_sorted[len(s_sorted)//2]:.3f}%  "
                  f"max={s_sorted[-1]:.3f}%")
    print()

    # Independent re-simulation to verify current code
    print("  Re-simulation (HIGH-based trail, same as current code):")
    mismatches = 0
    sim_results = {"TP": 0, "SL_BE": 0, "SL_worse": 0, "SL_orig": 0, "TIME": 0}
    for t in trades:
        reason_s, exit_s, be_s, tf_s, eh_s = simulate_exit(
            t["hold_candles"], t["entry_px"], t["orig_sl_px"], t["tp_px"], t["side"],
            use_close_trail=False)
        raw_s = pnl_raw(t["entry_px"], exit_s, t["side"])
        reason_actual = t["exit_reason"]
        be_actual     = t["be"]
        if reason_s != reason_actual or abs(be_s != be_actual):
            mismatches += 1
        # Classify sim outcome
        if reason_s == "TP":
            sim_results["TP"] += 1
        elif reason_s == "SL":
            if abs(exit_s - t["entry_px"]) < 0.50:  # within 50 cents = BE
                sim_results["SL_BE"] += 1
            elif raw_s > -t["orig_sl_dist"] * 0.9:  # better than 90% of orig SL
                sim_results["SL_worse"] += 1
            else:
                sim_results["SL_orig"] += 1
        else:
            sim_results["TIME"] += 1
    print(f"    Simulation mismatches with original: {mismatches}")
    print(f"    Outcome breakdown (from re-sim):")
    print(f"      TP hits:              {sim_results['TP']}")
    print(f"      SL at ~BE (entry):    {sim_results['SL_BE']}")
    print(f"      SL worse than BE:     {sim_results['SL_worse']}")
    print(f"      SL near orig SL:      {sim_results['SL_orig']}")
    print(f"      TIME exits:           {sim_results['TIME']}")
    print()

    # CLOSE-based trail simulation
    print("  Re-simulation with CLOSE-based trail (fires on candle CLOSE, not HIGH):")
    close_trades = {"TP": 0, "SL_BE": 0, "SL_worse": 0, "SL_orig": 0, "TIME": 0,
                    "net_total": 0.0}
    high_trades  = {"TP": 0, "SL_BE": 0, "SL_worse": 0, "SL_orig": 0, "TIME": 0,
                    "net_total": 0.0}
    wins_close, wins_high = 0, 0
    n = len(trades)

    for t in trades:
        for use_close, bucket in [(False, high_trades), (True, close_trades)]:
            reason_s, exit_s, be_s, _, _ = simulate_exit(
                t["hold_candles"], t["entry_px"], t["orig_sl_px"], t["tp_px"], t["side"],
                use_close_trail=use_close)
            raw_s = pnl_raw(t["entry_px"], exit_s, t["side"])
            net_s = raw_s - RT_COST
            bucket["net_total"] += net_s
            if net_s > 0:
                if use_close: wins_close += 1
                else:         wins_high  += 1
            if reason_s == "TP":
                bucket["TP"] += 1
            elif reason_s == "SL":
                if abs(exit_s - t["entry_px"]) < 0.50:
                    bucket["SL_BE"] += 1
                elif raw_s > -t["orig_sl_dist"] * 0.9:
                    bucket["SL_worse"] += 1
                else:
                    bucket["SL_orig"] += 1
            else:
                bucket["TIME"] += 1

    gw_h  = sum(t["net_pct"] for t in trades if t["net_pct"] > 0)
    gl_h  = abs(sum(t["net_pct"] for t in trades if t["net_pct"] <= 0))
    pf_h  = gw_h / gl_h if gl_h > 0 else float("inf")

    # Recompute net_pcts for close-trail
    close_nets = []
    for t in trades:
        reason_s, exit_s, _, _, _ = simulate_exit(
            t["hold_candles"], t["entry_px"], t["orig_sl_px"], t["tp_px"], t["side"],
            use_close_trail=True)
        close_nets.append(pnl_raw(t["entry_px"], exit_s, t["side"]) - RT_COST)
    gw_c = sum(v for v in close_nets if v > 0)
    gl_c = abs(sum(v for v in close_nets if v <= 0))
    pf_c = gw_c / gl_c if gl_c > 0 else float("inf")
    wr_c = sum(1 for v in close_nets if v > 0) / len(close_nets) * 100
    wr_h = wins_high / n * 100

    hr("-")
    print(f"  {'Metric':<20}  {'HIGH-trail (current)':>22}  {'CLOSE-trail (alt)':>20}")
    hr("-")
    print(f"  {'WR':<20}  {wr_h:>21.1f}%  {wr_c:>19.1f}%")
    print(f"  {'PF':<20}  {pf_h:>22.2f}  {pf_c:>20.2f}")
    print(f"  {'TP exits':<20}  {high_trades['TP']:>22}  {close_trades['TP']:>20}")
    print(f"  {'SL at BE':<20}  {high_trades['SL_BE']:>22}  {close_trades['SL_BE']:>20}")
    print(f"  {'SL worse-than-BE':<20}  {high_trades['SL_worse']:>22}  {close_trades['SL_worse']:>20}")
    print(f"  {'SL near orig SL':<20}  {high_trades['SL_orig']:>22}  {close_trades['SL_orig']:>20}")
    hr("-")
    print()


# ==============================================================================
# ANALYSIS 3 — Timing of trail-fire and SL-hit (36 high-MFE trades)
# ==============================================================================

def analysis3_timing(trades):
    section("ANALYSIS 3 -- Timing: Trail Fire vs SL Hit (high-MFE trades)")

    sl_trades = [t for t in trades if t["exit_reason"] == "SL"]
    mfe_ratio = lambda t: (t["mfe_pct"] / t["tp_dist"] if t["tp_dist"] > 0 else 0)
    high_mfe  = [t for t in sl_trades if mfe_ratio(t) >= 0.50]

    print(f"  Analysing {len(high_mfe)} SL trades with MFE >= 50% TP")
    print()

    # Use independent re-simulation to get trail_fire_hold for each trade
    tf_holds = []
    reversal_holds = []

    for t in high_mfe:
        _, _, _, trail_h, exit_h = simulate_exit(
            t["hold_candles"], t["entry_px"], t["orig_sl_px"], t["tp_px"], t["side"],
            use_close_trail=False)
        if trail_h is not None:
            tf_holds.append(trail_h)
            reversal_holds.append(exit_h - trail_h)

    def _q(vals):
        if not vals: return [None]*5
        s = sorted(vals)
        n = len(s)
        return [s[0], s[n//4], s[n//2], s[3*n//4], s[-1]]

    if tf_holds:
        q_tf = _q(tf_holds)
        q_rv = _q(reversal_holds)

        print(f"  A) Entry -> Trail-fire (candles):")
        print(f"     min={q_tf[0]}  Q1={q_tf[1]}  med={q_tf[2]}  Q3={q_tf[3]}  max={q_tf[4]}")
        print(f"     mean={sum(tf_holds)/len(tf_holds):.1f}  (1h candles)")
        print()
        print(f"  B) Trail-fire -> SL-hit (candles elapsed after trail fires):")
        print(f"     min={q_rv[0]}  Q1={q_rv[1]}  med={q_rv[2]}  Q3={q_rv[3]}  max={q_rv[4]}")
        print(f"     mean={sum(reversal_holds)/len(reversal_holds):.1f}  (1h candles)")
        print()

        # Histogram of reversal time
        buckets = [(0, 0), (1, 1), (2, 4), (5, 11), (12, 99)]
        labels  = ["same candle", "1 candle later", "2-4 candles", "5-11 candles", "12+ candles"]
        print(f"  C) Histogram — how many candles between trail fire and SL hit:")
        for (lo, hi), lbl in zip(buckets, labels):
            cnt = sum(1 for r in reversal_holds if lo <= r <= hi)
            bar = "#" * cnt
            print(f"     {lbl:<18}: {cnt:>3}  {bar}")
        print()

        same = sum(1 for r in reversal_holds if r == 0)
        fast = sum(1 for r in reversal_holds if r <= 2)
        print(f"  D) Summary:")
        print(f"     Trail fires AND SL hits on SAME candle: {same} ({same/len(reversal_holds)*100:.1f}%)")
        print(f"     Trail fires, SL hits within 2 candles:  {fast} ({fast/len(reversal_holds)*100:.1f}%)")
        n_slow = len(reversal_holds) - fast
        print(f"     Trail fires, SL hits after 3+ candles:  {n_slow} ({n_slow/len(reversal_holds)*100:.1f}%)")
        print()
        if same > len(reversal_holds) * 0.5:
            verdict = ("More than half fire and hit on the SAME candle. "
                       "Trail fires on intraday high, low reverses within same candle. "
                       "This is a 'wick trap': price briefly touches trail level then reverses. "
                       "Close-based trail would avoid these same-candle fires.")
        elif fast > len(reversal_holds) * 0.6:
            verdict = ("Most reversals happen within 1-2 candles of trail fire. "
                       "Very fast mean-reversion after trail trigger. "
                       "Trail fires at wick tips that don't hold.")
        else:
            verdict = ("Reversals spread across multiple candles. "
                       "Not a wick-trap issue. Market genuinely reverses over hours.")
        print(f"  VERDICT: {verdict}")
    else:
        print("  No trail fire data available (unexpected).")


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    log.info("Loading candles (cache)...")
    raw = download(days=183)
    candles = precompute(raw)
    log.info(f"  {len(candles):,} candles ready")

    log.info("Running instrumented Strategy B (full trace)...")
    trades = run_b_full_trace(candles)
    log.info(f"  {len(trades)} trades captured")

    analysis1_trail_code()
    analysis2_simulate(trades)
    analysis3_timing(trades)

    log.info("=== Diagnostic 3 complete ===")


if __name__ == "__main__":
    main()
