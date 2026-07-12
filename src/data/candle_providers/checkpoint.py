"""Checkpoint/resume for long candle backfills."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from src.data.candle_providers.base import ProviderName
from src.utils.helpers import safe_write_file, validate_safe_path

logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT_REL = Path("data") / "research" / "goldrush_backfill_checkpoint.json"


class CheckpointError(RuntimeError):
    """Invalid or unsafe checkpoint path/state."""


@dataclass
class BackfillCheckpoint:
    """Resume state for one symbol × interval backfill."""

    provider: ProviderName
    symbol: str
    interval: str
    window_start_ms: int
    window_end_ms: int
    cursor_ms: int
    rows_fetched: int
    updated_at_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BackfillCheckpoint":
        return cls(
            provider=str(data["provider"]),  # type: ignore[arg-type]
            symbol=str(data["symbol"]).upper(),
            interval=str(data["interval"]),
            window_start_ms=int(data["window_start_ms"]),
            window_end_ms=int(data["window_end_ms"]),
            cursor_ms=int(data["cursor_ms"]),
            rows_fetched=int(data.get("rows_fetched", 0)),
            updated_at_ms=int(data.get("updated_at_ms", 0)),
        )


def _resolve_checkpoint_path(path: Path) -> Path:
    project_root = Path(__file__).resolve().parents[3]
    resolved = (project_root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        rel = resolved.relative_to(project_root)
    except ValueError as exc:
        raise CheckpointError(f"Checkpoint path outside project: {path}") from exc
    if validate_safe_path(rel.as_posix()) is None:
        raise CheckpointError(f"Unsafe checkpoint path: {path}")
    return resolved


def load_checkpoint(path: Optional[Path] = None) -> Dict[str, BackfillCheckpoint]:
    """Load all checkpoint entries keyed by ``symbol:interval``."""
    p = _resolve_checkpoint_path(path or DEFAULT_CHECKPOINT_REL)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load checkpoint %s: %s", p, exc)
        return {}
    entries = raw.get("entries", raw) if isinstance(raw, dict) else {}
    out: Dict[str, BackfillCheckpoint] = {}
    if isinstance(entries, dict):
        for key, val in entries.items():
            if isinstance(val, dict):
                try:
                    out[str(key)] = BackfillCheckpoint.from_dict(val)
                except (KeyError, TypeError, ValueError):
                    continue
    return out


def save_checkpoint(
    entry: BackfillCheckpoint,
    path: Optional[Path] = None,
) -> None:
    """Persist one checkpoint entry (merge with existing)."""
    p = _resolve_checkpoint_path(path or DEFAULT_CHECKPOINT_REL)
    key = f"{entry.symbol}:{entry.interval}"
    all_entries = load_checkpoint(p)
    entry.updated_at_ms = int(time.time() * 1000)
    all_entries[key] = entry
    payload = {
        "version": 1,
        "updated_at_ms": entry.updated_at_ms,
        "entries": {k: v.to_dict() for k, v in all_entries.items()},
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    if not safe_write_file(tmp, json.dumps(payload, indent=2, sort_keys=True)):
        raise CheckpointError(f"Checkpoint write failed: {tmp}")
    tmp.replace(p)


def clear_checkpoint(
    symbol: str,
    interval: str,
    path: Optional[Path] = None,
) -> None:
    """Remove a completed symbol×interval checkpoint."""
    p = _resolve_checkpoint_path(path or DEFAULT_CHECKPOINT_REL)
    key = f"{symbol.upper()}:{interval}"
    all_entries = load_checkpoint(p)
    if key not in all_entries:
        return
    del all_entries[key]
    if not all_entries:
        if p.exists():
            p.unlink(missing_ok=True)
        return
    payload = {
        "version": 1,
        "updated_at_ms": int(time.time() * 1000),
        "entries": {k: v.to_dict() for k, v in all_entries.items()},
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    if not safe_write_file(tmp, json.dumps(payload, indent=2, sort_keys=True)):
        raise CheckpointError(f"Checkpoint write failed: {tmp}")
    tmp.replace(p)
