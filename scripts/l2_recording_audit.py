#!/usr/bin/env python3
"""Audit Hyperliquid full-depth L2 recording health (research-only).

Checks coverage, gaps, file sizes, and readiness for the 30-day L2 screen.
Never touches bot.db. Named ``l2_recording_audit`` (not ``audit_*``) so it is
not excluded by ``scripts/audit_*.py`` in ``.gitignore``.

Usage:
  python scripts/l2_recording_audit.py
  python scripts/l2_recording_audit.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.config import load_config  # noqa: E402

MIN_DAYS_FOR_SCREEN = 30
OUT_PATH = ROOT / "data" / "research" / "paper_oos_90d" / "l2_audit_latest.json"


def _day_dirs(root: Path) -> List[Path]:
    if not root.exists():
        return []
    days: List[Path] = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and len(p.name) == 10 and p.name[4] == "-" and p.name[7] == "-":
            days.append(p)
        # also accept YYYYMMDD or nested symbol folders
    if days:
        return days
    # flat gzip files
    return []


def _file_stats(path: Path) -> Dict[str, Any]:
    files = list(path.rglob("*.jsonl*")) + list(path.rglob("*.gz"))
    # de-dupe
    uniq = {f.resolve(): f for f in files}
    files = list(uniq.values())
    total = sum(f.stat().st_size for f in files if f.is_file())
    return {
        "n_files": len(files),
        "bytes": total,
        "mb": round(total / 1e6, 2),
    }


def audit_l2(*, config_path: Path = ROOT / "config" / "settings.yaml", write: bool = True) -> Dict[str, Any]:
    cfg = load_config(config_path)
    rec = cfg.get("market_data.l2_recording", {}) or {}
    enabled = bool(rec.get("enabled", False))
    interval = float(rec.get("interval_sec", 1.0))
    depth = int(rec.get("depth_levels", 25))
    path = Path(str(rec.get("path", "data/research/l2_books")))
    if not path.is_absolute():
        path = ROOT / path

    days = _day_dirs(path)
    # If no day dirs, treat immediate children files' mtimes as span
    file_stats = _file_stats(path) if path.exists() else {"n_files": 0, "bytes": 0, "mb": 0.0}

    n_days = len(days)
    if n_days == 0 and file_stats["n_files"] > 0:
        mtimes = [f.stat().st_mtime for f in path.rglob("*") if f.is_file()]
        if mtimes:
            span_days = (max(mtimes) - min(mtimes)) / 86400.0
            n_days = max(1, int(span_days) + 1)

    ready = enabled and n_days >= MIN_DAYS_FOR_SCREEN and file_stats["n_files"] > 0
    note = (
        f"ready for depth screen (≥{MIN_DAYS_FOR_SCREEN}d)"
        if ready
        else f"accumulate to {MIN_DAYS_FOR_SCREEN} valid days before feature_screening_l2_depth.py"
    )

    report: Dict[str, Any] = {
        "ok": enabled and path.exists(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": enabled,
        "path": str(path),
        "interval_sec": interval,
        "depth_levels": depth,
        "n_days": n_days,
        "min_days_for_screen": MIN_DAYS_FOR_SCREEN,
        "ready_for_screen": ready,
        "files": file_stats,
        "day_dirs_sample": [d.name for d in days[:5]],
        "note": note,
    }
    if write:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    report = audit_l2(write=not args.no_write)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("=== L2 recording audit ===")
        for k in (
            "ok",
            "enabled",
            "path",
            "interval_sec",
            "depth_levels",
            "n_days",
            "ready_for_screen",
            "note",
        ):
            print(f"{k}: {report.get(k)}")
        print(f"files: {report.get('files')}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
