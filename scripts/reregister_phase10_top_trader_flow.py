#!/usr/bin/env python3
"""Re-register Fase 10 / Phase08 after adding TopTraderFlow (shadow-only).

Research add only — execution_strategies unchanged (VWAPDeviation sole paper
control). Restarts the OOS window counter because config_hash includes the
new shadow strategy + tracker settings.

Usage:
  python scripts/reregister_phase10_top_trader_flow.py
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
    "research shadow add — TopTraderFlow (aggregate top-wallet bias via "
    "clearinghouseState poll). No execution_strategies change; no copy-trade. "
    "OOS counter restarts because config_hash includes shadow roster."
)
IN_SAMPLE_NOTE = (
    "VWAPDeviation remains the sole paper execution control (INCONCLUSIVE "
    "sample). TopTraderFlow is shadow-only pending feature screening / "
    "baseline-signal gate. Mainnet still blocked."
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
        r"  # Fase 10 window_start_ms (re-registered 2026-08-11 TopTraderFlow shadow)"
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
    if exec_strats != ["VWAPDeviation"]:
        raise SystemExit(
            f"Refusing re-register: expected execution_strategies=['VWAPDeviation'], "
            f"got {exec_strats}"
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

    print("Phase10 re-registered OK (TopTraderFlow shadow)")
    print(f"  experiment_id: {final['experiment_id']}")
    print(f"  window_start_ms: {final['window_start_ms']}")
    print(f"  execution_strategies: {final['execution_strategies']}")
    print(f"  config_hash: {final['config_hash']}")
    print(f"  reregistration_reason: {final.get('reregistration_reason', '')[:100]}...")
    print("Phase08 re-registered + both asserts PASS")
    print()
    print("NEXT: coordinated paper-bot restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
