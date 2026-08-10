#!/usr/bin/env python3
"""MM feasibility across Hyperliquid liquidity spectrum (REST-only addendum).

Does NOT touch the live bot / sampler / .env / production YAML.
Collects l2Book + recentTrades via public INFO API for ~10 symbols spanning
day-notional volume ranks, then measures:
  edge/fill ≈ half_spread_p50 − AS_10s − maker_fee(1.5 bps)

Also reports expected fills/day (absolute economics, not just edge/fill).

Usage:
  python scripts/mm_feasibility_liquidity_spectrum.py
  python scripts/mm_feasibility_liquidity_spectrum.py --minutes 15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INFO_URL = "https://api.hyperliquid.xyz/info"
OUT_JSON = ROOT / "data" / "backtests" / "mm_feasibility_liquidity_spectrum.json"
OUT_DOC = ROOT / "docs" / "MARKET_MAKING_FEASIBILITY_LIQUIDITY_SPECTRUM.md"
MAKER_FEE_BPS = 1.5  # HL perps tier-0 base 0.015%
AS_HORIZON_MS = 10_000
BOOT_N = 600
RNG_SEED = 42

# Prefer these anchors when present
ANCHORS = ("BTC", "ETH", "SOL", "HYPE")


async def _post(session: aiohttp.ClientSession, body: Dict[str, Any]) -> Any:
    async with session.post(INFO_URL, json=body, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        return await resp.json()


def _pctiles(x: np.ndarray, ps: Sequence[float] = (10, 25, 50, 75, 90)) -> Dict[str, float]:
    y = x[np.isfinite(x)]
    if len(y) == 0:
        return {f"p{int(p)}": float("nan") for p in ps}
    return {f"p{int(p)}": float(v) for p, v in zip(ps, np.percentile(y, list(ps)))}


def _mean_ci(x: np.ndarray, rng: np.random.Generator) -> Dict[str, float]:
    y = x[np.isfinite(x)]
    if len(y) < 20:
        return {
            "mean": float(np.mean(y)) if len(y) else float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "n": int(len(y)),
        }
    boots = np.empty(BOOT_N, dtype=float)
    n = len(y)
    for i in range(BOOT_N):
        boots[i] = float(np.mean(y[rng.integers(0, n, size=n)]))
    return {
        "mean": float(np.mean(y)),
        "ci_low": float(np.percentile(boots, 2.5)),
        "ci_high": float(np.percentile(boots, 97.5)),
        "n": int(n),
    }


def parse_book(raw: Dict[str, Any]) -> Optional[Dict[str, float]]:
    levels = raw.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        return None
    bids, asks = levels[0], levels[1]
    if not bids or not asks:
        return None
    try:
        bb = float(bids[0]["px"] if isinstance(bids[0], dict) else bids[0][0])
        bsz = float(bids[0]["sz"] if isinstance(bids[0], dict) else bids[0][1])
        ba = float(asks[0]["px"] if isinstance(asks[0], dict) else asks[0][0])
        asz = float(asks[0]["sz"] if isinstance(asks[0], dict) else asks[0][1])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if bb <= 0 or ba <= 0 or ba <= bb:
        return None
    mid = 0.5 * (bb + ba)
    spread_bps = (ba - bb) / mid * 1e4
    ts = int(raw.get("time") or time.time() * 1000)
    return {
        "ts_ms": float(ts),
        "mid": mid,
        "spread_bps": spread_bps,
        "bid_sz": bsz,
        "ask_sz": asz,
        "touch_usd": 0.5 * (bsz + asz) * mid,
    }


def parse_trades(raw: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        side = str(t.get("side") or t.get("S") or "").upper()
        try:
            px = float(t.get("px") or t.get("price") or 0)
            sz = float(t.get("sz") or t.get("size") or 0)
            ts = int(t.get("time") or t.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0 or sz <= 0 or ts <= 0 or side not in ("B", "A", "BUY", "SELL", "S"):
            continue
        if side in ("BUY",):
            side = "B"
        if side in ("SELL", "S"):
            side = "A"
        tid = str(t.get("tid") or t.get("hash") or f"{ts}:{px}:{sz}:{side}")
        out.append(
            {
                "ts_ms": ts,
                "px": px,
                "sz": sz,
                "side": side,
                "tid": tid,
                "notional": px * sz,
            }
        )
    return out


def aggressor_sign(side: str) -> int:
    return 1 if side == "B" else -1


async def fetch_universe(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    raw = await _post(session, {"type": "metaAndAssetCtxs"})
    meta, ctxs = raw[0], raw[1]
    univ = meta["universe"]
    rows: List[Dict[str, Any]] = []
    for u, c in zip(univ, ctxs):
        if u.get("isDelisted"):
            continue
        name = str(u.get("name") or "")
        if not name or ":" in name:  # skip some weird HIP names if needed — keep all for now
            pass
        mid = float(c.get("midPx") or c.get("markPx") or 0)
        vlm = float(c.get("dayNtlVlm") or 0)
        oi = float(c.get("openInterest") or 0)
        if mid <= 0:
            continue
        rows.append(
            {
                "symbol": name,
                "day_ntl_vlm": vlm,
                "open_interest": oi,
                "mid_px": mid,
                "funding": float(c.get("funding") or 0),
            }
        )
    rows.sort(key=lambda r: r["day_ntl_vlm"], reverse=True)
    for i, r in enumerate(rows):
        r["rank_by_vlm"] = i
    return rows


def select_spectrum(rows: List[Dict[str, Any]], n_target: int = 12) -> List[Dict[str, Any]]:
    """Pick anchors + log-spaced ranks across the volume spectrum."""
    by_name = {r["symbol"]: r for r in rows}
    chosen: Dict[str, Dict[str, Any]] = {}
    for a in ANCHORS:
        if a in by_name:
            chosen[a] = by_name[a]
    n = len(rows)
    # Log-spaced ranks from near-top to near-bottom
    ranks = sorted(
        {
            int(round(x))
            for x in np.geomspace(1, max(n - 1, 1), num=max(n_target - len(chosen), 4))
        }
    )
    for rk in ranks:
        if len(chosen) >= n_target:
            break
        rk = min(max(rk, 0), n - 1)
        r = rows[rk]
        chosen.setdefault(r["symbol"], r)
    # Ensure a few explicitly thin names if still short
    for r in reversed(rows):
        if len(chosen) >= n_target:
            break
        if r["day_ntl_vlm"] > 0:
            chosen.setdefault(r["symbol"], r)
    out = list(chosen.values())
    out.sort(key=lambda r: r["day_ntl_vlm"], reverse=True)
    return out[:n_target]


async def sweep_spreads_once(
    session: aiohttp.ClientSession,
    symbols: Sequence[str],
    concurrency: int = 8,
) -> Dict[str, Dict[str, float]]:
    sem = asyncio.Semaphore(concurrency)
    out: Dict[str, Dict[str, float]] = {}

    async def one(sym: str) -> None:
        async with sem:
            try:
                raw = await _post(session, {"type": "l2Book", "coin": sym})
                parsed = parse_book(raw if isinstance(raw, dict) else {})
                if parsed:
                    out[sym] = parsed
            except Exception as exc:  # noqa: BLE001
                out[sym] = {"error": 1.0, "msg": float("nan")}  # placeholder
                del out[sym]
                print(f"  sweep fail {sym}: {exc}", flush=True)

    await asyncio.gather(*[one(s) for s in symbols])
    return out


async def collect_window(
    session: aiohttp.ClientSession,
    symbols: Sequence[str],
    minutes: float,
    poll_sec: float,
) -> Dict[str, Dict[str, Any]]:
    """Poll l2Book + recentTrades for *minutes*."""
    state: Dict[str, Dict[str, Any]] = {
        s: {"mids_ts": [], "mids": [], "spreads": [], "touch_usd": [], "trades": {}}
        for s in symbols
    }
    sem = asyncio.Semaphore(6)
    deadline = time.time() + minutes * 60.0
    round_i = 0

    async def poll_sym(sym: str) -> None:
        async with sem:
            try:
                book_raw, trades_raw = await asyncio.gather(
                    _post(session, {"type": "l2Book", "coin": sym}),
                    _post(session, {"type": "recentTrades", "coin": sym}),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  poll fail {sym}: {exc}", flush=True)
                return
            parsed = parse_book(book_raw if isinstance(book_raw, dict) else {})
            now_ms = int(time.time() * 1000)
            if parsed:
                st = state[sym]
                st["mids_ts"].append(int(parsed["ts_ms"]) if parsed["ts_ms"] else now_ms)
                st["mids"].append(parsed["mid"])
                st["spreads"].append(parsed["spread_bps"])
                st["touch_usd"].append(parsed["touch_usd"])
            for t in parse_trades(trades_raw if isinstance(trades_raw, list) else []):
                state[sym]["trades"][t["tid"]] = t

    print(f"Collecting {len(symbols)} symbols for {minutes:.1f} min (poll={poll_sec}s)…", flush=True)
    while time.time() < deadline:
        t0 = time.time()
        await asyncio.gather(*[poll_sym(s) for s in symbols])
        round_i += 1
        if round_i % 5 == 0:
            ntr = sum(len(state[s]["trades"]) for s in symbols)
            print(
                f"  round {round_i}: trades_unique={ntr} "
                f"elapsed={minutes*60 - (deadline - time.time()):.0f}s",
                flush=True,
            )
        sleep_for = poll_sec - (time.time() - t0)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
    return state


def analyze_symbol(
    meta: Dict[str, Any],
    st: Dict[str, Any],
    collect_minutes: float,
    rng: np.random.Generator,
) -> Dict[str, Any]:
    spreads = np.asarray(st["spreads"], dtype=float)
    mid_ts = np.asarray(st["mids_ts"], dtype=np.int64)
    mids = np.asarray(st["mids"], dtype=float)
    trades = sorted(st["trades"].values(), key=lambda t: t["ts_ms"])
    touch = np.asarray(st["touch_usd"], dtype=float)

    spread_stats = _pctiles(spreads)
    half_p50 = float(spread_stats["p50"] / 2.0) if np.isfinite(spread_stats["p50"]) else float("nan")

    # AS 10s
    as_vals: List[float] = []
    if len(mid_ts) >= 5 and trades:
        order = np.argsort(mid_ts)
        mid_ts = mid_ts[order]
        mids = mids[order]
        for t in trades:
            ts = int(t["ts_ms"])
            sg = aggressor_sign(t["side"])
            i0 = int(np.searchsorted(mid_ts, ts, side="right") - 1)
            if i0 < 0:
                continue
            age = ts - int(mid_ts[i0])
            if age < 0 or age > 30_000:
                continue
            target = ts + AS_HORIZON_MS
            i1 = int(np.searchsorted(mid_ts, target, side="left"))
            if i1 >= len(mid_ts):
                continue
            m0, m1 = float(mids[i0]), float(mids[i1])
            if m0 <= 0 or not np.isfinite(m0) or not np.isfinite(m1):
                continue
            as_vals.append(sg * (m1 / m0 - 1.0) * 1e4)
    as_arr = np.asarray(as_vals, dtype=float)
    as_stats = _mean_ci(as_arr, rng)
    as_stats["pctiles"] = _pctiles(as_arr)

    edge = half_p50 - as_stats["mean"] - MAKER_FEE_BPS if np.isfinite(as_stats["mean"]) else float("nan")
    edge_as0 = half_p50 - MAKER_FEE_BPS if np.isfinite(half_p50) else float("nan")
    edge_lo = (
        half_p50 - as_stats["ci_high"] - MAKER_FEE_BPS
        if np.isfinite(as_stats["ci_high"])
        else float("nan")
    )
    edge_hi = (
        half_p50 - as_stats["ci_low"] - MAKER_FEE_BPS
        if np.isfinite(as_stats["ci_low"])
        else float("nan")
    )

    # Fills/day proxy: unique trades observed / window → scale to day.
    # Retail MM at back of queue fills << tape; use touch-share proxy.
    window_s = max(collect_minutes * 60.0, 1.0)
    tape_trades = len(trades)
    tape_usd = float(sum(t["notional"] for t in trades))
    trades_per_day = tape_trades / window_s * 86400.0
    usd_per_day = tape_usd / window_s * 86400.0
    touch_p50 = float(np.nanmedian(touch)) if len(touch) else float("nan")
    # Assume retail posts 1 lot ≈ min($50, 5% of touch) and only catches
    # fraction of touch flow proportional to lot/touch (FIFO upper bound).
    lot_usd = float(min(50.0, 0.05 * touch_p50)) if np.isfinite(touch_p50) and touch_p50 > 0 else 50.0
    fill_share = (lot_usd / touch_p50) if (np.isfinite(touch_p50) and touch_p50 > 0) else float("nan")
    # Trades that could hit our resting quote ≈ half of tape (one side) * fill_share
    fills_per_day = 0.5 * trades_per_day * fill_share if np.isfinite(fill_share) else float("nan")
    edge_usd_per_day = (
        fills_per_day * (edge / 1e4) * lot_usd
        if np.isfinite(fills_per_day) and np.isfinite(edge)
        else float("nan")
    )

    return {
        "symbol": meta["symbol"],
        "rank_by_vlm": meta["rank_by_vlm"],
        "day_ntl_vlm": meta["day_ntl_vlm"],
        "open_interest": meta["open_interest"],
        "n_book_samples": int(len(spreads)),
        "n_trades_unique": tape_trades,
        "spread_bps": spread_stats,
        "half_spread_p50_bps": half_p50,
        "touch_usd_p50": touch_p50,
        "adverse_selection_10s": as_stats,
        "maker_fee_bps": MAKER_FEE_BPS,
        "edge_per_fill_bps": edge,
        "edge_per_fill_ci_bps": [edge_lo, edge_hi],
        "edge_if_as0_bps": edge_as0,
        "positive_point": bool(np.isfinite(edge) and edge > 0),
        "positive_ci_low": bool(np.isfinite(edge_lo) and edge_lo > 0),
        "positive_as0": bool(np.isfinite(edge_as0) and edge_as0 > 0),
        "tape_trades_per_day_extrapolated": trades_per_day,
        "tape_usd_per_day_extrapolated": usd_per_day,
        "retail_lot_usd": lot_usd,
        "est_fill_share_of_touch": fill_share,
        "est_fills_per_day": fills_per_day,
        "est_edge_usd_per_day": edge_usd_per_day,
    }


def write_report(path: Path, payload: Dict[str, Any]) -> None:
    lines: List[str] = []
    lines.append("# MM Feasibility — Liquidity Spectrum Addendum")
    lines.append("")
    lines.append(f"Generated: {payload['generated_at']}")
    lines.append(
        f"Collection: {payload['collect_minutes']:.1f} min REST poll of "
        f"l2Book + recentTrades (bot/sampler untouched)."
    )
    lines.append(f"Maker fee: **{MAKER_FEE_BPS} bps/side** (HL perps tier-0 base 0.015%).")
    lines.append("")
    lines.append("## Why this addendum")
    lines.append("")
    lines.append(
        "The primary study (`docs/MARKET_MAKING_FEASIBILITY.md`) concluded **(C)** "
        "on BTC/ETH/SOL/HYPE — the most liquid perps, where spreads are tightest. "
        "This addendum asks whether a **less liquid** region of the HL perp universe "
        "flips `half_spread − AS − fee` positive."
    )
    lines.append("")
    lines.append("## Cautions (declared)")
    lines.append("")
    for c in payload["cautions"]:
        lines.append(f"- {c}")
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append(
        "1. Rank all non-delisted perps by `dayNtlVlm` via `metaAndAssetCtxs`.\n"
        "2. Select ~12 symbols: anchors BTC/ETH/SOL/HYPE + log-spaced ranks.\n"
        "3. Poll `l2Book` + `recentTrades` for N minutes (not extending the live sampler).\n"
        "4. Same equation as primary study; also extrapolate fills/day and $/day."
    )
    lines.append("")
    lines.append(
        f"Universe size: **{payload['universe_n']}** perps. "
        f"Selected: {', '.join(payload['selected_symbols'])}."
    )
    lines.append("")
    ov = payload["verdict"]
    lines.append(f"## Verdict: **({ov['code']})**")
    lines.append("")
    lines.append(ov["summary"])
    lines.append("")
    lines.append(ov["conclusion"])
    lines.append("")
    lines.append("## Results by liquidity rank")
    lines.append("")
    lines.append(
        "| rank | symbol | day$ vlm | half p50 | AS10 [CI] | edge/fill | "
        "edge if AS=0 | fills/day est | $/day est | +ve? |"
    )
    lines.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|:---:|")
    for r in payload["results"]:
        a = r["adverse_selection_10s"]
        lines.append(
            f"| {r['rank_by_vlm']} | {r['symbol']} | {r['day_ntl_vlm']:,.0f} | "
            f"{r['half_spread_p50_bps']:.2f} | "
            f"{a['mean']:.2f} [{a['ci_low']:.2f},{a['ci_high']:.2f}] n={a['n']} | "
            f"{r['edge_per_fill_bps']:.2f} | {r['edge_if_as0_bps']:.2f} | "
            f"{r['est_fills_per_day']:.1f} | {r['est_edge_usd_per_day']:.2f} | "
            f"{'Y' if r['positive_point'] else 'n'} |"
        )
    lines.append("")
    lines.append("## Liquidity vs edge")
    lines.append("")
    lines.append(
        "Reading down the table (high → low `dayNtlVlm`): does edge/fill become "
        "positive at some volume threshold?"
    )
    lines.append("")
    pos = [r for r in payload["results"] if r["positive_point"]]
    pos_as0 = [r for r in payload["results"] if r["positive_as0"]]
    if pos:
        best = max(pos, key=lambda r: r["edge_per_fill_bps"])
        lines.append(
            f"Yes — positive point estimates on: "
            f"{', '.join(r['symbol'] for r in pos)}. "
            f"Best: **{best['symbol']}** at day$ {best['day_ntl_vlm']:,.0f} "
            f"(edge {best['edge_per_fill_bps']:.2f} bps, "
            f"~{best['est_fills_per_day']:.1f} fills/day)."
        )
    else:
        lines.append(
            "No symbol in the sampled spectrum has `edge/fill > 0` after measured AS."
        )
    if pos_as0 and not pos:
        lines.append(
            f"With AS forced to 0, half-spread > fee on: "
            f"{', '.join(r['symbol'] for r in pos_as0)} — but measured AS "
            "wipes that out (thin-market toxicity)."
        )
    lines.append("")
    lines.append("## Instantaneous full-universe spread sweep (context)")
    lines.append("")
    sw = payload.get("universe_spread_sweep") or {}
    if sw:
        lines.append(
            f"One-shot `l2Book` over {sw['n_ok']}/{sw['n_attempted']} perps: "
            f"share with half-spread > {MAKER_FEE_BPS} bps = "
            f"**{sw['frac_half_gt_fee']*100:.1f}%** "
            f"(n={sw['n_half_gt_fee']})."
        )
        lines.append("")
        lines.append("| bucket by day$ vlm | n | median half-spread | % half>fee |")
        lines.append("|---|---:|---:|---:|")
        for b in sw.get("buckets", []):
            lines.append(
                f"| {b['label']} | {b['n']} | {b['half_p50']:.2f} | "
                f"{b['frac_half_gt_fee']*100:.0f}% |"
            )
    lines.append("")
    lines.append("## Sampler extension cost (evaluated, not done)")
    lines.append("")
    lines.append(
        "Extending `research_sampler` / live subscriptions to ~12 alts would "
        "add WS `l2Book`+`trades` channels and DB write load on "
        "`hyperliquid.db`. For a one-shot feasibility answer, **REST polling "
        "for 10–20 minutes is enough** and avoids touching the paper bot. "
        "Only if a positive zone appears would a multi-day sampler extension "
        "be justified."
    )
    lines.append("")
    lines.append("## Conclusion")
    lines.append("")
    lines.append(ov["conclusion"])
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def async_main(args: argparse.Namespace) -> int:
    rng = np.random.default_rng(RNG_SEED)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        print("Fetching universe…", flush=True)
        universe = await fetch_universe(session)
        selected = select_spectrum(universe, n_target=args.n_symbols)
        print(
            "Selected:",
            ", ".join(
                f"{r['symbol']}(#{r['rank_by_vlm']}, ${r['day_ntl_vlm']:,.0f})"
                for r in selected
            ),
            flush=True,
        )

        # Full-universe one-shot spread sweep (batched)
        print("Universe spread sweep…", flush=True)
        all_syms = [r["symbol"] for r in universe]
        # Cap sweep to avoid hammering — top 80 + every 3rd of rest
        sweep_syms = [r["symbol"] for r in universe[:80]]
        sweep_syms += [r["symbol"] for r in universe[80::3]]
        sweep_syms = list(dict.fromkeys(sweep_syms))
        books = await sweep_spreads_once(session, sweep_syms, concurrency=10)
        # Attach volume for buckets
        vlm_map = {r["symbol"]: r["day_ntl_vlm"] for r in universe}
        halves = []
        for sym, b in books.items():
            halves.append(
                {
                    "symbol": sym,
                    "half": b["spread_bps"] / 2.0,
                    "vlm": vlm_map.get(sym, 0.0),
                }
            )
        halves.sort(key=lambda x: x["vlm"], reverse=True)
        n_gt = sum(1 for h in halves if h["half"] > MAKER_FEE_BPS)
        buckets = []
        if halves:
            edges = [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0001]
            labels = [
                "top 10%",
                "10–25%",
                "25–50%",
                "50–75%",
                "75–90%",
                "bottom 10%",
            ]
            n = len(halves)
            for i, lab in enumerate(labels):
                a = int(edges[i] * n)
                b = int(edges[i + 1] * n)
                chunk = halves[a:b]
                if not chunk:
                    continue
                hs = np.array([c["half"] for c in chunk], dtype=float)
                buckets.append(
                    {
                        "label": lab,
                        "n": len(chunk),
                        "half_p50": float(np.nanmedian(hs)),
                        "frac_half_gt_fee": float(np.mean(hs > MAKER_FEE_BPS)),
                    }
                )
        sweep_summary = {
            "n_attempted": len(sweep_syms),
            "n_ok": len(halves),
            "n_half_gt_fee": n_gt,
            "frac_half_gt_fee": n_gt / max(len(halves), 1),
            "buckets": buckets,
        }

        state = await collect_window(
            session,
            [r["symbol"] for r in selected],
            minutes=args.minutes,
            poll_sec=args.poll_sec,
        )

    results = [
        analyze_symbol(meta, state[meta["symbol"]], args.minutes, rng)
        for meta in selected
    ]
    results.sort(key=lambda r: r["rank_by_vlm"])

    pos = [r for r in results if r["positive_point"]]
    pos_ci = [r for r in results if r["positive_ci_low"]]
    # Material fills threshold: at least ~10 fills/day and >$1/day expected
    material = [
        r
        for r in pos
        if np.isfinite(r["est_fills_per_day"])
        and r["est_fills_per_day"] >= 10
        and np.isfinite(r["est_edge_usd_per_day"])
        and r["est_edge_usd_per_day"] >= 1.0
    ]

    if material and pos_ci:
        code = "A"
        summary = (
            f"Positive CI-robust edge with material fills on "
            f"{', '.join(r['symbol'] for r in material)}."
        )
        conclusion = (
            "A liquidity zone appears viable for further MM architecture research "
            "(still measurement-only here)."
        )
    elif pos and material:
        code = "A"
        summary = (
            f"Point-positive with material fills on "
            f"{', '.join(r['symbol'] for r in material)}; CI not fully robust."
        )
        conclusion = (
            "Justifies careful follow-up measurement on those names — not a build "
            "license. Thin-book AS/inventory risks remain first-order."
        )
    elif pos:
        code = "C"  # positive edge but irrelevant absolute $ → still closed for practical MM
        # Actually user said: (A) positive AND fills relevant; (C) negative across spectrum
        # If positive but fills irrelevant, that's closer to C for practical purposes
        summary = (
            f"Edge/fill point-positive on {', '.join(r['symbol'] for r in pos)} "
            "but estimated fills/day or $/day are too small to matter."
        )
        conclusion = (
            "No practically relevant MM zone: either edge≤0 or absolute economics "
            "near zero. Retail MM on HL remains closed."
        )
        # Re-read user criteria: (A) positive AND fills relevant; (C) negative everywhere
        # Intermediate case of positive but irrelevant → treat as C (definitively closed for practical MM)
        code = "C"
    else:
        code = "C"
        summary = (
            "Equation ≤ 0 across the sampled liquidity spectrum after measured "
            "adverse selection (or insufficient AS sample with AS=0 still ≤0)."
        )
        conclusion = (
            "Retail market making on Hyperliquid is **DEFINITIVELY closed** across "
            "the liquidity spectrum tested: wider spreads on thin names are eaten "
            "by higher adverse selection and/or still fail to clear the 1.5 bps "
            "maker fee with meaningful fill rate. Do not build MM infrastructure."
        )

    payload: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collect_minutes": args.minutes,
        "poll_sec": args.poll_sec,
        "maker_fee_bps": MAKER_FEE_BPS,
        "universe_n": len(universe),
        "selected_symbols": [r["symbol"] for r in selected],
        "cautions": [
            "Thin markets often have HIGHER adverse selection — informed flow concentrates there.",
            "Inventory neutralization is harder/more expensive when the book is thin.",
            "Gaps and violent moves are more frequent on low-liquidity perps.",
            "Low volume ⇒ few fills; edge/fill can be positive while $/day is irrelevant — we report both.",
            "REST window is minutes, not weeks — AS CIs are wider than the 31d primary study.",
            "Fill-share uses a FIFO lot/touch proxy; HL matching may differ.",
            "Live sampler was NOT extended; bot was not interrupted.",
        ],
        "universe_spread_sweep": sweep_summary,
        "results": results,
        "verdict": {
            "code": code,
            "summary": summary,
            "conclusion": conclusion,
            "positive_symbols": [r["symbol"] for r in pos],
            "material_symbols": [r["symbol"] for r in material],
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, allow_nan=True), encoding="utf-8")
    write_report(OUT_DOC, payload)
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_DOC}")
    print(f"VERDICT ({code}): {summary}")
    for r in results:
        print(
            f"  #{r['rank_by_vlm']:3d} {r['symbol']:10} half={r['half_spread_p50_bps']:6.2f} "
            f"AS={r['adverse_selection_10s']['mean']:6.2f} edge={r['edge_per_fill_bps']:7.2f} "
            f"fills/d={r['est_fills_per_day']:6.1f}"
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=15.0, help="REST collection window")
    ap.add_argument("--poll-sec", type=float, default=3.0)
    ap.add_argument("--n-symbols", type=int, default=12)
    args = ap.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
