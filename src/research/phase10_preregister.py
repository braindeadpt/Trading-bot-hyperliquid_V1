"""Phase 10 pre-registration manifest — frozen 8-12 week paper-trading gate.

This module freezes, at the moment the validation window opens, every
number needed to judge the Fase 10 go/no-go gate later:

  * ``window_start_ms`` — the instant the window opens (now, at creation).
  * the target window length as a *range* (8-12 weeks), plus the computed
    min/max end timestamps.
  * the exact set of live-executing strategies read from
    ``strategy.phase08.execution_strategies`` at freeze time.
  * a hash of the full effective config (mirrors the
    ``phase08_preregister`` hashing approach — sorted-JSON SHA-256 over the
    manifest body, excluding the hash field itself).
  * the gate thresholds themselves (min_trades, min_profit_factor,
    expectancy_r_gt, max_drawdown_pct) — frozen NOW, before any results
    exist, so nobody can retroactively pick more favourable numbers.

Once written, the manifest is immutable: ``persist_preregister_manifest``
is first-write-wins (like ``phase08_preregister``), and
``verify_preregister_integrity`` recomputes the body hash to detect
tampering. ``assert_config_matches_preregister`` additionally detects
*live config drift* — i.e. someone changing
``strategy.phase08.execution_strategies`` or the effective config hash
mid-window, which the frozen-window protocol forbids.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from src.utils.config import Config, compute_config_hash, get_strategy_section
from src.utils.helpers import safe_write_file, validate_safe_path

logger = logging.getLogger(__name__)

PREREGISTER_PROTOCOL = "phase10-frozen-window-v1"
DEFAULT_PATH = Path("data") / "research" / "phase10" / "phase10_preregister.json"

WEEK_MS = 7 * 24 * 3600 * 1000

# Gate thresholds — frozen at window-open time, per Fase 10 spec.
GATE_MIN_TRADES = 100
GATE_MIN_PROFIT_FACTOR = 1.20
GATE_EXPECTANCY_R_GT = 0.0
GATE_MAX_DRAWDOWN_PCT = 5.0

MIN_WINDOW_WEEKS = 8
MAX_WINDOW_WEEKS = 12


class Phase10PreregisterError(RuntimeError):
    """Raised when live config/strategies drift from the frozen Fase 10 manifest,
    or when the persisted manifest is missing/tampered with."""


def _hash_manifest_body(manifest: Dict[str, Any]) -> str:
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_preregister_manifest(
    config: Config,
    *,
    experiment_id: Optional[str] = None,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the Fase 10 pre-registration document from the live config.

    ``now_ms`` is exposed only for deterministic testing; production callers
    should omit it so ``window_start_ms`` is the true freeze instant.
    """
    p08 = get_strategy_section(config, "phase08")
    execution_strategies = sorted(
        str(s) for s in p08.get("execution_strategies", []) or []
    )
    ts = int(now_ms if now_ms is not None else time.time() * 1000)

    manifest: Dict[str, Any] = {
        "protocol": PREREGISTER_PROTOCOL,
        "experiment_id": experiment_id or str(uuid.uuid4()),
        "registered_at_ms": int(time.time() * 1000),
        "window_start_ms": ts,
        "window": {
            "min_weeks": MIN_WINDOW_WEEKS,
            "max_weeks": MAX_WINDOW_WEEKS,
            "min_end_ms": ts + MIN_WINDOW_WEEKS * WEEK_MS,
            "max_end_ms": ts + MAX_WINDOW_WEEKS * WEEK_MS,
        },
        "config_hash": compute_config_hash(config),
        "execution_strategies": execution_strategies,
        "gate_thresholds": {
            "min_trades": GATE_MIN_TRADES,
            "min_profit_factor": GATE_MIN_PROFIT_FACTOR,
            "expectancy_r_gt": GATE_EXPECTANCY_R_GT,
            "max_drawdown_pct": GATE_MAX_DRAWDOWN_PCT,
        },
        "gate_note": (
            "All four criteria must be a real PASS (not merely 'no FAIL yet "
            "due to insufficient data') for the frozen window to be considered "
            "met. Drift in execution_strategies or config_hash after freeze "
            "invalidates the window — see assert_config_matches_preregister()."
        ),
    }
    manifest["manifest_hash"] = _hash_manifest_body(manifest)
    return manifest


