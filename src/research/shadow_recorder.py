"""Shadow decision recorder — Phase 08 prep (no execution, no Tier A promotion)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.data.research_database import ResearchDatabase, DEFAULT_RESEARCH_DB_PATH

logger = logging.getLogger(__name__)


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
