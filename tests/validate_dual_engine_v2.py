#!/usr/bin/env python3
"""
tests/validate_dual_engine_v2.py
Dual Engine Validation v2 -- Confluence-based signals.
Isolated from live bot (no src/ imports).
Reuses data cache from validate_dual_engine.py (tests/data/BTCUSDT_90d.json).

ENGINE A -- Short Exhaustion Reversal (SHORT only, 4/6 conditions)
  1. Price rose >1.5% in last 30min
  2. Volume Z-score > 2.0
  3. Upper wick > 50% of candle range
  4. OI stable/falling  [strict: needs real OI data | lenient: includes no-data]
  5. Funding > 0.01%
  6. Red candle (close < open)
  SL: 0.4% above signal candle high  |  TP: 1.0% below entry
  Time exits: hold==6 & PnL<-0.3% -> TIME_DEAD  |  hold>=12 & PnL<+0.3% -> TIME_FLAT

ENGINE B -- Long Breakout with Accumulation (LONG only, 4/6 conditions)
  1. Price in tight range last 2h (<1.5%)
  2. Volume Z-score > 2.0 AND progressive (3 candles increasing)
  3. Green candle + close > high of last 6 candles
  4. OI rising > 0.5% (strict by design: oi_chg=0.0 fails)
  5. Funding neutral (-0.005% to +0.005%)
  6. CVD positive last 12 candles (approximated via candle direction)
  SL: 0.4% below min low of last 6 candles  |  TP: 1.5% above entry
  Time exits: hold>=8 & PnL<+0.4% -> TIME_FLAT

Costs (Hyperliquid): 0.045% taker + 0.015% maker + 0.02%x2 slip = 0.11% RT
"""
import json, math, sys, logging
from pathlib import Path
from datetime import datetime
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("v2")

HERE     = Path(__file__).parent
DATA_DIR = HERE / "data"
CACHE    = DATA_DIR / "BTCUSDT_90d.json"

TAKER    = 0.00045
MAKER    = 0.00015
SLIP     = 0.0002
RT_COST  = TAKER + SLIP + MAKER + SLIP   # 0.11%
VOL_LOOKBACK = 20


# ==============================================================================
# ENGINE CONFIGS
# ==============================================================================

EA = dict(
    score_min=4,
    z_min=2.0,
    price_rise_min=0.015,      # >1.5% in 30min
    price_rise_candles=6,
    wick_ratio_min=0.50,       # upper wick > 50% of candle range
    oi_stable_max=0.005,       # OI change < 0.5%
    funding_min=0.0001,        # > 0.01%
    sl_offset=0.004,           # 0.4% above signal candle high
    tp_pct=0.010,              # 1.0% TP
    trail_pct=0.005,           # trail to BE after +0.5%
    dead_candles=6,            # TIME_DEAD check
    dead_threshold=-0.003,     # if PnL < -0.3% at candle 6
    flat_candles=12,           # TIME_FLAT check
    flat_threshold=0.003,      # if PnL < +0.3% at candle 12+
)

EB = dict(
    score_min=4,
    z_min=2.0,
    range_candles=24,          # 2h = 24x5m candles
    range_max=0.015,           # (max_high - min_low) / min_low < 1.5%
    oi_rise_min=0.005,         # OI change > 0.5%
    funding_lo=-0.00005,       # -0.005%
    funding_hi=0.00005,        # +0.005%
    sl_lookback=6,             # min low of last 6 candles (incl. current)
    sl_offset=0.004,           # 0.4% below that low
    tp_pct=0.015,              # 1.5% TP
    trail_pct=0.006,           # trail to BE after +0.6%
    dead_candles=None,
    dead_threshold=None,
    flat_candles=8,            # TIME_FLAT check
    flat_threshold=0.004,      # if PnL < +0.4% at candle 8+
)

