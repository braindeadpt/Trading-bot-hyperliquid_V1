#!/usr/bin/env python3
"""Standalone collector: per-wallet fills for tracked top-trader wallets.

Motivation (research): "Public Trader Identity" (Zhai 2026, arXiv
2608.04373) shows on Hyperliquid that scoring wallets by the
notional-weighted markout of their AGGRESSIVE (taker) orders produces a
persistent toxicity ranking (rank corr 0.52 across 10d windows) whose
top tail predicts short-horizon returns. Our existing bias feed only
stores per-symbol aggregate POSITION snapshots — this collector adds the
per-fill granularity the markout methodology needs.

Data path: POST /info {"type":"userFills","user":addr} is public and
returns each wallet's recent fills including the ``crossed`` flag
(taker/maker) and L1 tx hash. Dedup by (wallet, tid, hash).

Runs self-contained: reads the same wallets file the live tracker
refreshes (data/research/top_traders.json), writes to the configured
research DB via ResearchDatabase.resolve_path (E: drive). Read-only for
everything outside its own table — zero interaction with the live
engine, safe to run while the Phase-10 window is live.

Usage:
    python -X utf8 scripts/research/wallet_fills_collector.py            # loop, 60s
    python -X utf8 scripts/research/wallet_fills_collector.py --once     # one pass
    python -X utf8 scripts/research/wallet_fills_collector.py --interval-sec 120
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.research_database import ResearchDatabase  # noqa: E402
from src.utils.config import load_config  # noqa: E402

INFO_URL = "https://api.hyperliquid.xyz/info"
WALLETS_PATH = ROOT / "data" / "research" / "top_traders.json"
TABLE = "top_trader_fills"

CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet          TEXT    NOT NULL,
    coin            TEXT    NOT NULL,
    side            TEXT    NOT NULL,   -- 'B'|'A' (buy/sell)
    px              REAL    NOT NULL,
    sz              REAL    NOT NULL,
    time_ms         INTEGER NOT NULL,
    crossed         INTEGER NOT NULL,   -- 1 = taker (aggressive)
    dir             TEXT,
    closed_pnl      REAL,
    fee             REAL,
    oid             INTEGER,
    tid             INTEGER,
    hash            TEXT,
    twap_id         INTEGER,
    ingested_at_ms  INTEGER NOT NULL,
    UNIQUE(wallet, tid, hash)
);
"""


def _research_db_path() -> Path:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    return Path(ResearchDatabase.resolve_path(cfg))


def _open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(CREATE_SQL)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_ttf_coin_time ON {TABLE}(coin, time_ms);"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_ttf_wallet_time ON {TABLE}(wallet, time_ms);"
    )
    conn.commit()
    return conn


LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def _load_wallets() -> List[str]:
    try:
        raw = json.loads(WALLETS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"wallet_fills: cannot read {WALLETS_PATH}: {exc}")
        return []
    ws = raw.get("wallets", []) if isinstance(raw, dict) else raw
    out: List[str] = []
    for w in ws:
        if isinstance(w, str):
            out.append(w.lower())
        elif isinstance(w, dict):
            a = w.get("address") or w.get("wallet") or w.get("account")
            if a:
                out.append(str(a).lower())
    return out


