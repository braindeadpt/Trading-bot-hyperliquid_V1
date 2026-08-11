"""Seed / refresh ``data/research/top_traders.json`` from whale harvest inputs.

Usage examples:
  python scripts/refresh_top_traders.py --addresses 0xabc...,0xdef...
  python scripts/refresh_top_traders.py --from-json path/to/addresses.json --top-n 10

Does not invent leaderboard ranks — you supply addresses (from HL UI,
liquidation-map harvest, or third-party analytics). Output stays under
the project tree for TopTraderTracker.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.helpers import validate_safe_path

OUT_DEFAULT = ROOT / "data" / "research" / "top_traders.json"


def _normalize(addrs: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for a in addrs:
        addr = str(a).strip().lower()
        if addr.startswith("0x") and len(addr) >= 10 and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--addresses", default="", help="Comma-separated 0x wallets")
    p.add_argument("--from-json", default="", help="JSON file with wallets/addresses")
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--out", default=str(OUT_DEFAULT))
    args = p.parse_args()

    addrs: List[str] = []
    if args.addresses:
        addrs.extend(x.strip() for x in args.addresses.split(",") if x.strip())
    if args.from_json:
        raw_path = Path(args.from_json)
        if not raw_path.is_absolute():
            raw_path = ROOT / raw_path
        safe = validate_safe_path(str(raw_path))
        if safe is None:
            print(f"Rejected path: {args.from_json}", file=sys.stderr)
            return 2
        data = json.loads(Path(safe).read_text(encoding="utf-8"))
        rows = data.get("wallets") if isinstance(data, dict) else data
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, str):
                    addrs.append(row)
                elif isinstance(row, dict):
                    addrs.append(str(row.get("address") or row.get("user") or ""))

    uniq = _normalize(addrs)[: max(1, int(args.top_n))]
    payload: Dict[str, Any] = {
        "updated_ms": int(time.time() * 1000),
        "notes": "Seeded by scripts/refresh_top_traders.py",
        "wallets": [{"address": a, "rank": i + 1} for i, a in enumerate(uniq)],
    }
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    safe_out = validate_safe_path(str(out))
    if safe_out is None:
        print(f"Rejected out path: {args.out}", file=sys.stderr)
        return 2
    Path(safe_out).parent.mkdir(parents=True, exist_ok=True)
    Path(safe_out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(uniq)} wallets → {safe_out}")
    return 0 if uniq else 1


if __name__ == "__main__":
    raise SystemExit(main())
