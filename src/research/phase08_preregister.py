"""Phase 08 pre-registration manifest — frozen before Phase 06 OOS walk-forward.

``tier_a_hl_ohlc`` certifies *data availability* only. No edge is claimed until
the Phase 06 out-of-sample protocol completes successfully.

Once written, the manifest is immutable (content hash verified on startup).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

from src.utils.config import Config, compute_config_hash, get_strategy_section
from src.utils.helpers import safe_write_file, validate_safe_path

logger = logging.getLogger(__name__)

PREREGISTER_PROTOCOL = "phase08-preregister-v2"
DEFAULT_PATH = Path("data") / "research" / "phase08_preregister.json"

VB_PARAM_KEYS = (
    "bb_period", "bb_std", "squeeze_lookback", "squeeze_percentile",
    "min_squeeze_bars", "volume_surge", "min_adx", "max_adx",
    "stop_loss_atr_multiplier", "take_profit_atr_multiplier", "max_hold_hours",
    "min_confidence", "signal_throttle_ms", "require_trend_alignment",
)
VWAP_PARAM_KEYS = (
    "z_threshold", "min_adx", "max_adx", "volume_surge",
    "stop_loss_atr_multiplier", "take_profit_r_multiple", "max_hold_hours",
    "min_confidence", "signal_throttle_ms", "use_session_filter",
    "session_start_utc_h", "session_end_utc_h", "exit_z_threshold",
)
CHECKLIST_PARAM_KEYS = (
    "score_threshold", "min_confidence", "min_adx_gate", "dominance_margin",
    "flip_block_minutes", "base_size_pct", "max_size_pct",
    "stop_loss_atr_multiplier", "take_profit_atr_multiplier", "max_hold_hours",
    "signal_throttle_ms", "use_sl_to_be_after_1r", "sl_to_be_trigger_r",
)


class PreregisterManifestError(RuntimeError):
    """Raised when live config drifts from the frozen preregister manifest."""


def _pick_params(section: Dict[str, Any], keys: tuple) -> Dict[str, Any]:
    return {k: section.get(k) for k in keys if k in section}


def _expected_trades_per_week(throttle_ms: int) -> float:
    if throttle_ms <= 0:
        return 0.0
    per_symbol = (7 * 24 * 3600 * 1000) / throttle_ms
    return round(per_symbol * 4, 2)


def _hash_manifest_body(manifest: Dict[str, Any]) -> str:
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_preregister_manifest(
    config: Config,
    *,
    experiment_id: Optional[str] = None,
    reregistration_reason: Optional[str] = None,
    in_sample_selection_note: Optional[str] = None,
    supersedes_experiment_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the pre-registration document from live config."""
    p08 = get_strategy_section(config, "phase08")
    vb = get_strategy_section(config, "volatility_breakout")
    vwap = get_strategy_section(config, "vwap_deviation")
    checklist = get_strategy_section(config, "checklist_meta")
    regime = p08.get("regime_router", {}) or {}
    adx_cfg = p08.get("adx", {}) or {}

    vb_throttle = int(vb.get("signal_throttle_ms", 1_800_000))
    vwap_throttle = int(vwap.get("signal_throttle_ms", 300_000))
    checklist_throttle = int(checklist.get("signal_throttle_ms", 1_800_000))

    manifest: Dict[str, Any] = {
        "protocol": PREREGISTER_PROTOCOL,
        "experiment_id": experiment_id or str(uuid.uuid4()),
        "registered_at_ms": int(time.time() * 1000),
        "config_hash": compute_config_hash(config),
        "edge_demonstrated": False,
        "fidelity_note": (
            "tier_a_hl_ohlc certifies Hyperliquid OHLC data availability only; "
            "it does not certify strategy edge."
        ),
        "execution_scope": {
            "mode": str(config.get("mode", "paper")),
            "paper_only": True,
            "strategies": list(
                p08.get(
                    "execution_strategies",
                    ["ChecklistMeta", "VWAPDeviation"],
                )
            ),
            "shadow_strategies": list(p08.get("shadow_strategies", [])),
        },
        "oos_protocol": "phase06-train-val-test-v1",
        "oos_status": "pending",
        "adx_contract": {
            "timeframe": str(adx_cfg.get("timeframe", "15m")),
            "timeframe_s": int(adx_cfg.get("timeframe_s", 900)),
            "period": int(adx_cfg.get("period", config.get("strategy.adx_period", 14))),
            "closed_candles_only": True,
        },
        "regime_router": {
            "adx_range_threshold": float(regime.get("adx_range_threshold", 20.0)),
            "adx_trend_threshold": float(regime.get("adx_trend_threshold", 25.0)),
            "vb_regimes": ["trend", "expansion"],
            "vwap_regimes": ["range", "low_vol"],
            "checklist_regimes": ["trend", "expansion", "range", "low_vol"],
            "fallback_strategy": str(
                regime.get("fallback_strategy", "ChecklistMeta")
            ),
            "sequential_contradiction_block_ms": int(
                regime.get("sequential_contradiction_block_ms", 3_600_000),
            ),
        },
        "strategies": {
            "VolatilityBreakout": {
                "frozen_params": _pick_params(vb, VB_PARAM_KEYS),
                "max_hold_hours": float(vb.get("max_hold_hours", 6)),
                "signal_throttle_ms": vb_throttle,
                "expected_max_signals_per_symbol_per_week": _expected_trades_per_week(vb_throttle),
                "kill_criteria": {
                    "min_trades_before_eval": 30,
                    "min_rolling_sharpe": 0.0,
                    "max_drawdown_pct": 10.0,
                    "min_profit_factor": 1.0,
                    "halt_if_daily_loss_pct": 3.0,
                },
            },
            "VWAPDeviation": {
                "frozen_params": _pick_params(vwap, VWAP_PARAM_KEYS),
                "max_hold_hours": float(vwap.get("max_hold_hours", 4)),
                "signal_throttle_ms": vwap_throttle,
                "expected_max_signals_per_symbol_per_week": _expected_trades_per_week(vwap_throttle),
                "kill_criteria": {
                    "min_trades_before_eval": 30,
                    "min_rolling_sharpe": 0.0,
                    "max_drawdown_pct": 10.0,
                    "min_profit_factor": 1.0,
                    "halt_if_daily_loss_pct": 3.0,
                },
            },
            "ChecklistMeta": {
                "frozen_params": _pick_params(checklist, CHECKLIST_PARAM_KEYS),
                "max_hold_hours": float(checklist.get("max_hold_hours", 6)),
                "signal_throttle_ms": checklist_throttle,
                "expected_max_signals_per_symbol_per_week": _expected_trades_per_week(
                    checklist_throttle
                ),
                "kill_criteria": {
                    "min_trades_before_eval": 30,
                    "min_rolling_sharpe": 0.0,
                    "max_drawdown_pct": 10.0,
                    "min_profit_factor": 1.0,
                    "halt_if_daily_loss_pct": 3.0,
                },
            },
        },
    }
    if reregistration_reason:
        manifest["reregistration_reason"] = reregistration_reason
    if in_sample_selection_note:
        manifest["in_sample_selection_note"] = in_sample_selection_note
    if supersedes_experiment_id:
        manifest["supersedes_experiment_id"] = supersedes_experiment_id
    manifest["manifest_hash"] = _hash_manifest_body(manifest)
    return manifest


