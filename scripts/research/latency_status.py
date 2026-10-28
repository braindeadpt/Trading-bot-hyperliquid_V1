#!/usr/bin/env python3
"""Read-only latency-probe readout (prereg §Leitura).

Usage:
    python -m scripts.research.latency_status [--db PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from src.latency_probe import spec
    from src.latency_probe.read import evaluate

    res = evaluate(args.db or spec.DB_PATH)
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True, default=str))
        return
    print(f"verdict          : {res.get('verdict')}")
    for k in ("sessions", "events_total", "events_scored", "span_weeks",
              "mean_net_bps", "ci95_bps", "positive_sessions",
              "top2_gain_share", "median_delay_ms"):
        if k in res:
            print(f"{k:<17}: {res[k]}")
    for k, v in (res.get("checks") or {}).items():
        print(f"  check {k:<20}: {v}")


if __name__ == "__main__":
    main()
