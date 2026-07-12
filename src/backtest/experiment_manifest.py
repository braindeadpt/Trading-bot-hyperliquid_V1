"""Experiment manifest and cross-run comparability guards (Phase 06)."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Dict, Mapping, Optional, Sequence

from src.backtest.run_manifest import build_run_manifest

RESEARCH_PROTOCOL_VERSION = "phase06-train-val-test-v1"

COMPARABILITY_KEYS = (
    "sizing_version",
    "fidelity_tier",
    "gate_parity_version",
    "research_protocol_version",
    "data_source",
)


class ManifestIncompatibleError(ValueError):
    """Raised when two experiment manifests cannot be compared fairly."""


def new_experiment_id() -> str:
    """Generate a unique experiment identifier."""
    return str(uuid.uuid4())


def build_experiment_manifest(
    config: Any,
    *,
    run_manifest: Optional[Mapping[str, Any]] = None,
    experiment_id: Optional[str] = None,
    strategy_name: str = "",
    n_trials: int = 1,
    protocol: str = "train_val_test_holdout",
    window_config: Optional[Mapping[str, Any]] = None,
    param_grid_size: int = 1,
    seed: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach research-protocol metadata to a backtest / walk-forward run."""
    base = dict(run_manifest) if run_manifest else build_run_manifest(config)
    manifest: Dict[str, Any] = {
        **base,
        "experiment_id": experiment_id or new_experiment_id(),
        "research_protocol_version": RESEARCH_PROTOCOL_VERSION,
        "protocol": protocol,
        "strategy_name": strategy_name,
        "n_trials": int(max(1, n_trials)),
        "param_grid_size": int(max(1, param_grid_size)),
        "created_at_ms": int(time.time() * 1000),
    }
    if window_config is not None:
        manifest["window_config"] = dict(window_config)
    if seed is not None:
        manifest["seed"] = int(seed)
    if extra:
        manifest.update(extra)
    manifest["manifest_hash"] = _hash_manifest(manifest)
    return manifest


def _hash_manifest(manifest: Mapping[str, Any]) -> str:
    """Stable hash over comparability-relevant fields."""
    payload = {k: manifest.get(k) for k in sorted(manifest.keys()) if not k.startswith("_")}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def assert_manifests_comparable(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    keys: Sequence[str] = COMPARABILITY_KEYS,
) -> None:
    """Fail closed when fidelity / sizing / protocol versions differ."""
    mismatches: Dict[str, tuple] = {}
    for key in keys:
        lv = left.get(key)
        rv = right.get(key)
        if lv != rv:
            mismatches[key] = (lv, rv)
    if mismatches:
        parts = ", ".join(f"{k}: {v[0]!r} vs {v[1]!r}" for k, v in mismatches.items())
        raise ManifestIncompatibleError(
            f"Cannot compare runs with incompatible manifests: {parts}"
        )


def is_comparable(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    keys: Sequence[str] = COMPARABILITY_KEYS,
) -> bool:
    """Return True when manifests share the same comparability keys."""
    try:
        assert_manifests_comparable(left, right, keys=keys)
        return True
    except ManifestIncompatibleError:
        return False
