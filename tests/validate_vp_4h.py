#!/usr/bin/env python3
"""
tests/validate_vp_4h.py
Same 3 VP strategies on BTC 4h. Parameters adapted for 4h timeframe:
  SWING_MAX_AGE  168 -> 42  (7 days × 6 candles/day at 4h)
  TIME_EXIT_C     24 ->  6  (24h ÷ 4h per candle)
  MIN_SESSION_C    4 ->  2  (session = 6 candles; need 2 before VP valid)
  CANDLES_PER_DAY 24 ->  6  (for walk-forward windows)
  INTERVAL       1h -> 4h  (download)
All other logic, costs, signal conditions, walk-forward days identical to 1h run.
"""
import sys, logging
from pathlib import Path

# ── Patch module constants BEFORE any function from the module is called ──
sys.path.insert(0, str(Path(__file__).parent))
import validate_vp_strategies as vp

vp.INTERVAL        = "4h"
vp.CANDLES_PER_DAY = 6
vp.SWING_MAX_AGE   = 42   # 7 days × 6 candles/day
vp.TIME_EXIT_C     = 6    # 24h ÷ 4h
vp.MIN_SESSION_C   = 2    # need ≥2 candles (8h) before daily VP is valid
vp.CACHE           = vp.DATA_DIR / "BTCUSDT_6mo_4h.json"

# ── Re-import the symbols we'll call (they close over the patched module globals) ──
from validate_vp_strategies import (
    download, precompute,
    run_strategy_a, run_strategy_b, run_strategy_c,
    compute_metrics, walk_forward,
    print_block, print_wf, print_report,
)

import json, math

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("vp4h")

DATA_DIR  = vp.DATA_DIR
MIN_CANDLES = 1000   # ~167 days at 4h; equivalent coverage to 4000 1h candles


