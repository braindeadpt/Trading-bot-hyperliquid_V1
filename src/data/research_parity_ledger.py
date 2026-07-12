"""Research dataset parity gate — OOS requires validated GoldRush parity."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.utils.helpers import safe_write_file, validate_safe_path

DEFAULT_LEDGER_REL = Path("data") / "research" / "parity_ledger.json"


class ParityGateError(RuntimeError):
    """Dataset not cleared for OOS until parity is validated."""


def default_ledger_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / DEFAULT_LEDGER_REL


class ResearchParityLedger:
    """Persist parity validation status for the research dataset."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or default_ledger_path()
        self._state: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._state = {}
            return
        try:
            self._state = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._state = {}

    def save_validation(self, report: Dict[str, Any]) -> None:
        """Record latest parity diagnostic outcome."""
        rel = self._path
        if rel.is_absolute():
            project_root = Path(__file__).resolve().parents[2]
            try:
                rel = rel.relative_to(project_root)
            except ValueError:
                pass
        safe = validate_safe_path(rel.as_posix())
        if safe is None:
            raise ParityGateError(f"Unsafe parity ledger path: {self._path}")

        self._state = {
            "updated_at_ms": int(time.time() * 1000),
            "oos_dataset_ready": bool(report.get("all_passed", False)),
            "report_path": str(report.get("report_path", "")),
            "summary": report.get("summary", {}),
        }
        safe.parent.mkdir(parents=True, exist_ok=True)
        if not safe_write_file(safe, json.dumps(self._state, indent=2, sort_keys=True)):
            raise ParityGateError(f"Failed to write parity ledger: {safe}")

    @property
    def oos_dataset_ready(self) -> bool:
        return bool(self._state.get("oos_dataset_ready", False))

    def assert_oos_ready(self) -> None:
        if not self.oos_dataset_ready:
            raise ParityGateError(
                "GoldRush parity not validated — run scripts/goldrush_parity_diagnostic.py "
                "and resolve mismatches before OOS.",
            )