COND_NAMES_EA  = ["price_rise", "vol_z", "upper_wick", "oi_stable", "funding_hi", "red_candle"]
COND_NAMES_EB  = ["tight_range", "vol_z_prog", "breakout", "oi_rising", "fund_neutral", "cvd_pos"]
SHORT_LBLS_EA  = {"price_rise":"P_RISE","vol_z":"VOL_Z","upper_wick":"U_WICK",
                  "oi_stable":"OI_STB","funding_hi":"FUND_H","red_candle":"RED_C"}
SHORT_LBLS_EB  = {"tight_range":"RANGE","vol_z_prog":"VOL_ZP","breakout":"BRKOUT",
                  "oi_rising":"OI_UP","fund_neutral":"FUND_N","cvd_pos":"CVD_+"}
EXITS_ORDER    = ["TP", "SL", "TIME_DEAD", "TIME_FLAT"]


# ==============================================================================
# DATA
# ==============================================================================

def load_data() -> Dict:
    if not CACHE.exists():
        log.error(f"Cache not found: {CACHE}  -- run validate_dual_engine.py first")
        sys.exit(1)
    log.info(f"Loading {CACHE.name}")
    return json.loads(CACHE.read_text())


def align(raw: Dict) -> List[Dict]:
    spot_by_ts = {c["ts"]: c for c in raw["spot"]}
    perp_by_ts = {c["ts"]: c for c in raw["perp"]}
    oi_sorted  = sorted(raw["oi"],      key=lambda x: int(x["timestamp"]))
    fu_sorted  = sorted(raw["funding"], key=lambda x: int(x["fundingTime"]))
    oi_idx, oi_val, oi_prev = 0, 0.0, 0.0
    fu_idx, fu_val           = 0, 0.0
    out = []
    for ts in sorted(perp_by_ts):
        if ts not in spot_by_ts:
            continue
        while oi_idx < len(oi_sorted) and int(oi_sorted[oi_idx]["timestamp"]) <= ts:
            oi_prev = oi_val
            oi_val  = float(oi_sorted[oi_idx].get("sumOpenInterestValue", 0))
            oi_idx += 1
        oi_chg = (oi_val - oi_prev) / oi_prev if oi_prev > 0 else 0.0
        while fu_idx < len(fu_sorted) and int(fu_sorted[fu_idx]["fundingTime"]) <= ts:
            fu_val = float(fu_sorted[fu_idx]["fundingRate"])
            fu_idx += 1
        s, p = spot_by_ts[ts], perp_by_ts[ts]
        out.append({
            "ts": ts, "dt": datetime.utcfromtimestamp(ts / 1000),
            "spot_open": s["open"], "spot_high": s["high"],
            "spot_low":  s["low"],  "spot_close": s["close"], "spot_vol": s["volume"],
            "perp_open": p["open"], "perp_high": p["high"],
            "perp_low":  p["low"],  "perp_close": p["close"], "perp_vol": p["volume"],
            "oi_usd": oi_val, "oi_chg": oi_chg, "funding": fu_val,
        })
    return out


# ==============================================================================
# HELPERS
# ==============================================================================

def _zscore(history: list, value: float) -> float:
    n = len(history)
    if n < 5:
        return 0.0
    mu  = sum(history) / n
    var = sum((v - mu) ** 2 for v in history) / (n - 1)
    sig = math.sqrt(var) if var > 0 else 1e-9
    return (value - mu) / sig


# ==============================================================================
# SCORE FUNCTIONS
# ==============================================================================

