#!/usr/bin/env python3
"""
tests/validate_vp_strategies.py
Fase A -- Backtest comparativo: Volume Profile (A), Anchored VWAP (B), Confluencia (C).
BTC Futures 1h, 6 meses. Walk-forward 90d treino + 30d teste.
Isolated from live bot (no src/ imports).
"""
import json, math, sys, time, logging, requests
from pathlib import Path
from datetime import datetime, timezone
from collections import deque
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("vp")

HERE     = Path(__file__).parent
DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)
CACHE    = DATA_DIR / "BTCUSDT_6mo_1h.json"

PERP_URL = "https://fapi.binance.com/fapi/v1/klines"

TAKER   = 0.00045
MAKER   = 0.00015
SLIP    = 0.0002
RT_COST = TAKER + SLIP + MAKER + SLIP   # 0.11%

INTERVAL       = "1h"   # kline interval; override for other timeframes
CANDLES_PER_DAY = 24    # used by walk_forward; override to match INTERVAL

N_BUCKETS     = 100
VALUE_AREA    = 0.70   # 70% of session volume
ATR_PERIOD    = 14
ADX_PERIOD    = 14
SWING_CONFIRM = 5      # candles each side for swing detection
SWING_MAX_AGE = 168    # candles (7 days at 1h) for active AVWAP
MIN_SESSION_C = 4      # min candles in session before VP is considered "valid"
TIME_EXIT_C   = 24     # candles before time exit (24h at 1h)
TRAIL_PCT     = 0.50   # trail to BE when gain >= 50% of entry->TP distance


# ==============================================================================
# DATA
# ==============================================================================

def _ts(dt_str: str) -> int:
    return int(datetime.strptime(dt_str, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp() * 1000)


def _klines_to_dicts(raw: list) -> List[Dict]:
    return [{"ts": int(k[0]),
             "dt": datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc),
             "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]),
             "volume": float(k[5])}
            for k in raw]


def download(days: int = 183) -> List[Dict]:
    if CACHE.exists() and (time.time() - CACHE.stat().st_mtime) < 86400:
        log.info(f"Cache hit: {CACHE.name}")
        return _klines_to_dicts(json.loads(CACHE.read_text()))

    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000

    log.info(f"Downloading {days}d of BTCUSDT {INTERVAL} futures...")
    out, cur = [], start_ms
    while cur < end_ms:
        try:
            r = requests.get(PERP_URL, params={
                "symbol": "BTCUSDT", "interval": INTERVAL,
                "startTime": cur, "limit": 1500
            }, timeout=30)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            out.extend(batch)
            cur = int(batch[-1][0]) + 1
            time.sleep(0.05)
        except Exception as e:
            log.warning(f"Download error: {e}")
            break

    candles = _klines_to_dicts(out)
    log.info(f"  -> {len(candles):,} candles")
    CACHE.write_text(json.dumps(out))
    return candles


# ==============================================================================
# INDICATORS  (precomputed -- no look-ahead)
# ==============================================================================

def _wilder_smooth(prev: float, val: float, period: int) -> float:
    return prev * (period - 1) / period + val / period


