"""Dedicated WAL SQLite persistence for MultiLegCoordinator episodes & audit events.

Strictly isolated from single-leg ``bot.db`` and ``config_hash`` so multi-leg
Spot+Perp Carry and Perp-Perp pairs never pollute single-leg ``trades`` rows.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.multileg.types import (
    CarryMarginParams,
    LegSpec,
    LegState,
    MultiLegEpisode,
)
from src.utils.helpers import validate_safe_path


class MultiLegLedger:
    """Thread-safe SQLite ledger for multi-leg episodes, legs, and events."""

    def __init__(self, db_path: str = "data/live/multileg.db") -> None:
        p = Path(db_path)
        if not p.is_absolute():
            safe = validate_safe_path(db_path)
            if safe is None:
                raise ValueError(f"Unsafe multileg ledger path: {db_path}")
            p = safe
        p.parent.mkdir(parents=True, exist_ok=True)
        self._path = p
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=30000;")
            self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS multileg_episodes (
                    episode_id TEXT PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    margin_profile TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_ms INTEGER NOT NULL,
                    opened_ms INTEGER NOT NULL DEFAULT 0,
                    closed_ms INTEGER NOT NULL DEFAULT 0,
                    close_reason TEXT NOT NULL DEFAULT '',
                    open_qty REAL NOT NULL DEFAULT 0.0,
                    entry_spot_px REAL NOT NULL DEFAULT 0.0,
                    entry_perp_px REAL NOT NULL DEFAULT 0.0,
                    collateral_usd REAL NOT NULL DEFAULT 0.0,
                    funding_accrued_usd REAL NOT NULL DEFAULT 0.0,
                    total_fees_usd REAL NOT NULL DEFAULT 0.0,
                    deleverages_count INTEGER NOT NULL DEFAULT 0,
                    max_unlegged_adverse_bps REAL NOT NULL DEFAULT 0.0,
                    updated_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS multileg_legs (
                    episode_id TEXT NOT NULL,
                    leg_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market_kind TEXT NOT NULL,
                    side TEXT NOT NULL,
                    target_size REAL NOT NULL,
                    limit_price REAL,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    order_id TEXT,
                    cloid TEXT,
                    filled_size REAL NOT NULL DEFAULT 0.0,
                    avg_fill_price REAL NOT NULL DEFAULT 0.0,
                    cumulative_fee REAL NOT NULL DEFAULT 0.0,
                    filled_at_ms INTEGER,
                    PRIMARY KEY (episode_id, leg_id)
                );

                CREATE TABLE IF NOT EXISTS multileg_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    episode_id TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    event_kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_multileg_events_ep
                    ON multileg_events(episode_id, timestamp_ms);
                """
            )

    def save_episode(self, ep: MultiLegEpisode) -> None:
        """Upsert an episode and all its leg states atomically."""
        now_ms = int(time.time() * 1000)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO multileg_episodes (
                    episode_id, strategy, mode, margin_profile, status,
                    created_ms, opened_ms, closed_ms, close_reason,
                    open_qty, entry_spot_px, entry_perp_px, collateral_usd,
                    funding_accrued_usd, total_fees_usd, deleverages_count,
                    max_unlegged_adverse_bps, updated_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO UPDATE SET
                    status = excluded.status,
                    opened_ms = excluded.opened_ms,
                    closed_ms = excluded.closed_ms,
                    close_reason = excluded.close_reason,
                    open_qty = excluded.open_qty,
                    entry_spot_px = excluded.entry_spot_px,
                    entry_perp_px = excluded.entry_perp_px,
                    collateral_usd = excluded.collateral_usd,
                    funding_accrued_usd = excluded.funding_accrued_usd,
                    total_fees_usd = excluded.total_fees_usd,
                    deleverages_count = excluded.deleverages_count,
                    max_unlegged_adverse_bps = excluded.max_unlegged_adverse_bps,
                    updated_ms = excluded.updated_ms
                """,
                (
                    ep.episode_id,
                    ep.strategy,
                    ep.mode,
                    ep.margin_profile,
                    ep.status,
                    ep.created_ms,
                    ep.opened_ms,
                    ep.closed_ms,
                    ep.close_reason,
                    ep.open_qty,
                    ep.entry_spot_px,
                    ep.entry_perp_px,
                    ep.collateral_usd,
                    ep.funding_accrued_usd,
                    ep.total_fees_usd,
                    ep.deleverages_count,
                    ep.max_unlegged_adverse_bps,
                    now_ms,
                ),
            )
            for leg_id, leg in ep.legs.items():
                self._conn.execute(
                    """
                    INSERT INTO multileg_legs (
                        episode_id, leg_id, symbol, market_kind, side,
                        target_size, limit_price, reduce_only, status,
                        order_id, cloid, filled_size, avg_fill_price,
                        cumulative_fee, filled_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(episode_id, leg_id) DO UPDATE SET
                        status = excluded.status,
                        order_id = excluded.order_id,
                        cloid = excluded.cloid,
                        filled_size = excluded.filled_size,
                        avg_fill_price = excluded.avg_fill_price,
                        cumulative_fee = excluded.cumulative_fee,
                        filled_at_ms = excluded.filled_at_ms
                    """,
                    (
                        ep.episode_id,
                        leg_id,
                        leg.spec.symbol,
                        leg.spec.market_kind,
                        leg.spec.side,
                        leg.spec.target_size,
                        leg.spec.limit_price,
                        1 if leg.spec.reduce_only else 0,
                        leg.status,
                        leg.order_id,
                        leg.cloid,
                        leg.filled_size,
                        leg.avg_fill_price,
                        leg.cumulative_fee,
                        leg.filled_at_ms,
                    ),
                )

    def append_event(
        self,
        episode_id: str,
        event_kind: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        timestamp_ms: Optional[int] = None,
    ) -> None:
        ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
        encoded = json.dumps(payload or {}, sort_keys=True, default=str)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO multileg_events (episode_id, timestamp_ms, event_kind, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (episode_id, ts, event_kind, encoded),
            )

    def list_episodes(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if status:
                cur = self._conn.execute(
                    "SELECT * FROM multileg_episodes WHERE status = ? ORDER BY created_ms DESC",
                    (status,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM multileg_episodes ORDER BY created_ms DESC"
                )
            return [dict(r) for r in cur.fetchall()]

    def list_events(self, episode_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM multileg_events WHERE episode_id = ? ORDER BY id ASC",
                (episode_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_active_perp_symbols(self) -> set[str]:
        """Return uppercase perp symbols currently open/pending in MultiLegCoordinator.

        Pass ``ignored_symbols_provider=ledger.get_active_perp_symbols`` into
        ``ExchangeReconciler`` when sharing a single Hyperliquid wallet.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT DISTINCT l.symbol
                FROM multileg_legs l
                JOIN multileg_episodes e ON e.episode_id = l.episode_id
                WHERE e.status IN ('pending_entry', 'open', 'pending_exit')
                  AND l.market_kind = 'perp'
                """
            )
            return {str(r["symbol"]).upper() for r in cur.fetchall() if r["symbol"]}

    def load_active_episodes(
        self,
        *,
        default_max_leverage: float = 10.0,
    ) -> List[MultiLegEpisode]:
        """Reconstruct active ``MultiLegEpisode`` + ``LegState`` objects after restart."""
        with self._lock:
            ep_rows = self._conn.execute(
                """
                SELECT * FROM multileg_episodes
                WHERE status IN ('pending_entry', 'open', 'pending_exit')
                ORDER BY created_ms ASC
                """
            ).fetchall()
            out: List[MultiLegEpisode] = []
            for er in ep_rows:
                eid = str(er["episode_id"])
                leg_rows = self._conn.execute(
                    "SELECT * FROM multileg_legs WHERE episode_id = ? ORDER BY leg_id ASC",
                    (eid,),
                ).fetchall()
                legs = {}
                perp_sym = ""
                for lr in leg_rows:
                    if str(lr["market_kind"]) == "perp":
                        perp_sym = str(lr["symbol"])
                    spec = LegSpec(
                        leg_id=str(lr["leg_id"]),
                        symbol=str(lr["symbol"]),
                        market_kind=str(lr["market_kind"]),  # type: ignore[arg-type]
                        side=str(lr["side"]),  # type: ignore[arg-type]
                        target_size=float(lr["target_size"]),
                        limit_price=(
                            float(lr["limit_price"])
                            if lr["limit_price"] is not None
                            else None
                        ),
                        reduce_only=bool(lr["reduce_only"]),
                    )
                    legs[spec.leg_id] = LegState(
                        spec=spec,
                        status=str(lr["status"]),
                        order_id=str(lr["order_id"]) if lr["order_id"] else None,
                        cloid=str(lr["cloid"]) if lr["cloid"] else None,
                        filled_size=float(lr["filled_size"]),
                        avg_fill_price=float(lr["avg_fill_price"]),
                        cumulative_fee=float(lr["cumulative_fee"]),
                        filled_at_ms=(
                            int(lr["filled_at_ms"])
                            if lr["filled_at_ms"] is not None
                            else None
                        ),
                    )
                mp = (
                    CarryMarginParams.for_pair(
                        perp_sym or "BTC",
                        default_max_leverage,
                        pm_enabled=(str(er["margin_profile"]) == "portfolio_margin"),
                        account_value_usd=100_000.0 if str(er["margin_profile"]) == "portfolio_margin" else 0.0,
                    )
                    if str(er["strategy"]) == "CarryA1"
                    else None
                )
                ep = MultiLegEpisode(
                    episode_id=eid,
                    strategy=str(er["strategy"]),
                    mode=str(er["mode"]),
                    margin_profile=str(er["margin_profile"]),  # type: ignore[arg-type]
                    created_ms=int(er["created_ms"]),
                    legs=legs,
                    status=str(er["status"]),  # type: ignore[arg-type]
                    opened_ms=int(er["opened_ms"]),
                    closed_ms=int(er["closed_ms"]),
                    close_reason=str(er["close_reason"]),
                    max_unlegged_adverse_bps=float(er["max_unlegged_adverse_bps"]),
                    open_qty=float(er["open_qty"]),
                    entry_spot_px=float(er["entry_spot_px"]),
                    entry_perp_px=float(er["entry_perp_px"]),
                    collateral_usd=float(er["collateral_usd"]),
                    funding_accrued_usd=float(er["funding_accrued_usd"]),
                    total_fees_usd=float(er["total_fees_usd"]),
                    deleverages_count=int(er["deleverages_count"]),
                    margin_params=mp,
                )
                out.append(ep)
            return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()
