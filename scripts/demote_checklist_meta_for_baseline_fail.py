"""Apply ChecklistMeta demotion after baseline-gate FAIL (requires explicit confirm).

Moves ChecklistMeta from execution_strategies → shadow_strategies.
Leaves VWAPDeviation as sole execution strategy (INCONCLUSIVE / grandfathered).
Re-registers Phase08 + Phase10 preregisters with justification.

Usage:
  python scripts/demote_checklist_meta_for_baseline_fail.py --dry-run
  python scripts/demote_checklist_meta_for_baseline_fail.py --apply
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.phase08_preregister import (  # noqa: E402
    LEGACY_EXECUTION_WITHOUT_BASELINE_GATE,
    persist_preregister_manifest as persist_phase08,
)
from src.research.phase10_preregister import (  # noqa: E402
    persist_preregister_manifest as persist_phase10,
)
from src.utils.config import load_config  # noqa: E402

SETTINGS = ROOT / "config" / "settings.yaml"

REREG_REASON = (
    "Despromoção ChecklistMeta por FAIL no baseline-signal gate "
    "(B1 W2=48 / W3=43, n≥30, PF<1) — primeiro caso de aplicação real do portão; "
    "execution_strategies=[VWAPDeviation] only (INCONCLUSIVE grandfather)."
)
IN_SAMPLE_NOTE = (
    "Not a promotion. ChecklistMeta demoted after powered FAIL on both W2 and W3. "
    "VWAPDeviation retained solely because underpowered (n<30), not because it passed."
)

TARGET_EXEC = ["VWAPDeviation"]
TARGET_SHADOW = [
    "VolatilityBreakout",
    "CVDOrderFlow",
    "OrderBookScalper",
    "FundingArbitrage",
    "FundingMomentum",
    "SpotPerpCarry",
    "LeadLag",
    "LiquidationCatcher",
    "ChecklistMeta",
]


def _replace_phase08_lists(text: str) -> str:
    """Rewrite execution_strategies / shadow_strategies / fallback under phase08."""
    exec_block = (
        "    execution_strategies:\n"
        + "".join(f"      - {n}\n" for n in TARGET_EXEC)
    )
    shadow_block = (
        "    shadow_strategies:\n"
        + "".join(f"      - {n}\n" for n in TARGET_SHADOW)
    )

    # Replace from execution_strategies through shadow list end (before regime_router)
    pattern = re.compile(
        r"    execution_strategies:\n(?:      - .+\n)+"
        r"    shadow_strategies:\n(?:      - .+\n)+",
        re.MULTILINE,
    )
    new_text, n = pattern.subn(exec_block + shadow_block, text, count=1)
    if n != 1:
        raise SystemExit(f"Failed to rewrite phase08 strategy lists (matches={n})")

    # fallback_strategy must be an execution name
    new_text2, n2 = re.subn(
        r"(fallback_strategy:\s*)\S+",
        r"\1VWAPDeviation",
        new_text,
        count=1,
    )
    if n2 != 1:
        raise SystemExit(f"Failed to set fallback_strategy (matches={n2})")
    return new_text2


def _set_ignore_trades_before_ms(path: Path, window_start_ms: int) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(ignore_trades_before_ms:\s*)\d+(\s*#.*)?",
        re.MULTILINE,
    )
    replacement = (
        rf"\g<1>{window_start_ms}"
        r"  # Fase 10 window_start_ms (re-registered: CM baseline FAIL demotion)"
    )
    new_text, n = pattern.subn(replacement, text, count=1)
    if n != 1:
        raise SystemExit(
            f"Failed to update ignore_trades_before_ms (matches={n})"
        )
    path.write_text(new_text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if not args.dry_run and not args.apply:
        ap.error("Specify --dry-run or --apply")

    text = SETTINGS.read_text(encoding="utf-8")
    proposed = _replace_phase08_lists(text)
    print("=== Proposed phase08 lists ===")
    print(f"execution: {TARGET_EXEC}")
    print(f"shadow: {TARGET_SHADOW}")
    print(f"fallback_strategy: VWAPDeviation")
    print(f"legacy soft-exempt remains: {sorted(LEGACY_EXECUTION_WITHOUT_BASELINE_GATE)}")
    if args.dry_run:
        print("Dry-run only — settings.yaml not written.")
        return 0

    SETTINGS.write_text(proposed, encoding="utf-8")
    window_start_ms = int(time.time() * 1000)
    _set_ignore_trades_before_ms(SETTINGS, window_start_ms)
    cfg = load_config(SETTINGS)

    persist_phase08(
        cfg,
        overwrite=True,
        reregistration_reason=REREG_REASON,
        in_sample_selection_note=IN_SAMPLE_NOTE,
    )
    persist_phase10(
        cfg,
        overwrite=True,
        now_ms=window_start_ms,
        reregistration_reason=REREG_REASON,
        in_sample_selection_note=IN_SAMPLE_NOTE,
    )
    print(f"Applied. window_start_ms={window_start_ms}")
    print("Restart paper bot to load new execution set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
