#!/usr/bin/env python3
"""Re-register Fase 10 / Phase08 after making ``research.database`` hash-neutral.

Research data-collection change only — no strategy, risk or execution change:

  * ``l2_recording`` joins ``dvol_feed`` and ``feed_age_history`` in
    ``_sanitize_config_for_hash`` skip_keys. Its schedule and storage
    destination affect no trading/risk parameter, so relocating the L2
    snapshots must not trip the mid-window drift assert.
  * ``market_data.l2_recording.path`` moves back to the E: HDD, undoing the
    regression introduced by a26dee1 which sent ~29 MB/day of research data to
    the system SSD and split the dataset across two locations.

IMPORTANT — the OOS window is PRESERVED (``window_start_ms`` unchanged).
Unlike the tier-0 fee re-registration, nothing here alters the economics of an
executed trade, so discarding the accumulated out-of-sample evidence would be
wrong. ``ignore_trades_before_ms`` is deliberately NOT touched.

Usage:
  python scripts/reregister_phase10_l2_path_hash_neutral.py

Does NOT restart the paper bot. The new path only takes effect on the next
start; until then the recorder keeps writing to the old location.
"""

from __future__ import annotations

import sys
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
from src.utils.config import Config, compute_config_hash, load_config  # noqa: E402

REREG_REASON = ("research data-collection change only — research.database excluded from the config hash BY PATH (a bare `database` key also exists at top level for the operational DB and must stay frozen; research strict_mode / min_coverage_pct / gap_intervals remain hashed). The 3.8 GB research DB moves to the E: HDD per docs/DATA_ARCHITECTURE.md. No strategy, risk, fee or execution_strategies change. OOS window PRESERVED.")
IN_SAMPLE_NOTE = ("Research DB relocation only. VWAPDeviation remains the sole paper execution control (INCONCLUSIVE sample). No promotion; mainnet still blocked. Accumulated OOS evidence intentionally retained.")

EXPECTED_PATH_PREFIX = "E:"
P10_PATH = ROOT / "data" / "research" / "phase10" / "phase10_preregister.json"


def _assert_hash_neutral(cfg: Config) -> None:
    """Refuse to re-register unless l2_recording truly no longer affects the hash."""
    import copy

    base = compute_config_hash(cfg)
    for key, value in (("path", "X:/some/other/place"),):
        raw = copy.deepcopy(cfg.raw)
        raw["research"]["database"][key] = value
        if compute_config_hash(Config(raw)) != base:
            raise SystemExit(
                f"Refusing re-register: research.database.{key} still changes the "
                "config hash — add it to _sanitize_config_for_hash skip_keys first."
            )


def main() -> int:
    prior = load_preregister_manifest(P10_PATH)
    if prior is None:
        raise SystemExit(f"No existing Fase 10 manifest at {P10_PATH}")

    window_start_ms = int(prior["window_start_ms"])
    prior_execution = list(prior["execution_strategies"])

    cfg = load_config(ROOT / "config" / "settings.yaml")

    # Guard 1: execution set must be untouched — this is not a trading change.
    live_execution = list(cfg.get("strategy.phase08.execution_strategies", []))
    if live_execution != prior_execution:
        raise SystemExit(
            "Refusing re-register: execution_strategies changed "
            f"({prior_execution} -> {live_execution}). This script is only for "
            "research storage changes; use a dedicated re-registration instead."
        )

    # Guard 2: the L2 path must actually be off the system disk.
    l2_path = str(cfg.get("research.database.path", ""))
    if not l2_path.upper().startswith(EXPECTED_PATH_PREFIX):
        raise SystemExit(
            f"Refusing re-register: expected the L2 path on {EXPECTED_PATH_PREFIX}, "
            f"got {l2_path!r}"
        )

    # Guard 3: hash-neutrality must already hold.
    _assert_hash_neutral(cfg)

    p10_path = persist_phase10(
        cfg,
        overwrite=True,
        now_ms=window_start_ms,          # PRESERVE the OOS window
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
    assert int(final["window_start_ms"]) == window_start_ms, "OOS window drifted"
    assert list(final["execution_strategies"]) == prior_execution

    print("Phase10 re-registered OK (research.database hash-neutral + path -> E:)")
    print(f"  experiment_id:        {final['experiment_id']}")
    print(f"  window_start_ms:      {final['window_start_ms']}  (PRESERVED)")
    print(f"  execution_strategies: {final['execution_strategies']}")
    print(f"  config_hash:          {final['config_hash']}")
    print(f"  research.db path:     {l2_path}")
    print("Phase08 re-registered + both asserts PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
