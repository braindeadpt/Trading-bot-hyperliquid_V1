#!/usr/bin/env python3
"""
tests/validate_dual_engine.py
Dual Engine Validation -- Completely isolated from live bot (no src/ imports).

ENGINE A -- Spot Momentum (no OI requirement)
  Trigger : spot volume Z-score > 3.0  |  candle dir > 0.3%
  Entry   : open of next 5m candle (no look-ahead)
  Risk    : SL 1.0%  |  TP 2.5%  |  trail -> breakeven after +1.0%

ENGINE B -- Perp Momentum + OI Rising
  Trigger : perp volume Z-score > 2.5  |  OI change > 0.5% (requires real OI data)  |  candle dir > 0.2%
  Entry   : open of next 5m candle
  Risk    : SL 1.2%  |  TP 3.0%  |  trail -> breakeven after +1.2%

Costs (Hyperliquid): 0.045% taker + 0.015% maker + 0.02% slip each side = 0.11% RT
OI data: 1h granularity from Binance (max ~30d retention), forward-filled to 5m.
"""
import json, math, sys, time, logging, requests
from pathlib import Path
from datetime import datetime, timedelta
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
                    stream=sys.stdout)
log = logging.getLogger("dual_engine")

# -- Paths ---------------------------------------------------------------------
HERE     = Path(__file__).parent
DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)

# -- Binance endpoints ---------------------------------------------------------
SPOT_URL  = "https://api.binance.com/api/v3/klines"
PERP_URL  = "https://fapi.binance.com/fapi/v1/klines"
OI_URL    = "https://fapi.binance.com/futures/data/openInterestHist"
FUND_URL  = "https://fapi.binance.com/fapi/v1/fundingRate"

# -- Round-trip cost -----------------------------------------------------------
TAKER   = 0.00045   # 0.045%
MAKER   = 0.00015   # 0.015%
SLIP    = 0.0002    # 0.02% each side
RT_COST = TAKER + SLIP + MAKER + SLIP  # 0.11%

# -- Engine configs ------------------------------------------------------------
# Engine A: pure spot momentum, no OI filter (OI=0 candles would bypass any filter anyway)
EA = dict(sl=0.010, tp=0.025, trail=0.010, z_min=3.0,
          dir_min=0.003, use_spot_vol=True)

# Engine B: perp volume spike + confirmed OI rise (skips candles with no OI data)
EB = dict(sl=0.012, tp=0.030, trail=0.012, z_min=2.5,
          oi_min=0.005,                   # OI change > 0.5%
          dir_min=0.002, use_spot_vol=False)

VOL_LOOKBACK = 20   # Z-score baseline candles


# ==============================================================================
# DATA DOWNLOAD & ALIGNMENT
# ==============================================================================

def _fetch_klines(url: str, symbol: str, start_ms: int, end_ms: int) -> list:
    out, cur = [], start_ms
    while cur < end_ms:
        try:
            r = requests.get(url, params={"symbol": symbol, "interval": "5m",
                                           "startTime": cur, "limit": 1500}, timeout=30)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            out.extend(batch)
            cur = int(batch[-1][0]) + 1
            time.sleep(0.05)
        except Exception as e:
            log.warning(f"klines fetch error: {e}")
            break
    return out


