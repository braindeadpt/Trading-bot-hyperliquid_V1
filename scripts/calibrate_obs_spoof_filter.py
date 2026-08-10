"""Calibrate OrderBookScalper anti-spoof using live HL L2 (read-only).

Does not touch the running bot. Samples public L2 books and measures:
  - depth_q vs entry side (shows tautology of old filter)
  - wall_frac = largest_wall_size / same-side depth_1pct (orthogonal)
  - wall distance to mid

Writes: data/backtests/parity_diag/obs_spoof_calibration.json
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "backtests" / "parity_diag"

# Match OrderBookScalper defaults
BID_ASK_LONG = 1.5
BID_ASK_SHORT = 0.67
SPOOF_PROX = 0.001  # 0.1%
OLD_SKEW = 0.65


def _info(payload: dict) -> Any:
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _levels(raw: List) -> List[Tuple[float, float]]:
    out = []
    for row in raw or []:
        if isinstance(row, dict):
            px, sz = float(row.get("px", 0)), float(row.get("sz", 0))
        else:
            px, sz = float(row[0]), float(row[1])
        if px > 0 and sz > 0:
            out.append((px, sz))
    return out


def _depth_within(levels: List[Tuple[float, float]], mid: float, pct: float) -> float:
    thr = mid * pct / 100.0
    total = 0.0
    for px, sz in levels:
        if abs(px - mid) <= thr:
            total += sz
        else:
            break
    return total


def analyze_book(coin: str, book: dict) -> Optional[dict]:
    bids = _levels(book.get("levels", [[], []])[0] if "levels" in book else book.get("bids", []))
    asks = _levels(book.get("levels", [[], []])[1] if "levels" in book else book.get("asks", []))
    # HL l2Book format: {"coin":..., "levels":[bids, asks], ...} where each level is {px,sz,n}
    if not bids and "levels" in book:
        levels = book["levels"]
        if isinstance(levels, list) and len(levels) >= 2:
            bids = _levels(levels[0])
            asks = _levels(levels[1])
    if not bids or not asks:
        return None
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    bid_d = _depth_within(bids, mid, 1.0)
    ask_d = _depth_within(asks, mid, 1.0)
    total = bid_d + ask_d
    depth_q = bid_d / total if total > 0 else 0.5
    ratio = bid_d / ask_d if ask_d > 0 else 1.0
    largest_bid = max(bids, key=lambda x: x[1])
    largest_ask = max(asks, key=lambda x: x[1])
    bid_wall_frac = largest_bid[1] / bid_d if bid_d > 0 else 0.0
    ask_wall_frac = largest_ask[1] / ask_d if ask_d > 0 else 0.0
    bid_wall_dist = abs(mid - largest_bid[0]) / mid
    ask_wall_dist = abs(mid - largest_ask[0]) / mid

    signal = None
    imbalance_side = None
    if ratio >= BID_ASK_LONG:
        signal, imbalance_side = "short_or_long_mom_long", "bid"
    elif ratio <= BID_ASK_SHORT:
        signal, imbalance_side = "mom_short", "ask"

    old_spoof = False
    if imbalance_side == "bid":
        old_spoof = depth_q >= OLD_SKEW and bid_wall_dist <= SPOOF_PROX
    elif imbalance_side == "ask":
        old_spoof = depth_q <= (1.0 - OLD_SKEW) and ask_wall_dist <= SPOOF_PROX

    wall_frac = bid_wall_frac if imbalance_side == "bid" else (
        ask_wall_frac if imbalance_side == "ask" else None
    )
    wall_dist = bid_wall_dist if imbalance_side == "bid" else (
        ask_wall_dist if imbalance_side == "ask" else None
    )

    return {
        "coin": coin,
        "mid": mid,
        "ratio": round(ratio, 4),
        "depth_q": round(depth_q, 4),
        "bid_d": bid_d,
        "ask_d": ask_d,
        "bid_wall_frac": round(bid_wall_frac, 4),
        "ask_wall_frac": round(ask_wall_frac, 4),
        "bid_wall_dist_pct": round(bid_wall_dist * 100, 4),
        "ask_wall_dist_pct": round(ask_wall_dist * 100, 4),
        "signal": signal,
        "imbalance_side": imbalance_side,
        "wall_frac": None if wall_frac is None else round(wall_frac, 4),
        "wall_dist_pct": None if wall_dist is None else round(wall_dist * 100, 4),
        "old_spoof_blocks": old_spoof,
    }


def pctile(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    i = int(round((len(ys) - 1) * p / 100.0))
    return ys[i]


def main() -> int:
    coins = ["BTC", "ETH", "SOL", "HYPE"]
    samples: List[dict] = []
    # ~2 min of sampling at 2s → ~60 snaps × 4 coins
    rounds = 45
    for r in range(rounds):
        for coin in coins:
            try:
                book = _info({"type": "l2Book", "coin": coin})
                row = analyze_book(coin, book)
                if row:
                    row["ts"] = time.time()
                    samples.append(row)
            except Exception as exc:
                samples.append({"coin": coin, "error": str(exc)})
        time.sleep(2.0)
        if (r + 1) % 10 == 0:
            print(f"  sampled round {r+1}/{rounds} n={len(samples)}", flush=True)

    signal_rows = [s for s in samples if s.get("imbalance_side")]
    all_wall = [float(s["wall_frac"]) for s in signal_rows if s.get("wall_frac") is not None]
    near = [s for s in signal_rows if (s.get("wall_dist_pct") or 99) <= SPOOF_PROX * 100]
    near_fracs = [float(s["wall_frac"]) for s in near if s.get("wall_frac") is not None]

    # Choose threshold ≈ p80 of wall_frac among entry candidates (block top ~20%)
    thr_candidates = {}
    for p in (70, 75, 80, 85, 90):
        thr = pctile(all_wall, p)
        if thr is None:
            continue
        blocked = sum(1 for x in all_wall if x >= thr)
        thr_candidates[p] = {
            "wall_frac_threshold": thr,
            "block_rate_among_signals": blocked / len(all_wall) if all_wall else None,
            "n_signals": len(all_wall),
        }

    # Prefer ~15-20% block rate
    chosen_p = 80
    chosen = thr_candidates.get(chosen_p) or {}

    old_block = sum(1 for s in signal_rows if s.get("old_spoof_blocks"))
    payload = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "rounds": rounds,
        "n_samples": len(samples),
        "n_entry_candidates": len(signal_rows),
        "old_filter_block_rate": (old_block / len(signal_rows)) if signal_rows else None,
        "wall_frac_among_signals": {
            "n": len(all_wall),
            "min": min(all_wall) if all_wall else None,
            "p50": pctile(all_wall, 50),
            "p80": pctile(all_wall, 80),
            "p90": pctile(all_wall, 90),
            "max": max(all_wall) if all_wall else None,
        },
        "near_mid_wall_frac": {
            "n": len(near_fracs),
            "p50": pctile(near_fracs, 50),
            "p80": pctile(near_fracs, 80),
        },
        "threshold_grid": thr_candidates,
        "recommended": {
            "method": "wall_frac = largest_wall_size / same_side_depth_1pct; "
            "block if wall within spoof_wall_proximity_pct of mid AND wall_frac >= threshold",
            "spoof_wall_fraction_min": chosen.get("wall_frac_threshold"),
            "expected_block_rate": chosen.get("block_rate_among_signals"),
            "percentile_used": chosen_p,
            "rationale": (
                "depth_q is tautological with bid_ask_ratio (ask signals ⇒ depth_q≤0.35). "
                "wall_frac is orthogonal: a wall that is a large slice of THAT side's book "
                "is suspicious even when the book is ask-heavy."
            ),
        },
        "samples_tail": signal_rows[-20:],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "obs_spoof_calibration.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in payload if k != "samples_tail"}, indent=2))
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
