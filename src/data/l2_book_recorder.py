"""Research L2 book recorder — top-K levels to daily gzip JSONL.

Never writes to the live operational DB. Failures degrade with ERROR logs only
and must not block the trading event loop.

The destination is configurable by design (``market_data.l2_recording.path`` —
research storage may live on another volume). Paths outside the project root
are honoured only with the explicit ``allow_external_path`` opt-in; any
refused or unavailable destination DISABLES recording with an ERROR — the
recorder never silently redirects to another path (2026-08-14 audit: the
E: → C: silent regression).

Architecture map: ``docs/DATA_ARCHITECTURE.md``.
Backup: ``scripts/backup_research_data.py``.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.data.orderbook_metrics import PriceLevel, calculate_metrics
from src.exchanges.hyperliquid_ws import DataBus
from src.utils.helpers import safe_write_file, validate_safe_path

logger = logging.getLogger(__name__)
_SAFE_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Below this much free space the recorder disables itself with an ERROR
# (trading unaffected) instead of filling the volume — a full disk would
# silently stop persisting and look like a feed outage.
_MIN_FREE_BYTES = 512 * 1024 * 1024  # 512 MB


@dataclass(frozen=True)
class L2BookRecorderConfig:
    enabled: bool = True
    interval_sec: float = 2.0
    depth_levels: int = 10
    min_mid_change_bps: float = 1.0
    path: str = "data/research/l2_books"
    retention_days: int = 90
    prune_interval_sec: float = 3600.0
    queue_max: int = 5_000
    flush_interval_sec: float = 1.0
    # Opt-in for destinations OUTSIDE the project root (research HDD volume).
    # Without it an external path is refused — recording disabled with ERROR —
    # never silently redirected to the repo default.
    allow_external_path: bool = False


def resolve_l2_recording_root(
    cfg_path: str,
    project_root: Path,
    *,
    allow_external_path: bool = False,
) -> Optional[Path]:
    """Resolve the recorder root from the configured path.

    Returns the resolved root, or ``None`` when the configured path is
    refused — the recorder then DISABLES itself with an ERROR log (trading
    unaffected). It NEVER silently redirects to another path: a fallback that
    contradicts the config is worse than failing loud (2026-08-14 audit — the
    E: → C: silent regression).

    Rules:
      * ``..`` traversal is refused outright.
      * Paths resolving inside the repository keep the safe-path guard.
      * External paths (outside the repository — e.g. a research HDD volume)
        require the explicit ``allow_external_path`` opt-in; l2_recording is
        research storage and may live on another volume by design, but only
        when the deployment says so.
    """
    root = project_root.resolve()
    raw = str(cfg_path).strip()
    if not raw:
        logger.error(
            "L2BookRecorder path is empty — recording disabled (trading unaffected)"
        )
        return None
    raw_path = Path(raw)
    if ".." in raw_path.parts:
        logger.error(
            "L2BookRecorder path contains '..' (%s) — refusing; recording "
            "disabled (trading unaffected)",
            cfg_path,
        )
        return None
    resolved = (
        raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
    )
    try:
        repo_relative = resolved.relative_to(_SAFE_PROJECT_ROOT)
    except ValueError:
        # External storage (research HDD volume). Honoured only with the opt-in.
        if not allow_external_path:
            logger.error(
                "L2BookRecorder path outside project root (%s) requires "
                "market_data.l2_recording.allow_external_path: true — recording "
                "DISABLED (config never silently overridden; trading unaffected)",
                resolved,
            )
            return None
        logger.info("L2BookRecorder external path opted-in: %s", resolved)
        return resolved
    safe = validate_safe_path(repo_relative.as_posix())
    if safe is None:
        logger.error(
            "L2BookRecorder rejected unsafe path %s — recording disabled "
            "(trading unaffected)",
            resolved,
        )
        return None
    return safe


def config_from_mapping(md: Any, project_root: Path) -> L2BookRecorderConfig:
    """Parse ``market_data.l2_recording`` from a Config object or nested dict."""
    del project_root  # reserved for future path-relative defaults
    raw: Dict[str, Any] = {}
    if isinstance(md, dict):
        nested = md.get("market_data") if isinstance(md.get("market_data"), dict) else {}
        raw = dict(md.get("l2_recording") or nested.get("l2_recording") or {})
    elif md is not None and hasattr(md, "get"):
        section = md.get("market_data.l2_recording")
        if isinstance(section, dict):
            raw = dict(section)
        else:
            raw = {
                "enabled": md.get("market_data.l2_recording.enabled", True),
                "interval_sec": md.get("market_data.l2_recording.interval_sec", 2.0),
                "depth_levels": md.get("market_data.l2_recording.depth_levels", 10),
                "min_mid_change_bps": md.get(
                    "market_data.l2_recording.min_mid_change_bps", 1.0
                ),
                "path": md.get(
                    "market_data.l2_recording.path", "data/research/l2_books"
                ),
                "retention_days": md.get(
                    "market_data.l2_recording.retention_days", 90
                ),
                "prune_interval_sec": md.get(
                    "market_data.l2_recording.prune_interval_sec", 3600.0
                ),
                "queue_max": md.get("market_data.l2_recording.queue_max", 5_000),
                "flush_interval_sec": md.get(
                    "market_data.l2_recording.flush_interval_sec", 1.0
                ),
                "allow_external_path": md.get(
                    "market_data.l2_recording.allow_external_path", False
                ),
            }
    path = str(raw.get("path", "data/research/l2_books"))
    return L2BookRecorderConfig(
        enabled=bool(raw.get("enabled", True)),
        interval_sec=float(raw.get("interval_sec", 2.0)),
        depth_levels=max(1, int(raw.get("depth_levels", 10))),
        min_mid_change_bps=float(raw.get("min_mid_change_bps", 1.0)),
        path=path,
        retention_days=max(1, int(raw.get("retention_days", 90))),
        prune_interval_sec=max(60.0, float(raw.get("prune_interval_sec", 3600.0))),
        queue_max=max(100, int(raw.get("queue_max", 5_000))),
        flush_interval_sec=max(0.2, float(raw.get("flush_interval_sec", 1.0))),
        allow_external_path=bool(raw.get("allow_external_path", False)),
    )


class L2BookRecorder:
    """Subscribe to ``orderbook:{symbol}``, persist top-K levels off the hot path."""

    def __init__(
        self,
        bus: DataBus,
        symbols: Sequence[str],
        cfg: L2BookRecorderConfig,
        *,
        project_root: Optional[Path] = None,
        on_persist: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._bus = bus
        self._symbols = [s.strip().upper() for s in symbols]
        self._cfg = cfg
        root = project_root or Path.cwd()
        self._project_root = root.resolve()
        self._root = resolve_l2_recording_root(
            cfg.path, root, allow_external_path=cfg.allow_external_path
        )
        self._on_persist = on_persist  # beat FeedSilenceMonitor
        if self._root is None:
            # Refused path: disabled from birth — trading unaffected, and no
            # silent redirection to any other destination.
            self._disk_ok = False

        self._queue: Optional[asyncio.Queue] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self._disk_ok = True
        self._callbacks: Dict[str, Any] = {}

        self._last_write_mono: Dict[str, float] = {}
        # (best_bid_px, best_ask_px, mid) — size-only churn does not bypass interval
        self._last_bbo: Dict[str, Tuple[float, float, float]] = {}
        self._dropped = 0
        self._written = 0
        self._write_errors = 0
        self._bytes_written = 0
        self._pruned_files = 0
        self._last_prune_mono = 0.0
        self._started_mono = 0.0

    @property
    def active(self) -> bool:
        return self._running and self._disk_ok

    @property
    def stats(self) -> Dict[str, Any]:
        elapsed = max(1e-6, time.monotonic() - self._started_mono) if self._started_mono else 1.0
        return {
            "written": self._written,
            "dropped": self._dropped,
            "write_errors": self._write_errors,
            "bytes_written": self._bytes_written,
            "mb_per_hour": (self._bytes_written / (1024 * 1024)) / (elapsed / 3600.0),
            "path": str(self._root) if self._root is not None else "DISABLED",
            "interval_sec": self._cfg.interval_sec,
            "depth_levels": self._cfg.depth_levels,
            "retention_days": self._cfg.retention_days,
            "pruned_files": self._pruned_files,
            "disk_ok": self._disk_ok,
            "active": self.active,
        }

    def _ensure_writable(self) -> bool:
        """Create root + write probe + free-space check.

        Returns False on any failure — recording is disabled with an ERROR,
        trading unaffected. The root was already validated at construction
        (safe inside the repo, or external with opt-in), so the probe needs
        no further path guard.
        """
        if self._root is None:
            return False
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            probe = self._root / ".write_probe"
            # AUDIT-004 remediated (2026-08-14): atomic safe_write_file (temp
            # + move) instead of a raw write_text; works for repo-contained
            # and opted-in external roots alike.
            if not safe_write_file(probe, "ok"):
                return False
            probe.unlink(missing_ok=True)
        except OSError as exc:
            logger.error(
                "L2BookRecorder disk unavailable at %s: %s — "
                "recording disabled (trading unaffected)",
                self._root,
                exc,
            )
            return False
        try:
            usage = shutil.disk_usage(self._root)
        except OSError as exc:
            logger.error(
                "L2BookRecorder cannot read disk usage at %s: %s — "
                "recording disabled (trading unaffected)",
                self._root,
                exc,
            )
            return False
        if usage.free < _MIN_FREE_BYTES:
            logger.error(
                "L2BookRecorder disk nearly full at %s: %.1f MB free "
                "(need >= %d MB) — recording disabled (trading unaffected)",
                self._root,
                usage.free / (1024 * 1024),
                _MIN_FREE_BYTES // (1024 * 1024),
            )
            return False
        return True

    async def start(self) -> bool:
        """Start recorder. Returns False if path refused or disk unusable
        (trading unaffected)."""
        if self._running or not self._cfg.enabled:
            return self._running
        if self._root is None:
            self._disk_ok = False
            logger.error(
                "L2BookRecorder refused its configured path — recording disabled "
                "(trading unaffected; fix market_data.l2_recording.path or enable "
                "allow_external_path)"
            )
            return False
        ok = await asyncio.to_thread(self._ensure_writable)
        if not ok:
            self._disk_ok = False
            return False
        self._disk_ok = True
        self._queue = asyncio.Queue(maxsize=self._cfg.queue_max)
        self._running = True
        self._started_mono = time.monotonic()
        for sym in self._symbols:
            cb = self._make_callback(sym)
            self._callbacks[sym] = cb
            await self._bus.subscribe(f"orderbook:{sym}", cb)
        # Retention runs on start + every prune_interval_sec (not only on flush count)
        try:
            pruned = await asyncio.to_thread(self._prune_retention)
            self._pruned_files += pruned
            self._last_prune_mono = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            logger.error("L2BookRecorder initial retention prune failed: %s", exc)
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="l2_book_recorder_flush"
        )
        logger.info(
            "L2BookRecorder started -> %s (interval=%.1fs depth=%d retention=%dd "
            "prune_every=%.0fs symbols=%s)",
            self._root,
            self._cfg.interval_sec,
            self._cfg.depth_levels,
            self._cfg.retention_days,
            self._cfg.prune_interval_sec,
            self._symbols,
        )
        return True

    async def stop(self) -> None:
        self._running = False
        for sym, cb in list(self._callbacks.items()):
            try:
                await self._bus.unsubscribe(f"orderbook:{sym}", cb)
            except Exception:  # noqa: BLE001
                pass
        self._callbacks.clear()
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self._flush_once(force=True)
        try:
            pruned = await asyncio.to_thread(self._prune_retention)
            self._pruned_files += pruned
        except Exception as exc:  # noqa: BLE001
            logger.error("L2BookRecorder stop retention prune failed: %s", exc)
        logger.info("L2BookRecorder stopped — %s", self.stats)

    def _make_callback(self, symbol: str):
        # Sync callback: DataBus invokes directly (no create_task), enqueue only.
        def _on_book(book: Any) -> None:
            try:
                self._maybe_enqueue(symbol, book)
            except Exception as exc:  # noqa: BLE001
                # Never raise into DataBus / engine
                logger.debug("L2BookRecorder enqueue %s: %s", symbol, exc)

        return _on_book

    def _maybe_enqueue(self, symbol: str, book: Any) -> None:
        if not self._running or not self._disk_ok or self._queue is None:
            return
        received_ms = int(time.time() * 1000)
        bids_raw = getattr(book, "bids", None) or []
        asks_raw = getattr(book, "asks", None) or []
        if not bids_raw or not asks_raw:
            return
        k = self._cfg.depth_levels
        bids = [
            (float(getattr(lv, "price", 0.0)), float(getattr(lv, "size", 0.0)))
            for lv in bids_raw[:k]
        ]
        asks = [
            (float(getattr(lv, "price", 0.0)), float(getattr(lv, "size", 0.0)))
            for lv in asks_raw[:k]
        ]
        if not bids or not asks or bids[0][0] <= 0 or asks[0][0] <= 0:
            return

        mid = 0.5 * (bids[0][0] + asks[0][0])
        bbo = (bids[0][0], asks[0][0], mid)
        now_mono = time.monotonic()
        last_mono = self._last_write_mono.get(symbol, 0.0)
        interval_ok = (now_mono - last_mono) >= self._cfg.interval_sec
        prev = self._last_bbo.get(symbol)
        material = False
        if prev is None:
            material = True
        else:
            # Price move at BBO, or mid move ≥ threshold (ignore size-only flicker)
            if prev[0] != bbo[0] or prev[1] != bbo[1]:
                material = True
            elif prev[2] > 0:
                mid_chg_bps = abs(mid - prev[2]) / prev[2] * 1e4
                if mid_chg_bps >= self._cfg.min_mid_change_bps:
                    material = True
        if not interval_ok and not material:
            return

        # Metrics with the same helper the engine uses (on recorded K levels)
        pl_bids = [PriceLevel(p, s) for p, s in bids]
        pl_asks = [PriceLevel(p, s) for p, s in asks]
        exchange_ts = int(getattr(book, "timestamp_ms", received_ms))
        metrics = calculate_metrics(pl_bids, pl_asks, symbol, exchange_ts)

        row = {
            "symbol": symbol,
            "exchange_ts_ms": exchange_ts,
            "received_ts_ms": received_ms,
            "bids": [[p, s] for p, s in bids],
            "asks": [[p, s] for p, s in asks],
            "mid": float(metrics.mid_price),
            "spread_pct": float(metrics.spread_pct),
            "oir_10": float(metrics.oir_10levels),
            "depth_quality": float(metrics.depth_quality),
            "bid_ask_ratio": float(metrics.bid_ask_ratio),
        }
        try:
            self._queue.put_nowait(row)
            self._last_write_mono[symbol] = now_mono
            self._last_bbo[symbol] = bbo
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning(
                    "L2BookRecorder queue full — dropped %d snapshots (trading unaffected)",
                    self._dropped,
                )

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._cfg.flush_interval_sec)
                await self._flush_once(force=False)
                await self._maybe_prune(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._write_errors += 1
                logger.error(
                    "L2BookRecorder flush loop error: %s (trading unaffected)", exc
                )

    async def _maybe_prune(self, *, force: bool) -> None:
        now = time.monotonic()
        due = (now - self._last_prune_mono) >= self._cfg.prune_interval_sec
        if not force and not due:
            return
        try:
            pruned = await asyncio.to_thread(self._prune_retention)
            self._pruned_files += pruned
            self._last_prune_mono = now
            if pruned:
                logger.info(
                    "L2BookRecorder retention: pruned %d file(s) older than %dd",
                    pruned,
                    self._cfg.retention_days,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("L2 retention prune failed: %s (trading unaffected)", exc)

    async def _flush_once(self, *, force: bool) -> None:
        if self._queue is None:
            return
        batch: List[Dict[str, Any]] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch and not force:
            return
        if batch:
            # Offload blocking gzip I/O to a thread so the event loop stays free
            try:
                n, nbytes = await asyncio.to_thread(self._write_batch, batch)
                self._written += n
                self._bytes_written += nbytes
                self._disk_ok = True
                if self._on_persist is not None and batch:
                    try:
                        self._on_persist(int(batch[-1]["received_ts_ms"]))
                    except Exception:  # noqa: BLE001
                        pass
            except OSError as exc:
                self._write_errors += 1
                self._disk_ok = False
                logger.error(
                    "L2BookRecorder write failed (disk?): %s — "
                    "dropping batch, trading unaffected",
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                self._write_errors += 1
                logger.error(
                    "L2BookRecorder write failed: %s — trading unaffected", exc
                )

    def _write_batch(self, batch: List[Dict[str, Any]]) -> Tuple[int, int]:
        by_file: Dict[Path, List[str]] = {}
        for row in batch:
            sym = row["symbol"]
            day = datetime.fromtimestamp(
                row["exchange_ts_ms"] / 1000.0, tz=timezone.utc
            ).strftime("%Y-%m-%d")
            path = self._root / sym / f"{day}.jsonl.gz"
            by_file.setdefault(path, []).append(json.dumps(row, separators=(",", ":")))
        nbytes = 0
        n = 0
        for path, lines in by_file.items():
            # Defense-in-depth: every write stays inside the validated root
            # (safe repo path, or external root honoured via opt-in).
            try:
                resolved = path.resolve()
                resolved.relative_to(self._root)
            except ValueError as exc:
                raise OSError(f"L2 path escaped recorder root: {path}") from exc
            resolved.parent.mkdir(parents=True, exist_ok=True)
            payload = ("\n".join(lines) + "\n").encode("utf-8")
            # Append gzip members (concatenated gzip is valid)
            with resolved.open("ab") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb") as compressed:
                    compressed.write(payload)
            nbytes += len(payload)
            n += len(lines)
        return n, nbytes

    def _prune_retention(self) -> int:
        """Delete ``*.jsonl.gz`` older than retention_days. Returns files removed."""
        cutoff = datetime.now(timezone.utc).date() - timedelta(
            days=self._cfg.retention_days
        )
        if self._root is None or not self._root.exists():
            return 0
        removed = 0
        for sym_dir in self._root.iterdir():
            if not sym_dir.is_dir() or sym_dir.name.startswith("_"):
                continue
            for f in sym_dir.glob("*.jsonl.gz"):
                name = f.name
                if not name.endswith(".jsonl.gz"):
                    continue
                day_str = name[: -len(".jsonl.gz")]
                try:
                    day = datetime.strptime(day_str, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day < cutoff:
                    try:
                        f.unlink()
                        removed += 1
                        logger.info("L2BookRecorder pruned %s", f)
                    except OSError as exc:
                        logger.error("L2 prune failed %s: %s", f, exc)
        return removed


def start_l2_book_recorder_from_config(
    bus: DataBus,
    cfg: Any,
    *,
    project_root: Optional[Path] = None,
    on_persist: Optional[Callable[[int], None]] = None,
) -> Optional[L2BookRecorder]:
    root = project_root or Path.cwd()
    rec_cfg = config_from_mapping(cfg, root)
    if not rec_cfg.enabled:
        return None
    from src.utils.config import get_trading_symbols

    symbols = get_trading_symbols(cfg)
    return L2BookRecorder(
        bus,
        symbols,
        rec_cfg,
        project_root=root,
        on_persist=on_persist,
    )
