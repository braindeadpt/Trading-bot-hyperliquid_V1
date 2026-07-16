"""Hyperliquid research database — separate from live bot.db (Phase 07).

No pruning. Extended schema with source/venue/version/ingested_at metadata,
L2 snapshots, and trade tape for Tier-A replay preparation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.data.database import Candle, Database, FundingRecord, OIRecord
from src.data.series_metadata import SeriesMetadata

logger = logging.getLogger(__name__)

DEFAULT_RESEARCH_DB_PATH = Path("data") / "research" / "hyperliquid.db"

RESEARCH_CANDLE_EXTRA_COLS = (
    ("source", "TEXT"),
    ("venue", "TEXT"),
    ("api_version", "TEXT"),
    ("ingested_at_ms", "INTEGER"),
    ("open_time_ms", "INTEGER"),
    ("volume_unit", "TEXT DEFAULT 'base'"),
    ("quality_flags", "TEXT"),
)

FUNDING_META_COLS = (
    ("source", "TEXT"),
    ("venue", "TEXT"),
    ("api_version", "TEXT"),
    ("ingested_at_ms", "INTEGER"),
    ("freshness_ms", "INTEGER"),
    ("quality_flags", "TEXT"),
)

OI_META_COLS = FUNDING_META_COLS


@dataclass(frozen=True)
class L2SnapshotRecord:
    symbol: str
    timestamp_ms: int
    mid_price: float
    spread_bps: float
    bid_depth_usd: float
    ask_depth_usd: float
    oir: Optional[float]
    source: str
    venue: str
    api_version: str
    ingested_at_ms: int
    quality_flags: Optional[str] = None


@dataclass(frozen=True)
class TradeTapeRecord:
    symbol: str
    timestamp_ms: int
    price: float
    size: float
    side: str
    trade_id: Optional[str]
    source: str
    venue: str
    api_version: str
    ingested_at_ms: int


class ResearchDatabase(Database):
    """Research-only SQLite store — never pruned, never mixed with live bot.db."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        path = Path(db_path) if db_path is not None else DEFAULT_RESEARCH_DB_PATH
        super().__init__(path)

    def _init_db(self) -> None:
        super()._init_db()
        self._migrate_research_metadata()
        self._create_research_tables()

    def _migrate_research_metadata(self) -> None:
        for table in self.TIMEFRAME_TABLES.values():
            existing = {
                row[1]
                for row in self._conn().execute(f"PRAGMA table_info({table})").fetchall()
            }
            for col_name, col_type in RESEARCH_CANDLE_EXTRA_COLS:
                if col_name not in existing:
                    try:
                        self._conn().execute(
                            f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
                        )
                    except sqlite3.OperationalError:
                        pass

        for table, cols in (
            ("funding_history", FUNDING_META_COLS),
            ("oi_history", OI_META_COLS),
        ):
            existing = {
                row[1]
                for row in self._conn().execute(f"PRAGMA table_info({table})").fetchall()
            }
            for col_name, col_type in cols:
                if col_name not in existing:
                    try:
                        self._conn().execute(
                            f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
                        )
                    except sqlite3.OperationalError:
                        pass
        self._commit()

    def _create_research_tables(self) -> None:
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS l2_snapshots (
                symbol          TEXT    NOT NULL,
                timestamp_ms    INTEGER NOT NULL,
                mid_price       REAL    NOT NULL,
                spread_bps      REAL    NOT NULL,
                bid_depth_usd   REAL    NOT NULL,
                ask_depth_usd   REAL    NOT NULL,
                oir             REAL,
                source          TEXT    NOT NULL,
                venue           TEXT    NOT NULL,
                api_version     TEXT    NOT NULL,
                ingested_at_ms  INTEGER NOT NULL,
                quality_flags   TEXT,
                PRIMARY KEY (symbol, timestamp_ms, source)
            ) WITHOUT ROWID;
        """)
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS trade_tape (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                timestamp_ms    INTEGER NOT NULL,
                price           REAL    NOT NULL,
                size            REAL    NOT NULL,
                side            TEXT    NOT NULL,
                trade_id        TEXT,
                source          TEXT    NOT NULL,
                venue           TEXT    NOT NULL,
                api_version     TEXT    NOT NULL,
                ingested_at_ms  INTEGER NOT NULL
            );
        """)
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS coverage_reports (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                feed            TEXT    NOT NULL,
                window_start_ms INTEGER NOT NULL,
                window_end_ms   INTEGER NOT NULL,
                report_json     TEXT    NOT NULL,
                created_at_ms   INTEGER NOT NULL
            );
        """)
        self._conn().execute(
            "CREATE INDEX IF NOT EXISTS idx_l2_symbol_ts ON l2_snapshots(symbol, timestamp_ms);"
        )
        self._conn().execute(
            "CREATE INDEX IF NOT EXISTS idx_tape_symbol_ts ON trade_tape(symbol, timestamp_ms);"
        )
        self._conn().execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tape_trade_id "
            "ON trade_tape(symbol, trade_id) WHERE trade_id IS NOT NULL;"
        )
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS microstructure_gaps (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                feed            TEXT    NOT NULL,
                gap_start_ms    INTEGER NOT NULL,
                gap_end_ms      INTEGER NOT NULL,
                gap_ms          INTEGER NOT NULL,
                detail_json     TEXT,
                detected_at_ms  INTEGER NOT NULL
            );
        """)
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS feed_health_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT,
                snapshot_json   TEXT    NOT NULL,
                created_at_ms   INTEGER NOT NULL
            );
        """)
        self._conn().execute(
            "CREATE INDEX IF NOT EXISTS idx_gaps_symbol_ts ON microstructure_gaps(symbol, detected_at_ms);"
        )
        self._conn().execute("""
            CREATE TABLE IF NOT EXISTS liquidation_map_snapshots (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id             TEXT    NOT NULL,
                fetched_at_ms           INTEGER NOT NULL,
                coin                    TEXT    NOT NULL,
                side                    TEXT    NOT NULL,
                price_low               REAL    NOT NULL,
                price_high              REAL    NOT NULL,
                total_notional_usd      REAL    NOT NULL,
                position_count          INTEGER NOT NULL,
                distance_pct_from_mark  REAL,
                mark_px                 REAL,
                meta_json               TEXT
            );
        """)
        self._conn().execute(
            "CREATE INDEX IF NOT EXISTS idx_liq_map_coin_snap "
            "ON liquidation_map_snapshots(coin, fetched_at_ms);"
        )
        self._conn().execute(
            "CREATE INDEX IF NOT EXISTS idx_liq_map_snapshot_id "
            "ON liquidation_map_snapshots(snapshot_id);"
        )
        self._commit()

    def prune_old_data(self, days: int = 30) -> Dict[str, int]:
        """Research DB never prunes — explicit no-op."""
        logger.debug(
            "ResearchDatabase.prune_old_data ignored (research store is append-only)"
        )
        return {}

    def save_research_candles(
        self,
        candles: List[Candle],
        timeframe: str,
        meta: SeriesMetadata,
    ) -> None:
        """Persist candles with provenance metadata."""
        if not candles:
            return
        table = self._resolve_table(timeframe)
        sql = (
            f"INSERT OR REPLACE INTO {table} "
            "(symbol, timestamp_ms, open, high, low, close, volume, funding_rate, "
            "oi_total, oi_delta, buy_volume, sell_volume, trade_count, "
            "source, venue, api_version, ingested_at_ms, open_time_ms, "
            "volume_unit, quality_flags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        qf = meta.quality_json()
        rows = []
        for c in candles:
            open_time = getattr(c, "open_time_ms", None)
            rows.append((
                c.symbol, c.timestamp_ms, c.open, c.high, c.low, c.close,
                c.volume, c.funding_rate, c.oi_total, c.oi_delta,
                c.buy_volume, c.sell_volume, c.trade_count,
                meta.source, meta.venue, meta.api_version, meta.ingested_at_ms,
                open_time, meta.volume_unit, qf,
            ))
        with self._write_lock:
            conn = self._conn()
            conn.executemany(sql, rows)
            conn.commit()

    def get_candle_sources_in_range(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> Dict[int, str]:
        """Return ``timestamp_ms -> source`` for existing candle rows."""
        table = self._resolve_table(timeframe)
        sql = (
            f"SELECT timestamp_ms, source FROM {table} "
            "WHERE symbol = ? AND timestamp_ms >= ? AND timestamp_ms <= ?"
        )
        rows = self._conn().execute(
            sql, (symbol.upper(), int(start_ms), int(end_ms)),
        ).fetchall()
        out: Dict[int, str] = {}
        for ts, src in rows:
            out[int(ts)] = str(src or "")
        return out

    def save_research_candles_non_destructive(
        self,
        candles: List[Candle],
        timeframe: str,
        meta: SeriesMetadata,
        *,
        protect_sources: Optional[frozenset] = None,
    ) -> Tuple[int, int]:
        """Insert GoldRush rows without overwriting protected official sources.

        Returns ``(inserted, skipped_protected)``.
        """
        if not candles:
            return 0, 0
        from src.data.series_metadata import PROTECTED_OFFICIAL_SOURCES

        protected = protect_sources if protect_sources is not None else PROTECTED_OFFICIAL_SOURCES
        sym = candles[0].symbol.upper()
        ts_list = [c.timestamp_ms for c in candles]
        lo, hi = min(ts_list), max(ts_list)
        existing = self.get_candle_sources_in_range(sym, timeframe, lo, hi)

        to_write: List[Candle] = []
        skipped = 0
        for c in candles:
            prior = existing.get(c.timestamp_ms)
            if prior and prior in protected:
                skipped += 1
                continue
            to_write.append(c)

        if to_write:
            self.save_research_candles(to_write, timeframe, meta)
        return len(to_write), skipped

    def save_funding_with_meta(
        self,
        records: List[FundingRecord],
        meta: SeriesMetadata,
        *,
        freshness_ms: Optional[int] = None,
    ) -> None:
        if not records:
            return
        sql = """
            INSERT OR REPLACE INTO funding_history
            (symbol, current, predicted, timestamp,
             source, venue, api_version, ingested_at_ms, freshness_ms, quality_flags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        qf = meta.quality_json()
        params = [
            (
                r.symbol, r.current, r.predicted, r.timestamp,
                meta.source, meta.venue, meta.api_version, meta.ingested_at_ms,
                freshness_ms, qf,
            )
            for r in records
        ]
        with self._write_lock:
            conn = self._conn()
            conn.executemany(sql, params)
            conn.commit()

    def save_oi_with_meta(
        self,
        records: List[OIRecord],
        meta: SeriesMetadata,
        *,
        freshness_ms: Optional[int] = None,
    ) -> None:
        if not records:
            return
        sql = """
            INSERT OR REPLACE INTO oi_history
            (symbol, oi_total, oi_delta, timestamp,
             source, venue, api_version, ingested_at_ms, freshness_ms, quality_flags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        qf = meta.quality_json()
        params = [
            (
                r.symbol, r.oi_total, r.oi_delta, r.timestamp,
                meta.source, meta.venue, meta.api_version, meta.ingested_at_ms,
                freshness_ms, qf,
            )
            for r in records
        ]
        with self._write_lock:
            conn = self._conn()
            conn.executemany(sql, params)
            conn.commit()

    def save_l2_snapshots(self, records: List[L2SnapshotRecord]) -> None:
        if not records:
            return
        sql = """
            INSERT OR REPLACE INTO l2_snapshots
            (symbol, timestamp_ms, mid_price, spread_bps, bid_depth_usd, ask_depth_usd,
             oir, source, venue, api_version, ingested_at_ms, quality_flags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = [
            (
                r.symbol, r.timestamp_ms, r.mid_price, r.spread_bps,
                r.bid_depth_usd, r.ask_depth_usd, r.oir,
                r.source, r.venue, r.api_version, r.ingested_at_ms, r.quality_flags,
            )
            for r in records
        ]
        with self._write_lock:
            conn = self._conn()
            conn.executemany(sql, params)
            conn.commit()

    def save_trade_tape(self, records: List[TradeTapeRecord]) -> None:
        if not records:
            return
        sql = """
            INSERT OR IGNORE INTO trade_tape
            (symbol, timestamp_ms, price, size, side, trade_id,
             source, venue, api_version, ingested_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = [
            (
                r.symbol, r.timestamp_ms, r.price, r.size, r.side, r.trade_id,
                r.source, r.venue, r.api_version, r.ingested_at_ms,
            )
            for r in records
        ]
        with self._write_lock:
            conn = self._conn()
            conn.executemany(sql, params)
            conn.commit()

    def save_microstructure_gap(
        self,
        symbol: str,
        feed: str,
        gap_start_ms: int,
        gap_end_ms: int,
        *,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        gap_ms = max(0, gap_end_ms - gap_start_ms)
        detected = int(time.time() * 1000)
        detail_json = json.dumps(detail, sort_keys=True) if detail else None
        sql = """
            INSERT INTO microstructure_gaps
            (symbol, feed, gap_start_ms, gap_end_ms, gap_ms, detail_json, detected_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                sql,
                (symbol.upper(), feed, gap_start_ms, gap_end_ms, gap_ms, detail_json, detected),
            )
            conn.commit()

    def save_feed_health_snapshot(
        self,
        snapshot: Dict[str, Any],
        *,
        symbol: Optional[str] = None,
    ) -> None:
        created = int(time.time() * 1000)
        sql = """
            INSERT INTO feed_health_snapshots (symbol, snapshot_json, created_at_ms)
            VALUES (?, ?, ?)
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute(sql, (symbol, json.dumps(snapshot, sort_keys=True), created))
            conn.commit()

    def save_coverage_report(
        self,
        symbol: str,
        feed: str,
        window_start_ms: int,
        window_end_ms: int,
        report_json: str,
        created_at_ms: int,
    ) -> None:
        sql = """
            INSERT INTO coverage_reports
            (symbol, feed, window_start_ms, window_end_ms, report_json, created_at_ms)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                sql,
                (symbol, feed, window_start_ms, window_end_ms, report_json, created_at_ms),
            )
            conn.commit()

    def save_liquidation_map_zones(
        self,
        rows: List[Dict[str, Any]],
    ) -> int:
        """Insert zone rows into ``liquidation_map_snapshots``. Returns row count."""
        if not rows:
            return 0
        sql = """
            INSERT INTO liquidation_map_snapshots (
                snapshot_id, fetched_at_ms, coin, side, price_low, price_high,
                total_notional_usd, position_count, distance_pct_from_mark,
                mark_px, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = []
        for r in rows:
            params.append((
                str(r["snapshot_id"]),
                int(r["fetched_at_ms"]),
                str(r["coin"]).upper(),
                str(r["side"]),
                float(r["price_low"]),
                float(r["price_high"]),
                float(r["total_notional_usd"]),
                int(r["position_count"]),
                r.get("distance_pct_from_mark"),
                r.get("mark_px"),
                r.get("meta_json"),
            ))
        with self._write_lock:
            conn = self._conn()
            conn.executemany(sql, params)
            conn.commit()
        return len(params)

    def load_latest_liquidation_map(
        self,
        coin: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Load zones from the most recent snapshot (optionally filtered by coin)."""
        conn = self._conn()
        if coin:
            row = conn.execute(
                "SELECT MAX(fetched_at_ms) FROM liquidation_map_snapshots WHERE coin = ?",
                (coin.upper(),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT MAX(fetched_at_ms) FROM liquidation_map_snapshots"
            ).fetchone()
        if not row or row[0] is None:
            return []
        latest_ms = int(row[0])
        if coin:
            cur = conn.execute(
                "SELECT * FROM liquidation_map_snapshots "
                "WHERE fetched_at_ms = ? AND coin = ? "
                "ORDER BY total_notional_usd DESC",
                (latest_ms, coin.upper()),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM liquidation_map_snapshots "
                "WHERE fetched_at_ms = ? "
                "ORDER BY coin, total_notional_usd DESC",
                (latest_ms,),
            )
        return [dict(r) for r in cur.fetchall()]

    def get_candle_metadata_sample(
        self, symbol: str, timeframe: str = "1m", limit: int = 1,
    ) -> Optional[Dict[str, Any]]:
        table = self._resolve_table(timeframe)
        sql = f"""
            SELECT source, venue, api_version, volume_unit, quality_flags
            FROM {table}
            WHERE symbol = ?
            ORDER BY timestamp_ms DESC
            LIMIT ?
        """
        with self._conn():
            row = self._conn().execute(sql, (symbol, limit)).fetchone()
        return dict(row) if row else None

    def is_research_db(self) -> bool:
        return True

    def count_taker_split_bars(
        self,
        symbol: str,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> int:
        table = self._resolve_table("1m")
        cond = ["symbol = ?", "buy_volume IS NOT NULL", "sell_volume IS NOT NULL"]
        params: List[Any] = [symbol.upper()]
        if start_ms is not None:
            cond.append("timestamp_ms >= ?")
            params.append(start_ms)
        if end_ms is not None:
            cond.append("timestamp_ms <= ?")
            params.append(end_ms)
        sql = f"SELECT COUNT(*) FROM {table} WHERE {' AND '.join(cond)}"
        with self._conn():
            row = self._conn().execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def count_l2_in_window(
        self,
        symbol: str,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> int:
        cond = ["symbol = ?"]
        params: List[Any] = [symbol.upper()]
        if start_ms is not None:
            cond.append("timestamp_ms >= ?")
            params.append(start_ms)
        if end_ms is not None:
            cond.append("timestamp_ms <= ?")
            params.append(end_ms)
        sql = f"SELECT COUNT(*) FROM l2_snapshots WHERE {' AND '.join(cond)}"
        with self._conn():
            row = self._conn().execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def count_trade_tape_in_window(
        self,
        symbol: str,
        start_ms: Optional[int],
        end_ms: Optional[int],
    ) -> int:
        cond = ["symbol = ?"]
        params: List[Any] = [symbol.upper()]
        if start_ms is not None:
            cond.append("timestamp_ms >= ?")
            params.append(start_ms)
        if end_ms is not None:
            cond.append("timestamp_ms <= ?")
            params.append(end_ms)
        sql = f"SELECT COUNT(*) FROM trade_tape WHERE {' AND '.join(cond)}"
        with self._conn():
            row = self._conn().execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def resolve_path(config: Any) -> Path:
        rel = config.get("research.database.path", str(DEFAULT_RESEARCH_DB_PATH))
        return Path(str(rel))
