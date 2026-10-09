#!/usr/bin/env python3
"""Shared public Hyperliquid `info` history layer for Phase-A feasibility
studies (A1 carry, A2 funding-extreme MR).

Scope: research-only downloader + SQLite cache. Everything is public market
data — no keys, no engine contact, no writes to any bot DB.

Facts baked in (verified 2026-10-06):
  * `fundingHistory` returns hourly rows, 500/page, paginate via startTime.
  * `candleSnapshot` serves intervals >= 2h only ('1h'/'15m'/'1m' return
    empty). Spot pairs are queryable by their `spotMeta` universe name
    ('@142', 'PURR/USDC').
  * `l2Book` accepts perp names AND spot pair names.
  * `metaAndAssetCtxs` enumerates perps (':'-names are HIP-3, skipped).
  * `spotMeta` enumerates spot tokens/pairs.

The cache is append-only SQLite keyed (symbol, ts_ms): re-runs reuse it via
`--skip-download` (or automatically resume when interrupted).
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HL_INFO = "https://api.hyperliquid.xyz/info"
SLEEP_SEC = 0.06
UA = "hl-phaseA-feasibility/1.0"


def hl_post(payload: dict, retries: int = 8) -> Any:
    last: Optional[Exception] = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                HL_INFO, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "User-Agent": UA},
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001 — network retry
            last = e
            if "429" in str(e) or "Too Many" in str(e):
                time.sleep(min(30.0, 2.0 * (i + 1) ** 2))
            else:
                time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"HL post failed after {retries} tries: {last}")


# ─── universe enumeration ────────────────────────────────────────────────────

def fetch_perp_universe() -> List[Dict[str, Any]]:
    """metaAndAssetCtxs → perp names (':' HIP-3 excluded) + current ctx."""
    meta, ctxs = hl_post({"type": "metaAndAssetCtxs"})
    out = []
    for u, ctx in zip(meta["universe"], ctxs):
        name = str(u["name"])
        if ":" in name:
            continue
        out.append({
            "name": name,
            "is_delisted": bool(u.get("isDelisted")),
            "day_ntl_vlm": float(ctx.get("dayNtlVlm") or 0),
            "mark_px": float(ctx.get("markPx") or 0),
            "open_interest": float(ctx.get("openInterest") or 0),
        })
    return out


def fetch_spot_pairs() -> Tuple[List[Dict[str, Any]], Dict[int, str]]:
    """spotMeta → (universe entries, token_index→name map).

    Each entry: {'pair_name', 'index', 'base_name', 'quote_name'} for
    base/USDC pairs only (the carry study only cares about USDC legs).
    """
    sm = hl_post({"type": "spotMeta"})
    tok_name = {int(t["index"]): str(t["name"]) for t in sm["tokens"]}
    pairs = []
    for u in sm["universe"]:
        base_i, quote_i = int(u["tokens"][0]), int(u["tokens"][1])
        if tok_name.get(quote_i) != "USDC":
            continue
        pairs.append({
            "pair_name": str(u["name"]),
            "index": int(u["index"]),
            "base_name": tok_name.get(base_i, f"#{base_i}"),
        })
    return pairs, tok_name


def fetch_l2_half_spread_bps(coin: str) -> Optional[float]:
    """Current top-of-book half-spread in bps; None if book empty/error."""
    try:
        b = hl_post({"type": "l2Book", "coin": coin})
        levels = b.get("levels") or [[], []]
        if not levels[0] or not levels[1]:
            return None
        bb, ba = float(levels[0][0]["px"]), float(levels[1][0]["px"])
        return (ba - bb) / (ba + bb) * 1e4
    except Exception:
        return None


# ─── history pulls (paginated, cache-friendly) ───────────────────────────────

def fetch_funding(coin: str, start_ms: int, end_ms: int) -> List[Tuple[int, float]]:
    """fundingHistory hourly rows → [(ts_ms, funding_rate)] ascending."""
    out: Dict[int, float] = {}
    cursor = int(start_ms)
    for _ in range(400):
        fh = hl_post({"type": "fundingHistory", "coin": coin, "startTime": cursor})
        if not fh:
            break
        for r in fh:
            ts = int(r["time"])
            if start_ms <= ts <= end_ms:
                out[ts] = float(r["fundingRate"])
        last = int(fh[-1]["time"])
        if last <= cursor or last >= end_ms:
            break
        cursor = last + 1
        time.sleep(SLEEP_SEC)
    return sorted(out.items())


def fetch_candles(coin: str, interval: str, start_ms: int, end_ms: int,
                  page_ms: int) -> List[Tuple[int, float, float, float, float, float, int]]:
    """candleSnapshot → [(ts_ms,o,h,l,c,v,n)]. Windowed pagination because the
    API caps rows per call; `page_ms` must be < cap × interval_ms.
    For 2h use page_ms = 60d (720 bars/call, well under the ~1500 cap)."""
    out: Dict[int, Tuple] = {}
    cursor = int(start_ms)
    while cursor < end_ms:
        page_end = min(cursor + page_ms, end_ms)
        bars = hl_post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval,
            "startTime": cursor, "endTime": page_end,
        }})
        for b in bars or []:
            ts = int(b["t"])
            if start_ms <= ts <= end_ms:
                out[ts] = (float(b["o"]), float(b["h"]), float(b["l"]),
                           float(b["c"]), float(b.get("v") or 0), int(b.get("n") or 0))
        cursor = page_end + 1
        time.sleep(SLEEP_SEC)
    return [(ts,) + v for ts, v in sorted(out.items())]


# ─── sqlite cache ────────────────────────────────────────────────────────────

def init_cache(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS funding (
            symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            funding_rate REAL NOT NULL,
            PRIMARY KEY (symbol, ts_ms));
        CREATE TABLE IF NOT EXISTS candles (
            symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, n_trades INTEGER,
            PRIMARY KEY (symbol, ts_ms));
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    con.commit()


def cache_put_funding(con: sqlite3.Connection, symbol: str,
                      rows: List[Tuple[int, float]]) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO funding (symbol,ts_ms,funding_rate) VALUES (?,?,?)",
        [(symbol, ts, fr) for ts, fr in rows])
    con.commit()


def cache_put_candles(con: sqlite3.Connection, symbol: str, rows) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO candles (symbol,ts_ms,open,high,low,close,volume,n_trades)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(symbol,) + tuple(r) for r in rows])
    con.commit()


def cache_symbols(con: sqlite3.Connection, table: str) -> set:
    return {r[0] for r in con.execute(f"SELECT DISTINCT symbol FROM {table}")}


def cache_get_funding(con: sqlite3.Connection, symbol: str) -> List[Tuple[int, float]]:
    return con.execute(
        "SELECT ts_ms, funding_rate FROM funding WHERE symbol=? ORDER BY ts_ms",
        (symbol,)).fetchall()


def cache_get_candles(con: sqlite3.Connection, symbol: str):
    return con.execute(
        "SELECT ts_ms,open,high,low,close,volume,n_trades FROM candles "
        "WHERE symbol=? ORDER BY ts_ms", (symbol,)).fetchall()


def meta_get(con: sqlite3.Connection, key: str) -> Optional[str]:
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (key, value))
    con.commit()