def score_ea(c: Dict, candles: List[Dict], i: int, z_hist: list,
             strict_oi: bool = True) -> Tuple[int, Dict[str, bool]]:
    """
    Engine A signal score (0-6) for SHORT exhaustion reversal.

    z_hist : list of spot volumes BEFORE current candle (no look-ahead).

    strict_oi=True  -- condition 4 requires c["oi_usd"] > 0  AND  oi_chg < 0.005.
                       Candles with no OI data (oi_usd==0) fail condition 4.
    strict_oi=False -- condition 4 passes whenever oi_chg < 0.005.
                       No-data candles have oi_chg=0.0, which passes (0.0 < 0.005).
                       Comparing strict vs lenient exposes whether edge comes from
                       real OI confirmation or from the no-data bypass.
    """
    conds: Dict[str, bool] = {k: False for k in COND_NAMES_EA}

    # 1. Price rose >1.5% in last 30min (6 candles before current, not including it)
    if i >= EA["price_rise_candles"]:
        ref = candles[i - EA["price_rise_candles"]]["spot_close"]
        conds["price_rise"] = (c["spot_close"] - ref) / ref > EA["price_rise_min"]

    # 2. Volume Z-score > 2.0  (z_hist is history BEFORE this candle -- no look-ahead)
    conds["vol_z"] = _zscore(z_hist, c["spot_vol"]) > EA["z_min"]

    # 3. Upper wick > 50% of candle range
    candle_range = c["spot_high"] - c["spot_low"]
    if candle_range > 0:
        upper_wick = c["spot_high"] - max(c["spot_open"], c["spot_close"])
        conds["upper_wick"] = upper_wick / candle_range > EA["wick_ratio_min"]
    # doji (range==0): condition stays False

    # 4. OI stable or falling
    if strict_oi:
        conds["oi_stable"] = c["oi_usd"] > 0 and c["oi_chg"] < EA["oi_stable_max"]
    else:
        conds["oi_stable"] = c["oi_chg"] < EA["oi_stable_max"]

    # 5. Funding > 0.01% (longs over-leveraged, squeeze risk)
    conds["funding_hi"] = c["funding"] > EA["funding_min"]

    # 6. Red candle (close < open -- bearish exhaustion)
    conds["red_candle"] = c["spot_close"] < c["spot_open"]

    return sum(conds.values()), conds


def score_eb(c: Dict, candles: List[Dict], i: int, z_hist: list) -> Tuple[int, Dict[str, bool]]:
    """
    Engine B signal score (0-6) for LONG breakout with accumulation.

    z_hist : list of perp volumes BEFORE current candle (no look-ahead).
    """
    conds: Dict[str, bool] = {k: False for k in COND_NAMES_EB}

    # 1. Tight range in last 2h (24 candles, NOT including current)
    if i >= EB["range_candles"]:
        window  = candles[i - EB["range_candles"]:i]
        h_max   = max(x["spot_high"] for x in window)
        l_min   = min(x["spot_low"]  for x in window)
        if l_min > 0:
            conds["tight_range"] = (h_max - l_min) / l_min < EB["range_max"]

    # 2. Z-score > 2.0 AND volume increasing over last 3 candles
    z = _zscore(z_hist, c["perp_vol"])
    progressive = (
        i >= 2 and
        candles[i - 2]["perp_vol"] < candles[i - 1]["perp_vol"] < c["perp_vol"]
    )
    conds["vol_z_prog"] = z > EB["z_min"] and progressive

    # 3. Green candle + close breaks above high of last 6 candles (excl. current)
    if i >= 6:
        prev_6_highs = [candles[i - k]["spot_high"] for k in range(1, 7)]
        conds["breakout"] = (
            c["spot_close"] > c["spot_open"] and
            c["spot_close"] > max(prev_6_highs)
        )

    # 4. OI rising > 0.5%  (oi_chg=0.0 from no-data naturally fails -- strict by design)
    conds["oi_rising"] = c["oi_chg"] > EB["oi_rise_min"]

    # 5. Funding neutral (-0.005% to +0.005%)
    conds["fund_neutral"] = EB["funding_lo"] <= c["funding"] <= EB["funding_hi"]

    # 6. CVD positive over last 12 candles (excl. current)
    #    Approximation: green candle volume counts positive, red counts negative.
    #    Real CVD requires tick-level bid/ask data not available here.
    if i >= 12:
        cvd = sum(
            x["spot_vol"] * (1 if x["spot_close"] >= x["spot_open"] else -1)
            for x in candles[i - 12:i]
        )
        conds["cvd_pos"] = cvd > 0

    return sum(conds.values()), conds


# ==============================================================================
# POSITION
# ==============================================================================

