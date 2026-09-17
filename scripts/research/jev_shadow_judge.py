#!/usr/bin/env python3
"""Jev shadow judge — TypeSafe System One trading-judgment experiment.

Hypothesis (honest): a general-purpose judgment model (Jev) given a compact
computed market state may produce directional probabilities with predictive
signal. Prior is LOW — it has no market training data beyond what we feed it.
This script MEASURES that hypothesis; it never trades.

Design (mirrors wallet_fills_collector — standalone, zero engine contact):
  * runs on a schedule (hourly via Hyperliquid-Jev-Shadow task)
  * builds a compact feature state per symbol from live bot.db candles
    (read-only) + top_trader_bias_samples from the research DB
  * asks Jev ONE request per symbol containing a fan-out of independent
    questions (per TypeSafe docs): Choice action {long/short/flat},
    Noul long_edge, Noul short_edge, Choice regime
  * persists everything to `jev_decisions` for offline evaluation
    (scripts/research/jev_eval.py)

Cost discipline: hard daily call cap + cumulative token ledger in
data/research/jev_usage.json. With $5 of credits the pilot stays bounded;
`--daily-cap` and `--max-tokens` control the burn.

Usage:
    python -X utf8 scripts/research/jev_shadow_judge.py --once        # one pass
    python -X utf8 scripts/research/jev_shadow_judge.py --dry-run     # print request, no call
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


def one_pass(symbols: List[str], *, dry_run: bool, daily_cap: int,
             max_tokens: int) -> Dict[str, int]:
    stats = {"asked": 0, "inserted": 0, "skipped": 0, "errors": 0}
    api_key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
    usage = _load_usage()
    if usage.get("input_tokens", 0) + usage.get("output_tokens", 0) >= max_tokens:
        print(f"jev: token budget exhausted ({usage}) — stopping")
        return stats

    if not LIVE_DB.exists():
        print(f"jev: live DB missing at {LIVE_DB}")
        return stats
    db = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    rdb = sqlite3.connect(f"file:{_research_db_path()}?mode=ro", uri=True)
    wdb = sqlite3.connect(str(_research_db_path()), timeout=30)
    wdb.execute("PRAGMA journal_mode=WAL")
    wdb.execute(CREATE_SQL)
    wdb.commit()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_start = int(datetime.strptime(today, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp() * 1000)
    done_today = wdb.execute(
        f"select count(*) from {TABLE} where ts_ms >= ?", (day_start,),
    ).fetchone()[0]
    if done_today >= daily_cap:
        print(f"jev: daily cap reached ({done_today}/{daily_cap})")
        return stats

    for sym in symbols:
        if done_today + stats["asked"] >= daily_cap:
            break
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
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--symbols", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--daily-cap", type=int, default=96)
    ap.add_argument("--max-tokens", type=int, default=2_000_000)
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"jev: pass -> {one_pass(symbols, dry_run=args.dry_run, daily_cap=args.daily_cap, max_tokens=args.max_tokens)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
