#!/usr/bin/env python3
"""Re-register Fase 10 / Phase08 after disabling WS trade-tape collection.

PREPARED 2026-10-07 — NOT YET APPLIED. Run only with the owner's explicit
go-ahead, together with the settings.yaml diff below and a coordinated
bot restart.

Observability-scope change only — no strategy, risk, fee or execution change:

  config/settings.yaml (research section):
      ws_microstructure_enabled: true   ->  false
      (optionally) sample_microstructure: false   — vestigial flag, no
      runtime consumer; add only if the owner wants the marker in YAML.

  Effects when the bot next starts:
    * ``start_microstructure_recorder_from_config`` returns None ->
      ResearchMicrostructureRecorder never starts. Stops persistence of
      ``trade_tape`` (~103 MB/day measured) AND, as collateral,
      ``l2_snapshots``, ``microstructure_gaps`` and
      ``feed_health_snapshots`` — the recorder owns all four writers.
      ``market_data.l2_recording`` (the gzip L2 book files used by
      OrderBookScalper research) is a SEPARATE writer and stays on.
    * No runtime or backtest code reads ``trade_tape`` today
      (CVDOrderFlow was retired 2026-09-10); future OrderBookScalper
      Tier-A backtests degrade because ``l2_snapshots`` stops growing —
      accepted by the owner as part of the tape-off decision.
    * Feed-silence watchdog: no contract covers trade_tape / l2_snapshots /
      microstructure_gaps / feed_health_snapshots, so no false alarms.
      ``l2_book_recording`` stays contracted (l2_recording stays on).

  The change DOES alter ``config_hash`` (the flag is hashed by design —
  unlike the hash-skipped research.database path). Measured 2026-10-07:
      hash before (ws_microstructure_enabled: true)   = b5b5d62c50b551da
      hash after  (ws_microstructure_enabled: false)  = 4077927fec6a880c
      hash after  (both flags false, incl. vestigial
                   sample_microstructure)             = 525d249666a719a4

IMPORTANT — the OOS window is PRESERVED (``window_start_ms`` unchanged).
Nothing here alters the economics of an executed trade, so discarding the
accumulated out-of-sample evidence would be wrong. ``experiment_id`` /
``supersedes`` bookkeeping follows the persist overwrite convention.
The JevJudge EXPERIMENT ``baseline_signal_gate`` record is carried forward
verbatim into both manifests.

Usage (after applying the settings.yaml diff):
  python scripts/ops/reregister_phase10_tape_off.py

Does NOT restart the bot. The tape-off takes effect on the next start.
"""

from __future__ import annotations

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
from src.utils.config import load_config  # noqa: E402

REREG_REASON = (
    "research cost reduction — stop WS trade_tape collection "
    "(research.ws_microstructure_enabled: false; ~103 MB/day measured, no "
    "runtime or backtest consumer since CVDOrderFlow retired 2026-09-10). "
    "Collateral: l2_snapshots/microstructure_gaps/feed_health_snapshots "
    "stop too (same recorder). market_data.l2_recording untouched. "
    "config_hash legitimately drifts — OOS window PRESERVED."
)
IN_SAMPLE_NOTE = (
    "Tape-off only. JevJudge remains the sole paper execution control "
    "(owner-requested EXPERIMENT, zero evidence claimed). No promotion; "
    "mainnet still blocked. Accumulated OOS evidence intentionally retained."
)

EXPECTED_EXECUTION = ["JevJudge"]


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
            "the tape-off change; use a dedicated re-registration instead."
        )

    # Guard 2: the tape flag must actually be off — refuse to stamp a
    # manifest claiming tape-off while the recorder still runs.
    if bool(cfg.get("research.ws_microstructure_enabled", True)):
        raise SystemExit(
            "Refusing re-register: research.ws_microstructure_enabled is "
            "still true — apply the settings.yaml diff first."
        )

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

    print("Phase10 re-registered OK (tape-off; OOS window preserved)")
    print(f"  experiment_id:        {final['experiment_id']}")
    print(f"  window_start_ms:      {final['window_start_ms']}  (PRESERVED)")
    print(f"  execution_strategies: {final['execution_strategies']}")
    print(f"  config_hash:          {final['config_hash']}")
    print("Phase08 re-registered + both asserts PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
