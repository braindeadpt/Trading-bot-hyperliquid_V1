#!/usr/bin/env python3
"""Re-register Fase 10 / Phase08 after the LiquidationCatcher bracket retune.

Shadow-only change — execution_strategies unchanged (JevJudge sole paper
control). take_profit_r 2.0→1.5 and max_hold_minutes 30→480 come from the
offline shadow_decisions sweep (scripts/research/shadow_bracket_sweep.py,
n≈190, NET tier-0 + funding fallback). The config_hash includes strategy
params, so the OOS window counter restarts — documented reason recorded.

Usage:
  python scripts/ops/reregister_phase10_lc_bracket.py
"""

from __future__ import annotations

import re
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
    "research shadow — LiquidationCatcher bracket retune (TP 2R→1.5R, "
    "max_hold 30min→8h) from offline shadow_decisions sweep n≈190: holds <2h "
    "net-negative (fees eat the bounce), 24h gives it back, 4–8h sweet spot. "
    "No execution_strategies change; OOS counter restarts because "
    "config_hash includes strategy params."
)
IN_SAMPLE_NOTE = (
    "JevJudge remains the sole paper execution control (owner-requested "
    "EXPERIMENT). LiquidationCatcher stays shadow-only — sweep was in-sample "
    "argmax; fresh decisions under the tuned bracket are the forward OOS "
    "confirmation before any baseline_signal_gate candidacy. "
    "Mainnet still blocked."
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
        r"  # Fase 10 window_start_ms (re-registered 2026-09-18 LC bracket retune)"
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

    exec_strats = list(
        (cfg.get("strategy.phase08") or {}).get("execution_strategies") or []
    )
    if exec_strats != ["JevJudge"]:
        raise SystemExit(
            f"Refusing re-register: expected execution_strategies=['JevJudge'], "
            f"got {exec_strats}"
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

    print("Phase10 re-registered OK (LiquidationCatcher bracket retune)")
    print(f"  experiment_id: {final['experiment_id']}")
    print(f"  window_start_ms: {final['window_start_ms']}")
    print(f"  execution_strategies: {final['execution_strategies']}")
    print(f"  config_hash: {final['config_hash']}")
    reason_preview = str(final.get("reregistration_reason", ""))[:100].encode(
        "ascii", "replace"
    ).decode()
    print(f"  reregistration_reason: {reason_preview}...")
    print("Phase08 re-registered + both asserts PASS")
    print()
    print("NEXT: coordinated paper-bot restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