class Position:
    def __init__(self, engine: str, side: str, entry: float,
                 sl_px: float, tp_pct: float, trail_pct: float,
                 entry_ts: int, idx: int, score: int, conds: Dict):
        self.engine    = engine
        self.side      = side
        self.entry     = entry
        self.sl_px     = sl_px        # absolute price
        self.trail_pct = trail_pct
        self.entry_ts  = entry_ts
        self.idx       = idx
        self.be        = False
        self.score     = score
        self.conds     = conds
        self.tp_px     = entry * (1 - tp_pct) if side == "short" else entry * (1 + tp_pct)

    def check_sl_tp(self, hi: float, lo: float) -> Optional[Tuple[str, float]]:
        if self.side == "short":
            gain = (self.entry - lo) / self.entry
            if not self.be and gain >= self.trail_pct:
                self.be    = True
                self.sl_px = self.entry
            if hi >= self.sl_px: return "SL", self.sl_px
            if lo <= self.tp_px: return "TP", self.tp_px
        else:
            gain = (hi - self.entry) / self.entry
            if not self.be and gain >= self.trail_pct:
                self.be    = True
                self.sl_px = self.entry
            if lo <= self.sl_px: return "SL", self.sl_px
            if hi >= self.tp_px: return "TP", self.tp_px
        return None

    def unrealized(self, price: float) -> float:
        return (self.entry - price) / self.entry if self.side == "short" \
               else (price - self.entry) / self.entry

    def pnl(self, exit_px: float) -> float:
        return (self.entry - exit_px) / self.entry if self.side == "short" \
               else (exit_px - self.entry) / self.entry


# ==============================================================================
# ENGINE RUNNER
# ==============================================================================

def run_engine(candles: List[Dict], eng_id: str,
               score_fn: Callable, side: str, cfg: Dict) -> List[Dict]:
    trades: List[Dict]       = []
    vol_hist: deque          = deque(maxlen=VOL_LOOKBACK)
    pos: Optional[Position]  = None

    dead_c = cfg.get("dead_candles")
    dead_t = cfg.get("dead_threshold")
    flat_c = cfg["flat_candles"]
    flat_t = cfg["flat_threshold"]

    for i, c in enumerate(candles):

        # -- Time exit checks (at candle open, before intra-candle SL/TP) -----
        if pos is not None:
            hold_c = i - pos.idx
            unreal  = pos.unrealized(c["perp_open"])
            t_reason: Optional[str] = None

            if dead_c is not None and hold_c == dead_c and unreal < dead_t:
                t_reason = "TIME_DEAD"
            elif hold_c >= flat_c and unreal < flat_t:
                t_reason = "TIME_FLAT"

            if t_reason is not None:
                raw = pos.pnl(c["perp_open"])
                trades.append(_record(eng_id, pos, c, c["perp_open"],
                                      t_reason, raw, raw - RT_COST, hold_c))
                pos = None

        # -- Intra-candle SL / TP ---------------------------------------------
        if pos is not None:
            result = pos.check_sl_tp(c["perp_high"], c["perp_low"])
            if result is not None:
                reason, exit_px = result
                raw  = pos.pnl(exit_px)
                hold = i - pos.idx
                trades.append(_record(eng_id, pos, c, exit_px,
                                      reason, raw, raw - RT_COST, hold))
                pos = None

        # -- Signal detection -------------------------------------------------
        z_hist = list(vol_hist)    # snapshot BEFORE appending current candle
        vol_hist.append(c["spot_vol"] if side == "short" else c["perp_vol"])

        if pos is not None:               continue
        if i + 1 >= len(candles):         continue
        if len(vol_hist) < VOL_LOOKBACK:  continue

        score, conds = score_fn(c, candles, i, z_hist)
        if score < cfg["score_min"]:      continue

        # -- Compute absolute SL and gap guard --------------------------------
        nxt = candles[i + 1]
        if side == "short":
            sl_px = c["spot_high"] * (1 + cfg["sl_offset"])
            if nxt["perp_open"] >= sl_px:
                continue   # gap up: would open at or above SL
        else:
            low_window = [candles[i - k]["spot_low"] for k in range(0, cfg["sl_lookback"])]
            sl_px = min(low_window) * (1 - cfg["sl_offset"])
            if nxt["perp_open"] <= sl_px:
                continue   # gap down: would open at or below SL

        pos = Position(eng_id, side, nxt["perp_open"], sl_px,
                       cfg["tp_pct"], cfg["trail_pct"],
                       nxt["ts"], i + 1, score, conds)

    return trades


