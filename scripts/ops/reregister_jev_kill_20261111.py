#!/usr/bin/env python3
"""Re-register Phase08/Phase10 after JevJudge final KILL readout (2026-11-11).

Per docs/ops/PREREGISTER_JEV_OOS_KILL_2026-10-08.md:
  - Post-boundary (entry_time > 1791124780000) independent closed trades:
    indep_n = 325 (target >= 100 met, 0 stale verdicts).
  - Net PF on recorded pnl_pct = 0.8373 (<= 1.0 kill threshold).
  - Net PnL = -$679.35 USD (WR 50.77%, rolling Sharpe -1.715).

Verdict: KILL — JevJudge disabled, hl-jev-judge stopped, execution_strategies
emptied to []. Shadow evaluations (CarryA1, BorosLockedSpread, UnlockShort,
ResMom, VWAP iv_gate) continue unaffected.
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
from src.utils.config import compute_config_hash, load_config  # noqa: E402

REREG_REASON = (
    "JevJudge KILLED at final preregistered readout (2026-11-11, "
    "docs/ops/PREREGISTER_JEV_OOS_KILL_2026-10-08.md): post-boundary "
    "indep_n=325 (>=100 target), net PF=0.8373 (<=1.0 kill threshold), "
    "net PnL=-$679.35 USD (BTC -$203.91/87t, ETH -$277.88/66t, SOL "
    "-$265.53/67t, HYPE +$67.97/105t). JevJudge disabled and removed from "
    "execution_strategies (now []); hl-jev-judge container stopped. "
    "Independent shadow studies continue unaffected."
)
IN_SAMPLE_NOTE = (
    "One-shot preregistered kill execution — zero strategies executing in "
    "TradingEngine; no retuning or second JevJudge version."
)

SETTINGS = ROOT / "config" / "settings.yaml"


def main() -> int:
    window_start_ms = int(time.time() * 1000)
    cfg = load_config(SETTINGS)

    exec_list = cfg.get("strategy.phase08.execution_strategies") or []
    if exec_list:
        raise SystemExit(
            f"Refusing Jev kill re-register: execution_strategies must be empty, got {exec_list}"
        )
    if bool(cfg.get("strategy.jev_judge.enabled", False)):
        raise SystemExit(
            "Refusing Jev kill re-register: strategy.jev_judge.enabled must be false"
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
    new_hash = compute_config_hash(cfg)

    print("Phase10+Phase08 re-registered OK (JevJudge KILLED 2026-11-11)")
    print(f"  experiment_id:        {final['experiment_id']}")
    print(f"  window_start_ms:      {final['window_start_ms']}")
    print(f"  config_hash:          {new_hash}")
    print(f"  execution_strategies: {final['execution_strategies']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