def _leaderboard_top_volume(n: int) -> List[str]:
    """Top-n wallets by allTime volume from the public HL leaderboard.

    The tracked-file set (~10 "consistent durable" winners) is too small
    and too PnL-selected for the markout-ranking methodology — the toxic-
    tail concept needs a broader cohort. High-volume wallets maximise
    fill density per poll regardless of their PnL sign.
    """
    try:
        req = urllib.request.Request(
            LEADERBOARD_URL,
            headers={"User-Agent": "hl-premium-bot/wallet-fills-collector"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"wallet_fills: leaderboard fetch failed: {exc}")
        return []
    rows = data.get("leaderboardRows") or []
    scored = []
    for row in rows:
        try:
            addr = str(row.get("ethAddress") or "").strip().lower()
            if not (addr.startswith("0x") and len(addr) >= 42):
                continue
            perfs = row.get("windowPerformances") or []
            vlm = 0.0
            for wname, perf in perfs:
                if wname == "allTime":
                    vlm = float(perf.get("vlm") or 0.0)
            scored.append((vlm, addr))
        except (TypeError, ValueError):
            continue
    scored.sort(reverse=True)
    return [a for _v, a in scored[: max(1, n)]]


def _universe(tracked_only: bool) -> List[str]:
    wallets = set(_load_wallets())
    if not tracked_only:
        wallets.update(_leaderboard_top_volume(60))
    return sorted(wallets)


def _fetch_fills(wallet: str, *, retries: int = 3) -> Optional[List[Dict[str, Any]]]:
    body = json.dumps({"type": "userFills", "user": wallet}).encode()
    for attempt in range(retries):
        req = urllib.request.Request(
            INFO_URL, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
            return data if isinstance(data, list) else None
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries - 1:
                time.sleep(5.0 * (attempt + 1))  # HL info is weight-limited
                continue
            print(f"wallet_fills: fetch {wallet[:10]}.. failed: {exc}")
            return None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print(f"wallet_fills: fetch {wallet[:10]}.. failed: {exc}")
            return None
    return None


def _insert_fills(conn: sqlite3.Connection, wallet: str,
                  fills: List[Dict[str, Any]]) -> int:
    ingested = int(time.time() * 1000)
    rows = []
    for f in fills:
        try:
            rows.append((
                wallet,
                str(f["coin"]).upper(),
                str(f["side"]),
                float(f["px"]),
                float(f["sz"]),
                int(f["time"]),
                1 if f.get("crossed") else 0,
                str(f.get("dir") or ""),
                float(f["closedPnl"]) if f.get("closedPnl") not in (None, "") else None,
                float(f["fee"]) if f.get("fee") not in (None, "") else None,
                int(f["oid"]) if f.get("oid") is not None else None,
                int(f["tid"]) if f.get("tid") is not None else None,
                str(f.get("hash") or ""),
                int(f["twapId"]) if f.get("twapId") is not None else None,
                ingested,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return 0
    cur = conn.executemany(
        f"""INSERT OR IGNORE INTO {TABLE}
            (wallet, coin, side, px, sz, time_ms, crossed, dir, closed_pnl,
             fee, oid, tid, hash, twap_id, ingested_at_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return cur.rowcount


def one_pass(conn: sqlite3.Connection, *, tracked_only: bool) -> Dict[str, int]:
    wallets = _universe(tracked_only)
    stats = {"wallets": len(wallets), "fetched": 0, "inserted": 0, "errors": 0}
    for w in wallets:
        fills = _fetch_fills(w)
        if fills is None:
            stats["errors"] += 1
            continue
        stats["fetched"] += 1
        try:
            stats["inserted"] += _insert_fills(conn, w, fills)
        except sqlite3.Error as exc:
            stats["errors"] += 1
            print(f"wallet_fills: insert {w[:10]}.. failed: {exc}")
        time.sleep(1.0)  # gentle pacing; info endpoint weight-limited
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval-sec", type=int, default=60)
    ap.add_argument("--tracked-only", action="store_true",
                    help="poll only the tracker's wallet file (no leaderboard top-volume merge)")
    args = ap.parse_args()

    db_path = _research_db_path()
    conn = _open_db(db_path)
    print(f"wallet_fills: db={db_path}")

    if args.once:
        print(f"wallet_fills: pass -> {one_pass(conn, tracked_only=args.tracked_only)}")
        return 0

    while True:
        try:
            stats = one_pass(conn, tracked_only=args.tracked_only)
            print(f"wallet_fills: {time.strftime('%H:%M:%S')} -> {stats}",
                  flush=True)
        except Exception as exc:  # noqa: BLE001 — keep the daemon alive
            print(f"wallet_fills: pass error: {exc}", flush=True)
        time.sleep(max(15, args.interval_sec))


if __name__ == "__main__":
    sys.exit(main())