def compute_atr_adx(candles: List[Dict]) -> None:
    """Attach atr, adx, di_plus, di_minus, regime to each candle in-place."""
    p = ATR_PERIOD
    tr_list, pdm_list, mdm_list = [], [], []

    for i, c in enumerate(candles):
        if i == 0:
            c["atr"] = c["high"] - c["low"]
            c["adx"] = 0.0
            c["di_plus"] = c["di_minus"] = 0.0
            c["regime"] = "unknown"
            continue

        prev = candles[i - 1]
        tr  = max(c["high"] - c["low"],
                  abs(c["high"] - prev["close"]),
                  abs(c["low"]  - prev["close"]))
        pdm = max(c["high"] - prev["high"], 0) if (c["high"] - prev["high"]) > (prev["low"] - c["low"]) else 0
        mdm = max(prev["low"] - c["low"],   0) if (prev["low"] - c["low"])   > (c["high"] - prev["high"]) else 0

        tr_list.append(tr)
        pdm_list.append(pdm)
        mdm_list.append(mdm)

        if i < p:
            c["atr"] = sum(tr_list) / len(tr_list)
            c["adx"] = 0.0
            c["di_plus"] = c["di_minus"] = 0.0
            c["regime"] = "unknown"
            continue

        if i == p:
            atr_s  = sum(tr_list)
            pdm_s  = sum(pdm_list)
            mdm_s  = sum(mdm_list)
        else:
            atr_s  = _wilder_smooth(candles[i-1]["_atr_s"],  tr,  p) * p
            pdm_s  = _wilder_smooth(candles[i-1]["_pdm_s"],  pdm, p) * p
            mdm_s  = _wilder_smooth(candles[i-1]["_mdm_s"],  mdm, p) * p

        c["_atr_s"] = atr_s / p
        c["_pdm_s"] = pdm_s / p
        c["_mdm_s"] = mdm_s / p
        c["atr"] = atr_s / p

        dip = 100 * pdm_s / atr_s if atr_s > 0 else 0
        dim = 100 * mdm_s / atr_s if atr_s > 0 else 0
        dx  = 100 * abs(dip - dim) / (dip + dim) if (dip + dim) > 0 else 0

        if i == p:
            c["_adx_s"] = dx
            c["adx"]    = dx
        else:
            adx_s = _wilder_smooth(candles[i-1]["_adx_s"], dx, p)
            c["_adx_s"] = adx_s
            c["adx"]    = adx_s

        c["di_plus"]  = dip
        c["di_minus"] = dim
        c["regime"]   = "trending" if c["adx"] > 25 else "ranging"


def compute_volume_profile(candles: List[Dict]) -> None:
    """
    Attach vp dict to each candle. VP is computed from session start (00:00 UTC)
    to this candle (inclusive) -- no look-ahead.
    """
    session_candles: List[Dict] = []
    prev_session_vp: Optional[Dict] = None

    def _build_vp(sc: List[Dict]) -> Dict:
        if not sc:
            return {"valid": False, "poc": 0, "vah": 0, "val": 0}
        s_high = max(c["high"] for c in sc)
        s_low  = min(c["low"]  for c in sc)
        if s_high <= s_low:
            s_high = s_low + 1e-9
        bucket_w = (s_high - s_low) / N_BUCKETS
        buckets  = [0.0] * N_BUCKETS

        for c in sc:
            tp  = (c["high"] + c["low"] + c["close"]) / 3
            idx = int((tp - s_low) / bucket_w)
            idx = min(max(idx, 0), N_BUCKETS - 1)
            buckets[idx] += c["volume"]

        poc_idx = max(range(N_BUCKETS), key=lambda b: buckets[b])
        poc_px  = s_low + (poc_idx + 0.5) * bucket_w

        total_vol = sum(buckets)
        target    = total_vol * VALUE_AREA
        lo = hi   = poc_idx
        accum     = buckets[poc_idx]

        while accum < target:
            next_hi = hi + 1 if hi + 1 < N_BUCKETS else None
            next_lo = lo - 1 if lo - 1 >= 0        else None
            up_vol  = buckets[next_hi] if next_hi is not None else -1
            dn_vol  = buckets[next_lo] if next_lo is not None else -1
            if up_vol < 0 and dn_vol < 0:
                break
            if up_vol >= dn_vol:
                hi     = next_hi
                accum += up_vol
            else:
                lo     = next_lo
                accum += dn_vol

        return {
            "valid":   len(sc) >= MIN_SESSION_C,
            "poc":     poc_px,
            "vah":     s_low + (hi + 1) * bucket_w,
            "val":     s_low + lo * bucket_w,
            "prev_poc": prev_session_vp["poc"] if prev_session_vp else None,
        }

    for c in candles:
        c_date = c["dt"].date()
        if session_candles and session_candles[-1]["dt"].date() != c_date:
            prev_session_vp = _build_vp(session_candles)
            session_candles = []
        session_candles.append(c)
        c["vp"] = _build_vp(session_candles)
        c["vp"]["prev_poc"] = prev_session_vp["poc"] if prev_session_vp else None


