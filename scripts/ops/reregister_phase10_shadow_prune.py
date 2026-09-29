#!/usr/bin/env python3
"""Re-register Fase 10 / Phase08 after the 2026-09-29 shadow-strategy prune.

Observability-scope change only — no strategy, risk, fee or execution change:

  * ``strategy.phase08.shadow_strategies`` is now excluded by path in
    ``_sanitize_config_for_hash`` — same hash-neutral precedent as
    ``dvol_feed`` / ``feed_age_history`` / ``l2_recording`` /
    ``research.database``. Shadow strategies are signal-tracked and never
    reach risk, sizing or execution, so pruning the watched set cannot
    drift live behaviour mid-window.
  * The active shadow list drops four strategies on decisive persisted
    evidence (net PF, tier-0 fees already applied):
      OrderBookScalper 0.21 / 25,002 evals, TopTraderFlow 0.31 / 3,786,
      ChecklistMeta 0.63 / 157 (plus -905.27 USD / 184t live),
      VWAPDeviation 0.52 / 28 (plus -220.84 USD / 44t live).
    Historical ``shadow_decisions`` rows are untouched — the prune only
    stops new recording. VWAPDeviation/ChecklistMeta param sections remain
    frozen in the manifest (frozen_params asserted against live config).
    ``regime_router.fallback_strategy`` stays VWAPDeviation — vestigial
    (no VWAP signals exist to route), and changing it would alter hashed
    router params.
  * A ``phase08_shadow_maker`` variant was added to the shadow outcome
    evaluator (resting-limit counterfactual, maker-in/taker-out fees) —
    research-side scoring only, no live-config effect.

IMPORTANT — the OOS window is PRESERVED (``window_start_ms`` unchanged).
Nothing here alters the economics of an executed trade, so discarding the
accumulated out-of-sample evidence would be wrong. ``experiment_id`` /
``supersedes`` bookkeeping follows the persist overwrite convention.
The JevJudge EXPERIMENT ``baseline_signal_gate`` record is carried forward
verbatim into both manifests — dropping it would silently remove the
paper-only enforcement the bridged gate assert depends on.

Usage:
  python scripts/ops/reregister_phase10_shadow_prune.py

Does NOT restart the bot. The pruned shadow set takes effect on the next
start; until then the pruned strategies keep recording signals.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.research.phase08_preregister import (  # noqa: E402
    assert_config_matches_preregister as assert_phase08,
    load_preregister_manifest as load_phase08,
    persist_preregister_manifest as persist_phase08,
)
from src.research.phase10_preregister import (  # noqa: E402
    assert_config_matches_preregister as assert_phase10,
    load_preregister_manifest as load_phase10,
    persist_preregister_manifest as persist_phase10,
)
from src.utils.config import Config, compute_config_hash, load_config  # noqa: E402

REREG_REASON = (
    "research observability-scope change only — strategy.phase08.shadow_strategies "
    "excluded from the config hash by path (same precedent as research.database / "
    "l2_recording: watch-list scope, not trading params). Shadow prune drops "
    "OrderBookScalper (net pf 0.21, n=25,002), TopTraderFlow (0.31, n=3,786), "
    "ChecklistMeta (0.63, n=157; -905USD/184t live), VWAPDeviation (0.52, n=28; "
    "-221USD/44t live) on decisive net-of-tier-0-fee evidence; historical "
    "shadow_decisions preserved. No strategy, risk, fee or execution_strategies "
    "change. OOS window PRESERVED."
)
IN_SAMPLE_NOTE = (
    "Shadow-scope prune + maker-variant evaluator only. JevJudge remains the "
    "sole paper execution control (owner-requested EXPERIMENT, zero evidence "
    "claimed). No promotion; mainnet still blocked. Accumulated OOS evidence "
    "intentionally retained."
)

EXPECTED_EXECUTION = ["JevJudge"]


def _assert_hash_neutral(cfg: Config) -> None:
    """Refuse to re-register unless shadow_strategies truly leaves the hash."""
    base = compute_config_hash(cfg)
    raw = copy.deepcopy(cfg.raw)
    raw["strategy"]["phase08"]["shadow_strategies"] = ["X", "Y", "Z"]
    if compute_config_hash(Config(raw)) != base:
        raise SystemExit(
            "Refusing re-register: shadow_strategies still changes the config "
            "hash — add it to _sanitize_config_for_hash skip_paths first."
        )


def _gate_from(manifest: dict) -> list:
    """Carry the baseline_signal_gate record forward verbatim."""
    gate = manifest.get("baseline_signal_gate")
    if not gate:
        raise SystemExit(
            "Refusing re-register: prior manifest has no baseline_signal_gate — "
            "dropping the JevJudge EXPERIMENT record would remove the "
            "paper-only gate. Carry it forward explicitly."
        )
    return gate


def main() -> int:
    p10_prior = load_phase10()
    if p10_prior is None:
        raise SystemExit("No existing Fase 10 manifest")
    p08_prior = load_phase08()
    if p08_prior is None:
        raise SystemExit("No existing Phase08 manifest")

    window_start_ms = int(p10_prior["window_start_ms"])
    prior_execution = list(p10_prior["execution_strategies"])
    gate10 = _gate_from(p10_prior)
    gate08 = _gate_from(p08_prior)

    cfg = load_config(ROOT / "config" / "settings.yaml")

    # Guard 1: execution set must be untouched — this is not a trading change.
    live_execution = sorted(
        str(s) for s in cfg.get("strategy.phase08.execution_strategies", []) or []
    )
    if live_execution != prior_execution or live_execution != EXPECTED_EXECUTION:
        raise SystemExit(
            "Refusing re-register: execution_strategies changed "
            f"({prior_execution} -> {live_execution}). This script is only for "
            "shadow-scope changes; use a dedicated re-registration instead."
        )

    # Guard 2: hash-neutrality must already hold for the pruned knob.
    _assert_hash_neutral(cfg)

    persist_phase10(
        cfg,
        overwrite=True,
        now_ms=window_start_ms,          # PRESERVE the OOS window
        reregistration_reason=REREG_REASON,
        in_sample_selection_note=IN_SAMPLE_NOTE,
        baseline_signal_gate=gate10,
    )
    persist_phase08(
        cfg,
        overwrite=True,
        reregistration_reason=REREG_REASON,
        in_sample_selection_note=IN_SAMPLE_NOTE,
        baseline_signal_gate=gate08,
    )

    assert_phase08(cfg)
    assert_phase10(cfg)

    final = load_phase10()
    assert final is not None
    assert int(final["window_start_ms"]) == window_start_ms, "OOS window drifted"
    assert list(final["execution_strategies"]) == prior_execution

    print("Phase10 re-registered OK (shadow_strategies hash-neutral + 4 pruned)")
    print(f"  experiment_id:        {final['experiment_id']}")
    print(f"  window_start_ms:      {final['window_start_ms']}  (PRESERVED)")
    print(f"  execution_strategies: {final['execution_strategies']}")
    print(f"  config_hash:          {final['config_hash']}")
    print("Phase08 re-registered + both asserts PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
