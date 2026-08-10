#!/usr/bin/env python3
"""Re-register Fase 10 / Phase08 after aligning paper fees to HL tier-0.

Economic-model correction only — no strategy changes:
  taker 0.045%/side, maker 0.015%/side (official Hyperliquid perps tier-0).

Archives prior manifests, freezes a new OOS window, and stamps
``ignore_trades_before_ms`` so the governor ignores pre-window trades.

Usage:
  python scripts/reregister_phase10_tier0_fees.py

Does NOT restart the paper bot — apply with a coordinated restart after
reviewing the printed window_start_ms.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.phase08_preregister import (  # noqa: E402
    assert_config_matches_preregister as assert_phase08,
    persist_preregister_manifest as persist_phase08,
)
from src.research.phase10_preregister import (  # noqa: E402
    assert_config_matches_preregister as assert_phase10,
    load_preregister_manifest,
    persist_preregister_manifest as persist_phase10,
)
from src.utils.config import load_config  # noqa: E402

REREG_REASON = (
    "economic-model correction — align paper/backtest fees to Hyperliquid "
    "perps tier-0 (taker 0.045%/side, maker 0.015%/side). Prior YAML "
    "understated maker (0.01) and used 0.035 taker (Tier-2+). No strategy "
    "param or execution_strategies change. OOS counter restarts."
)
IN_SAMPLE_NOTE = (
    "Fee alignment only. VWAPDeviation remains the sole paper execution "
    "control (INCONCLUSIVE sample). No promotion; mainnet still blocked."
)

SETTINGS = ROOT / "config" / "settings.yaml"


def _set_ignore_trades_before_ms(path: Path, window_start_ms: int) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(ignore_trades_before_ms:\s*)\d+(\s*#.*)?",
        re.MULTILINE,
    )
    replacement = (
        rf"\g<1>{window_start_ms}"
        r"  # Fase 10 window_start_ms (re-registered 2026-08-10 tier-0 fees)"
    )
    new_text, n = pattern.subn(replacement, text, count=1)
    if n != 1:
        raise SystemExit(
            "Failed to update ignore_trades_before_ms in settings.yaml "
            f"(matches={n})"
        )
    path.write_text(new_text, encoding="utf-8")


def main() -> int:
    window_start_ms = int(time.time() * 1000)
    _set_ignore_trades_before_ms(SETTINGS, window_start_ms)
    cfg = load_config(SETTINGS)

    # Sanity: effective fees must already be tier-0 before freeze.
    taker = float(cfg.get("risk.taker_fee_pct", 0))
    maker = float(cfg.get("execution.maker_orders.maker_fee_pct", 0))
    commission = float(cfg.get("backtest.commission_pct", 0))
    if abs(taker - 0.045) > 1e-9 or abs(maker - 0.015) > 1e-9:
        raise SystemExit(
            f"Refusing re-register: expected taker=0.045 maker=0.015, "
            f"got taker={taker} maker={maker}"
        )
    if abs(commission - 0.045) > 1e-9:
        raise SystemExit(
            f"Refusing re-register: expected backtest.commission_pct=0.045, "
            f"got {commission}"
        )

    p10_path = persist_phase10(
        cfg,
        overwrite=True,
        now_ms=window_start_ms,
        reregistration_reason=REREG_REASON,
        in_sample_selection_note=IN_SAMPLE_NOTE,
    )
    persist_phase08(
        cfg,
        overwrite=True,
        reregistration_reason=REREG_REASON,
        in_sample_selection_note=IN_SAMPLE_NOTE,
    )

    assert_phase08(cfg)
    assert_phase10(cfg)

    final = load_preregister_manifest(p10_path)
    assert final is not None
    assert int(final["window_start_ms"]) == window_start_ms

    print("Phase10 re-registered OK (tier-0 fee alignment)")
    print(f"  experiment_id: {final['experiment_id']}")
    print(f"  window_start_ms: {final['window_start_ms']}")
    print(f"  execution_strategies: {final['execution_strategies']}")
    print(f"  config_hash: {final['config_hash']}")
    print(f"  reregistration_reason: {final.get('reregistration_reason', '')[:100]}...")
    print("Phase08 re-registered + both asserts PASS")
    print()

    # Paper OOS 90d cycle manifest (immutable snapshot of freeze intent)
    oos_dir = ROOT / "data" / "research" / "paper_oos_90d"
    oos_dir.mkdir(parents=True, exist_ok=True)
    oos_manifest = {
        "protocol": "paper-oos-90d-v1",
        "frozen_at_ms": window_start_ms,
        "phase10_experiment_id": final["experiment_id"],
        "phase10_config_hash": final["config_hash"],
        "execution_strategies": final["execution_strategies"],
        "fees": {
            "taker_fee_pct": 0.045,
            "maker_fee_pct": 0.015,
            "backtest_commission_pct": 0.045,
        },
        "criteria": {
            "min_calendar_days": 90,
            "min_closed_trades": 30,
            "net_pf_gt": 1.0,
            "expectancy_r_gt": 0.0,
            "min_funding_coverage": 0.90,
            "b1_random_direction_p95": True,
            "min_b1_seeds": 200,
            "mainnet_promotion": False,
        },
        "docs": "docs/PAPER_OOS_90D_PROTOCOL.md",
        "reregistration_reason": REREG_REASON,
    }
    oos_path = oos_dir / "manifest.json"
    oos_path.write_text(
        __import__("json").dumps(oos_manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Paper OOS manifest: {oos_path}")
    print()
    print("NEXT: coordinated paper-bot restart to load new fees + window.")
    print("  Do not restart from this script — stop.bat / start.bat when ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