def compute_swings_avwap(candles: List[Dict]) -> None:
    """
    Attach avwap_supports and avwap_resistances (list of float, up to 2 each)
    to each candle. Swings confirmed with 5-candle delay.
    """
    # Running AVWAP state per swing: {idx: {"type":"high"|"low", "cum_tpv", "cum_vol"}}
    active: Dict[int, Dict] = {}
    confirmed_highs: List[int] = []  # indices of confirmed swing highs
    confirmed_lows:  List[int] = []

    for i, c in enumerate(candles):
        tp  = (c["high"] + c["low"] + c["close"]) / 3
        vol = c["volume"]

        # Update all active AVWAPs
        for k in list(active):
            active[k]["cum_tpv"] += tp * vol
            active[k]["cum_vol"] += vol

        # Expire old swings
        for k in list(active):
            if i - k > SWING_MAX_AGE:
                del active[k]
                confirmed_highs = [x for x in confirmed_highs if x != k]
                confirmed_lows  = [x for x in confirmed_lows  if x != k]

        # Check if candle i-SWING_CONFIRM is a new confirmed swing
        candidate = i - SWING_CONFIRM
        if candidate >= SWING_CONFIRM:
            window_highs = [candles[j]["high"] for j in range(candidate - SWING_CONFIRM,
                                                               candidate + SWING_CONFIRM + 1)]
            window_lows  = [candles[j]["low"]  for j in range(candidate - SWING_CONFIRM,
                                                               candidate + SWING_CONFIRM + 1)]
            swing_h = candles[candidate]["high"] >= max(window_highs)
            swing_l = candles[candidate]["low"]  <= min(window_lows)

            if swing_h and candidate not in active:
                active[candidate] = {
                    "type": "high",
                    "cum_tpv": sum(
                        (candles[j]["high"] + candles[j]["low"] + candles[j]["close"]) / 3
                        * candles[j]["volume"]
                        for j in range(candidate, i + 1)
                    ),
                    "cum_vol": sum(candles[j]["volume"] for j in range(candidate, i + 1)),
                }
                confirmed_highs.append(candidate)

            if swing_l and candidate not in active:
                active[candidate] = {
                    "type": "low",
                    "cum_tpv": sum(
                        (candles[j]["high"] + candles[j]["low"] + candles[j]["close"]) / 3
                        * candles[j]["volume"]
                        for j in range(candidate, i + 1)
                    ),
                    "cum_vol": sum(candles[j]["volume"] for j in range(candidate, i + 1)),
                }
                confirmed_lows.append(candidate)

        # Extract up to 2 most-recent AVWAPs of each type
        sup_vals = []
        res_vals = []
        for k in sorted(active, reverse=True):
            s = active[k]
            av = s["cum_tpv"] / s["cum_vol"] if s["cum_vol"] > 0 else 0
            if s["type"] == "low"  and len(sup_vals) < 2:
                sup_vals.append(av)
            if s["type"] == "high" and len(res_vals) < 2:
                res_vals.append(av)

        c["avwap_sup"] = sup_vals   # support AVWAPs (from swing lows)
        c["avwap_res"] = res_vals   # resistance AVWAPs (from swing highs)


def precompute(candles: List[Dict]) -> List[Dict]:
    log.info("Precomputing ATR/ADX...")
    compute_atr_adx(candles)
    log.info("Precomputing Volume Profile...")
    compute_volume_profile(candles)
    log.info("Precomputing Swings + AVWAP...")
    compute_swings_avwap(candles)
    # Remove internal smoothing keys
    for c in candles:
        for k in ("_atr_s", "_pdm_s", "_mdm_s", "_adx_s"):
            c.pop(k, None)
    return candles


# ==============================================================================
# SIGNAL HELPERS
# ==============================================================================

def _is_rejection_long(c: Dict, support_level: float) -> bool:
    """
    Rejection wick to the downside: lower wick > 50% of range,
    close > support_level, candle green or near-doji.
    """
    rng = c["high"] - c["low"]
    if rng <= 0:
        return False
    lower_wick = min(c["open"], c["close"]) - c["low"]
    if lower_wick / rng <= 0.50:
        return False
    if c["close"] <= support_level:
        return False
    # green or near-doji (body <= 10% of range)
    if c["close"] < c["open"] and (c["open"] - c["close"]) / rng > 0.10:
        return False
    return True


def _is_rejection_short(c: Dict, resistance_level: float) -> bool:
    rng = c["high"] - c["low"]
    if rng <= 0:
        return False
    upper_wick = c["high"] - max(c["open"], c["close"])
    if upper_wick / rng <= 0.50:
        return False
    if c["close"] >= resistance_level:
        return False
    if c["close"] > c["open"] and (c["close"] - c["open"]) / rng > 0.10:
        return False
    return True