def _resolve_preregister_path(path: Path) -> Path:
    """Ensure preregister path is inside the project and passes validate_safe_path."""
    project_root = Path(__file__).resolve().parents[2]
    resolved = (project_root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        rel = resolved.relative_to(project_root)
    except ValueError as exc:
        raise PreregisterManifestError(
            f"Preregister path must stay inside project: {path}"
        ) from exc
    safe = validate_safe_path(rel.as_posix())
    if safe is None:
        raise PreregisterManifestError(f"Unsafe preregister path: {path}")
    return resolved


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    safe = _resolve_preregister_path(path)
    safe.parent.mkdir(parents=True, exist_ok=True)
    tmp = safe.with_suffix(safe.suffix + ".tmp")
    content = json.dumps(data, indent=2, sort_keys=True)
    if not safe_write_file(tmp, content):
        raise PreregisterManifestError(f"Preregister manifest write failed: {tmp}")
    shutil.move(str(tmp), str(safe))


def persist_preregister_manifest(
    config: Config,
    path: Optional[Path] = None,
    *,
    overwrite: bool = False,
    reregistration_reason: Optional[str] = None,
    in_sample_selection_note: Optional[str] = None,
    baseline_signal_gate: Optional[Any] = None,
) -> Path:
    """Write immutable pre-registration manifest (first write wins).

    When ``overwrite=True``, archive the prior file before replacement so the
    assert is never silently skipped — a new window is opened deliberately.

    ``baseline_signal_gate`` may be attached at write time. Non-legacy strategies
    in ``execution_strategies`` must carry a PASS record (hard entry gate).
    """
    out = path or DEFAULT_PATH
    supersedes: Optional[str] = None
    existing: Optional[Dict[str, Any]] = None
    if out.exists() and not overwrite:
        existing = load_preregister_manifest(out)
        if existing is not None:
            verify_preregister_integrity(existing)
            logger.info("Phase08 preregister manifest exists — immutable: %s", out)
        return out
    if out.exists() and overwrite:
        existing = load_preregister_manifest(out)
        if existing is not None:
            verify_preregister_integrity(existing)
            supersedes = str(existing.get("experiment_id") or "")
            archive = out.with_name(
                f"{out.stem}.superseded.{supersedes or 'unknown'}{out.suffix}"
            )
            shutil.copy2(out, archive)
            logger.warning(
                "Phase08 preregister OVERWRITE — archived prior window to %s",
                archive,
            )
    manifest = build_preregister_manifest(
        config,
        reregistration_reason=reregistration_reason,
        in_sample_selection_note=in_sample_selection_note,
        supersedes_experiment_id=supersedes or None,
    )
    if baseline_signal_gate is not None:
        manifest["baseline_signal_gate"] = baseline_signal_gate
        manifest["manifest_hash"] = _hash_manifest_body(manifest)

    # Hard at entry: any non-grandfathered execution strategy needs PASS.
    # Also refuse promoting a newly added name without PASS even if someone
    # tries to expand LEGACY later — check explicit promotions vs prior set.
    prev_exec = set()
    if existing is not None:
        prev_exec = {
            str(s)
            for s in (existing.get("execution_scope") or {}).get("strategies", []) or []
        }
    new_exec = {
        str(s)
        for s in (manifest.get("execution_scope") or {}).get("strategies", []) or []
    }
    for name in sorted(new_exec - prev_exec):
        # Newly added names: legacy soft-exempt; everything else hard PASS.
        if name in LEGACY_EXECUTION_WITHOUT_BASELINE_GATE:
            continue
        entries = {
            str(e.get("strategy") or ""): e
            for e in _normalize_gate_entries(manifest.get("baseline_signal_gate"))
            if e.get("strategy")
        }
        assert_can_promote_to_execution(name, entries.get(name))

    assert_baseline_signal_gate(manifest, require=False, hard_for_new=True)

    _atomic_write_json(out, manifest)
    logger.info(
        "Phase08 preregister manifest written: %s experiment_id=%s hash=%s",
        out,
        manifest["experiment_id"],
        manifest["manifest_hash"][:16],
    )
    return out


def load_preregister_manifest(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    p = path or DEFAULT_PATH
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def verify_preregister_integrity(manifest: Dict[str, Any]) -> None:
    """Recompute hash; raise if manifest was tampered with."""
    expected = manifest.get("manifest_hash")
    if not expected:
        raise PreregisterManifestError("preregister manifest missing manifest_hash")
    actual = _hash_manifest_body(manifest)
    if actual != expected:
        raise PreregisterManifestError(
            f"preregister manifest hash mismatch (expected {expected[:16]}… got {actual[:16]}…)"
        )


# Strategies allowed to remain in execution_strategies without a baseline_signal_gate
# PASS record. Hard gate applies only when *adding* a strategy that is not in this set.
# Asymmetry is intentional: do not brick the current paper bot for a measurement that
# post-dates these names being live. New promotions must still pass the three-condition gate.
LEGACY_EXECUTION_WITHOUT_BASELINE_GATE = frozenset({"ChecklistMeta", "VWAPDeviation"})


def _normalize_gate_entries(raw: Any) -> list[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict) and "verdict" in raw:
        return [raw]
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, dict):
        return [
            {**v, "strategy": k} if isinstance(v, dict) and "strategy" not in v else v
            for k, v in raw.items()
            if isinstance(v, dict)
        ]
    raise PreregisterManifestError(
        "baseline_signal_gate must be a dict or list of gate records"
    )


def assert_baseline_signal_gate(
    manifest: Dict[str, Any],
    *,
    require: bool = False,
    grandfathered: Optional[FrozenSet[str]] = None,
    hard_for_new: bool = True,
) -> None:
    """Enforce baseline-signal gate for strategies in ``execution_scope``.

    Expected optional manifest field (per strategy or as a list)::

        "baseline_signal_gate": {
          "protocol": "baseline-signal-gate-v1",
          "strategy": "<name>",
          "fold": "W2|W3",
          "b1_pf_percentile": 95.0,
          "n_trades": 30,
          "profit_factor": 1.1,
          "expectancy": 0.5,
          "verdict": "PASS",
          "artifact": "data/backtests/..."
        }

    Asymmetry (hard at entry, soft for legacy):
    - Strategies in ``grandfathered`` (default:
      ``LEGACY_EXECUTION_WITHOUT_BASELINE_GATE``) may run **without** a gate
      record so the current paper bot keeps booting.
    - Any *other* strategy in ``execution_scope`` must have ``verdict == PASS``
      (three-condition gate). Promoting without the field fails closed.
    - If a gate record is present for a grandfathered strategy and is not PASS,
      that still fails (do not claim PASS falsely).
    - ``require=True`` / ``BOT_REQUIRE_BASELINE_GATE=1`` removes grandfathering
      and requires PASS for every execution strategy.
    """
    import os

    require = require or os.environ.get("BOT_REQUIRE_BASELINE_GATE", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
    )
    gf = grandfathered if grandfathered is not None else LEGACY_EXECUTION_WITHOUT_BASELINE_GATE
    exec_strats = [
        str(s)
        for s in (manifest.get("execution_scope") or {}).get("strategies", []) or []
    ]
    if not exec_strats:
        return

    raw = manifest.get("baseline_signal_gate")
    entries = _normalize_gate_entries(raw)
    by_name = {
        str(e.get("strategy") or ""): e for e in entries if e.get("strategy")
    }

    for name in exec_strats:
        rec = by_name.get(name)
        legacy_ok = (not require) and hard_for_new and (name in gf)

        if rec is None:
            if legacy_ok:
                continue
            raise PreregisterManifestError(
                f"baseline_signal_gate missing PASS record for execution "
                f"strategy {name} — run scripts/baseline_signal_gate.py "
                f"--strategy {name} --gate before promoting to execution "
                f"(legacy soft-exempt: {sorted(gf)})"
            )
        verdict = str(rec.get("verdict") or "").upper()
        if verdict != "PASS":
            raise PreregisterManifestError(
                f"baseline_signal_gate for {name} is {verdict!r} "
                f"(need PASS: B1≥p95 AND n_trades≥30 AND expectancy>0 / PF>1)"
            )


def assert_can_promote_to_execution(
    strategy_name: str,
    gate_record: Optional[Dict[str, Any]],
) -> None:
    """Hard entry check: a newly promoted strategy must carry a PASS gate record.

    Does not grandfather — use this when adding to ``execution_strategies``.
    """
    name = str(strategy_name)
    if gate_record is None:
        raise PreregisterManifestError(
            f"cannot promote {name} to execution_strategies without "
            f"baseline_signal_gate PASS — run scripts/baseline_signal_gate.py "
            f"--strategy {name} --folds W2,W3 --seeds 200 --gate"
        )
    verdict = str(gate_record.get("verdict") or "").upper()
    if verdict != "PASS":
        raise PreregisterManifestError(
            f"cannot promote {name}: baseline_signal_gate verdict={verdict!r} "
            f"(need PASS with B1≥p95, n≥30, expectancy>0 / PF>1); "
            f"reason={gate_record.get('reason')!r}"
        )


def assert_config_matches_preregister(
    config: Config,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fail closed if frozen strategy params drift from registered manifest."""
    manifest = load_preregister_manifest(path)
    if manifest is None:
        raise PreregisterManifestError("preregister manifest not found")
    verify_preregister_integrity(manifest)

    live = build_preregister_manifest(config, experiment_id=manifest.get("experiment_id"))
    for strat in ("VolatilityBreakout", "VWAPDeviation", "ChecklistMeta"):
        frozen = manifest.get("strategies", {}).get(strat, {}).get("frozen_params", {})
        current = live.get("strategies", {}).get(strat, {}).get("frozen_params", {})
        if not frozen and strat == "ChecklistMeta":
            # Older manifests predate ChecklistMeta execution — require overwrite.
            raise PreregisterManifestError(
                "frozen params missing for ChecklistMeta — re-register required "
                "(execution set changed; assert never skipped)"
            )
        if frozen != current:
            raise PreregisterManifestError(
                f"frozen params drift for {strat} — re-register forbidden before OOS"
            )
    live_exec = sorted(
        str(s)
        for s in (live.get("execution_scope") or {}).get("strategies", []) or []
    )
    frozen_exec = sorted(
        str(s)
        for s in (manifest.get("execution_scope") or {}).get("strategies", []) or []
    )
    if live_exec != frozen_exec:
        raise PreregisterManifestError(
            f"execution_strategies drift: live={live_exec} frozen={frozen_exec}"
        )
    # Soft for legacy execution names; hard for any non-grandfathered name in scope.
    assert_baseline_signal_gate(manifest, require=False, hard_for_new=True)
    return manifest
