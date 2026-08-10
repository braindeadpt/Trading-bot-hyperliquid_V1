#!/usr/bin/env python3
"""Validate L2 book recordings: reconstruct metrics from stored levels.

Usage:
  python scripts/validate_l2_book_recording.py
  python scripts/validate_l2_book_recording.py --path data/research/l2_books --max-rows 500
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.orderbook_metrics import PriceLevel, calculate_metrics  # noqa: E402


def iter_rows(path: Path) -> Iterator[Dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def validate_row(row: Dict[str, Any], *, atol: float = 1e-12) -> Optional[str]:
    bids = [PriceLevel(float(p), float(s)) for p, s in row["bids"]]
    asks = [PriceLevel(float(p), float(s)) for p, s in row["asks"]]
    m = calculate_metrics(
        bids, asks, str(row["symbol"]), int(row["exchange_ts_ms"])
    )
    checks = [
        ("spread_pct", m.spread_pct, float(row["spread_pct"])),
        ("oir_10", m.oir_10levels, float(row["oir_10"])),
        ("depth_quality", m.depth_quality, float(row["depth_quality"])),
        ("mid", m.mid_price, float(row["mid"])),
    ]
    for name, got, exp in checks:
        if abs(got - exp) > atol:
            return f"{name}: got={got} expected={exp}"
    if int(row["received_ts_ms"]) < int(row["exchange_ts_ms"]) - 60_000:
        # Allow clock skew; only flag wildly inverted latency
        return "received_ts_ms << exchange_ts_ms"
    return None


def collect_files(root: Path, symbol: Optional[str]) -> List[Path]:
    if not root.exists():
        return []
    files: List[Path] = []
    for sym_dir in sorted(root.iterdir()):
        if not sym_dir.is_dir() or sym_dir.name.startswith("_"):
            continue
        if symbol and sym_dir.name.upper() != symbol.upper():
            continue
        files.extend(sorted(sym_dir.glob("*.jsonl.gz")))
    return files


def disk_stats(root: Path) -> Tuple[float, int]:
    total = 0
    n = 0
    for f in root.rglob("*.jsonl.gz") if root.exists() else []:
        total += f.stat().st_size
        n += 1
    return total / (1024 * 1024), n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--path",
        default="data/research/l2_books",
        help="Recorder root (relative to project)",
    )
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--max-rows", type=int, default=2_000)
    ap.add_argument("--atol", type=float, default=1e-12)
    args = ap.parse_args()

    root = Path(args.path)
    if not root.is_absolute():
        root = PROJECT_ROOT / root

    files = collect_files(root, args.symbol)
    mb, nfiles = disk_stats(root)
    print(f"path={root}")
    print(f"files={nfiles} size_mb={mb:.3f}")
    if not files:
        print("FAIL: no .jsonl.gz files found — recorder not running or empty")
        return 2

    checked = 0
    failures: List[str] = []
    latencies: List[int] = []
    for fpath in files:
        for row in iter_rows(fpath):
            err = validate_row(row, atol=args.atol)
            checked += 1
            latencies.append(int(row["received_ts_ms"]) - int(row["exchange_ts_ms"]))
            if err:
                failures.append(f"{fpath.name} row#{checked}: {err}")
                if len(failures) >= 20:
                    break
            if checked >= args.max_rows:
                break
        if checked >= args.max_rows or len(failures) >= 20:
            break

    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        print(
            f"rows_checked={checked} latency_ms p50={p50} p95={p95} "
            f"min={latencies[0]} max={latencies[-1]}"
        )
    else:
        print(f"rows_checked={checked}")

    if failures:
        print(f"FAIL: {len(failures)} metric mismatches (showing up to 20)")
        for f in failures:
            print(f"  {f}")
        return 1

    print("PASS: reconstructed spread/OIR/depth_quality/mid match stored values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