def _vp_level(c: Dict) -> Dict:
    """Return the VP to use: current session if valid, else fall back to prev POC."""
    vp = c["vp"]
    if vp["valid"]:
        return vp
    if vp["prev_poc"] is not None:
        return {"valid": True, "poc": vp["prev_poc"], "vah": vp["prev_poc"],
                "val": vp["prev_poc"], "prev_poc": None}
    return {"valid": False}


# ==============================================================================
# POSITION
# ==============================================================================

class Position:
    def __init__(self, engine: str, side: str, entry: float,
                 sl_px: float, tp_px: float,
                 entry_ts: int, entry_idx: int,
                 setup_context: Dict):
        self.engine  = engine
        self.side    = side
        self.entry   = entry
        self.sl_px   = sl_px
        self.tp_px   = tp_px
        self.entry_ts  = entry_ts
        self.entry_idx = entry_idx
        self.setup     = setup_context
        self.be        = False
        self.trail_triggered = entry + TRAIL_PCT * (tp_px - entry) if side == "long" \
                                else entry - TRAIL_PCT * (entry - tp_px)

    def check(self, c: Dict, hold_c: int) -> Optional[Tuple[str, float]]:
        hi, lo, op = c["high"], c["low"], c["open"]

        # Time exit (at candle open, before intra-candle checks)
        if hold_c >= TIME_EXIT_C:
            return "TIME", op

        if self.side == "long":
            # Trail to breakeven
            if not self.be and hi >= self.trail_triggered:
                self.be    = True
                self.sl_px = self.entry

            # Gap-open past SL or TP (fill at open)
            if op <= self.sl_px: return "SL",   op
            if op >= self.tp_px: return "TP",   op

            # Intra-candle
            if lo <= self.sl_px: return "SL",   self.sl_px
            if hi >= self.tp_px: return "TP",   self.tp_px
        else:
            if not self.be and lo <= self.trail_triggered:
                self.be    = True
                self.sl_px = self.entry

            if op >= self.sl_px: return "SL",   op
            if op <= self.tp_px: return "TP",   op

            if hi >= self.sl_px: return "SL",   self.sl_px
            if lo <= self.tp_px: return "TP",   self.tp_px

        return None

    def pnl(self, exit_px: float) -> float:
        return (exit_px - self.entry) / self.entry if self.side == "long" \
               else (self.entry - exit_px) / self.entry


