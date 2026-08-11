"""Seed / refresh ``data/research/top_traders.json``.

Preferred (durable Top 5/10 from official HL stats leaderboard):
  python scripts/refresh_top_traders.py --from-leaderboard --top-n 10

Manual seed still supported:
  python scripts/refresh_top_traders.py --addresses 0xabc...,0xdef...
  python scripts/refresh_top_traders.py --from-json path/to/addresses.json --top-n 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.exchanges.hl_leaderboard import (  # noqa: E402
    fetch_durable_top_wallets,
    wallets_payload,
)
from src.utils.helpers import validate_safe_path  # noqa: E402

OUT_DEFAULT = ROOT / "data" / "research" / "top_traders.json"


def _normalize(addrs: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for a in addrs:
        addr = str(a).strip().lower()
        if addr.startswith("0x") and len(addr) >= 42 and addr not in seen:
            if "replace" in addr:
                continue
            seen.add(addr)
            out.append(addr)
    return out


def _write(payload: Dict[str, Any], out: Path) -> Path:
    if not out.is_absolute():
        out = ROOT / out
    # validate relative form for safety
    try:
        rel = out.resolve().relative_to(ROOT)
    except ValueError as exc:
        raise SystemExit(f"Out path outside project: {out}") from exc
    safe = validate_safe_path(rel.as_posix())
    if safe is None:
        raise SystemExit(f"Rejected out path: {out}")
    target = Path(safe) if Path(safe).is_absolute() else ROOT / safe
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


async def _from_leaderboard(args: argparse.Namespace) -> Dict[str, Any]:
    wallets = await fetch_durable_top_wallets(
        top_n=int(args.top_n),
        window=str(args.window),
        min_account_value=float(args.min_account_value),
        min_volume=float(args.min_volume),
        min_pnl=float(args.min_pnl),
        require_month_positive=not bool(args.no_month_filter),
        require_consistent_windows=not bool(args.no_consistency_filter),
        min_month_volume=float(args.min_month_volume),
        min_all_time_pnl=float(args.min_all_time_pnl),
    )
    return wallets_payload(
        wallets,
        notes=(
            f"Consistent top {args.top_n}: week+month+allTime>0, "
            f"ranked by consistency score "
            f"(min_av={args.min_account_value}, min_vlm={args.min_volume})"
        ),
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--from-leaderboard",
        action="store_true",
        help="Pull durable top wallets from HL stats-data leaderboard",
    )
    p.add_argument("--window", default="allTime", choices=["day", "week", "month", "allTime"])
    p.add_argument("--min-account-value", type=float, default=100_000.0)
    p.add_argument("--min-volume", type=float, default=5_000_000.0)
    p.add_argument("--min-pnl", type=float, default=0.0)
    p.add_argument("--min-all-time-pnl", type=float, default=1_000_000.0)
    p.add_argument(
        "--no-month-filter",
        action="store_true",
        help="Do not require positive month PnL when consistency filter is off",
    )
    p.add_argument(
        "--no-consistency-filter",
        action="store_true",
        help="Disable week+month+allTime positivity (falls back toward allTime rank)",
    )
    p.add_argument("--min-month-volume", type=float, default=1_000_000.0)
    p.add_argument("--addresses", default="", help="Comma-separated 0x wallets")
    p.add_argument("--from-json", default="", help="JSON file with wallets/addresses")
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--out", default=str(OUT_DEFAULT))
    args = p.parse_args()

    if args.from_leaderboard:
        payload = asyncio.run(_from_leaderboard(args))
        target = _write(payload, Path(args.out))
        print(f"Wrote {len(payload['wallets'])} wallets -> {target}")
        for w in payload["wallets"]:
            print(
                f"  #{w['rank']} {w['address'][:12]}... "
                f"pnl={w['pnl']:.0f} av={w['account_value']:.0f}"
            )
        return 0 if payload["wallets"] else 1

    addrs: List[str] = []
    if args.addresses:
        addrs.extend(x.strip() for x in args.addresses.split(",") if x.strip())
    if args.from_json:
        raw_path = Path(args.from_json)
        if not raw_path.is_absolute():
            raw_path = ROOT / raw_path
        try:
            rel = raw_path.resolve().relative_to(ROOT)
        except ValueError:
            print(f"Rejected path: {args.from_json}", file=sys.stderr)
            return 2
        safe = validate_safe_path(rel.as_posix())
        if safe is None:
            print(f"Rejected path: {args.from_json}", file=sys.stderr)
            return 2
        data = json.loads((ROOT / safe).read_text(encoding="utf-8"))
        rows = data.get("wallets") if isinstance(data, dict) else data
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, str):
                    addrs.append(row)
                elif isinstance(row, dict):
                    addrs.append(str(row.get("address") or row.get("user") or ""))

    uniq = _normalize(addrs)[: max(1, int(args.top_n))]
    payload = {
        "updated_ms": int(time.time() * 1000),
        "notes": "Seeded by scripts/refresh_top_traders.py (manual)",
        "wallets": [{"address": a, "rank": i + 1} for i, a in enumerate(uniq)],
    }
    target = _write(payload, Path(args.out))
    print(f"Wrote {len(uniq)} wallets -> {target}")
    return 0 if uniq else 1


if __name__ == "__main__":
    raise SystemExit(main())
