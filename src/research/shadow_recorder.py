"""Shadow decision recorder — Phase 08 prep (no execution, no Tier A promotion).

Observability only. ``market_snapshot`` may include bracket fields used by the
offline shadow outcome evaluator:

* ``price``, ``confidence`` (legacy + current)
* ``stop_loss_pct``, ``take_profit_pct``, ``size_pct`` (enriched; may be absent
  on historical rows — evaluator must skip those, never invent stops)
* ``metadata`` — optional copy of ``Signal.metadata`` (JSON-safe)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.data.research_database import ResearchDatabase, DEFAULT_RESEARCH_DB_PATH
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)


def _json_safe(obj: Any) -> Any:
    """Coerce nested values to JSON-serializable types."""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def build_enriched_market_snapshot(
    *,
    price: float,
    confidence: float,
    stop_loss_pct: float,
    take_profit_pct: Optional[float],
    size_pct: float,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the snapshot dict persisted into ``snapshot_json``.

    Additive / backward compatible: old readers that only look at ``price`` /
    ``confidence`` keep working. New fields are optional for loaders.
    """
    snap: Dict[str, Any] = {
        "price": float(price),
        "confidence": float(confidence),
        "stop_loss_pct": float(stop_loss_pct),
        "size_pct": float(size_pct),
        "metadata": _json_safe(dict(metadata or {})),
    }
    if take_profit_pct is not None:
        snap["take_profit_pct"] = float(take_profit_pct)
    else:
        snap["take_profit_pct"] = None
    return snap


def parse_market_snapshot(raw: Optional[str]) -> Dict[str, Any]:
    """Parse ``snapshot_json``; empty / invalid → ``{}`` (never raises)."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def extract_bracket_params(
    snapshot: Optional[Dict[str, Any]],
) -> Optional[Dict[str, float]]:
    """Return ``{stop_loss_pct, take_profit_pct, size_pct, price}`` or None.

    Historical rows without stops return None — callers must skip, not guess.
    """
    if not snapshot:
        return None
    stop = safe_float(snapshot.get("stop_loss_pct"), default=0.0)
    tp_raw = snapshot.get("take_profit_pct")
    if tp_raw is None:
        return None
    take = safe_float(tp_raw, default=0.0)
    price = safe_float(snapshot.get("price"), default=0.0)
    size = safe_float(snapshot.get("size_pct"), default=0.0)
    if stop <= 0.0 or take <= 0.0 or price <= 0.0:
        return None
    return {
        "stop_loss_pct": stop,
        "take_profit_pct": take,
        "size_pct": size,
        "price": price,
    }


@dataclass(frozen=True)
class ShadowDecision:
    """A hypothetical decision logged for offline comparison."""

    symbol: str
    strategy: str
    variant: str
    side: Optional[str]
    would_enter: bool
    reason: str
    timestamp_ms: int
    market_snapshot: Optional[Dict[str, Any]] = None
    # Loaded rows only (not required when recording)
    row_id: Optional[int] = None


class ShadowRecorder:
    """Append-only shadow decision log in the research DB.

    Shadow mode never affects execution or fidelity tiers — observability only.
    """

    def __init__(self, db: Optional[ResearchDatabase] = None) -> None:
        self._db = db or ResearchDatabase(DEFAULT_RESEARCH_DB_PATH)
        self._lock = threading.Lock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._db._conn():
            self._db._conn().execute("""
                CREATE TABLE IF NOT EXISTS shadow_decisions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol          TEXT    NOT NULL,
                    strategy        TEXT    NOT NULL,
                    variant         TEXT    NOT NULL,
                    side            TEXT,
                    would_enter     INTEGER NOT NULL,
                    reason          TEXT    NOT NULL,
                    timestamp_ms    INTEGER NOT NULL,
                    snapshot_json   TEXT,
                    ingested_at_ms  INTEGER NOT NULL
                );
            """)
            self._db._conn().execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_decisions(timestamp_ms);"
            )
            self._db._commit()

    def record(self, decision: ShadowDecision) -> None:
        """Persist one shadow decision (best-effort)."""
        snap = json.dumps(decision.market_snapshot) if decision.market_snapshot else None
        ingested = int(time.time() * 1000)
        sql = """
            INSERT INTO shadow_decisions
            (symbol, strategy, variant, side, would_enter, reason,
             timestamp_ms, snapshot_json, ingested_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._lock:
            try:
                with self._db._write_lock:
                    conn = self._db._conn()
                    conn.execute(
                        sql,
                        (
                            decision.symbol,
                            decision.strategy,
                            decision.variant,
                            decision.side,
                            1 if decision.would_enter else 0,
                            decision.reason,
                            decision.timestamp_ms,
                            snap,
                            ingested,
                        ),
                    )
                    conn.commit()
            except sqlite3.Error as exc:
                logger.debug("Shadow record failed: %s", exc)

    def record_batch(self, decisions: List[ShadowDecision]) -> None:
        for d in decisions:
            self.record(d)

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> ShadowDecision:
        keys = set(row.keys())
        snap = parse_market_snapshot(row["snapshot_json"] if "snapshot_json" in keys else None)
        return ShadowDecision(
            symbol=str(row["symbol"]),
            strategy=str(row["strategy"]),
            variant=str(row["variant"]),
            side=row["side"],
            would_enter=bool(row["would_enter"]),
            reason=str(row["reason"]),
            timestamp_ms=int(row["timestamp_ms"]),
            market_snapshot=snap or None,
            row_id=int(row["id"]) if "id" in keys and row["id"] is not None else None,
        )

    def load_decisions(
        self,
        *,
        strategy: Optional[str] = None,
        variant: Optional[str] = None,
        since_ms: Optional[int] = None,
        until_ms: Optional[int] = None,
        would_enter_only: bool = True,
        limit: Optional[int] = None,
    ) -> List[ShadowDecision]:
        """Load shadow decisions (read path for the outcome evaluator)."""
        conditions: List[str] = []
        params: List[Any] = []
        if would_enter_only:
            conditions.append("would_enter = 1")
        if strategy:
            conditions.append("strategy = ?")
            params.append(strategy)
        if variant:
            conditions.append("variant = ?")
            params.append(variant)
        if since_ms is not None:
            conditions.append("timestamp_ms >= ?")
            params.append(int(since_ms))
        if until_ms is not None:
            conditions.append("timestamp_ms <= ?")
            params.append(int(until_ms))
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = (
            f"SELECT * FROM shadow_decisions{where} "
            "ORDER BY timestamp_ms ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            conn = self._db._conn()
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_decision(r) for r in rows]