def _record(pos: Position, c: Dict, exit_px: float, reason: str) -> Dict:
    raw = pos.pnl(exit_px)
    return {
        "engine":   pos.engine,
        "side":     pos.side,
        "entry_ts": pos.entry_ts,
        "exit_ts":  c["ts"],
        "entry_dt": datetime.fromtimestamp(pos.entry_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "exit_dt":  datetime.fromtimestamp(c["ts"]       / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "entry_hour": datetime.fromtimestamp(pos.entry_ts / 1000, tz=timezone.utc).hour,
        "entry_price": pos.entry,
        "exit_price":  exit_px,
        "sl_price":    pos.sl_px,
        "tp_price":    pos.tp_px,
        "exit_reason": reason,
        "raw_pct":     raw,
        "net_pct":     raw - RT_COST,
        "hold_c":      c["ts"] // 3_600_000 - pos.entry_ts // 3_600_000,
        "be":          pos.be,
        "regime":      pos.setup.get("regime", "unknown"),
        "setup_context": pos.setup,
    }


# ==============================================================================
# STRATEGY A — VOLUME PROFILE
# ==============================================================================

def run_strategy_a(candles: List[Dict]) -> List[Dict]:
    trades: List[Dict] = []
    pos: Optional[Position] = None

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

        touch   = candles[i - 1]   # candle that touched level
        reject  = c                 # current candle -- potential rejection
        vp      = _vp_level(touch)

        if not vp["valid"]:
            continue

        atr = reject.get("atr", 0)
        if atr <= 0:
            continue

        # Long setup: touch candle reached VAL
        if touch["low"] <= vp["val"]:
            if _is_rejection_long(reject, vp["val"]):
                poc = vp["poc"] if vp["poc"] > vp["val"] else (vp["prev_poc"] or vp["vah"])
                nxt = candles[i + 1]
                entry = nxt["open"]
                sl    = reject["low"] - 0.5 * atr
                tp    = poc
                if tp <= entry or (tp - entry) < (entry - sl):
                    continue   # bad R:R or invalid
                if entry <= sl:
                    continue
                pos = Position("A", "long", entry, sl, tp, nxt["ts"], i + 1,
                               {"vp_val": vp["val"], "vp_vah": vp["vah"], "vp_poc": poc,
                                "atr": atr, "regime": reject.get("regime", "unknown")})
                continue

        # Short setup: touch candle reached VAH
        if touch["high"] >= vp["vah"]:
            if _is_rejection_short(reject, vp["vah"]):
                poc = vp["poc"] if vp["poc"] < vp["vah"] else (vp["prev_poc"] or vp["val"])
                nxt = candles[i + 1]
                entry = nxt["open"]
                sl    = reject["high"] + 0.5 * atr
                tp    = poc
                if tp >= entry or (entry - tp) < (sl - entry):
                    continue
                if entry >= sl:
                    continue
                pos = Position("A", "short", entry, sl, tp, nxt["ts"], i + 1,
                               {"vp_val": vp["val"], "vp_vah": vp["vah"], "vp_poc": poc,
                                "atr": atr, "regime": reject.get("regime", "unknown")})

    return trades


# ==============================================================================
# STRATEGY B — ANCHORED VWAP
# ==============================================================================

def run_strategy_b(candles: List[Dict]) -> List[Dict]:
    trades: List[Dict] = []
    pos: Optional[Position] = None

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

        # Long: touch any support AVWAP + rejection
        for av_sup in sups:
            if touch["low"] <= av_sup:
                if _is_rejection_long(reject, av_sup):
                    nxt   = candles[i + 1]
                    entry = nxt["open"]
                    sl    = reject["low"] - 0.5 * atr
                    if entry <= sl:
                        continue
                    # TP: closest resistance AVWAP above entry
                    res_above = [r for r in ress if r > entry]
                    if res_above:
                        tp = min(res_above)
                    else:
                        tp = entry + 2 * (entry - sl)   # 2× SL distance
                    if (tp - entry) < (entry - sl):
                        continue   # R:R < 1:1
                    if tp <= entry:
                        continue
                    pos = Position("B", "long", entry, sl, tp, nxt["ts"], i + 1,
                                   {"avwap_touched": av_sup, "avwap_sup": sups, "avwap_res": ress,
                                    "atr": atr, "regime": reject.get("regime", "unknown")})
                    break

        if pos is not None:
            continue

        # Short: touch any resistance AVWAP + rejection
        for av_res in ress:
            if touch["high"] >= av_res:
                if _is_rejection_short(reject, av_res):
                    nxt   = candles[i + 1]
                    entry = nxt["open"]
                    sl    = reject["high"] + 0.5 * atr
                    if entry >= sl:
                        continue
                    sup_below = [s for s in sups if s < entry]
                    if sup_below:
                        tp = max(sup_below)
                    else:
                        tp = entry - 2 * (sl - entry)
                    if (entry - tp) < (sl - entry):
                        continue
                    if tp >= entry:
                        continue
                    pos = Position("B", "short", entry, sl, tp, nxt["ts"], i + 1,
                                   {"avwap_touched": av_res, "avwap_sup": sups, "avwap_res": ress,
                                    "atr": atr, "regime": reject.get("regime", "unknown")})
                    break

    return trades


# ==============================================================================
# STRATEGY C — CONFLUENCE (A + B simultaneous)
# ==============================================================================

def run_strategy_c(candles: List[Dict]) -> List[Dict]:
    trades: List[Dict] = []
    pos: Optional[Position] = None
    LEVEL_TOL = 0.003   # 0.3% tolerance between VP level and AVWAP

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

        vp   = _vp_level(touch)
        sups = touch.get("avwap_sup", [])
        ress = touch.get("avwap_res", [])

        if not vp["valid"]:
            continue

        # Long confluence: VAL touch + AVWAP support within 0.3%
        if touch["low"] <= vp["val"]:
            for av_sup in sups:
                mid = (vp["val"] + av_sup) / 2 if av_sup > 0 else vp["val"]
                if mid > 0 and abs(vp["val"] - av_sup) / mid <= LEVEL_TOL:
                    if _is_rejection_long(reject, vp["val"]):
                        nxt   = candles[i + 1]
                        entry = nxt["open"]
                        sl    = reject["low"] - 0.5 * atr   # conservative (wider)
                        poc   = vp["poc"] if vp["poc"] > vp["val"] else (vp["prev_poc"] or vp["vah"])
                        res_above = [r for r in ress if r > entry]
                        tp_avwap  = min(res_above) if res_above else None
                        tp = min(poc, tp_avwap) if tp_avwap else poc  # closest of POC / AVWAP res
                        if tp <= entry or (tp - entry) < (entry - sl) or entry <= sl:
                            continue
                        pos = Position("C", "long", entry, sl, tp, nxt["ts"], i + 1,
                                       {"vp_val": vp["val"], "avwap_sup": av_sup,
                                        "atr": atr, "regime": reject.get("regime", "unknown")})
                        break
            if pos is not None:
                continue

        # Short confluence: VAH touch + AVWAP resistance within 0.3%
        if touch["high"] >= vp["vah"]:
            for av_res in ress:
                mid = (vp["vah"] + av_res) / 2 if av_res > 0 else vp["vah"]
                if mid > 0 and abs(vp["vah"] - av_res) / mid <= LEVEL_TOL:
                    if _is_rejection_short(reject, vp["vah"]):
                        nxt   = candles[i + 1]
                        entry = nxt["open"]
                        sl    = reject["high"] + 0.5 * atr
                        poc   = vp["poc"] if vp["poc"] < vp["vah"] else (vp["prev_poc"] or vp["val"])
                        sup_below = [s for s in sups if s < entry]
                        tp_avwap  = max(sup_below) if sup_below else None
                        tp = max(poc, tp_avwap) if tp_avwap else poc
                        if tp >= entry or (entry - tp) < (sl - entry) or entry >= sl:
                            continue
                        pos = Position("C", "short", entry, sl, tp, nxt["ts"], i + 1,
                                       {"vp_vah": vp["vah"], "avwap_res": av_res,
                                        "atr": atr, "regime": reject.get("regime", "unknown")})
                        break

    return trades


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
    r_unit = avg_l if avg_l > 0 else 0.005
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

    holds   = [t["hold_c"] for t in trades]
    avg_h   = sum(holds) / n
    med_h   = sorted(holds)[n // 2]

    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    by_pnl = sorted(trades, key=lambda x: x["net_pct"])

    # Regime split
    trend_trades = [t for t in trades if t["regime"] == "trending"]
    range_trades = [t for t in trades if t["regime"] == "ranging"]

    def _pf(ts):
        w = sum(t["net_pct"] for t in ts if t["net_pct"] > 0)
        l = abs(sum(t["net_pct"] for t in ts if t["net_pct"] <= 0))
        return w / l if l > 0 else float("inf") if w > 0 else 0.0

    def _wr(ts):
        return sum(1 for t in ts if t["net_pct"] > 0) / len(ts) * 100 if ts else 0.0

    return {
        "label": label, "n": n,
        "wr": wr, "pf": pf, "exp_r": exp_r,
        "avg_w": avg_w, "avg_l": avg_l,
        "breakeven_wr": avg_l / (avg_w + avg_l) if (avg_w + avg_l) > 0 else 0,
        "total_ret_pct": (eq - 1) * 100,
        "max_dd_pct": dd_max * 100,
        "sharpe": sharpe, "sortino": sortino,
        "avg_hold_h": avg_h, "med_hold_h": med_h,
        "reasons": reasons,
        "top5w": by_pnl[-5:][::-1],
        "top5l": by_pnl[:5],
        "regime": {
            "trending": {"n": len(trend_trades), "pf": _pf(trend_trades), "wr": _wr(trend_trades)},
            "ranging":  {"n": len(range_trades),  "pf": _pf(range_trades),  "wr": _wr(range_trades)},
        }
    }


def walk_forward(candles: List[Dict],
                 run_fn,
                 label: str,
                 train_days: int = 90,
                 test_days:  int = 30) -> List[Dict]:
    """
    Walk-forward: windows of train_days + test_days, slide test_days.
    Returns list of {window, train_pf, test_pf, train_n, test_n}.
    """
    train_c = train_days * CANDLES_PER_DAY
    test_c  = test_days  * CANDLES_PER_DAY
    results = []
    w       = 0

    start = 0
    while start + train_c + test_c <= len(candles):
        train_end = start + train_c
        test_end  = train_end + test_c

        t_train = run_fn(candles[start:train_end])
        t_test  = run_fn(candles[train_end:test_end])

        def _pf(ts):
            gw = sum(t["net_pct"] for t in ts if t["net_pct"] > 0)
            gl = abs(sum(t["net_pct"] for t in ts if t["net_pct"] <= 0))
            return gw / gl if gl > 0 else float("inf") if gw > 0 else 0.0

        w += 1
        train_start_dt = candles[start]["dt"].date()
        test_end_dt    = candles[test_end - 1]["dt"].date()
        results.append({
            "window":   w,
            "period":   f"{train_start_dt} -> {test_end_dt}",
            "train_n":  len(t_train),
            "train_pf": _pf(t_train),
            "test_n":   len(t_test),
            "test_pf":  _pf(t_test),
        })
        start += test_c

    return results


# ==============================================================================
# REPORT
# ==============================================================================

W = 72
def hr(c="-"):  print(c * W)
def blank():    print()
EXITS = ["TP", "SL", "TIME", "TRAIL_BE"]


def _verdict(m: Dict) -> str:
    if m["n"] == 0:
        return "[X] No trades"
    flags = []
    pf = m["pf"]
    if   pf >= 1.3:  flags.append("[OK] PF>=1.3")
    elif pf >= 1.0:  flags.append("[~] 1.0<=PF<1.3")
    else:            flags.append("[X] PF<1.0")
    if m["n"] < 30:  flags.append(f"[!] n={m['n']}<30")
    return "  ".join(flags)


def print_block(m: Dict) -> None:
    hr()
    print(f"  {m['label']}")
    hr()
    if m["n"] == 0:
        print("  No trades generated.")
        blank()
        return

    n = m["n"]
    print(f"  Trades    : {n:>5}")
    print(f"  Win Rate  : {m['wr']*100:>6.1f}%  (breakeven: {m['breakeven_wr']*100:.0f}%)")
    print(f"  Prof Fact : {m['pf']:>6.2f}")
    print(f"  Expectancy: {m['exp_r']:>+6.3f}R"
          f"  (avg W {m['avg_w']*100:+.3f}% | avg L {-m['avg_l']*100:.3f}%)")
    print(f"  Total Ret : {m['total_ret_pct']:>+7.2f}%")
    print(f"  Max DD    : {m['max_dd_pct']:>7.2f}%")
    print(f"  Sharpe    : {m['sharpe']:>7.2f}   Sortino: {m['sortino']:.2f}")
    print(f"  Avg Hold  : {m['avg_hold_h']:>5.1f}h  |  Median: {m['med_hold_h']}h")
    exits_str = "  |  ".join(f"{k}: {m['reasons'].get(k,0)}" for k in EXITS if m["reasons"].get(k,0) > 0)
    print(f"  Exits     : {exits_str}")

    r = m["regime"]
    print(f"  Regime    : trending n={r['trending']['n']} PF={r['trending']['pf']:.2f}"
          f"  |  ranging n={r['ranging']['n']} PF={r['ranging']['pf']:.2f}")

    blank()
    print("  Top 5 Winners:")
    for t in m["top5w"]:
        be = "BE" if t["be"] else "  "
        print(f"    {t['entry_dt']}  {t['side'].upper():<5}"
              f"  +{t['net_pct']*100:.3f}%  {t['hold_c']:>3}h"
              f"  [{t['exit_reason']}] {be}")
    blank()
    print("  Top 5 Losers:")
    for t in m["top5l"]:
        be = "BE" if t["be"] else "  "
        print(f"    {t['entry_dt']}  {t['side'].upper():<5}"
              f"  {t['net_pct']*100:.3f}%  {t['hold_c']:>3}h"
              f"  [{t['exit_reason']}] {be}")
    blank()


def print_wf(wf: List[Dict], label: str) -> None:
    print(f"  {label} -- Walk-Forward (90d train + 30d test):")
    if not wf:
        print("    (insufficient data)")
        return
    for w in wf:
        train_flag = "[OK]" if w["train_pf"] >= 1.3 else "[X]"
        test_flag  = "[OK]" if w["test_pf"]  >= 1.3 else "[X]"
        print(f"    W{w['window']} {w['period']}"
              f"  train: {w['train_pf']:.2f}(n={w['train_n']}) {train_flag}"
              f"  test: {w['test_pf']:.2f}(n={w['test_n']}) {test_flag}")
    pfs = [w["test_pf"] for w in wf if w["test_pf"] != float("inf")]
    if pfs:
        avg_pf = sum(pfs) / len(pfs)
        std_pf = math.sqrt(sum((p - avg_pf) ** 2 for p in pfs) / len(pfs)) if len(pfs) > 1 else 0
        oos = "[OK]" if avg_pf >= 1.3 and std_pf < 0.5 else "[X]"
        print(f"    OOS avg PF: {avg_pf:.2f}  std: {std_pf:.2f}  {oos}")


def print_report(results: List[Tuple], info: Dict) -> None:
    blank()
    hr("=")
    print("  VP STRATEGY VALIDATION -- BTC/USDT Futures 1h")
    print(f"  Period  : {info['period']}")
    print(f"  Candles : {info['n_candles']:,}  (~{info['n_candles']//24:.0f} days)")
    print(f"  Costs   : {RT_COST*100:.3f}% RT")
    hr("=")

    for (m, wf) in results:
        blank()
        print_block(m)
        print_wf(wf, m["label"])
        blank()

    blank()
    hr("=")
    print("  COMPARATIVE SUMMARY")
    hr("-")
    print(f"  {'Strategy':<12} {'N':>5} {'WR':>7} {'PF':>6} {'ExpR':>7}"
          f" {'Sharpe':>7} {'MaxDD':>7} {'AvgH':>6}  Verdict")
    hr("-")
    for (m, _) in results:
        if m["n"] == 0:
            print(f"  {m['label'][:12]:<12} {'---':>5}")
            continue
        print(f"  {m['label'][:12]:<12} {m['n']:>5} {m['wr']*100:>6.1f}%"
              f" {m['pf']:>6.2f} {m['exp_r']:>+7.3f}"
              f" {m['sharpe']:>7.2f} {m['max_dd_pct']:>6.1f}%"
              f" {m['avg_hold_h']:>5.1f}h  {_verdict(m)}")
    hr("=")
    blank()

    # Edge criteria check
    print("  EDGE CRITERIA (PF OOS >= 1.3, std < 0.5, N >= 30):")
    for (m, wf) in results:
        if m["n"] == 0:
            print(f"  {m['label'][:12]}: FAIL (no trades)")
            continue
        pfs = [w["test_pf"] for w in wf if w["test_pf"] != float("inf")]
        avg = sum(pfs) / len(pfs) if pfs else 0
        std = math.sqrt(sum((p-avg)**2 for p in pfs)/len(pfs)) if len(pfs)>1 else 0
        min_n = min((w["test_n"] for w in wf), default=0)
        ok_pf = avg >= 1.3
        ok_std = std < 0.5
        ok_n   = min_n >= 30
        verdict = "PASS" if ok_pf and ok_std and ok_n else \
                  "MARGINAL" if ok_pf or (avg >= 1.1 and ok_n) else "FAIL"
        print(f"  {m['label'][:12]}: {verdict}"
              f"  OOS_PF={avg:.2f}  std={std:.2f}  min_N={min_n}")
    hr("=")
    blank()


# ==============================================================================
# MAIN
# ==============================================================================

def main() -> None:
    log.info("=== VP Strategy Validation -- Fase A ===")

    raw = download(days=183)
    if len(raw) < 4000:
        log.error(f"Only {len(raw)} candles -- need 4000+. Aborting.")
        sys.exit(1)

    candles = precompute(raw)
    log.info(f"Candles after precompute: {len(candles):,}")

    total_days = (candles[-1]["ts"] - candles[0]["ts"]) / 86_400_000
    info = {
        "period":    f"{candles[0]['dt'].date()} -> {candles[-1]['dt'].date()}",
        "n_candles": len(candles),
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

    results = [(ma, wf_a), (mb, wf_b), (mc, wf_c)]
    print_report(results, info)

    # Save JSON
    out = DATA_DIR / "vp_v1_results.json"
    def safe(m):
        return {k: v for k, v in m.items() if k not in ("top5w", "top5l")}
    result = {
        "info":       info,
        "strategy_a": safe(ma), "wf_a": wf_a,
        "strategy_b": safe(mb), "wf_b": wf_b,
        "strategy_c": safe(mc), "wf_c": wf_c,
    }
    out.write_text(json.dumps(result, indent=2, default=str))
    log.info(f"Results -> {out}")
    log.info("=== Fase A concluida. PAUSA para confirmacao. ===")


if __name__ == "__main__":
    main()