def _resolve_preregister_path(path: Path) -> Path:
    """Ensure preregister path is inside the project and passes validate_safe_path."""
    project_root = Path(__file__).resolve().parents[2]
    resolved = (project_root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        rel = resolved.relative_to(project_root)
    except ValueError as exc:
        raise Phase10PreregisterError(
            f"Preregister path must stay inside project: {path}"
        ) from exc
    safe = validate_safe_path(rel.as_posix())
    if safe is None:
        raise Phase10PreregisterError(f"Unsafe preregister path: {path}")
    return resolved


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    safe = _resolve_preregister_path(path)
    safe.parent.mkdir(parents=True, exist_ok=True)
    tmp = safe.with_suffix(safe.suffix + ".tmp")
    content = json.dumps(data, indent=2, sort_keys=True)
    if not safe_write_file(tmp, content):
        raise Phase10PreregisterError(f"Preregister manifest write failed: {tmp}")
    shutil.move(str(tmp), str(safe))


def persist_preregister_manifest(
    config: Config,
    path: Optional[Path] = None,
    *,
    overwrite: bool = False,
) -> Path:
    """Write the immutable Fase 10 pre-registration manifest (first write wins)."""
    out = path or DEFAULT_PATH
    if out.exists() and not overwrite:
        existing = load_preregister_manifest(out)
        if existing is not None:
            verify_preregister_integrity(existing)
            logger.info("Phase10 preregister manifest exists — immutable: %s", out)
        return out
    manifest = build_preregister_manifest(config)
    _atomic_write_json(out, manifest)
    logger.info(
        "Phase10 preregister manifest written: %s experiment_id=%s hash=%s",
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
        raise Phase10PreregisterError("preregister manifest missing manifest_hash")
    actual = _hash_manifest_body(manifest)
    if actual != expected:
        raise Phase10PreregisterError(
            f"preregister manifest hash mismatch (expected {expected[:16]}… got {actual[:16]}…)"
        )


def assert_config_matches_preregister(
    config: Config,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fail LOUDLY if the live config has drifted from the frozen Fase 10 manifest.

    Checks (both must hold):
      1. The live ``strategy.phase08.execution_strategies`` set is identical
         to the frozen set (a strategy being added/removed mid-window breaks
         the frozen-config assertion the gate depends on).
      2. The live full-config hash matches the frozen ``config_hash`` (any
         parameter change anywhere in the effective config, not just the
         two execution strategies, is drift).

    Returns the loaded (and integrity-verified) manifest on success.
    """
    manifest = load_preregister_manifest(path)
    if manifest is None:
        raise Phase10PreregisterError(
            "Phase10 preregister manifest not found — run "
            "persist_preregister_manifest() before starting/continuing the "
            "frozen validation window."
        )
    verify_preregister_integrity(manifest)

    p08 = get_strategy_section(config, "phase08")
    live_strategies = sorted(str(s) for s in p08.get("execution_strategies", []) or [])
    frozen_strategies = list(manifest.get("execution_strategies", []))
    if live_strategies != frozen_strategies:
        raise Phase10PreregisterError(
            "Fase 10 drift detected: strategy.phase08.execution_strategies "
            f"changed mid-window (frozen={frozen_strategies}, live={live_strategies})"
        )

    live_hash = compute_config_hash(config)
    frozen_hash = manifest.get("config_hash")
    if live_hash != frozen_hash:
        raise Phase10PreregisterError(
            "Fase 10 drift detected: effective config_hash changed mid-window "
            f"(frozen={frozen_hash}, live={live_hash}). No parameter changes "
            "are allowed during the frozen validation window."
        )
    return manifest
