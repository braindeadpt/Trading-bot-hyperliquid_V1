#!/usr/bin/env python3
"""Re-register Phase08/Phase10 for the JevJudge exit-geometry fix (2026-10-04).

Diagnosed on 90 closed paper trades (window 2026-09-18 -> 2026-10-04):
net -$21.43 vs gross +$300.99 — fees $322.42 and a broken exit geometry:

  * ``sl_pct_min: 0.01`` beat ``2 x ATR(15m)`` (0.37-0.82%) on ALL four
    symbols — ``sl_atr_mult`` was dead code, SL identical everywhere.
  * TP = 2R = 2% was unreachable inside ``max_hold_hours: 4`` — measured
    P(>=2% move in 4h): BTC ~6%, ETH ~12%, SOL ~14%, HYPE ~28% vs SL hit
    odds 22-56%. Observed SL:TP exits 27:8, exactly the asymmetry priced in.
  * ``JevJudge`` was absent from ``execution.maker_orders.strategies`` —
    a 4h-horizon hourly verdict has no entry urgency; taker entry cost
    ~$3.58/trade vs ~$1.19 maker.

Change (single atomic re-registration):

  * ``sl_pct_min`` 1% -> 0.5% — floor becomes a fee-distance sanity bound,
    ATR scaling decides on ETH/SOL/HYPE (0.53/0.54/0.82%), BTC floored.
  * ``tp_r_mult`` 2.0 -> 1.0 — symmetric barriers. The experiment measures
    Jev's directional accuracy, not geometry luck. NOTE: at the measured
    ~47% directional accuracy this is expected to read net-negative —
    the fix corrects the MEASUREMENT, it does not claim profit.
  * ``JevJudge`` added to ``execution.maker_orders.strategies`` — passive
    entry, exits stay taker. CAVEAT declared: paper mode fills instantly
    at limit (no adverse-selection or missed-fill modelling), so the fee
    saving is modelled, not measured.

OOS window RESTARTED (owner decision 2026-10-04): the new geometry changes
the economics of every trade — the 90-trade population is not poolable
with what follows (same precedent as the tier-0 fee re-registration).
Prior manifests archived as .superseded.*.json by persist overwrite.

The JevJudge EXPERIMENT gate record is carried forward verbatim —
verdict stays EXPERIMENT, zero evidence claimed, paper-only enforced.

Usage:
  python scripts/ops/reregister_jev_geometry_v2.py

Does NOT restart the bot — apply with a coordinated restart.
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
    "JevJudge exit-geometry fix (2026-10-04) — measurement correction, not "
    "a profit claim: sl_pct_min 1%->0.5% (floor beat 2xATR on all symbols; "
    "sl_atr_mult was dead code), tp_r_mult 2R->1R (2% TP unreachable in the "
    "4h hold; observed SL:TP 27:8 mirrored the asymmetry), JevJudge added "
    "to maker_orders.strategies (passive entry; paper fill model is "
    "optimistic — saving modelled not measured). Directional accuracy "
    "~47% on n=90 — no edge demonstrated; verdict stays EXPERIMENT. "
    "OOS window RESTARTED: trade economics changed, populations not poolable."
)
IN_SAMPLE_NOTE = (
    "Geometry-fix re-registration. 90-trade geometry-v1 population archived "
    "separately (pre-fix window). JevJudge remains the sole paper execution "
    "control (EXPERIMENT, zero evidence); no promotion; mainnet blocked."
)

SETTINGS = ROOT / "config" / "settings.yaml"


def main() -> int:
    cfg = load_config(SETTINGS)

    # Guard 1: execution set must be exactly the JevJudge experiment.
    exec_list = cfg.get("strategy.phase08.execution_strategies") or []
    if exec_list != ["JevJudge"]:
        raise SystemExit(
            f"Refusing re-register: expected execution_strategies="
            f"['JevJudge'], got {exec_list}."
        )

    # Guard 2: the geometry change must actually be in the config —
    # refusing to re-register a no-op protects the manifest's meaning.
    jcfg = cfg.get("strategy.jev_judge", {}) or {}
    if float(jcfg.get("sl_pct_min", -1)) != 0.005:
        raise SystemExit(
            f"Refusing re-register: sl_pct_min={jcfg.get('sl_pct_min')!r} "
            "— expected 0.005 (floor fix not applied)."
        )
    if float(jcfg.get("tp_r_mult", -1)) != 1.0:
        raise SystemExit(
            f"Refusing re-register: tp_r_mult={jcfg.get('tp_r_mult')!r} "
            "— expected 1.0 (symmetric-barrier fix not applied)."
        )
    makers = cfg.get("execution.maker_orders.strategies", []) or []
    if "JevJudge" not in [str(s) for s in makers]:
        raise SystemExit(
            "Refusing re-register: JevJudge missing from "
            "execution.maker_orders.strategies."
        )

    # Window RESTARTS: omitting now_ms makes persist stamp window_start_ms
    # with the freeze instant — a deliberate new population, per the
    # tier-0 fee re-registration precedent.
    p10_path = persist_phase10(
        cfg,
        overwrite=True,
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
    assert int(final["window_start_ms"]) > 1789694015461, (
        "window must restart — expected window_start_ms > prior freeze"
    )

    print("Phase10+Phase08 re-registered OK (JevJudge geometry fix)")
    print(f"  experiment_id:        {final['experiment_id']}")
    print(f"  window_start_ms:      {final['window_start_ms']}  (RESTARTED)")
    print(f"  window_min_end_ms:    {final['window']['min_end_ms']}")
    print(f"  execution_strategies: {final['execution_strategies']}")
    print(f"  config_hash:          {final['config_hash']}")
    print()
    print("NEXT: update FROZEN_FASE10_HASH in the frozen-hash tests to the")
    print("  printed config_hash, then coordinated bot restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
