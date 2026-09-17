#!/usr/bin/env python3
"""Jev shadow judge — TypeSafe System One trading-judgment experiment.

Hypothesis (honest): a general-purpose judgment model (Jev) given a compact
computed market state may produce directional probabilities with predictive
signal. Prior is LOW — it has no market training data beyond what we feed it.
This script MEASURES that hypothesis; it never reaches the engine or OMS.

Design (mirrors wallet_fills_collector — standalone, zero engine contact):
  * runs on a schedule (5-min via Hyperliquid-Jev-Shadow task; the Jev API
    is only asked once per --ask-interval-s per symbol, the extra runs just
    manage virtual exits cheaply)
  * builds a compact feature state per symbol from live bot.db candles
    (read-only) + top_trader_bias_samples from the research DB
  * asks Jev ONE request per symbol containing a fan-out of independent
    questions (per TypeSafe docs): Choice action {long/short/flat},
    Noul long_edge, Noul short_edge, Choice regime
  * persists everything to `jev_decisions` for offline evaluation
    (scripts/research/jev_eval.py)

Virtual paper trading (--paper, default on): when Jev answers long/short
with action confidence >= ENTRY_CONF_MIN and no virtual position is open
on that symbol, a virtual position is opened at the NEXT 1m candle open
(no lookahead — the decision uses data up to ts, the fill uses a bar that
starts after ts). Exits: SL = max(1%, 2*ATR%) / TP = 2R / max-hold 4h,
walked against real candles_1m (SL checked before TP on the same bar —
conservative intrabar rule). Closed virtual trades are written into the
LIVE bot.db `trades` + `strategy_pnl` tables (strategy='JevJudge',
status='closed') so the dashboard shows them — OPEN virtual positions
live only in the research DB so the engine's open-trade restore path
never touches them. Fees: tier-0 taker 0.045% per side on notional.

Cost discipline: hard daily call cap + cumulative token ledger in
data/research/jev_usage.json. With $5 of credits the pilot stays bounded;
`--daily-cap` and `--max-tokens` control the burn.

Usage:
    python -X utf8 scripts/research/jev_shadow_judge.py --once        # one pass
    python -X utf8 scripts/research/jev_shadow_judge.py --dry-run     # print request, no call
    python -X utf8 scripts/research/jev_shadow_judge.py --once --no-paper  # judge only
    TYPESAFE_API_KEY must be set in the environment (never in YAML/git).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.research_database import ResearchDatabase  # noqa: E402
from src.utils.config import load_config  # noqa: E402

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
LIVE_DB = ROOT / "data" / "live" / "bot.db"
USAGE_PATH = ROOT / "data" / "research" / "jev_usage.json"
TABLE = "jev_decisions"

CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    state_json      TEXT    NOT NULL,
    action_choice   TEXT,
    action_probs    TEXT,
    action_conf     REAL,
    long_noul       REAL,
    short_noul      REAL,
    regime_choice   TEXT,
    regime_probs    TEXT,
    regime_conf     REAL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    latency_ms      INTEGER,
    model           TEXT,
    error           TEXT,
    ingested_at_ms  INTEGER NOT NULL,
    UNIQUE(symbol, ts_ms)
);
"""

VPOS_TABLE = "jev_virtual_positions"
CREATE_VPOS_SQL = f"""
CREATE TABLE IF NOT EXISTS {VPOS_TABLE} (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id   INTEGER NOT NULL,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    entry_price   REAL,
    entry_time    INTEGER,
    size          REAL,
    notional      REAL    NOT NULL,
    sl_pct        REAL    NOT NULL,
    sl_price      REAL,
    tp_price      REAL,
    max_hold_ms   INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    exit_price    REAL,
    exit_time     INTEGER,
    exit_reason   TEXT,
    pnl_usd       REAL,
    trade_id      INTEGER,
    created_ms    INTEGER NOT NULL
);
"""

# Virtual-paper constants (isolated experiment — not engine params)
VIRTUAL_NOTIONAL_USD = 1_000.0
ENTRY_CONF_MIN = 0.60
SL_FRACTION_MIN = 0.01          # 1% of price
SL_ATR_MULT = 2.0               # or 2 x ATR% if wider
TP_R_MULT = 2.0                 # take-profit at 2R
MAX_HOLD_MS = 4 * 3_600_000     # matches the 4h horizon in the questions
TAKER_FEE = 0.00045             # tier-0 taker, per side, on notional

QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "Best trading action for this asset over the next 4 hours",
        "criteria": {
            "long": "open or maintain a LONG position",
            "short": "open or maintain a SHORT position",
            "flat": "stay flat — no clear edge in either direction",
        },
    },
    "long_edge": {
        "type": "noul",
        "instructions": "Would a LONG position opened now likely be "
                        "profitable 4 hours later, after taker fees?",
    },
    "short_edge": {
        "type": "noul",
        "instructions": "Would a SHORT position opened now likely be "
                        "profitable 4 hours later, after taker fees?",
    },
    "regime": {
        "type": "choice",
        "instructions": "Current market regime for this asset",
        "criteria": {
            "trend_up": "sustained directional move up",
            "trend_down": "sustained directional move down",
            "chop": "mean-reverting range, no direction",
            "expansion": "volatility expansion, direction unclear",
        },
    },
}


# ── feature state (compact — Jev only knows what we tell it) ─────────

def _sma(xs: List[float], n: int) -> Optional[float]:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _ema_series(xs: List[float], n: int) -> List[float]:
    if not xs:
        return []
    k = 2.0 / (n + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(out[-1] + k * (x - out[-1]))
    return out


def _atr_pct(highs: List[float], lows: List[float], closes: List[float],
             n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    atr = sum(trs[-n:]) / n
    return atr / closes[-1] * 100 if closes[-1] else None


def _adx(highs: List[float], lows: List[float], closes: List[float],
         n: int = 14) -> Optional[float]:
    if len(closes) < 2 * n + 1:
        return None
    tr, pdm, ndm = [], [], []
    for i in range(1, len(closes)):
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)

    def smooth(xs: List[float]) -> List[float]:
        out = [sum(xs[:n])]
        for x in xs[n:]:
            out.append(out[-1] - out[-1] / n + x)
        return out

    str_, spdm, sndm = smooth(tr), smooth(pdm), smooth(ndm)
    dxs = []
    for i in range(len(str_)):
        if str_[i] == 0:
            continue
        pdi, ndi = 100 * spdm[i] / str_[i], 100 * sndm[i] / str_[i]
        s = pdi + ndi
        if s > 0:
            dxs.append(100 * abs(pdi - ndi) / s)
    return sum(dxs[-n:]) / n if len(dxs) >= n else None


def _vwap_z(c15: List[tuple]) -> Optional[float]:
    """z-score of close vs rolling 24h anchored VWAP (96 x 15m bars)."""
    bars = c15[-96:]
    if len(bars) < 30:
        return None
    pv = sum(((h + l + c) / 3) * v for _t, o, h, l, c, v in bars)
    vv = sum(v for _t, o, h, l, c, v in bars)
    if vv <= 0:
        return None
    vwap = pv / vv
    devs = [((h + l + c) / 3 - vwap) for _t, o, h, l, c, v in bars]
    mean = sum(devs) / len(devs)
    var = sum((d - mean) ** 2 for d in devs) / max(1, len(devs) - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return None
    return (bars[-1][4] - vwap) / sd


def _pct(a: float, b: float) -> Optional[float]:
    return (a / b - 1) * 100 if b else None


def build_state(db: sqlite3.Connection, rdb: sqlite3.Connection,
                symbol: str) -> Optional[Dict[str, Any]]:
    """Compact market state for Jev — computed features, not raw candles."""
    rows15 = db.execute(
        "select timestamp_ms, open, high, low, close, volume from candles_15m "
        "where symbol=? order by timestamp_ms desc limit 300", (symbol,),
    ).fetchall()
    if len(rows15) < 100:
        return None
    c15 = list(reversed(rows15))
    closes = [r[4] for r in c15]
    highs = [r[2] for r in c15]
    lows = [r[3] for r in c15]
    vols = [r[5] for r in c15]
    price = closes[-1]

    rows1h = db.execute(
        "select close from candles_1h where symbol=? order by timestamp_ms "
        "desc limit 200", (symbol,),
    ).fetchall()
    closes1h = list(reversed([r[0] for r in rows1h]))

    ema50 = _ema_series(closes, 50)[-1] if len(closes) >= 50 else None
    ema200 = _ema_series(closes, 200)[-1] if len(closes) >= 200 else None
    vol_mean = _sma(vols, 20)

    hi24 = max(highs[-96:]) if len(highs) >= 24 else None
    lo24 = min(lows[-96:]) if len(lows) >= 24 else None
    range_pos = (price - lo24) / (hi24 - lo24) if hi24 and lo24 and hi24 > lo24 else None

    funding = db.execute(
        "select funding_rate from candles_1m where symbol=? and "
        "funding_rate is not null order by timestamp_ms desc limit 1",
        (symbol,),
    ).fetchone()

    bias = rdb.execute(
        "select net_bias, timestamp_ms from top_trader_bias_samples "
        "where symbol=? order by timestamp_ms desc limit 1", (symbol,),
    ).fetchone() if rdb else None
    bias_age_h = ((time.time() * 1000 - bias[1]) / 3_600_000) if bias else None

    state: Dict[str, Any] = {
        "asset": f"{symbol} perpetual futures (Hyperliquid)",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "price": round(price, 6),
        "returns_pct": {
            "last_15m": _pct(price, closes[-2]) if len(closes) >= 2 else None,
            "last_1h": _pct(price, closes[-5]) if len(closes) >= 5 else None,
            "last_4h": _pct(price, closes[-17]) if len(closes) >= 17 else None,
            "last_24h": _pct(price, closes[-97]) if len(closes) >= 97 else None,
        },
        "volatility": {
            "atr_pct_15m": _atr_pct(highs, lows, closes),
        },
        "trend": {
            "adx_15m": _adx(highs, lows, closes),
            "price_vs_ema50_pct": _pct(price, ema50) if ema50 else None,
            "price_vs_ema200_pct": _pct(price, ema200) if ema200 else None,
            "ema50_above_ema200": (ema50 > ema200) if (ema50 and ema200) else None,
            "position_in_24h_range": range_pos,
        },
        "mean_reversion": {
            "z_score_vs_vwap_24h": _vwap_z(c15),
        },
        "volume": {
            "last_15m_vs_mean20": (vols[-1] / vol_mean) if vol_mean else None,
        },
        "funding": {
            "last_rate_per_8h": funding[0] if funding else None,
        },
        "top_trader_bias": {
            "net_bias": bias[0] if bias and (bias_age_h or 99) < 2 else None,
            "sample_age_hours": round(bias_age_h, 2) if bias_age_h else None,
        },
    }
    # drop None leaves for readability/token economy
    def clean(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if v is not None}
        return x
    return clean(state)


# ── jev call + persistence ───────────────────────────────────────────

def _load_usage() -> Dict[str, int]:
    try:
        return json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def _save_usage(u: Dict[str, int]) -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(u, indent=2), encoding="utf-8")


def _call_jev(api_key: str, state: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps({
        "state": state,
        "model": MODEL,
        "questions": QUESTIONS,
    }).encode()
    req = urllib.request.Request(
        API_URL, data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    data["_latency_ms"] = int((time.time() - t0) * 1000)
    return data


def _insert(conn: sqlite3.Connection, symbol: str, ts_ms: int,
            state: Dict[str, Any], resp: Optional[Dict[str, Any]],
            error: Optional[str]) -> None:
    ans = (resp or {}).get("answers") or {}
    act = ans.get("action") or {}
    reg = ans.get("regime") or {}
    usage = (resp or {}).get("usage") or {}
    conn.execute(
        f"""INSERT OR IGNORE INTO {TABLE}
        (ts_ms, symbol, state_json, action_choice, action_probs, action_conf,
         long_noul, short_noul, regime_choice, regime_probs, regime_conf,
         input_tokens, output_tokens, latency_ms, model, error, ingested_at_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts_ms, symbol, json.dumps(state),
            act.get("choice"), json.dumps(act.get("probabilities")),
            act.get("confidence"),
            (ans.get("long_edge") or {}).get("noul"),
            (ans.get("short_edge") or {}).get("noul"),
            reg.get("choice"), json.dumps(reg.get("probabilities")),
            reg.get("confidence"),
            usage.get("input_tokens"), usage.get("output_tokens"),
            (resp or {}).get("_latency_ms"), (resp or {}).get("model"),
            error, int(time.time() * 1000),
        ),
    )
    conn.commit()


def _research_db_path() -> Path:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    return Path(ResearchDatabase.resolve_path(cfg))


# ── virtual paper trading (isolated — closed trades only reach bot.db) ──

def _fill_pending_entries(db: sqlite3.Connection,
                          wdb: sqlite3.Connection) -> int:
    """Fill pending virtual positions at the next 1m candle open."""
    filled = 0
    pending = wdb.execute(
        f"select id, symbol, notional, sl_pct, created_ms from {VPOS_TABLE} "
        "where status='pending'",
    ).fetchall()
    for pid, sym, notional, sl_pct, created_ms in pending:
        row = db.execute(
            "select timestamp_ms, open from candles_1m where symbol=? "
            "and timestamp_ms > ? order by timestamp_ms limit 1",
            (sym, created_ms),
        ).fetchone()
        if row is None:
            continue
        ts, open_ = row
        size = notional / open_
        side = wdb.execute(
            f"select side from {VPOS_TABLE} where id=?", (pid,),
        ).fetchone()[0]
        if side == "long":
            sl_price = open_ * (1 - sl_pct)
            tp_price = open_ * (1 + TP_R_MULT * sl_pct)
        else:
            sl_price = open_ * (1 + sl_pct)
            tp_price = open_ * (1 - TP_R_MULT * sl_pct)
        wdb.execute(
            f"update {VPOS_TABLE} set status='open', entry_price=?, "
            "entry_time=?, size=?, sl_price=?, tp_price=? where id=?",
            (open_, ts, size, sl_price, tp_price, pid),
        )
        filled += 1
        print(f"jev: VIRTUAL {side} {sym} filled @{open_} "
              f"(sl={sl_price:.4f} tp={tp_price:.4f})")
    if filled:
        wdb.commit()
    return filled


def _record_virtual_trade(ldb: sqlite3.Connection, sym: str, side: str,
                          entry: float, exit_: float, entry_t: int,
                          exit_t: int, size: float, pnl: float,
                          reason: str, meta: Dict[str, Any]) -> int:
    """Insert a closed-trade row into live bot.db trades + strategy_pnl."""
    fee = entry * size * TAKER_FEE + exit_ * size * TAKER_FEE
    pnl_pct = pnl / (entry * size) if entry and size else 0.0
    cur = ldb.execute(
        """INSERT INTO trades
        (symbol, side, entry_price, exit_price, entry_time, exit_time,
         size, pnl_usd, pnl_pct, strategy, exit_reason, status,
         signal_metadata, entry_fee, cumulative_order_fee)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sym, side, entry, exit_, entry_t, exit_t, size, pnl, pnl_pct,
         "JevJudge", reason, "closed", json.dumps(meta), fee, fee),
    )
    trade_id = cur.lastrowid
    ldb.execute(
        """INSERT INTO strategy_pnl
        (strategy, symbol, side, pnl_usd, pnl_pct, size,
         entry_time, exit_time, exit_reason, trade_id, is_win)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("JevJudge", sym, side, pnl, pnl_pct, size, entry_t, exit_t,
         reason, trade_id, 1 if pnl > 0 else 0),
    )
    ldb.commit()
    return int(trade_id)


def _check_virtual_exits(db: sqlite3.Connection, wdb: sqlite3.Connection,
                         ldb: sqlite3.Connection) -> int:
    """Walk open virtual positions against candles_1m; close on SL/TP/hold.

    Conservative intrabar rule: SL checked before TP within the same bar.
    Idempotent — if the live-DB insert fails the position stays 'open' and
    the same exit is rediscovered next run.
    """
    closed = 0
    opens = wdb.execute(
        f"select id, decision_id, symbol, side, entry_price, entry_time, "
        f"size, notional, sl_price, tp_price, max_hold_ms from {VPOS_TABLE} "
        "where status='open'",
    ).fetchall()
    for (pid, did, sym, side, entry, entry_t, size, notional,
         sl, tp, max_hold) in opens:
        candles = db.execute(
            "select timestamp_ms, high, low, close from candles_1m "
            "where symbol=? and timestamp_ms >= ? order by timestamp_ms",
            (sym, entry_t),
        ).fetchall()
        exit_px, exit_t, reason = None, None, None
        for ts, hi, lo, cl in candles:
            if side == "long":
                if lo <= sl:
                    exit_px, exit_t, reason = sl, ts, "sl"
                    break
                if hi >= tp:
                    exit_px, exit_t, reason = tp, ts, "tp"
                    break
            else:
                if hi >= sl:
                    exit_px, exit_t, reason = sl, ts, "sl"
                    break
                if lo <= tp:
                    exit_px, exit_t, reason = tp, ts, "tp"
                    break
            if ts - entry_t >= max_hold:
                exit_px, exit_t, reason = cl, ts, "max_hold"
                break
        if exit_px is None:
            continue
        sign = 1.0 if side == "long" else -1.0
        gross = (exit_px - entry) * size * sign
        fees = (entry + exit_px) * size * TAKER_FEE
        pnl = gross - fees
        meta = {"virtual": True, "experiment": "jev-shadow",
                "decision_id": did, "vpos_id": pid}
        try:
            tid = _record_virtual_trade(
                ldb, sym, side, entry, exit_px, entry_t, exit_t,
                size, pnl, reason, meta,
            )
        except sqlite3.Error as exc:
            print(f"jev: VIRTUAL close {sym} trade insert failed: {exc}")
            continue
        wdb.execute(
            f"update {VPOS_TABLE} set status='closed', exit_price=?, "
            "exit_time=?, exit_reason=?, pnl_usd=?, trade_id=? where id=?",
            (exit_px, exit_t, reason, pnl, tid, pid),
        )
        closed += 1
        print(f"jev: VIRTUAL {side} {sym} closed @{exit_px} "
              f"reason={reason} pnl={pnl:+.2f} -> trades#{tid}")
    if closed:
        wdb.commit()
    return closed


def _maybe_open_virtual(wdb: sqlite3.Connection, symbol: str,
                        decision_id: int, side: str, conf: float,
                        atr_pct: Optional[float], ts_ms: int) -> bool:
    """Create a pending virtual position if confidence clears the bar."""
    if conf < ENTRY_CONF_MIN or side not in ("long", "short"):
        return False
    live = wdb.execute(
        f"select count(*) from {VPOS_TABLE} where symbol=? and "
        "status in ('pending','open')", (symbol,),
    ).fetchone()[0]
    if live:
        return False
    atr_frac = (atr_pct or 0.0) / 100.0
    sl_pct = max(SL_FRACTION_MIN, SL_ATR_MULT * atr_frac)
    wdb.execute(
        f"insert into {VPOS_TABLE} (decision_id, symbol, side, notional, "
        "sl_pct, max_hold_ms, status, created_ms) values (?,?,?,?,?,?,?,?)",
        (decision_id, symbol, side, VIRTUAL_NOTIONAL_USD, sl_pct,
         MAX_HOLD_MS, "pending", ts_ms),
    )
    wdb.commit()
    print(f"jev: VIRTUAL {side} {symbol} queued (conf={conf:.2f} "
          f"sl_pct={sl_pct * 100:.2f}%)")
    return True


def one_pass(symbols: List[str], *, dry_run: bool, daily_cap: int,
             max_tokens: int, ask_interval_s: int,
             paper: bool) -> Dict[str, int]:
    stats = {"asked": 0, "inserted": 0, "skipped": 0, "errors": 0,
             "v_filled": 0, "v_closed": 0, "v_opened": 0}
    api_key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
    usage = _load_usage()

    if not LIVE_DB.exists():
        print(f"jev: live DB missing at {LIVE_DB}")
        return stats
    db = sqlite3.connect(str(LIVE_DB), timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    rdb = sqlite3.connect(f"file:{_research_db_path()}?mode=ro", uri=True)
    wdb = sqlite3.connect(str(_research_db_path()), timeout=30)
    wdb.execute("PRAGMA journal_mode=WAL")
    wdb.execute(CREATE_SQL)
    wdb.execute(CREATE_VPOS_SQL)
    wdb.commit()

    # Virtual-paper housekeeping runs every pass — cheap local DB work.
    if paper:
        try:
            stats["v_filled"] = _fill_pending_entries(db, wdb)
            stats["v_closed"] = _check_virtual_exits(db, wdb, db)
        except sqlite3.Error as exc:
            print(f"jev: virtual paper housekeeping failed: {exc}")
            stats["errors"] += 1

    if usage.get("input_tokens", 0) + usage.get("output_tokens", 0) >= max_tokens:
        print(f"jev: token budget exhausted ({usage}) — judge skipped")
        return stats

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_start = int(datetime.strptime(today, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp() * 1000)
    done_today = wdb.execute(
        f"select count(*) from {TABLE} where ts_ms >= ?", (day_start,),
    ).fetchone()[0]
    if done_today >= daily_cap:
        print(f"jev: daily cap reached ({done_today}/{daily_cap})")
        return stats

    now_ms = int(time.time() * 1000)
    for sym in symbols:
        if done_today + stats["asked"] >= daily_cap:
            break
        # one Jev question per symbol per ask_interval — extra runs only
        # manage virtual exits, they do not spend API calls
        last_ask = wdb.execute(
            f"select max(ts_ms) from {TABLE} where symbol=? and error is null",
            (sym,),
        ).fetchone()[0]
        if last_ask and now_ms - last_ask < ask_interval_s * 1000:
            continue
        ts_ms = int(time.time() * 1000)
        try:
            state = build_state(db, rdb, sym)
        except Exception as exc:  # noqa: BLE001
            print(f"jev: state build {sym} failed: {exc}")
            stats["errors"] += 1
            continue
        if state is None:
            stats["skipped"] += 1
            continue
        if dry_run:
            print(f"jev: DRY-RUN {sym} state={json.dumps(state)[:400]}")
            continue
        if not api_key:
            print("jev: TYPESAFE_API_KEY not set — set it and rerun")
            return stats
        try:
            resp = _call_jev(api_key, state)
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, ValueError) as exc:
            _insert(wdb, sym, ts_ms, state, None, str(exc))
            stats["errors"] += 1
            print(f"jev: call {sym} failed: {exc}")
            continue
        _insert(wdb, sym, ts_ms, state, resp, None)
        decision_id = wdb.execute(
            f"select id from {TABLE} where symbol=? and ts_ms=?",
            (sym, ts_ms),
        ).fetchone()[0]
        u = resp.get("usage") or {}
        usage["calls"] = usage.get("calls", 0) + 1
        usage["input_tokens"] = usage.get("input_tokens", 0) + int(u.get("input_tokens") or 0)
        usage["output_tokens"] = usage.get("output_tokens", 0) + int(u.get("output_tokens") or 0)
        _save_usage(usage)
        stats["asked"] += 1
        stats["inserted"] += 1
        a = (resp.get("answers") or {}).get("action") or {}
        print(f"jev: {sym} -> action={a.get('choice')} conf={a.get('confidence')} "
              f"tok={u.get('input_tokens')}+{u.get('output_tokens')}")
        if paper:
            atr = ((state.get("volatility") or {}).get("atr_pct_15m"))
            if _maybe_open_virtual(
                    wdb, sym, decision_id, str(a.get("choice") or ""),
                    float(a.get("confidence") or 0.0), atr, ts_ms):
                stats["v_opened"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-paper", action="store_true",
                    help="judge only — no virtual paper trades")
    ap.add_argument("--symbols", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--daily-cap", type=int, default=96)
    ap.add_argument("--max-tokens", type=int, default=2_000_000)
    ap.add_argument("--ask-interval-s", type=int, default=3600,
                    help="min seconds between Jev questions per symbol")
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"jev: pass -> {one_pass(symbols, dry_run=args.dry_run, daily_cap=args.daily_cap, max_tokens=args.max_tokens, ask_interval_s=args.ask_interval_s, paper=not args.no_paper)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
