#!/usr/bin/env python3
"""Re-register Phase08/Phase10 after emptying the execution path (2026-09-17).

Cumulative evidence says no registered strategy deserves to execute:

- Live paper `strategy_pnl` (real executed positions, tier-0 fees):
  VWAPDeviation −220.84 USD / 44 trades (57% WR, negative expectancy),
  ChecklistMeta −905.27 / 184, VolatilityBreakout −454.30 / 26,
  TrendPyramid −19.69 / 8, SmartMoneyFlow −3.54 / 12.
- Overnight research program: every VWAPDeviation refinement family
  DISCARD'd under window discipline (Night 2 thresholds, Q4 exhaustion,
  Q5 deceleration, Q6 HYPE refine, Q7 exit-econ). ~40 cells, zero KEEP.

`execution_strategies` is now [] — all surviving names run shadow-only so
signal telemetry keeps accumulating without spending paper capital on a
proven-negative expectancy. Promotion back requires a fresh
baseline_signal_gate PASS; this script only re-freezes the manifests so
boot assertions match the new config.

Usage:
  python scripts/ops/reregister_phase10_execution_demotion.py

Does NOT restart the paper bot — apply with a coordinated restart.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
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
    "execution-path demotion — cumulative paper + backtest evidence shows "
    "negative expectancy for every registered strategy (VWAPDeviation "
    "-220.84USD/44t live paper, 5 refinement families DISCARD'd; "
    "ChecklistMeta -905.27/184t already demoted). execution_strategies "
    "emptied; all names shadow-only. Promotion requires fresh "
    "baseline_signal_gate PASS."
)
IN_SAMPLE_NOTE = (
    "Demotion, not promotion. Zero execution strategies is the honest "
    "state given the evidence; shadow telemetry continues. Mainnet "
    "remains blocked."
)

SETTINGS = ROOT / "config" / "settings.yaml"


def main() -> int:
    window_start_ms = int(time.time() * 1000)
    cfg = load_config(SETTINGS)

    exec_list = cfg.get("strategy.phase08.execution_strategies") or []
    if exec_list:
        raise SystemExit(
            f"Refusing demotion re-register: execution_strategies not empty "
            f"({exec_list}) — edit settings.yaml first."
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

    print("Phase10 re-registered OK (execution-path demotion)")
    print(f"  experiment_id: {final['experiment_id']}")
    print(f"  window_start_ms: {final['window_start_ms']}")
    print(f"  execution_strategies: {final['execution_strategies']}")
    print("Phase08 re-registered + both asserts PASS")
    print()
    print("NEXT: coordinated paper-bot restart to load the empty execution set.")
    print("  Do not restart from this script — stop.bat / start.bat when ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
