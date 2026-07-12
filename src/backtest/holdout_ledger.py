"""Persistent holdout access ledger (Phase 06).

Prevents re-consulting the same frozen-parameter holdout slice across
process restarts.  State is stored in ``data/research/holdout_ledger.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from src.utils.helpers import safe_write_file, validate_safe_path

logger = logging.getLogger(__name__)

DEFAULT_LEDGER_REL = Path("data") / "research" / "holdout_ledger.json"


class HoldoutViolationError(RuntimeError):
    """Raised when holdout data is accessed illegally or already consumed."""


def _resolve_project_path(path: Path) -> Path:
    """Ensure *path* resolves inside the repository (Windows-safe)."""
    project_root = Path(__file__).resolve().parents[2]
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(project_root)
    except ValueError as exc:
        raise HoldoutViolationError(
            f"Holdout ledger path must stay inside project: {path}"
        ) from exc
    safe = validate_safe_path(rel.as_posix())
    if safe is None:
        raise HoldoutViolationError(f"Unsafe holdout ledger path: {path}")
    return resolved


def default_ledger_path() -> Path:
    """Return the project-default holdout ledger path."""
    project_root = Path(__file__).resolve().parents[2]
    return project_root / DEFAULT_LEDGER_REL


def compute_holdout_key(
    *,
    strategy_name: str,
    test_start_ms: int,
    test_end_ms: int,
    frozen_params: Mapping[str, Any],
    config_hash: str = "",
) -> str:
    """Stable key for a strategy + test window + frozen parameters."""
    clean_params = {
        str(k): v for k, v in frozen_params.items() if not str(k).startswith("__")
    }
    payload = {
        "strategy": str(strategy_name),
        "test_start_ms": int(test_start_ms),
        "test_end_ms": int(test_end_ms),
        "params": sorted(clean_params.items()),
        "config_hash": str(config_hash or ""),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class HoldoutLedger:
    """File-backed registry of consumed holdout slices."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or default_ledger_path()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            self._entries = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            entries = raw.get("entries", raw) if isinstance(raw, dict) else {}
            self._entries = dict(entries) if isinstance(entries, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("HoldoutLedger: could not load %s: %s", self._path, exc)
            self._entries = {}

    def _save(self) -> None:
        safe = _resolve_project_path(self._path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at_ms": int(time.time() * 1000),
            "entries": self._entries,
        }
        tmp = safe.with_suffix(safe.suffix + ".tmp")
        content = json.dumps(payload, indent=2, sort_keys=True)
        if not safe_write_file(tmp, content):
            raise HoldoutViolationError(f"Holdout ledger write failed: {tmp}")
        shutil.move(str(tmp), str(safe))

    def is_consumed(self, key: str) -> bool:
        return key in self._entries

    def get_entry(self, key: str) -> Optional[Dict[str, Any]]:
        row = self._entries.get(key)
        return dict(row) if isinstance(row, dict) else None

    def mark_consumed(self, key: str, metadata: Optional[Mapping[str, Any]] = None) -> None:
        self._entries[key] = {
            "consumed_at_ms": int(time.time() * 1000),
            **(dict(metadata) if metadata else {}),
        }
        self._save()


class HoldoutGuard:
    """In-process + persistent holdout protection."""

    def __init__(self, ledger: Optional[HoldoutLedger] = None) -> None:
        self._ledger = ledger or HoldoutLedger()
        self._frozen_params: Optional[Dict[str, Any]] = None
        self._holdout_used: bool = False
        self._holdout_key: Optional[str] = None
        self._context: Dict[str, Any] = {}

    @property
    def ledger(self) -> HoldoutLedger:
        return self._ledger

    @property
    def frozen_params(self) -> Optional[Dict[str, Any]]:
        return dict(self._frozen_params) if self._frozen_params is not None else None

    @property
    def holdout_used(self) -> bool:
        return self._holdout_used

    @property
    def holdout_key(self) -> Optional[str]:
        return self._holdout_key

    def begin_window(self) -> None:
        """Reset per-window in-memory state (ledger persists across windows)."""
        self._frozen_params = None
        self._holdout_used = False
        self._holdout_key = None
        self._context = {}

    def freeze_params(self, params: Mapping[str, Any]) -> None:
        clean = {k: v for k, v in params.items() if not str(k).startswith("__")}
        self._frozen_params = dict(clean)

    def set_holdout_context(
        self,
        *,
        strategy_name: str,
        test_start_ms: int,
        test_end_ms: int,
        config_hash: str = "",
    ) -> str:
        """Bind the upcoming holdout evaluation to a persistent ledger key."""
        if self._frozen_params is None:
            raise HoldoutViolationError("Parameters must be frozen before holdout context")
        key = compute_holdout_key(
            strategy_name=strategy_name,
            test_start_ms=int(test_start_ms),
            test_end_ms=int(test_end_ms),
            frozen_params=self._frozen_params,
            config_hash=config_hash,
        )
        self._holdout_key = key
        self._context = {
            "strategy_name": strategy_name,
            "test_start_ms": int(test_start_ms),
            "test_end_ms": int(test_end_ms),
            "config_hash": config_hash,
        }
        if self._ledger.is_consumed(key):
            prior = self._ledger.get_entry(key) or {}
            raise HoldoutViolationError(
                "Holdout slice already consumed in a prior run "
                f"(key={key[:12]}…, consumed_at_ms={prior.get('consumed_at_ms')})"
            )
        return key

    def assert_selection_phase(self) -> None:
        if self._holdout_used:
            raise HoldoutViolationError(
                "Holdout already evaluated — cannot use test data for selection"
            )

    def evaluate_holdout(self, fn):
        """Run holdout once in-process and persist consumption to the ledger."""
        if self._holdout_used:
            raise HoldoutViolationError("Holdout can only be consulted once per window")
        if self._frozen_params is None:
            raise HoldoutViolationError("Parameters must be frozen before holdout")
        if not self._holdout_key:
            raise HoldoutViolationError(
                "Holdout context not set — call set_holdout_context() first"
            )
        if self._ledger.is_consumed(self._holdout_key):
            raise HoldoutViolationError(
                "Holdout slice already consumed in a prior run "
                f"(key={self._holdout_key[:12]}…)"
            )
        self._holdout_used = True
        result = fn()
        self._ledger.mark_consumed(
            self._holdout_key,
            metadata={
                **self._context,
                "frozen_params": self._frozen_params,
            },
        )
        return result