def _record(eng_id, pos, c, exit_px, reason, raw, net, hold_c):
    return {
        "engine":     eng_id,
        "side":       pos.side,
        "entry_ts":   pos.entry_ts,
        "exit_ts":    c["ts"],
        "entry_dt":   datetime.utcfromtimestamp(pos.entry_ts / 1000).strftime("%Y-%m-%d %H:%M"),
        "exit_dt":    datetime.utcfromtimestamp(c["ts"]       / 1000).strftime("%Y-%m-%d %H:%M"),
        "entry_hour": datetime.utcfromtimestamp(pos.entry_ts  / 1000).hour,
        "entry_px":   pos.entry,
        "exit_px":    exit_px,
        "reason":     reason,
        "raw_pct":    raw,
        "net_pct":    net,
        "hold_c":     hold_c,
        "hold_min":   hold_c * 5,
        "be":         pos.be,
        "score":      pos.score,
        "conds":      pos.conds,
    }


# ==============================================================================
# METRICS
# ==============================================================================

def compute_metrics(trades: List[Dict], label: str, total_days: float) -> Dict:
    if not trades:
        return {"label": label, "n": 0}

    wins   = [t for t in trades if t["net_pct"] >  0]
    losses = [t for t in trades if t["net_pct"] <= 0]
    n      = len(trades)
    wr     = len(wins) / n
    gw     = sum(t["net_pct"] for t in wins)
    gl     = abs(sum(t["net_pct"] for t in losses))
    pf     = gw / gl if gl > 0 else float("inf")
    avg_w  = gw / len(wins)   if wins   else 0.0
    avg_l  = gl / len(losses) if losses else 0.0
    r_unit = avg_l if avg_l > 0 else 0.006
    exp_r  = (wr * avg_w - (1 - wr) * avg_l) / r_unit

    eq, pk, dd_max, rets = 1.0, 1.0, 0.0, []
    for t in sorted(trades, key=lambda x: x["entry_ts"]):
        eq *= (1 + t["net_pct"])
        rets.append(t["net_pct"])
        pk  = max(pk, eq)
        dd_max = max(dd_max, (pk - eq) / pk)

    sharpe = sortino = 0.0
    if len(rets) > 1:
        mu  = sum(rets) / len(rets)
        sig = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        tpy = (n / max(total_days, 1)) * 252
        if sig > 0:
            sharpe = (mu / sig) * math.sqrt(tpy)
        down = [r for r in rets if r < 0]
        if down:
            dsig = math.sqrt(sum(r ** 2 for r in down) / len(down))
            if dsig > 0:
                sortino = (mu / dsig) * math.sqrt(tpy)

    holds = [t["hold_min"] for t in trades]
    avg_h = sum(holds) / n
    hold_dist: Dict[str, int] = {"<5m": 0, "5-15m": 0, "15-60m": 0, "1-4h": 0, ">4h": 0}
    for h in holds:
        if   h <   5: hold_dist["<5m"]    += 1
        elif h <  15: hold_dist["5-15m"]  += 1
        elif h <  60: hold_dist["15-60m"] += 1
        elif h < 240: hold_dist["1-4h"]   += 1
        else:         hold_dist[">4h"]    += 1

    mcl = cur = 0
    for t in sorted(trades, key=lambda x: x["entry_ts"]):
        cur = cur + 1 if t["net_pct"] <= 0 else 0
        mcl = max(mcl, cur)

    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    by_pnl = sorted(trades, key=lambda x: x["net_pct"])
    return {
        "label": label, "n": n,
        "wr": wr, "pf": pf, "exp_r": exp_r,
        "avg_w": avg_w, "avg_l": avg_l,
        "breakeven_wr": avg_l / (avg_w + avg_l) if (avg_w + avg_l) > 0 else 0,
        "total_ret_pct": (eq - 1) * 100,
        "max_dd_pct": dd_max * 100,
        "sharpe": sharpe, "sortino": sortino,
        "avg_hold_min": avg_h, "hold_dist": hold_dist, "mcl": mcl,
        "be_rate": sum(1 for t in trades if t["be"]) / n,
        "reasons": reasons,
        "top5w": by_pnl[-5:][::-1],
        "top5l": by_pnl[:5],
    }