def main() -> None:
    log.info("=== VP Strategy Validation -- 4h Timeframe ===")

    raw = download(days=183)
    if len(raw) < MIN_CANDLES:
        log.error(f"Only {len(raw)} candles -- need {MIN_CANDLES}+. Aborting.")
        sys.exit(1)

    candles = precompute(raw)
    log.info(f"Candles after precompute: {len(candles):,}")

    total_days = (candles[-1]["ts"] - candles[0]["ts"]) / 86_400_000
    info = {
        "period":    f"{candles[0]['dt'].date()} -> {candles[-1]['dt'].date()}",
        "n_candles": len(candles),
        "timeframe": "4h",
    }
    log.info(f"Period: {info['period']}")

    log.info("Running Strategy A (Volume Profile)...")
    ta = run_strategy_a(candles)
    log.info(f"  -> {len(ta)} trades")

    log.info("Running Strategy B (Anchored VWAP)...")
    tb = run_strategy_b(candles)
    log.info(f"  -> {len(tb)} trades")

    log.info("Running Strategy C (Confluence A+B)...")
    tc = run_strategy_c(candles)
    log.info(f"  -> {len(tc)} trades")

    log.info("Running walk-forward analysis...")
    wf_a = walk_forward(candles, run_strategy_a, "A")
    wf_b = walk_forward(candles, run_strategy_b, "B")
    wf_c = walk_forward(candles, run_strategy_c, "C")

    ma = compute_metrics(ta, "A - Volume Profile", total_days)
    mb = compute_metrics(tb, "B - Anchored VWAP",  total_days)
    mc = compute_metrics(tc, "C - Confluence",      total_days)

    # ── Print report ──
    import math as _math

    W = 72
    def hr(c="-"): print(c * W)
    def blank(): print()
    EXITS = ["TP", "SL", "TIME", "TRAIL_BE"]

    blank()
    hr("=")
    print("  VP STRATEGY VALIDATION -- BTC/USDT Futures 4h")
    print(f"  Period  : {info['period']}")
    print(f"  Candles : {info['n_candles']:,}  (~{info['n_candles']//6:.0f} days)")
    print(f"  Params  : SWING_MAX_AGE=42  TIME_EXIT=6c(24h)  MIN_SESSION=2c")
    print(f"  Costs   : {vp.RT_COST*100:.3f}% RT")
    hr("=")

    results = [(ma, wf_a), (mb, wf_b), (mc, wf_c)]
    for (m, wf) in results:
        print_block(m)
        print_wf(wf, m["label"])
        blank()

    # Comparative summary
    blank()
    hr("=")
    print("  COMPARATIVE SUMMARY -- 4h")
    hr("-")
    print(f"  {'Strategy':<12} {'N':>5} {'WR':>7} {'PF':>6} {'ExpR':>7}"
          f" {'Sharpe':>7} {'MaxDD':>7} {'AvgH':>6}  Verdict")
    hr("-")
    for (m, _) in results:
        if m["n"] == 0:
            print(f"  {m['label'][:12]:<12} {'---':>5}")
            continue
        pf = m["pf"]
        flags = []
        if   pf >= 1.3:  flags.append("[OK] PF>=1.3")
        elif pf >= 1.0:  flags.append("[~] 1<=PF<1.3")
        else:            flags.append("[X] PF<1.0")
        if m["n"] < 30:  flags.append(f"[!] n={m['n']}<30")
        verdict = "  ".join(flags)
        print(f"  {m['label'][:12]:<12} {m['n']:>5} {m['wr']*100:>6.1f}%"
              f" {m['pf']:>6.2f} {m['exp_r']:>+7.3f}"
              f" {m['sharpe']:>7.2f} {m['max_dd_pct']:>6.1f}%"
              f" {m['avg_hold_h']:>5.1f}h  {verdict}")
    hr("=")
    blank()

    # Edge criteria
    print("  EDGE CRITERIA (PF OOS >= 1.3, std < 0.5, N >= 30):")
    for (m, wf) in results:
        if m["n"] == 0:
            print(f"  {m['label'][:12]}: FAIL (no trades)")
            continue
        pfs = [w["test_pf"] for w in wf if w["test_pf"] != float("inf")]
        avg = sum(pfs) / len(pfs) if pfs else 0
        std = _math.sqrt(sum((p-avg)**2 for p in pfs)/len(pfs)) if len(pfs)>1 else 0
        min_n = min((w["test_n"] for w in wf), default=0)
        ok_pf  = avg >= 1.3
        ok_std = std < 0.5
        ok_n   = min_n >= 30
        verdict = ("PASS"     if ok_pf and ok_std and ok_n else
                   "MARGINAL" if ok_pf or (avg >= 1.1 and ok_n) else "FAIL")
        print(f"  {m['label'][:12]}: {verdict}"
              f"  OOS_PF={avg:.2f}  std={std:.2f}  min_N={min_n}")
    hr("=")
    blank()

    # Decision rule
    print("  DECISION:")
    any_pass = any(True for (m, wf) in results
                   if min((w["test_n"] for w in wf), default=0) >= 30
                   and (sum(w["test_pf"] for w in wf if w["test_pf"] != float("inf")) /
                        max(len([w for w in wf if w["test_pf"] != float("inf")]), 1)) >= 1.3)
    all_pf_below_1 = all(m["pf"] < 1.0 for (m, _) in results if m["n"] > 0)
    if any_pass:
        print("  -> PF OOS >= 1.3 with N >= 30: open discussion on testnet.")
    elif all_pf_below_1:
        print("  -> All engines PF < 1.0 in 4h as well. Tese falsificada.")
    else:
        print("  -> No engine passes edge criteria. Results marginal/inconclusive.")
    blank()

    # Save JSON
    out = DATA_DIR / "vp_4h_results.json"
    def safe(x): return {k: v for k, v in x.items() if k not in ("top5w", "top5l")}
    result = {
        "info":       info,
        "strategy_a": safe(ma), "wf_a": wf_a,
        "strategy_b": safe(mb), "wf_b": wf_b,
        "strategy_c": safe(mc), "wf_c": wf_c,
    }
    out.write_text(json.dumps(result, indent=2, default=str))
    log.info(f"Results -> {out}")
    log.info("=== 4h validation complete. ===")


if __name__ == "__main__":
    main()