def _parse_klines(raw: list) -> List[Dict]:
    return [{"ts": int(k[0]), "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
            for k in raw]


def _fetch_oi_1h(symbol: str, start_ms: int, end_ms: int) -> list:
    """
    Binance openInterestHist retains only ~30 days.
    If startTime is rejected (400), fetch once without startTime (latest 500 records)
    and stop — do not paginate further to avoid an infinite loop.
    """
    out, cur = [], start_ms
    use_start = True
    while cur < end_ms:
        params = {"symbol": symbol, "period": "1h", "limit": 500}
        if use_start:
            params["startTime"] = cur
        try:
            r = requests.get(OI_URL, params=params, timeout=30)
            if r.status_code == 400 and use_start:
                log.warning("OI startTime rejected (data only retained ~30d). Fetching latest 500 records.")
                # One-shot: fetch without startTime, then stop — no pagination possible
                r2 = requests.get(OI_URL,
                                  params={"symbol": symbol, "period": "1h", "limit": 500},
                                  timeout=30)
                if r2.status_code == 200:
                    out.extend(r2.json())
                else:
                    log.warning(f"OI fallback {r2.status_code}: {r2.text[:80]}")
                break   # cannot paginate without startTime
            if r.status_code != 200:
                log.warning(f"OI API {r.status_code}: {r.text[:120]}")
                break
            batch = r.json()
            if not batch:
                break
            out.extend(batch)
            last_ts = int(batch[-1]["timestamp"])
            if last_ts >= end_ms:
                break
            cur = last_ts + 1
            time.sleep(0.1)
        except Exception as e:
            log.warning(f"OI fetch error: {e}")
            break
    return out


def _fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list:
    out, cur = [], start_ms
    while cur < end_ms:
        try:
            r = requests.get(FUND_URL, params={"symbol": symbol,
                                                "startTime": cur, "limit": 1000}, timeout=30)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            out.extend(batch)
            cur = int(batch[-1]["fundingTime"]) + 1
            time.sleep(0.05)
        except Exception as e:
            log.warning(f"Funding fetch error: {e}")
            break
    return out


def download(symbol: str = "BTCUSDT", days: int = 90) -> Dict:
    cache = DATA_DIR / f"{symbol}_{days}d.json"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 86400:
        log.info(f"Cache hit ({cache.name}, <24h old)")
        return json.loads(cache.read_text())

    end_ms   = int(datetime.now().timestamp() * 1000)
    start_ms = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    # OI history capped at 30d by Binance
    oi_start_ms = max(start_ms, int((datetime.now() - timedelta(days=30)).timestamp() * 1000))

    log.info(f"Downloading {days}d of {symbol}...")
    log.info("  Binance Spot 5m candles...")
    spot = _parse_klines(_fetch_klines(SPOT_URL, symbol, start_ms, end_ms))
    log.info(f"  -> {len(spot):,} spot candles")

    log.info("  Binance Perp 5m candles...")
    perp = _parse_klines(_fetch_klines(PERP_URL, symbol, start_ms, end_ms))
    log.info(f"  -> {len(perp):,} perp candles")

    log.info("  OI history (1h, Binance retains ~30d)...")
    oi = _fetch_oi_1h(symbol, oi_start_ms, end_ms)
    log.info(f"  -> {len(oi):,} OI records")

    log.info("  Funding rate history...")
    funding = _fetch_funding(symbol, start_ms, end_ms)
    log.info(f"  -> {len(funding):,} funding records")

    data = {"spot": spot, "perp": perp, "oi": oi, "funding": funding,
            "symbol": symbol, "days": days, "downloaded": datetime.now().isoformat()}
    cache.write_text(json.dumps(data))
    log.info(f"Saved to {cache}")
    return data


def align(raw: Dict) -> List[Dict]:
    """
    Merge spot + perp + OI(1h->5m forward-fill) + funding by 5m timestamp.
    OI change = (oi_now - oi_prev) / oi_prev between consecutive 1h records,
    assigned to every 5m candle in that hour window.
    """
    spot_by_ts = {c["ts"]: c for c in raw["spot"]}
    perp_by_ts = {c["ts"]: c for c in raw["perp"]}
    oi_sorted  = sorted(raw["oi"],     key=lambda x: int(x["timestamp"]))
    fu_sorted  = sorted(raw["funding"], key=lambda x: int(x["fundingTime"]))

    oi_idx, oi_val, oi_prev = 0, 0.0, 0.0
    fu_idx, fu_val           = 0, 0.0

    out = []
    for ts in sorted(perp_by_ts):
        if ts not in spot_by_ts:
            continue

        # advance OI pointer (1h records, forward-fill)
        while oi_idx < len(oi_sorted) and int(oi_sorted[oi_idx]["timestamp"]) <= ts:
            oi_prev = oi_val
            oi_val  = float(oi_sorted[oi_idx].get("sumOpenInterestValue", 0))
            oi_idx += 1
        oi_chg = (oi_val - oi_prev) / oi_prev if oi_prev > 0 else 0.0

        # advance funding pointer (8h records, forward-fill)
        while fu_idx < len(fu_sorted) and int(fu_sorted[fu_idx]["fundingTime"]) <= ts:
            fu_val = float(fu_sorted[fu_idx]["fundingRate"])
            fu_idx += 1

        s, p = spot_by_ts[ts], perp_by_ts[ts]
        out.append({
            "ts":         ts,
            "dt":         datetime.fromtimestamp(ts / 1000),
            "spot_open":  s["open"],  "spot_high":  s["high"],
            "spot_low":   s["low"],   "spot_close": s["close"],
            "spot_vol":   s["volume"],
            "perp_open":  p["open"],  "perp_high":  p["high"],
            "perp_low":   p["low"],   "perp_close": p["close"],
            "perp_vol":   p["volume"],
            "oi_usd":     oi_val,
            "oi_chg":     oi_chg,     # 1h change, same for all 5m in that hour
            "funding":    fu_val,
        })
    return out


# ==============================================================================
# SIGNAL DETECTION + POSITION MANAGEMENT
# ==============================================================================

def _zscore(history: list, value: float) -> float:
    """Z-score of value against list of previous values (sample std, ddof=1)."""
    n = len(history)
    if n < 5:
        return 0.0
    mu  = sum(history) / n
    var = sum((v - mu) ** 2 for v in history) / (n - 1)
    sig = math.sqrt(var) if var > 0 else 1e-9
    return (value - mu) / sig


class Position:
    """Tracks one open trade with SL/TP/trail-to-BE logic."""

    def __init__(self, engine: str, side: str, entry: float,
                 sl_pct: float, tp_pct: float, trail_pct: float,
                 entry_ts: int, idx: int):
        self.engine    = engine
        self.side      = side
        self.entry     = entry
        self.sl_pct    = sl_pct
        self.trail_pct = trail_pct
        self.entry_ts  = entry_ts
        self.idx       = idx
        self.be        = False   # breakeven stop activated

        if side == "long":
            self.sl_px = entry * (1 - sl_pct)
            self.tp_px = entry * (1 + tp_pct)
        else:
            self.sl_px = entry * (1 + sl_pct)
            self.tp_px = entry * (1 - tp_pct)

    def update(self, hi: float, lo: float) -> Optional[Tuple[str, float]]:
        """
        Check if SL or TP was hit during this candle.
        If both hit: assume SL wins (conservative).
        Trail -> BE: once gain >= trail_pct (using high for longs, low for shorts).
        Returns (reason, exit_price) or None.
        """
        if self.side == "long":
            gain = (hi - self.entry) / self.entry
            if not self.be and gain >= self.trail_pct:
                self.be    = True
                self.sl_px = self.entry   # move SL to breakeven
            if lo <= self.sl_px:
                return "SL", self.sl_px
            if hi >= self.tp_px:
                return "TP", self.tp_px
        else:
            gain = (self.entry - lo) / self.entry
            if not self.be and gain >= self.trail_pct:
                self.be    = True
                self.sl_px = self.entry
            if hi >= self.sl_px:
                return "SL", self.sl_px
            if lo <= self.tp_px:
                return "TP", self.tp_px
        return None

    def pnl(self, exit_px: float) -> float:
        if self.side == "long":
            return (exit_px - self.entry) / self.entry
        return (self.entry - exit_px) / self.entry


def run_engine(candles: List[Dict], eng_id: str, cfg: Dict,
               oi_filter: Callable[[float], bool]) -> List[Dict]:
    """
    Run one engine over full candle list. Returns list of completed trades.

    Signal logic:
      - Z-score of current vol vs. previous VOL_LOOKBACK candles > z_min
      - oi_filter(oi_chg) passes
      - |spot candle direction| > dir_min
      - Entry at NEXT candle perp open (no look-ahead)
    """
    trades: List[Dict]  = []
    vol_hist: deque     = deque(maxlen=VOL_LOOKBACK)
    pos: Optional[Position] = None

    for i, c in enumerate(candles):
        vol = c["spot_vol"] if cfg["use_spot_vol"] else c["perp_vol"]

        # -- manage open position ----------------------------------------------
        if pos is not None:
            result = pos.update(c["perp_high"], c["perp_low"])
            if result is not None:
                reason, exit_px = result
                raw  = pos.pnl(exit_px)
                net  = raw - RT_COST
                hold = i - pos.idx
                trades.append({
                    "engine":    eng_id,
                    "side":      pos.side,
                    "entry_ts":  pos.entry_ts,
                    "exit_ts":   c["ts"],
                    "entry_dt":  datetime.fromtimestamp(pos.entry_ts / 1000).strftime("%Y-%m-%d %H:%M"),
                    "exit_dt":   datetime.fromtimestamp(c["ts"]      / 1000).strftime("%Y-%m-%d %H:%M"),
                    "entry_px":  pos.entry,
                    "exit_px":   exit_px,
                    "reason":    reason,
                    "raw_pct":   raw,
                    "net_pct":   net,
                    "hold_c":    hold,
                    "hold_min":  hold * 5,
                    "be":        pos.be,
                    "oi_chg":    c["oi_chg"],
                    "funding":   c["funding"],
                })
                pos = None

        # -- signal detection --------------------------------------------------
        # Z-score uses history BEFORE current candle (no look-ahead in baseline)
        z = _zscore(list(vol_hist), vol)
        vol_hist.append(vol)   # now add current to history

        if pos is not None:               continue   # already in trade
        if len(vol_hist) < VOL_LOOKBACK:  continue   # not enough history
        if i + 1 >= len(candles):         continue   # no next candle to enter on
        if z < cfg["z_min"]:              continue   # volume not spiking
        if not oi_filter(c["oi_chg"]):    continue   # OI condition

        # direction from spot candle (market consensus)
        delta = (c["spot_close"] - c["spot_open"]) / c["spot_open"]
        if abs(delta) < cfg["dir_min"]:   continue   # doji / no direction

        side = "long" if delta > 0 else "short"
        nxt  = candles[i + 1]
        pos  = Position(eng_id, side, nxt["perp_open"],
                        cfg["sl"], cfg["tp"], cfg["trail"],
                        nxt["ts"], i + 1)

    return trades


# ==============================================================================
# METRICS
# ==============================================================================

def compute_metrics(trades: List[Dict], label: str, total_days: int) -> Dict:
    if not trades:
        return {"label": label, "n": 0}

    longs  = [t for t in trades if t["side"] == "long"]
    shorts = [t for t in trades if t["side"] == "short"]
    wins   = [t for t in trades if t["net_pct"] >  0]
    losses = [t for t in trades if t["net_pct"] <= 0]

    n     = len(trades)
    wr    = len(wins) / n
    gw    = sum(t["net_pct"] for t in wins)
    gl    = abs(sum(t["net_pct"] for t in losses))
    pf    = gw / gl if gl > 0 else float("inf")
    avg_w = gw / len(wins)   if wins   else 0.0
    avg_l = gl / len(losses) if losses else 0.0

    # Expectancy in R  (1R = avg loss, or configured SL if no losses)
    r_unit   = avg_l if avg_l > 0 else 0.006
    exp_r    = (wr * avg_w - (1 - wr) * avg_l) / r_unit

    # Equity curve, drawdown, returns list
    eq, pk, dd_max, rets = 1.0, 1.0, 0.0, []
    for t in sorted(trades, key=lambda x: x["entry_ts"]):
        eq  *= (1 + t["net_pct"])
        rets.append(t["net_pct"])
        if eq > pk:
            pk = eq
        dd = (pk - eq) / pk
        if dd > dd_max:
            dd_max = dd

    # Sharpe & Sortino (annualised via trade frequency)
    sharpe = sortino = 0.0
    if len(rets) > 1:
        mu  = sum(rets) / len(rets)
        sig = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        tpy = (n / max(total_days, 1)) * 252   # estimated trades/year
        if sig > 0:
            sharpe = (mu / sig) * math.sqrt(tpy)
        down = [r for r in rets if r < 0]
        if down:
            dsig    = math.sqrt(sum(r ** 2 for r in down) / len(down))
            sortino = (mu / dsig) * math.sqrt(tpy) if dsig > 0 else float("inf")

    # Hold-time distribution
    holds = [t["hold_min"] for t in trades]
    avg_h = sum(holds) / n
    buckets: Dict[str, int] = {"<5m": 0, "5-15m": 0, "15-60m": 0, "1-4h": 0, ">4h": 0}
    for h in holds:
        if   h <   5: buckets["<5m"]   += 1
        elif h <  15: buckets["5-15m"] += 1
        elif h <  60: buckets["15-60m"]+= 1
        elif h < 240: buckets["1-4h"]  += 1
        else:         buckets[">4h"]   += 1

    # Max consecutive losses
    mcl = cur = 0
    for t in sorted(trades, key=lambda x: x["entry_ts"]):
        if t["net_pct"] <= 0:
            cur += 1
            mcl  = max(mcl, cur)
        else:
            cur = 0

    # Exit reasons
    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    # Top 5 by net PnL
    by_pnl = sorted(trades, key=lambda x: x["net_pct"])
    top5l  = by_pnl[:5]
    top5w  = by_pnl[-5:][::-1]

    return {
        "label": label, "n": n,
        "longs": len(longs), "shorts": len(shorts),
        "wr": wr, "pf": pf, "exp_r": exp_r,
        "avg_w": avg_w, "avg_l": avg_l,
        "breakeven_wr": avg_l / (avg_w + avg_l) if (avg_w + avg_l) > 0 else 0,
        "total_ret_pct": (eq - 1) * 100,
        "max_dd_pct": dd_max * 100,
        "sharpe": sharpe, "sortino": sortino,
        "avg_hold_min": avg_h,
        "hold_dist": buckets,
        "mcl": mcl,
        "be_rate": sum(1 for t in trades if t["be"]) / n,
        "reasons": reasons,
        "top5w": top5w, "top5l": top5l,
    }


def compute_interaction(ta: List[Dict], tb: List[Dict]) -> Dict:
    """Analyse candles where both engines fired simultaneously."""
    # Signal candle ts = entry_ts - 5 min (one 5m bar before entry)
    sig_a = {t["entry_ts"] - 300_000: t for t in ta}
    sig_b = {t["entry_ts"] - 300_000: t for t in tb}
    both  = set(sig_a) & set(sig_b)

    agree = disagree = 0
    agree_pnl: List[float] = []
    disagree_cases: List[Dict] = []

    for ts in sorted(both):
        ta_t, tb_t = sig_a[ts], sig_b[ts]
        if ta_t["side"] == tb_t["side"]:
            agree += 1
            agree_pnl.append((ta_t["net_pct"] + tb_t["net_pct"]) / 2)
        else:
            disagree += 1
            disagree_cases.append({
                "dt":     ta_t["entry_dt"],
                "a_side": ta_t["side"], "a_pnl": ta_t["net_pct"],
                "b_side": tb_t["side"], "b_pnl": tb_t["net_pct"],
            })

    return {
        "n_a": len(sig_a), "n_b": len(sig_b),
        "simultaneous":    len(both),
        "pct_of_a":        len(both) / max(len(sig_a), 1) * 100,
        "pct_of_b":        len(both) / max(len(sig_b), 1) * 100,
        "agree":           agree,
        "disagree":        disagree,
        "agree_avg_pnl":   sum(agree_pnl) / len(agree_pnl) * 100 if agree_pnl else 0,
        "disagree_cases":  disagree_cases[:5],   # sample for inspection
    }


# ==============================================================================
# REPORT (ASCII only -- Windows cp1252 safe)
# ==============================================================================

def print_report(ma: Dict, mb: Dict, ia: Dict, info: Dict) -> None:
    W = 72
    def hr(c: str = "-") -> None: print(c * W)
    def blank()                  : print()

    blank()
    hr("=")
    print(f"  DUAL ENGINE VALIDATION -- BTC/USDT 5m")
    print(f"  Period  : {info['period']}")
    print(f"  Candles : {info['n_candles']:,}  |  OI coverage: {info['oi_pct']:.0f}%"
          f"  (1h granularity, forward-filled)")
    print(f"  Costs   : taker {TAKER*100:.3f}% + maker {MAKER*100:.3f}% + slip {SLIP*100:.3f}%x2"
          f" = {RT_COST*100:.3f}% RT")
    hr("=")

    for m in (ma, mb):
        blank()
        hr()
        print(f"  {m['label']}")
        hr()
        if m["n"] == 0:
            print("  [!] ZERO TRADES -- signal thresholds too tight or no OI data")
            blank()
            continue

        print(f"  Trades    : {m['n']:>5}  "
              f"(Long: {m['longs']} | Short: {m['shorts']} | "
              f"Freq: {m['n']/info['days']:.1f}/day)")
        print(f"  Win Rate  : {m['wr']*100:>6.1f}%   "
              f"(breakeven: {m['breakeven_wr']*100:.0f}%)")
        print(f"  Prof Fact : {m['pf']:>6.2f}")
        print(f"  Expectancy: {m['exp_r']:>+6.3f}R  "
              f"(avg W {m['avg_w']*100:+.3f}% | avg L {-m['avg_l']*100:.3f}%)")
        print(f"  Total Ret : {m['total_ret_pct']:>+7.2f}%")
        print(f"  Max DD    : {m['max_dd_pct']:>7.2f}%")
        print(f"  Sharpe    : {m['sharpe']:>7.2f}   Sortino: {m['sortino']:.2f}")
        print(f"  Avg Hold  : {m['avg_hold_min']:>6.1f} min  |  Max Consec L: {m['mcl']}")
        print(f"  BE Rate   : {m['be_rate']*100:>6.1f}%  "
              f"(trades that reached breakeven trigger before exit)")

        blank()
        print(f"  Hold Distribution (n={m['n']}):")
        max_count = max(m["hold_dist"].values()) if m["hold_dist"] else 1
        for bucket, cnt in m["hold_dist"].items():
            bar = "#" * round(cnt / max(max_count, 1) * 28)
            print(f"    {bucket:>8}: {cnt:>4}  {bar}")

        print(f"\n  Exit Reasons: "
              + "  |  ".join(f"{k}: {v}" for k, v in m["reasons"].items()))

        blank()
        print(f"  Top 5 Winners:")
        for t in m["top5w"]:
            be = "BE" if t["be"] else "  "
            print(f"    {t['entry_dt']}  {t['side'].upper():<5}  "
                  f"+{t['net_pct']*100:.3f}%  {t['hold_min']:>4}min  "
                  f"[{t['reason']}] {be}")

        blank()
        print(f"  Top 5 Losers:")
        for t in m["top5l"]:
            be = "BE" if t["be"] else "  "
            print(f"    {t['entry_dt']}  {t['side'].upper():<5}  "
                  f"{t['net_pct']*100:.3f}%  {t['hold_min']:>4}min  "
                  f"[{t['reason']}] {be}")

    blank()
    hr()
    print(f"  ENGINE INTERACTION")
    hr()
    print(f"  Engine A signals: {ia['n_a']:>4}")
    print(f"  Engine B signals: {ia['n_b']:>4}")
    print(f"  Simultaneous    : {ia['simultaneous']:>4}  "
          f"({ia['pct_of_a']:.1f}% of A | {ia['pct_of_b']:.1f}% of B)")
    print(f"  Agree direction : {ia['agree']:>4}")
    print(f"  Disagree        : {ia['disagree']:>4}")
    if ia["agree"] > 0:
        print(f"  Avg PnL (agree) : {ia['agree_avg_pnl']:>+7.3f}%")
    if ia["disagree_cases"]:
        print(f"\n  Sample disagree cases (first {len(ia['disagree_cases'])}):")
        for c in ia["disagree_cases"]:
            print(f"    {c['dt']}  A={c['a_side'].upper()} ({c['a_pnl']*100:+.3f}%)  "
                  f"B={c['b_side'].upper()} ({c['b_pnl']*100:+.3f}%)")

    blank()
    hr("=")
    print(f"  VERDICT")
    hr("-")
    for m in (ma, mb):
        if m["n"] == 0:
            print(f"  {m['label'][:9]}: [X] No trades generated")
            continue
        flags = []
        if   m["pf"] >= 1.5 and m["exp_r"] > 0.05: flags.append("[OK] Positive edge")
        elif m["pf"] < 1.0:                          flags.append("[X] PF < 1.0 (losing)")
        else:                                         flags.append("[~] Marginal edge")
        if m["n"] < 30:
            flags.append(f"[!] Low sample ({m['n']} trades -- need 30+ for significance)")
        if m["max_dd_pct"] > 20:
            flags.append(f"[X] High DD ({m['max_dd_pct']:.1f}%)")
        elif m["max_dd_pct"] > 10:
            flags.append(f"[!] DD {m['max_dd_pct']:.1f}%")
        if   m["avg_hold_min"] <= 30:  flags.append(f"[OK] Scalping range ({m['avg_hold_min']:.0f}min avg)")
        elif m["avg_hold_min"] <= 90:  flags.append(f"[~] Borderline ({m['avg_hold_min']:.0f}min avg)")
        else:                           flags.append(f"[X] Swing-trade range ({m['avg_hold_min']:.0f}min)")
        print(f"  {m['label'][:9]}: {'  '.join(flags)}")
    hr("=")
    blank()


# ==============================================================================
# MAIN
# ==============================================================================

def main() -> None:
    log.info("=== Dual Engine Validation ===")

    raw     = download(symbol="BTCUSDT", days=90)
    candles = align(raw)

    if len(candles) < VOL_LOOKBACK + 10:
        log.error(f"Only {len(candles)} aligned candles -- aborting.")
        sys.exit(1)

    n_oi  = sum(1 for c in candles if c["oi_usd"] > 0)
    total_days = max((candles[-1]["ts"] - candles[0]["ts"]) / 86_400_000, 1)

    info = {
        "period":    f"{candles[0]['dt'].date()} -> {candles[-1]['dt'].date()}",
        "n_candles": len(candles),
        "oi_pct":    n_oi / len(candles) * 100,
        "days":      total_days,
    }
    log.info(f"Aligned {len(candles):,} candles  |  period: {info['period']}")
    log.info(f"OI coverage: {info['oi_pct']:.0f}% of candles have OI data")
    if info["oi_pct"] < 10:
        log.warning("[!] OI coverage < 10% -- Engine B may produce zero trades."
                    " Binance retains only ~30d of 1h OI history.")

    log.info("Running Engine A (Spot Momentum)...")
    ta = run_engine(candles, "A", EA,
                    oi_filter=lambda x: True)   # no OI requirement
    log.info(f"Engine A -> {len(ta)} trades")

    log.info("Running Engine B (Perp + OI Rising)...")
    # x > oi_min also excludes x==0.0 (no-data candles), so no separate guard needed
    tb = run_engine(candles, "B", EB,
                    oi_filter=lambda x: x > EB["oi_min"])
    log.info(f"Engine B -> {len(tb)} trades")

    ma = compute_metrics(ta, "ENGINE A -- Spot Momentum   (Z>3.0 | no OI  | SL1.0% TP2.5%)",
                         int(total_days))
    mb = compute_metrics(tb, "ENGINE B -- Perp + OI Rise  (Z>2.5 | OI>0.5% | SL1.2% TP3.0%)",
                         int(total_days))
    ia = compute_interaction(ta, tb)

    print_report(ma, mb, ia, info)

    # Save JSON for later analysis
    out = DATA_DIR / "dual_engine_results.json"
    safe = lambda m: {k: v for k, v in m.items()
                      if k not in ("top5w", "top5l") and not callable(v)}
    result = {
        "engine_a":   safe(ma),
        "engine_b":   safe(mb),
        "interaction": ia,
        "info":        {**info, "period": str(info["period"])},
        "config": {"EA": {k: v for k, v in EA.items() if not callable(v)},
                   "EB": {k: v for k, v in EB.items() if not callable(v)},
                   "RT_COST_PCT": RT_COST * 100},
    }
    out.write_text(json.dumps(result, indent=2, default=str))
    log.info(f"Results -> {out}")


if __name__ == "__main__":
    main()