# ==============================================================================
# DIAGNOSTICS
# ==============================================================================

def diag_score_wr(trades: List[Dict]) -> Dict:
    result = {}
    for s in range(4, 7):
        grp = [t for t in trades if t.get("score") == s]
        wins = sum(1 for t in grp if t["net_pct"] > 0)
        result[s] = {"n": len(grp), "wr": wins / len(grp) * 100 if grp else 0.0}
    return result


def diag_hour_wr(trades: List[Dict]) -> Dict:
    bkt = {h: [0, 0] for h in range(24)}
    for t in trades:
        h = t.get("entry_hour", 0)
        bkt[h][0] += 1
        if t["net_pct"] > 0:
            bkt[h][1] += 1
    return {h: {"n": v[0], "wr": v[1] / v[0] * 100 if v[0] > 0 else 0.0}
            for h, v in bkt.items()}


def diag_cooc(trades: List[Dict], cond_names: List[str]) -> List[List[float]]:
    """
    6x6 matrix: pct of trades where both condition[a] and condition[b] are True.
    Diagonal = activation rate of each condition.
    """
    nc = len(cond_names)
    counts = [[0] * nc for _ in range(nc)]
    n = len(trades)
    if n == 0:
        return counts
    for t in trades:
        flags = [t["conds"].get(k, False) for k in cond_names]
        for a in range(nc):
            if flags[a]:
                for b in range(nc):
                    if flags[b]:
                        counts[a][b] += 1
    return [[counts[a][b] / n * 100 for b in range(nc)] for a in range(nc)]


# ==============================================================================
# REPORT
# ==============================================================================

W = 72
def hr(c="-"): print(c * W)
def blank():   print()


