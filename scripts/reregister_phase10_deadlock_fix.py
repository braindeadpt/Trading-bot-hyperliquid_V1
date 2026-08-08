"""Re-register Fase 10 (and Phase08) after the v3.1.48 structural deadlock fix.

Archives prior manifests, freezes a new window with explicit justification,
and writes ``ignore_trades_before_ms`` = new ``window_start_ms`` into
``config/settings.yaml`` so the governor ignores pre-window trades.

Usage:
  python scripts/reregister_phase10_deadlock_fix.py
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
    "deadlock estrutural — 138 bloqueios regime_expansion_no_allowed_strategies; "
    "execution_strategies desativadas a 100% pelo governor; "
    "bot inoperante (1 trade/semana)."
)
IN_SAMPLE_NOTE = (
    "Promoting ChecklistMeta is partly informed by in-sample results "
    "(59% WR vs 42% VolatilityBreakout in the 10/07–08/08 audit). "
    "This is in-sample selection and must be treated as such — out-of-sample "
    "walk-forward validation is mandatory before treating this ruleset as validated. "
    "Does not invalidate the change (alternative was an inoperable bot)."
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
        r"  # Fase 10 window_start_ms (re-registered 2026-08-08 deadlock fix)"
    )
    new_text, n = pattern.subn(replacement, text, count=1)
    if n != 1:
        raise SystemExit(
            "Failed to update ignore_trades_before_ms in settings.yaml "
            f"(matches={n})"
        )
    path.write_text(new_text, encoding="utf-8")


def main() -> int:
    # Freeze the window start first, stamp YAML, then persist once so
    # config_hash and window_start_ms are consistent without re-loops.
    window_start_ms = int(time.time() * 1000)
    _set_ignore_trades_before_ms(SETTINGS, window_start_ms)
    cfg = load_config(SETTINGS)

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

    print("Phase10 re-registered OK")
    print(f"  experiment_id: {final['experiment_id']}")
    print(f"  window_start_ms: {final['window_start_ms']}")
    print(f"  execution_strategies: {final['execution_strategies']}")
    print(f"  config_hash: {final['config_hash']}")
    print(f"  reregistration_reason: {final.get('reregistration_reason', '')[:80]}...")
    print("Phase08 re-registered + both asserts PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
