#!/usr/bin/env python3
"""Re-register Phase08/Phase10 for the JevJudge EXPERIMENT promotion (2026-09-17).

Owner requested the TypeSafe/Jev experiment run as a real execution
strategy in paper mode — open positions visible on the dashboard, engine
manages SL/TP/hold like any other trade.

This is NOT a baseline_signal_gate PASS and does not pretend to be one:
the manifests carry verdict=EXPERIMENT, an explicit owner-declared
promotion with zero evidence. Layers that keep it bounded:

- ``assert_experiment_paper_only`` (phase08+phase10 boot asserts) refuses
  to boot when mode != paper and phase08.paper_only is off.
- ``JevJudge`` itself emits nothing unless ``_mode == 'paper'``.
- All other strategies remain shadow-only per the 09-17 demotion
  evidence; promotion back still requires a real PASS.

Usage:
  python scripts/ops/reregister_jev_experiment.py

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

GATE_RECORD = {
    "protocol": "experiment-paper-override-v1",
    "strategy": "JevJudge",
    "verdict": "EXPERIMENT",
    "reason": (
        "owner-requested TypeSafe/Jev paper experiment (2026-09-17) — "
        "zero evidence claimed; paper-only enforced by "
        "assert_experiment_paper_only + strategy _mode guard"
    ),
}

REREG_REASON = (
    "JevJudge EXPERIMENT promotion — owner-requested TypeSafe/Jev paper "
    "experiment. Verdict EXPERIMENT recorded (no PASS claimed); all other "
    "strategies stay shadow-only per the 09-17 demotion evidence."
)
IN_SAMPLE_NOTE = (
    "Experiment, not validated promotion. JevJudge trades paper positions "
    "so the experiment is dashboard-visible; offline evaluation via "
    "jev_decisions + jev_eval.py continues in parallel."
)

SETTINGS = ROOT / "config" / "settings.yaml"


def main() -> int:
    window_start_ms = int(time.time() * 1000)
    cfg = load_config(SETTINGS)

    exec_list = cfg.get("strategy.phase08.execution_strategies") or []
    if exec_list != ["JevJudge"]:
        raise SystemExit(
            f"Refusing experiment re-register: expected "
            f"execution_strategies=['JevJudge'], got {exec_list}."
        )

    p10_path = persist_phase10(
        cfg,
        overwrite=True,
        now_ms=window_start_ms,
        reregistration_reason=REREG_REASON,
        in_sample_selection_note=IN_SAMPLE_NOTE,
        baseline_signal_gate=[GATE_RECORD],
    )
    persist_phase08(
        cfg,
        overwrite=True,
        reregistration_reason=REREG_REASON,
        in_sample_selection_note=IN_SAMPLE_NOTE,
        baseline_signal_gate=[GATE_RECORD],
    )

    assert_phase08(cfg)
    assert_phase10(cfg)

    final = load_preregister_manifest(p10_path)
    assert final is not None
    assert int(final["window_start_ms"]) == window_start_ms

    print("Phase10+Phase08 re-registered OK (JevJudge EXPERIMENT)")
    print(f"  experiment_id: {final['experiment_id']}")
    print(f"  window_start_ms: {final['window_start_ms']}")
    print(f"  execution_strategies: {final['execution_strategies']}")
    print(f"  gate record: {GATE_RECORD['verdict']} "
          f"({GATE_RECORD['protocol']})")
    print()
    print("NEXT: coordinated paper-bot restart to load JevJudge.")
    print("  Do not restart from this script — stop.bat / start.bat when ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