def _print_block(m: Dict, cond_names: List[str], short_lbl: Dict,
                 diag_s: Dict, diag_h: Dict, cooc: List[List[float]],
                 total_days: float) -> None:
    hr()
    print(f"  {m['label']}")
    hr()

    if m["n"] == 0:
        print("  [!] ZERO TRADES -- thresholds too strict or insufficient OI data")
        blank()
        return

    n   = m["n"]
    pf  = m["pf"]
    pf_flag = "[OK]" if pf >= 1.5 else ("[X] PF<1.3 -- STOP" if pf < 1.3 else "[~]")

    print(f"  Trades    : {n:>5}  (Freq: {n/total_days:.1f}/day)")
    print(f"  Win Rate  : {m['wr']*100:>6.1f}%  (breakeven: {m['breakeven_wr']*100:.0f}%)")
    print(f"  Prof Fact : {pf:>6.2f}  {pf_flag}")
    print(f"  Expectancy: {m['exp_r']:>+6.3f}R"
          f"  (avg W {m['avg_w']*100:+.3f}% | avg L {-m['avg_l']*100:.3f}%)")
    print(f"  Total Ret : {m['total_ret_pct']:>+7.2f}%")
    print(f"  Max DD    : {m['max_dd_pct']:>7.2f}%")
    print(f"  Sharpe    : {m['sharpe']:>7.2f}   Sortino: {m['sortino']:.2f}")
    print(f"  Avg Hold  : {m['avg_hold_min']:>6.1f} min  |  Max Consec L: {m['mcl']}")
    print(f"  BE Rate   : {m['be_rate']*100:>6.1f}%")

    blank()
    print(f"  Hold Distribution (n={n}):")
    max_c = max(m["hold_dist"].values()) if m["hold_dist"] else 1
    for bucket, cnt in m["hold_dist"].items():
        bar = "#" * round(cnt / max(max_c, 1) * 24)
        print(f"    {bucket:>8}: {cnt:>4}  {bar}")

    exits_str = "  |  ".join(
        f"{k}: {m['reasons'].get(k,0)}"
        for k in EXITS_ORDER if m["reasons"].get(k, 0) > 0
    )
    print(f"\n  Exits: {exits_str}")

    blank()
    print("  Top 5 Winners:")
    for t in m["top5w"]:
        be = "BE" if t["be"] else "  "
        print(f"    {t['entry_dt']}  {t['side'].upper():<5}"
              f"  +{t['net_pct']*100:.3f}%  {t['hold_min']:>4}min"
              f"  [{t['reason']}] {be}  score={t['score']}")
    blank()
    print("  Top 5 Losers:")
    for t in m["top5l"]:
        be = "BE" if t["be"] else "  "
        print(f"    {t['entry_dt']}  {t['side'].upper():<5}"
              f"  {t['net_pct']*100:.3f}%  {t['hold_min']:>4}min"
              f"  [{t['reason']}] {be}  score={t['score']}")

    # -- Diagnostic 1: WR by score ------------------------------------------
    blank()
    print("  WR by Score:")
    for s, v in sorted(diag_s.items()):
        bar = "#" * round(v["wr"] / 100 * 20) if v["n"] > 0 else ""
        tag = "[!] low-N" if v["n"] < 10 else ""
        print(f"    Score {s}: {v['wr']:>5.1f}%  {bar:<20}  (n={v['n']}) {tag}")

    # -- Diagnostic 2: WR by UTC hour (>= 3 trades) -------------------------
    blank()
    print("  WR by UTC Hour (>= 3 trades, sorted by WR desc):")
    active = [(h, v) for h, v in diag_h.items() if v["n"] >= 3]
    if active:
        for h, v in sorted(active, key=lambda x: -x[1]["wr"]):
            bar = "#" * round(v["wr"] / 100 * 20)
            print(f"    {h:02d}h: {v['wr']:>5.1f}%  {bar:<20}  (n={v['n']})")
    else:
        print("    (no hours with >= 3 trades)")

    # -- Diagnostic 3: Condition co-occurrence matrix -----------------------
    blank()
    labels = [short_lbl.get(k, k[:6]) for k in cond_names]
    col_w  = 7
    print("  Condition Co-occurrence (% of trades -- diagonal = activation rate):")
    header = "          " + "".join(f"{lb:>{col_w}}" for lb in labels)
    print(header)
    for a, row_lb in enumerate(labels):
        row = f"  {row_lb:<8}" + "".join(f"{cooc[a][b]:>{col_w}.0f}" for b in range(len(labels)))
        print(row)

    if n < 30:
        blank()
        print(f"  *** [!] LOW SAMPLE ({n} trades -- need 30+ for significance)")
    blank()


def print_report(blocks: list, info: Dict) -> None:
    blank()
    hr("=")
    print("  DUAL ENGINE VALIDATION v2 -- BTC/USDT 5m  (confluence signals)")
    print(f"  Period  : {info['period']}")
    print(f"  Candles : {info['n_candles']:,}  |  OI coverage: {info['oi_pct']:.0f}%")
    print(f"  Costs   : {RT_COST*100:.3f}% RT")
    hr("=")

    for (m, cond_names, short_lbl, ds, dh, cooc) in blocks:
        blank()
        _print_block(m, cond_names, short_lbl, ds, dh, cooc, info["days"])

    blank()
    hr("=")
    print("  VERDICT")
    hr("-")
    for (m, *_) in blocks:
        if m["n"] == 0:
            print(f"  {m['label'][:28]}: [X] No trades")
            continue
        flags = []
        if   m["pf"] >= 1.5 and m["exp_r"] > 0.05: flags.append("[OK] Positive edge")
        elif m["pf"] < 1.3:                          flags.append("[X] PF<1.3 -- STOP")
        else:                                         flags.append("[~] Marginal")
        if m["n"] < 30:
            flags.append(f"[!] Low sample ({m['n']})")
        if   m["avg_hold_min"] <= 30:  flags.append("[OK] Scalping range")
        elif m["avg_hold_min"] <= 90:  flags.append("[~] Borderline")
        else:                           flags.append(f"[X] Swing ({m['avg_hold_min']:.0f}min)")
        print(f"  {m['label'][:28]}: {'  '.join(flags)}")
    hr("=")
    blank()


