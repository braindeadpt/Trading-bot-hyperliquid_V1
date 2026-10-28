#!/usr/bin/env python3
"""Standalone collector: per-wallet fills for tracked top-trader wallets.

Motivation (research): "Public Trader Identity" (Zhai 2026, arXiv
2608.04373) shows on Hyperliquid that scoring wallets by the
notional-weighted markout of their AGGRESSIVE (taker) orders produces a
persistent toxicity ranking (rank corr 0.52 across 10d windows) whose
top tail predicts short-horizon returns. Our existing bias feed only
stores per-symbol aggregate POSITION snapshots — this collector adds the
per-fill granularity the markout methodology needs.

Data path: POST /info {"type":"userFillsByTime","user":addr,
"startTime":ms} is public and returns each wallet's fills in the window,
oldest-first, capped at 2000 rows/call. A per-wallet cursor
(``wallet_fills_cursor.max_time_ms``) pages forward incrementally with a
60s overlap; the cursor commits in the SAME transaction as the page's
inserts, so a SIGINT mid-pass loses at most the uncommitted page and the
next pass resumes — no loss, no duplicates (UNIQUE dedups the overlap).

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
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.research_database import ResearchDatabase  # noqa: E402
from src.utils.config import load_config  # noqa: E402

INFO_URL = "https://api.hyperliquid.xyz/info"
WALLETS_PATH = ROOT / "data" / "research" / "top_traders.json"
TABLE = "top_trader_fills"
CURSOR_TABLE = "wallet_fills_cursor"

# userFillsByTime returns the OLDEST 2000 fills in [startTime, endTime),
# ascending (verified against mainnet 2026-10-06). Page forward by
# re-requesting startTime = last page's max time_ms — the boundary row
# refetches and dedups.
PAGE_CAP = 2000
# Refetch this far behind the cursor — tolerates late-arriving rows and
# guarantees the page-boundary row is re-checked. UNIQUE dedups.
OVERLAP_MS = 60_000
# A wallet mid-backfill must not eat the whole pass: each wallet gets at
# most this many pages per pass; the cursor makes the rest resumable.
MAX_PAGES_PER_WALLET = 10
# Stop serving new wallets this many seconds into a --once pass so the run
# exits cleanly before the hourly cron's SIGINT instead of dying mid-fetch
# mid-page. Unserved wallets resume next pass (cursor sort is fair).
MAX_PASS_SEC = 45 * 60

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

CURSOR_SQL = f"""
CREATE TABLE IF NOT EXISTS {CURSOR_TABLE} (
    wallet        TEXT PRIMARY KEY,
    max_time_ms   INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
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
    conn.execute(CURSOR_SQL)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_ttf_coin_time ON {TABLE}(coin, time_ms);"
    )
    # idx_ttf_wallet_time (wallet, time_ms) is deliberately NOT created. Measured
    # 2026-10-06 with dbstat on a 14 GB research DB: it weighed 1.33 GB (9.5% of
    # the file, ~74 MB/day) because every key carries a 42-char wallet address,
    # versus 0.31 GB for idx_ttf_coin_time. No code reads this table by wallet:
    # the only consumer (wallet_markout_screen.py) filters on coin, and this
    # collector only INSERT OR IGNOREs against the UNIQUE(wallet, tid, hash)
    # autoindex. If a per-wallet reader is ever added, recreate it then.
    # Leaving the CREATE here would silently rebuild the index on the next
    # hourly --once run after a DROP.
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


def _read_body(r, budget_sec: float) -> bytes:
    """Read a response body with a TOTAL wall-clock budget. urllib's
    timeout is per socket recv — a server slow-dripping a multi-MB body
    (observed: leaderboard GET stalled a whole pass >7 min on the
    congested Mac host, 2026-10-06) never trips it. Chunked reads let us
    cut the transfer when the total budget expires."""
    chunks: List[bytes] = []
    t0 = time.monotonic()
    while True:
        chunk = r.read(1 << 20)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        if time.monotonic() - t0 > budget_sec:
            raise TimeoutError(
                f"body read exceeded {budget_sec:.0f}s total budget"
            )


