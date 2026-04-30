#!/usr/bin/env python3
"""
tests/diag_vp_b.py
4 diagnostic checks on Strategy B as requested.
Runs from cached candle data -- no extra download needed.
"""
import sys, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from validate_vp_strategies import (
    download, precompute,
    run_strategy_a, run_strategy_b, run_strategy_c,
    Position, _record,
    _is_rejection_long, _is_rejection_short,
    RT_COST,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("diag")

W = 72
def hr(c="-"): print(c * W)
def section(title):
    print()
    hr("=")
    print(f"  {title}")
    hr("=")


# ==============================================================================
# CHECK 1 -- Direction Sanity
# ==============================================================================

def check1_direction(trades):
    section("CHECK 1 -- Direction Sanity (Strategy B)")
    longs  = [t for t in trades if t["side"] == "long"][:5]
    shorts = [t for t in trades if t["side"] == "short"][:5]

    print(f"\n  NOTE: sl_price may be moved to entry if BE was triggered (be=True).")
    print(f"        tp_price is NEVER modified -- always original target.")
    print()
    print(f"  {'side':<10} {'entry':>10} {'sl_stored':>12} {'tp_stored':>12}  {'be':<5}  check")
    hr()

    all_ok = True
    for t in longs:
        e, sl, tp = t["entry_price"], t["sl_price"], t["tp_price"]
        be = t["be"]
        # tp must always be above entry for LONG
        tp_ok = tp > e
        # sl: if be=False, must be below entry; if be=True, sl was moved to entry (expected)
        sl_ok = (sl < e) if not be else (abs(sl - e) < 0.01)
        ok = tp_ok and sl_ok
        if not ok:
            all_ok = False
        flag = "[OK]" if ok else "[BUG!]"
        print(f"  LONG      {e:>10.2f} {sl:>12.2f} {tp:>12.2f}  {str(be):<5}  {flag}"
              + ("  <-- tp<=entry" if not tp_ok else "")
              + ("  <-- sl>=entry (no BE)" if not sl_ok and not be else ""))

    hr("-")
    for t in shorts:
        e, sl, tp = t["entry_price"], t["sl_price"], t["tp_price"]
        be = t["be"]
        # tp must always be below entry for SHORT
        tp_ok = tp < e
        sl_ok = (sl > e) if not be else (abs(sl - e) < 0.01)
        ok = tp_ok and sl_ok
        if not ok:
            all_ok = False
        flag = "[OK]" if ok else "[BUG!]"
        print(f"  SHORT     {e:>10.2f} {sl:>12.2f} {tp:>12.2f}  {str(be):<5}  {flag}"
              + ("  <-- tp>=entry" if not tp_ok else "")
              + ("  <-- sl<=entry (no BE)" if not sl_ok and not be else ""))

    print()
    print(f"  VERDICT: {'ALL OK' if all_ok else '*** BUG FOUND ***'}")


# ==============================================================================
# CHECK 2 -- PnL Sign Verification
# ==============================================================================

def check2_pnl(trades):
    section("CHECK 2 -- PnL Sign Verification (Strategy B top5 W + top5 L)")
    by_net  = sorted(trades, key=lambda t: t["net_pct"])
    winners = by_net[-5:][::-1]
    losers  = by_net[:5]

    print(f"\n  raw = (exit-entry)/entry [LONG] or (entry-exit)/entry [SHORT]")
    print(f"  If move is favorable (price went our way), raw must be > 0.")
    print()
    hdr = f"  {'side':<5} {'entry':>9} {'exit':>9} {'move':>9} {'raw%':>8} {'net%':>8}  {'reason':<8}  sign_ok?"
    print(hdr)
    hr()

    all_ok = True

    def _show(t, tag):
        e, x = t["entry_price"], t["exit_price"]
        raw  = t["raw_pct"]
        side = t["side"]
        move = (x - e) if side == "long" else (e - x)
        # move > 0 means price went in our direction
        expected_positive = move > 0
        actual_positive   = raw > 0
        # if move is zero (e.g. gap fill at SL), both should be non-positive
        sign_ok = (expected_positive == actual_positive) or (abs(move) < 0.01)
        flag = "[OK]" if sign_ok else "[BUG!]"
        nonlocal all_ok
        if not sign_ok:
            all_ok = False
        print(f"  {side.upper():<5} {e:>9.1f} {x:>9.1f} {move:>+8.1f}  {raw*100:>+7.3f}% {t['net_pct']*100:>+7.3f}%"
              f"  {t['exit_reason']:<8}  {flag}")

    print("  -- Winners --")
    for t in winners:
        _show(t, "W")
    print("  -- Losers --")
    for t in losers:
        _show(t, "L")

    print()
    print(f"  VERDICT: {'ALL OK' if all_ok else '*** BUG FOUND ***'}")


# ==============================================================================
# CHECK 3 -- Exit Reason Distribution
# ==============================================================================

def check3_exits(ta, tb, tc):
    section("CHECK 3 -- Exit Reason Distribution (A, B, C)")
    print()
    for label, trades in [("A", ta), ("B", tb), ("C", tc)]:
        n = len(trades)
        if n == 0:
            print(f"  {label}: no trades"); continue
        reasons = {}
        for t in trades:
            reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
        parts = []
        for k in ["TP", "SL", "TIME", "TRAIL_BE"]:
            v = reasons.get(k, 0)
            parts.append(f"{k}: {v:>3} ({v/n*100:>5.1f}%)")
        print(f"  {label} n={n:>3}:  " + "  |  ".join(parts))

        longs  = [t for t in trades if t["side"] == "long"]
        shorts = [t for t in trades if t["side"] == "short"]
        l_wr = (sum(1 for t in longs  if t["net_pct"] > 0) / len(longs)  * 100) if longs  else 0.0
        s_wr = (sum(1 for t in shorts if t["net_pct"] > 0) / len(shorts) * 100) if shorts else 0.0
        l_sl = sum(1 for t in longs  if t["exit_reason"] == "SL")
        s_sl = sum(1 for t in shorts if t["exit_reason"] == "SL")
        print(f"         Long:  n={len(longs):>3} WR={l_wr:>5.1f}%  SL_hits={l_sl}"
              f"  |  Short: n={len(shorts):>3} WR={s_wr:>5.1f}%  SL_hits={s_sl}")
    print()


# ==============================================================================
# CHECK 4 -- Inverted Strategy B
# ==============================================================================

def run_strategy_b_inverted(candles):
    """
    Identical signal detection to B, but sides swapped:
      - Original LONG signal  -> enter SHORT (sl=orig_tp, tp=orig_sl)
      - Original SHORT signal -> enter LONG  (sl=orig_tp, tp=orig_sl)
    'Nao muda mais nada' -- only the side is flipped, exit levels use original SL/TP prices.
    """
    trades, pos = [], None

    for i, c in enumerate(candles):
        if pos is not None:
            hold_c = i - pos.entry_idx
            result = pos.check(c, hold_c)
            if result:
                reason, exit_px = result
                trades.append(_record(pos, c, exit_px, reason))
                pos = None

        if pos is not None or i < 2 or i + 1 >= len(candles):
            continue

        touch  = candles[i - 1]
        reject = c
        atr    = reject.get("atr", 0)
        if atr <= 0:
            continue

        sups = touch.get("avwap_sup", [])
        ress = touch.get("avwap_res", [])

        # ---- LONG signal -> enter SHORT ----
        for av_sup in sups:
            if touch["low"] <= av_sup:
                if _is_rejection_long(reject, av_sup):
                    nxt   = candles[i + 1]
                    entry = nxt["open"]
                    orig_sl = reject["low"] - 0.5 * atr           # below entry
                    res_above = [r for r in ress if r > entry]
                    orig_tp = (min(res_above) if res_above
                               else entry + 2 * (entry - orig_sl)) # above entry
                    # Inverted SHORT: SL = orig_tp (above), TP = orig_sl (below)
                    sl_new, tp_new = orig_tp, orig_sl
                    if sl_new <= entry or tp_new >= entry:
                        continue
                    pos = Position("B_inv", "short", entry, sl_new, tp_new,
                                   nxt["ts"], i + 1,
                                   {"atr": atr, "regime": reject.get("regime", "unknown")})
                    break

        if pos is not None:
            continue

        # ---- SHORT signal -> enter LONG ----
        for av_res in ress:
            if touch["high"] >= av_res:
                if _is_rejection_short(reject, av_res):
                    nxt   = candles[i + 1]
                    entry = nxt["open"]
                    orig_sl = reject["high"] + 0.5 * atr           # above entry
                    sup_below = [s for s in sups if s < entry]
                    orig_tp = (max(sup_below) if sup_below
                               else entry - 2 * (orig_sl - entry)) # below entry
                    # Inverted LONG: SL = orig_tp (below), TP = orig_sl (above)
                    sl_new, tp_new = orig_tp, orig_sl
                    if sl_new >= entry or tp_new <= entry:
                        continue
                    pos = Position("B_inv", "long", entry, sl_new, tp_new,
                                   nxt["ts"], i + 1,
                                   {"atr": atr, "regime": reject.get("regime", "unknown")})
                    break

    return trades


def check4_inverted(candles, trades_b):
    section("CHECK 4 -- Inverted Strategy B (LONG->SHORT, SHORT->LONG)")
    print()
    print("  Running inverted B...")
    trades_inv = run_strategy_b_inverted(candles)
    print(f"  Done: {len(trades_inv)} inverted trades vs {len(trades_b)} original")
    print()

    print(f"  {'label':<15} {'n':>5} {'WR':>8} {'PF':>7}  exit breakdown")
    hr()
    for label, trades in [("B original",  trades_b),
                           ("B inverted",  trades_inv)]:
        n = len(trades)
        if n == 0:
            print(f"  {label}: no trades"); continue
        wins   = [t for t in trades if t["net_pct"] > 0]
        losses = [t for t in trades if t["net_pct"] <= 0]
        wr     = len(wins) / n
        gw     = sum(t["net_pct"] for t in wins)
        gl     = abs(sum(t["net_pct"] for t in losses))
        pf     = gw / gl if gl > 0 else float("inf")
        reasons = {}
        for t in trades:
            reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
        r_parts = [f"{k}:{reasons.get(k,0):>3}" for k in ["TP", "SL", "TIME"]]
        print(f"  {label:<15} {n:>5} {wr*100:>7.1f}% {pf:>7.2f}  [{' | '.join(r_parts)}]")

    print()
    inv_wr = (sum(1 for t in trades_inv if t["net_pct"] > 0) / len(trades_inv) * 100
              if trades_inv else 0)
    b_wr   = (sum(1 for t in trades_b  if t["net_pct"] > 0) / len(trades_b)  * 100
              if trades_b else 0)
    print("  INTERPRETATION:")
    if inv_wr > 80:
        print(f"  -> Inverted WR={inv_wr:.1f}% >> original WR={b_wr:.1f}%")
        print("     CONFIRMED: direction inversion bug in original code.")
    elif inv_wr > b_wr + 20:
        print(f"  -> Inverted WR={inv_wr:.1f}% significantly > original WR={b_wr:.1f}%")
        print("     PROBABLE direction bug -- investigate entry logic.")
    else:
        print(f"  -> Inverted WR={inv_wr:.1f}%  ~  original WR={b_wr:.1f}%")
        print("     No major direction inversion. Signal simply has no edge.")


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    log.info("Loading candles (cache)...")
    raw = download(days=183)
    candles = precompute(raw)
    log.info(f"  {len(candles):,} candles ready")

    log.info("Running A, B, C...")
    ta = run_strategy_a(candles)
    tb = run_strategy_b(candles)
    tc = run_strategy_c(candles)
    log.info(f"  A={len(ta)}, B={len(tb)}, C={len(tc)}")

    check1_direction(tb)
    check2_pnl(tb)
    check3_exits(ta, tb, tc)
    check4_inverted(candles, tb)

    print()
    log.info("=== Diagnostic complete ===")


if __name__ == "__main__":
    main()