# ==============================================================================
# MAIN
# ==============================================================================

def main() -> None:
    log.info("=== Dual Engine Validation v2 ===")

    raw     = load_data()
    candles = align(raw)

    if len(candles) < VOL_LOOKBACK + 30:
        log.error(f"Only {len(candles)} aligned candles -- aborting.")
        sys.exit(1)

    n_oi       = sum(1 for c in candles if c["oi_usd"] > 0)
    total_days = max((candles[-1]["ts"] - candles[0]["ts"]) / 86_400_000, 1)

    info = {
        "period":    f"{candles[0]['dt'].date()} -> {candles[-1]['dt'].date()}",
        "n_candles": len(candles),
        "oi_pct":    n_oi / len(candles) * 100,
        "days":      total_days,
    }
    log.info(f"Aligned {len(candles):,} candles | period: {info['period']}")
    log.info(f"OI coverage: {info['oi_pct']:.0f}%")

    log.info("Running Engine A STRICT (OI condition requires real data)...")
    ta_s = run_engine(candles, "A-STRICT",
                      lambda c, cs, i, zh: score_ea(c, cs, i, zh, strict_oi=True),
                      "short", EA)
    log.info(f"  -> {len(ta_s)} trades")

    log.info("Running Engine A LENIENT (OI condition passes when no data)...")
    ta_l = run_engine(candles, "A-LENIENT",
                      lambda c, cs, i, zh: score_ea(c, cs, i, zh, strict_oi=False),
                      "short", EA)
    log.info(f"  -> {len(ta_l)} trades")

    log.info("Running Engine B (Perp breakout + OI, strict by design)...")
    tb = run_engine(candles, "B",
                    lambda c, cs, i, zh: score_eb(c, cs, i, zh),
                    "long", EB)
    log.info(f"  -> {len(tb)} trades")

    def build_block(trades, label, cond_names, short_lbl):
        m   = compute_metrics(trades, label, total_days)
        ds  = diag_score_wr(trades)
        dh  = diag_hour_wr(trades)
        coo = diag_cooc(trades, cond_names)
        return (m, cond_names, short_lbl, ds, dh, coo)

    blocks = [
        build_block(ta_s, "ENGINE A STRICT  -- Short Exhaustion  (real OI required)",
                    COND_NAMES_EA, SHORT_LBLS_EA),
        build_block(ta_l, "ENGINE A LENIENT -- Short Exhaustion  (no-data OI passes)",
                    COND_NAMES_EA, SHORT_LBLS_EA),
        build_block(tb,   "ENGINE B         -- Long Breakout + OI (strict by design)",
                    COND_NAMES_EB, SHORT_LBLS_EB),
    ]

    print_report(blocks, info)

    # Save JSON results
    out = DATA_DIR / "dual_engine_v2_results.json"
    def safe(m):
        return {k: v for k, v in m.items() if k not in ("top5w", "top5l", "trades")}
    result = {
        "info": {**info, "period": str(info["period"])},
        "engine_a_strict":  safe(blocks[0][0]),
        "engine_a_lenient": safe(blocks[1][0]),
        "engine_b":         safe(blocks[2][0]),
        "config": {"EA": EA, "EB": EB, "RT_COST_PCT": RT_COST * 100},
    }
    out.write_text(json.dumps(result, indent=2, default=str))
    log.info(f"Results -> {out}")


if __name__ == "__main__":
    main()