def _leaderboard_rows() -> List[Dict[str, Any]]:
    try:
        req = urllib.request.Request(
            LEADERBOARD_URL,
            headers={"User-Agent": "hl-premium-bot/wallet-fills-collector"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(_read_body(r, 120).decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"wallet_fills: leaderboard fetch failed: {exc}")
        return []
    return data.get("leaderboardRows") or []


def _leaderboard_top(rows: List[Dict[str, Any]], metric: str,
                     window: str, n: int) -> List[str]:
    """Top-n leaderboard wallets by ``metric`` (vlm/pnl) over ``window``.

    The markout-ranking methodology needs cohort DISPERSION, not a
    pre-selected winner set: high-volume wallets contribute fill density
    (and contain the toxic tail), high-PnL wallets contribute the
    informed-candidate tail. The union of both rankings is the universe.
    """
    scored = []
    for row in rows:
        try:
            addr = str(row.get("ethAddress") or "").strip().lower()
            if not (addr.startswith("0x") and len(addr) >= 42):
                continue
            val = 0.0
            for wname, perf in row.get("windowPerformances") or []:
                if wname == window:
                    val = float(perf.get(metric) or 0.0)
            scored.append((val, addr))
        except (TypeError, ValueError):
            continue
    scored.sort(reverse=True)
    return [a for _v, a in scored[: max(1, n)]]


def _universe(tracked_only: bool) -> List[str]:
    wallets = set(_load_wallets())
    if not tracked_only:
        rows = _leaderboard_rows()
        wallets.update(_leaderboard_top(rows, "vlm", "allTime", 150))
        wallets.update(_leaderboard_top(rows, "pnl", "month", 100))
    return sorted(wallets)


def _fetch_fills_by_time(
    wallet: str, start_ms: int, *, retries: int = 3
) -> Optional[List[Dict[str, Any]]]:
    """One userFillsByTime page: oldest <=2000 fills in [start_ms, now),
    ascending. None on failure (caller keeps the cursor and retries the
    same window next pass)."""
    body = json.dumps(
        {"type": "userFillsByTime", "user": wallet, "startTime": int(start_ms)}
    ).encode()
    for attempt in range(retries):
        req = urllib.request.Request(
            INFO_URL, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            # Generous TOTAL budget: on the congested Mac host a 2000-row
            # page slow-drips for 30-70s and still delivers (measured
            # 2026-10-06). Killing it earlier just restarts the drip; the
            # socket timeout alone is per-recv and never trips on a drip.
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(_read_body(r, 240).decode())
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


def _fill_row(wallet: str, f: Dict[str, Any], ingested: int):
    try:
        return (
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
        )
    except (KeyError, TypeError, ValueError):
        return None


def _apply_page(
    conn: sqlite3.Connection, wallet: str, fills: List[Dict[str, Any]]
) -> Tuple[int, Optional[int]]:
    """Insert one fetched page AND advance the wallet cursor in the same
    transaction. A kill between the two is impossible: either the page and
    the new cursor commit together, or both roll back and the next pass
    refetches the window (deduped). Returns (inserted, page_max)."""
    ingested = int(time.time() * 1000)
    rows = [r for r in (_fill_row(wallet, f, ingested) for f in fills) if r]
    times = []
    for f in fills:
        try:
            times.append(int(f["time"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not times:
        return 0, None  # unusable page — leave the cursor where it is
    page_max = max(times)
    with conn:  # atomic: rows + cursor commit or roll back together
        inserted = 0
        if rows:
            cur = conn.executemany(
                f"""INSERT OR IGNORE INTO {TABLE}
                    (wallet, coin, side, px, sz, time_ms, crossed, dir,
                     closed_pnl, fee, oid, tid, hash, twap_id, ingested_at_ms)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            inserted = cur.rowcount
        conn.execute(
            f"""INSERT INTO {CURSOR_TABLE} (wallet, max_time_ms, updated_at_ms)
                VALUES (?,?,?)
                ON CONFLICT(wallet) DO UPDATE SET
                    max_time_ms = MAX({CURSOR_TABLE}.max_time_ms,
                                      excluded.max_time_ms),
                    updated_at_ms = excluded.updated_at_ms""",
            (wallet, page_max, ingested),
        )
    return inserted, page_max


def _load_cursors(conn: sqlite3.Connection) -> Dict[str, int]:
    return {
        str(w): int(t)
        for w, t in conn.execute(
            f"SELECT wallet, max_time_ms FROM {CURSOR_TABLE}"
        )
    }


def _collect_wallet(
    conn: sqlite3.Connection,
    wallet: str,
    cursor_ms: int,
    *,
    fetch_fn,
    sleep_fn,
) -> Dict[str, Any]:
    """Page forward from the cursor until caught up or page budget spent."""
    out = {"pages": 0, "fetched": 0, "inserted": 0, "error": False}
    start = max(0, cursor_ms - OVERLAP_MS)
    for _ in range(MAX_PAGES_PER_WALLET):
        fills = fetch_fn(wallet, start)
        if fills is None:
            out["error"] = True
            return out
        out["pages"] += 1
        out["fetched"] += len(fills)
        if not fills:
            break
        inserted, page_max = _apply_page(conn, wallet, fills)
        out["inserted"] += inserted
        if page_max is None or len(fills) < PAGE_CAP:
            break  # unusable page, or short page = caught up to present
        # Full page: keep walking forward. The boundary row is refetched
        # (deduped); +1ms only if the page couldn't advance the window.
        start = page_max if page_max > start else start + 1
        sleep_fn(0.4)
    return out


def one_pass(
    conn: sqlite3.Connection,
    *,
    tracked_only: bool,
    fetch_fn=None,
    sleep_fn=time.sleep,
) -> Dict[str, int]:
    wallets = _universe(tracked_only)
    cursors = _load_cursors(conn)
    # Oldest cursor first: wallets never fetched (cursor 0) lead, and no
    # position in the sorted list can be permanently starved by a pass
    # that dies early — the fairest rotation the cursor table gives us.
    wallets.sort(key=lambda w: cursors.get(w, 0))
    stats = {
        "wallets": len(wallets),
        "fetched_wallets": 0,
        "pages": 0,
        "fetched": 0,
        "inserted": 0,
        "errors": 0,
    }
    fetch = fetch_fn or _fetch_fills_by_time
    t0 = time.monotonic()
    for w in wallets:
        if time.monotonic() - t0 > MAX_PASS_SEC:
            print("wallet_fills: pass budget reached; remaining wallets "
                  "resume next pass")
            break
        try:
            r = _collect_wallet(
                conn, w, cursors.get(w, 0), fetch_fn=fetch, sleep_fn=sleep_fn
            )
        except sqlite3.Error as exc:
            r = {"pages": 0, "fetched": 0, "inserted": 0, "error": True}
            print(f"wallet_fills: insert {w[:10]}.. failed: {exc}")
        stats["pages"] += r["pages"]
        stats["fetched"] += r["fetched"]
        stats["inserted"] += r["inserted"]
        if r["error"]:
            stats["errors"] += 1
        else:
            stats["fetched_wallets"] += 1
        sleep_fn(1.0)  # gentle pacing; info endpoint weight-limited
    return stats


def main() -> int:
    # urllib's per-request timeout does not cover a stalled DNS lookup or a
    # hung TLS handshake — on this host a single leaderboard GET blocked a
    # pass for >7 min (observed 2026-10-06). A global default puts a hard
    # ceiling on every socket op in this process.
    import socket
    socket.setdefaulttimeout(120)

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
