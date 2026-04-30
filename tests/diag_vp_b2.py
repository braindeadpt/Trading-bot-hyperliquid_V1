#!/usr/bin/env python3
"""
tests/diag_vp_b2.py
3 descriptive analyses on the 96 Strategy B trades.
No parameter changes, no re-optimisation. Pure post-trade analytics.
"""
import sys, math, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from validate_vp_strategies import (
    download, precompute,
    Position, _record,
    _is_rejection_long, _is_rejection_short,
    TIME_EXIT_C,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("diag2")

W = 72
def hr(c="-"): print(c * W)
def section(t): print(); hr("="); print(f"  {t}"); hr("="); print()


# ==============================================================================
# Instrumented Strategy B runner
# Captures MFE + extra metrics at position open and during hold.
# ==============================================================================

def _recent_swing_levels(candles, entry_idx, lookback=48):
    """Highest high and lowest low in the last `lookback` candles before entry."""
    start = max(0, entry_idx - lookback)
    window = candles[start:entry_idx]
    if not window:
        return None, None
    return max(c["high"] for c in window), min(c["low"] for c in window)


def run_b_instrumented(candles):
    """
    Same logic as run_strategy_b. Extra data captured per trade:
      mfe_pct       : max favorable excursion (raw fraction) before exit
      mae_pct       : max adverse excursion (raw fraction) before exit
      orig_sl_dist  : |sl - entry| / entry at trade open (before any BE move)
      tp_dist       : |tp - entry| / entry
      atr_entry     : ATR(14) at entry candle
      adx_entry     : ADX(14) at entry candle
      regime_entry  : 'trending'/'ranging'/'unknown'
      dist_swing_hi : |entry - recent_swing_high| / entry  (last 48 candles)
      dist_swing_lo : |entry - recent_swing_low|  / entry
    """
    trades, pos = [], None
    mfe, mae    = 0.0, 0.0
    orig_sl_dist = 0.0
    tp_dist_open = 0.0

    for i, c in enumerate(candles):
        if pos is not None:
            # Track MFE / MAE on each candle before checking exit
            if pos.side == "long":
                fav = (c["high"] - pos.entry) / pos.entry
                adv = (pos.entry - c["low"])  / pos.entry
            else:
                fav = (pos.entry - c["low"])  / pos.entry
                adv = (c["high"] - pos.entry) / pos.entry
            mfe = max(mfe, fav)
            mae = max(mae, adv)

            hold_c = i - pos.entry_idx
            result = pos.check(c, hold_c)
            if result:
                reason, exit_px = result
                t = _record(pos, c, exit_px, reason)
                t["mfe_pct"]      = mfe
                t["mae_pct"]      = mae
                t["orig_sl_dist"] = orig_sl_dist
                t["tp_dist"]      = tp_dist_open
                t["atr_entry"]    = pos.setup.get("atr", 0)
                t["adx_entry"]    = pos.setup.get("adx", 0)
                t["regime_entry"] = pos.setup.get("regime", "unknown")
                t["dist_swing_hi"]= pos.setup.get("dist_swing_hi", None)
                t["dist_swing_lo"]= pos.setup.get("dist_swing_lo", None)
                trades.append(t)
                pos = None
                mfe = mae = orig_sl_dist = tp_dist_open = 0.0

        if pos is not None or i < 2 or i + 1 >= len(candles):
            continue

        touch  = candles[i - 1]
        reject = c
        atr    = reject.get("atr", 0)
        if atr <= 0:
            continue

        sups = touch.get("avwap_sup", [])
        ress = touch.get("avwap_res", [])

        # Long signal
        for av_sup in sups:
            if touch["low"] <= av_sup:
                if _is_rejection_long(reject, av_sup):
                    nxt   = candles[i + 1]
                    entry = nxt["open"]
                    sl    = reject["low"] - 0.5 * atr
                    if entry <= sl:
                        continue
                    res_above = [r for r in ress if r > entry]
                    tp = min(res_above) if res_above else entry + 2 * (entry - sl)
                    if (tp - entry) < (entry - sl) or tp <= entry:
                        continue
                    sh, sl_sw = _recent_swing_levels(candles, i + 1)
                    orig_sl_dist  = (entry - sl) / entry
                    tp_dist_open  = (tp - entry) / entry
                    adx_e = nxt.get("adx", reject.get("adx", 0))
                    reg_e = nxt.get("regime", reject.get("regime", "unknown"))
                    pos = Position("B", "long", entry, sl, tp, nxt["ts"], i + 1, {
                        "atr": atr, "adx": adx_e, "regime": reg_e,
                        "avwap_touched": av_sup,
                        "dist_swing_hi": abs(entry - sh) / entry if sh else None,
                        "dist_swing_lo": abs(entry - sl_sw) / entry if sl_sw else None,
                    })
                    break

        if pos is not None:
            continue

        # Short signal
        for av_res in ress:
            if touch["high"] >= av_res:
                if _is_rejection_short(reject, av_res):
                    nxt   = candles[i + 1]
                    entry = nxt["open"]
                    sl    = reject["high"] + 0.5 * atr
                    if entry >= sl:
                        continue
                    sup_below = [s for s in sups if s < entry]
                    tp = max(sup_below) if sup_below else entry - 2 * (sl - entry)
                    if (entry - tp) < (sl - entry) or tp >= entry:
                        continue
                    sh, sl_sw = _recent_swing_levels(candles, i + 1)
                    orig_sl_dist  = (sl - entry) / entry
                    tp_dist_open  = (entry - tp) / entry
                    adx_e = nxt.get("adx", reject.get("adx", 0))
                    reg_e = nxt.get("regime", reject.get("regime", "unknown"))
                    pos = Position("B", "short", entry, sl, tp, nxt["ts"], i + 1, {
                        "atr": atr, "adx": adx_e, "regime": reg_e,
                        "avwap_touched": av_res,
                        "dist_swing_hi": abs(entry - sh) / entry if sh else None,
                        "dist_swing_lo": abs(entry - sl_sw) / entry if sl_sw else None,
                    })
                    break

    return trades


# ==============================================================================
# Stat helpers
# ==============================================================================

def _pct_fmt(v): return f"{v*100:+.3f}%" if v is not None else "  N/A   "
def _f(v, fmt=".3f"): return f"{v:{fmt}}" if v is not None else "N/A"

def _quartiles(vals):
    if not vals:
        return None, None, None, None, None
    s = sorted(vals)
    n = len(s)
    return (s[0],
            s[n // 4],
            s[n // 2],
            s[3 * n // 4],
            s[-1])

def _pf(trades):
    gw = sum(t["net_pct"] for t in trades if t["net_pct"] > 0)
    gl = abs(sum(t["net_pct"] for t in trades if t["net_pct"] <= 0))
    return gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)

def _wr(trades):
    return (sum(1 for t in trades if t["net_pct"] > 0) / len(trades)) if trades else 0.0


# ==============================================================================
# ANALYSIS 1 — MFE on SL trades
# ==============================================================================

def analysis1(trades):
    section("ANALYSIS 1 -- Max Favorable Excursion on SL Trades")

    sl_trades  = [t for t in trades if t["exit_reason"] == "SL"]
    tp_trades  = [t for t in trades if t["exit_reason"] == "TP"]
    all_trades = trades

    print(f"  Total trades: {len(trades)}  |  SL exits: {len(sl_trades)}  |"
          f"  TP exits: {len(tp_trades)}")
    print()

    # MFE distribution on SL trades
    mfes     = [t["mfe_pct"] for t in sl_trades]
    tp_dists = [t["tp_dist"] for t in sl_trades]
    sl_dists = [t["orig_sl_dist"] for t in sl_trades]

    q = _quartiles(mfes)
    print(f"  MFE distribution on {len(sl_trades)} SL trades (% of entry price):")
    print(f"    min={q[0]*100:+.3f}%  Q1={q[1]*100:+.3f}%  median={q[2]*100:+.3f}%"
          f"  Q3={q[3]*100:+.3f}%  max={q[4]*100:+.3f}%")
    print(f"    mean={sum(mfes)/len(mfes)*100:+.3f}%")
    print()

    # MFE as fraction of TP distance
    mfe_ratio = [t["mfe_pct"] / t["tp_dist"] if t["tp_dist"] > 0 else 0
                 for t in sl_trades]
    qr = _quartiles(mfe_ratio)
    print(f"  MFE / TP_distance ratio (0=never moved, 1=reached TP):")
    print(f"    min={qr[0]:.3f}  Q1={qr[1]:.3f}  median={qr[2]:.3f}"
          f"  Q3={qr[3]:.3f}  max={qr[4]:.3f}")
    print(f"    mean={sum(mfe_ratio)/len(mfe_ratio):.3f}")
    print()

    # How many reached 50% of TP distance (trail trigger)
    reached_50 = sum(1 for r in mfe_ratio if r >= 0.50)
    reached_25 = sum(1 for r in mfe_ratio if r >= 0.25)
    reached_10 = sum(1 for r in mfe_ratio if r >= 0.10)
    never_pos  = sum(1 for t in sl_trades if t["mfe_pct"] <= 0)
    print(f"  Of {len(sl_trades)} SL trades, how many reached:")
    print(f"    MFE <= 0  (never in profit)   : {never_pos:>4} ({never_pos/len(sl_trades)*100:.1f}%)")
    print(f"    MFE >= 10% of TP distance      : {reached_10:>4} ({reached_10/len(sl_trades)*100:.1f}%)")
    print(f"    MFE >= 25% of TP distance      : {reached_25:>4} ({reached_25/len(sl_trades)*100:.1f}%)")
    print(f"    MFE >= 50% of TP distance (BE) : {reached_50:>4} ({reached_50/len(sl_trades)*100:.1f}%)")
    print()

    # Histogram of MFE/TP ratio in buckets
    buckets = [0, 0.1, 0.25, 0.5, 0.75, 1.01]
    labels  = ["0-10%", "10-25%", "25-50%", "50-75%", "75-100%"]
    counts  = [sum(1 for r in mfe_ratio if buckets[k] <= r < buckets[k+1])
               for k in range(len(labels))]
    print(f"  Histogram of MFE/TP ratio (SL trades only):")
    for lbl, cnt in zip(labels, counts):
        bar = "#" * cnt
        print(f"    {lbl:<10}: {cnt:>3}  {bar}")
    print()

    # Key question answer
    avg_mfe_pct = sum(mfes) / len(mfes) * 100
    avg_ratio   = sum(mfe_ratio) / len(mfe_ratio)
    if never_pos > len(sl_trades) * 0.5:
        verdict = "Trades never progress -- SL hit almost immediately after entry."
    elif avg_ratio < 0.10:
        verdict = "Minimal favorable progress (avg <10% of TP). SL hit quickly."
    elif avg_ratio < 0.30:
        verdict = "Some progress but weak (avg <30% of TP). Not reaching midpoint."
    else:
        verdict = "Trades reach meaningful progress before reverting to SL."
    print(f"  VERDICT: {verdict}")


# ==============================================================================
# ANALYSIS 2 — Volatility at Entry
# ==============================================================================

def analysis2(trades):
    section("ANALYSIS 2 -- Volatility (ATR ratio) at Entry")

    winners = [t for t in trades if t["net_pct"] > 0]
    losers  = [t for t in trades if t["net_pct"] <= 0]

    def _ratio_stats(group, label):
        ratios = [t["orig_sl_dist"] / (t["atr_entry"] / (t["entry_price"]))
                  if t["atr_entry"] > 0 else None
                  for t in group]
        ratios = [r for r in ratios if r is not None]
        if not ratios:
            print(f"  {label}: no data"); return
        q = _quartiles(ratios)
        print(f"  {label} (n={len(group)}):")
        print(f"    sl_dist/ATR  min={q[0]:.3f}  Q1={q[1]:.3f}  med={q[2]:.3f}"
              f"  Q3={q[3]:.3f}  max={q[4]:.3f}  mean={sum(ratios)/len(ratios):.3f}")

        # ATR absolute distribution
        atrs = [t["atr_entry"] / t["entry_price"] * 100 for t in group if t["atr_entry"] > 0]
        print(f"    ATR%  min={min(atrs):.3f}%  med={sorted(atrs)[len(atrs)//2]:.3f}%"
              f"  max={max(atrs):.3f}%  mean={sum(atrs)/len(atrs):.3f}%")

        # SL distance distribution
        sls = [t["orig_sl_dist"] * 100 for t in group]
        print(f"    SL%   min={min(sls):.3f}%  med={sorted(sls)[len(sls)//2]:.3f}%"
              f"  max={max(sls):.3f}%  mean={sum(sls)/len(sls):.3f}%")
        print()

    _ratio_stats(trades,  "ALL  trades")
    _ratio_stats(winners, "WIN  trades")
    _ratio_stats(losers,  "LOSS trades")

    # Quantile split: is SL/ATR ratio predictive?
    ratios_all = [(t["orig_sl_dist"] / (t["atr_entry"] / t["entry_price"])
                   if t["atr_entry"] > 0 else None, t)
                  for t in trades]
    ratios_all = [(r, t) for r, t in ratios_all if r is not None]
    ratios_all.sort(key=lambda x: x[0])
    q_size = len(ratios_all) // 4 or 1

    print(f"  WR by sl_dist/ATR quartile:")
    hr("-")
    print(f"  {'Quartile':<12} {'ATR ratio range':>20}  {'n':>4}  {'WR':>7}  {'PF':>6}")
    hr("-")
    for qi in range(4):
        chunk = ratios_all[qi*q_size : (qi+1)*q_size]
        if not chunk:
            continue
        r_vals = [r for r, _ in chunk]
        t_vals = [t for _, t in chunk]
        wr = _wr(t_vals)
        pf = _pf(t_vals)
        print(f"  Q{qi+1} (bottom {25*(qi+1)}%)"
              f"  {min(r_vals):.2f} - {max(r_vals):.2f}"
              f"  {len(chunk):>4}  {wr*100:>6.1f}%  {pf:>6.2f}")
    print()

    # Key question answer
    w_sls = [t["orig_sl_dist"] * 100 for t in winners] if winners else [0]
    l_sls = [t["orig_sl_dist"] * 100 for t in losers]  if losers  else [0]
    w_med = sorted(w_sls)[len(w_sls)//2]
    l_med = sorted(l_sls)[len(l_sls)//2]
    print(f"  Winners median SL%: {w_med:.3f}%  |  Losers median SL%: {l_med:.3f}%")
    diff = abs(w_med - l_med)
    if diff < 0.05:
        verdict = "SL sizing similar between winners and losers -- ATR ratio not a differentiator."
    else:
        verdict = f"SL sizing differs by {diff:.3f}% -- {'tighter SL wins more' if w_med < l_med else 'wider SL wins more'}."
    print(f"  VERDICT: {verdict}")


# ==============================================================================
# ANALYSIS 3 — Market Regime at Entry
# ==============================================================================

def analysis3(trades):
    section("ANALYSIS 3 -- Market Regime at Entry")

    # --- 3a: ADX regime split ---
    print("  3a. ADX regime split:")
    hr("-")
    print(f"  {'Regime':<18} {'n':>4}  {'WR':>7}  {'PF':>6}  {'avg_MFE':>9}  {'avg_SL%':>8}")
    hr("-")
    for label, fn in [
        ("ADX < 20 (ranging)",    lambda t: t.get("adx_entry", 0) < 20),
        ("ADX 20-25 (neutral)",   lambda t: 20 <= t.get("adx_entry", 0) < 25),
        ("ADX > 25 (trending)",   lambda t: t.get("adx_entry", 0) >= 25),
        ("regime==ranging",       lambda t: t.get("regime_entry") == "ranging"),
        ("regime==trending",      lambda t: t.get("regime_entry") == "trending"),
    ]:
        grp = [t for t in trades if fn(t)]
        if not grp:
            print(f"  {label:<18}    0   (no data)")
            continue
        wr   = _wr(grp)
        pf   = _pf(grp)
        mfes = [t["mfe_pct"] * 100 for t in grp]
        sls  = [t["orig_sl_dist"] * 100 for t in grp]
        print(f"  {label:<18} {len(grp):>4}  {wr*100:>6.1f}%  {pf:>6.2f}"
              f"  {sum(mfes)/len(mfes):>+8.3f}%  {sum(sls)/len(sls):>7.3f}%")
    print()

    # --- 3b: Long vs Short split by regime ---
    print("  3b. Long vs Short by regime:")
    hr("-")
    print(f"  {'Group':<25} {'n':>4}  {'WR':>7}  {'PF':>6}")
    hr("-")
    for regime in ["ranging", "trending", "unknown"]:
        for side in ["long", "short"]:
            grp = [t for t in trades
                   if t.get("regime_entry") == regime and t["side"] == side]
            if not grp:
                continue
            print(f"  {regime} / {side:<8}           {len(grp):>4}  {_wr(grp)*100:>6.1f}%  {_pf(grp):>6.2f}")
    print()

    # --- 3c: Swing stretch analysis ---
    # dist_swing_lo for longs (how far below the most recent swing low is the entry)
    # dist_swing_hi for shorts
    has_swing = [t for t in trades if t.get("dist_swing_hi") is not None
                                    and t.get("dist_swing_lo") is not None]
    if has_swing:
        print(f"  3c. Swing stretch (distance from entry to recent 48-candle swing):")
        print(f"      For LONG: dist_to_swing_LOW (lower = entered closer to recent bottom)")
        print(f"      For SHORT: dist_to_swing_HIGH (lower = entered closer to recent top)")
        hr("-")

        longs  = [t for t in has_swing if t["side"] == "long"]
        shorts = [t for t in has_swing if t["side"] == "short"]

        for label, grp, key in [
            ("LONG  dist_to_swing_lo", longs,  "dist_swing_lo"),
            ("SHORT dist_to_swing_hi", shorts, "dist_swing_hi"),
        ]:
            if not grp:
                continue
            vals = [t[key] * 100 for t in grp if t[key] is not None]
            if not vals:
                continue
            q = _quartiles(vals)
            print(f"\n  {label} (n={len(grp)}):")
            print(f"    min={q[0]:.2f}%  Q1={q[1]:.2f}%  med={q[2]:.2f}%"
                  f"  Q3={q[3]:.2f}%  max={q[4]:.2f}%")

            # WR by quartile of stretch
            grp_s = sorted(grp, key=lambda t: t[key] or 0)
            q_size = len(grp_s) // 4 or 1
            print(f"  WR by stretch quartile ({label}):")
            for qi in range(4):
                chunk = grp_s[qi*q_size : (qi+1)*q_size]
                if not chunk:
                    continue
                v = [t[key]*100 for t in chunk if t[key] is not None]
                wr = _wr(chunk)
                pf = _pf(chunk)
                print(f"    Q{qi+1} ({min(v):.1f}%-{max(v):.1f}%):"
                      f"  n={len(chunk)}  WR={wr*100:.1f}%  PF={pf:.2f}")
        print()

    # --- 3d: ADX quartile split ---
    print("  3d. WR by ADX quartile:")
    hr("-")
    adx_vals = sorted(trades, key=lambda t: t.get("adx_entry", 0))
    q_size = len(adx_vals) // 4 or 1
    print(f"  {'Quartile':<12} {'ADX range':>15}  {'n':>4}  {'WR':>7}  {'PF':>6}")
    hr("-")
    for qi in range(4):
        chunk = adx_vals[qi*q_size : (qi+1)*q_size]
        if not chunk:
            continue
        adxs = [t.get("adx_entry", 0) for t in chunk]
        wr = _wr(chunk)
        pf = _pf(chunk)
        print(f"  Q{qi+1} (bot {25*(qi+1)}%)    {min(adxs):>6.1f} - {max(adxs):>5.1f}"
              f"  {len(chunk):>4}  {wr*100:>6.1f}%  {pf:>6.2f}")
    print()

    # Key question answer
    ranging = [t for t in trades if t.get("regime_entry") == "ranging"]
    trending= [t for t in trades if t.get("regime_entry") == "trending"]
    r_wr = _wr(ranging)  if ranging  else 0
    t_wr = _wr(trending) if trending else 0
    best = "ranging" if r_wr > t_wr else "trending"
    best_wr = max(r_wr, t_wr)
    if best_wr < 0.15:
        verdict = f"Strategy loses in BOTH regimes (best WR={best_wr*100:.1f}% in {best}). No sub-regime edge found."
    else:
        verdict = f"Better performance in {best} regime (WR={best_wr*100:.1f}%). Marginal differentiation."
    print(f"  VERDICT: {verdict}")


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    log.info("Loading candles (cache)...")
    raw = download(days=183)
    candles = precompute(raw)
    log.info(f"  {len(candles):,} candles ready")

    log.info("Running instrumented Strategy B...")
    trades = run_b_instrumented(candles)
    log.info(f"  {len(trades)} trades captured")

    analysis1(trades)
    analysis2(trades)
    analysis3(trades)

    log.info("=== Diagnostic 2 complete ===")


if __name__ == "__main__":
    main()
