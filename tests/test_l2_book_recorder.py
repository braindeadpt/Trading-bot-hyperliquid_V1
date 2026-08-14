"""Unit tests for research L2 book recorder (levels → gzip JSONL)."""

from __future__ import annotations

import asyncio
import gzip
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.data.l2_book_recorder import (
    L2BookRecorder,
    L2BookRecorderConfig,
    config_from_mapping,
)
from src.data.orderbook_metrics import PriceLevel, calculate_metrics
from src.exchanges.hyperliquid_ws import DataBus


@pytest.fixture()
def tmp_path() -> Path:
    """Use a disposable project-contained path required by safe-path policy."""
    root = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "research"
        / f"_test_l2_recorder_{uuid.uuid4().hex}"
    )
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _book(bids, asks, ts_ms: int | None = None):
    if ts_ms is None:
        import time

        ts_ms = int(time.time() * 1000)
    return SimpleNamespace(
        bids=[SimpleNamespace(price=p, size=s) for p, s in bids],
        asks=[SimpleNamespace(price=p, size=s) for p, s in asks],
        timestamp_ms=ts_ms,
    )


@pytest.mark.unit
def test_resolve_external_path_refused_without_optin(tmp_path: Path) -> None:
    """External destination (outside the repo) without the opt-in is REFUSED
    (None — recording disabled), never silently redirected to the repo default."""
    from src.data.l2_book_recorder import resolve_l2_recording_root

    project = tmp_path / "project"
    project.mkdir()
    with tempfile.TemporaryDirectory() as external_dir:
        external = Path(external_dir) / "l2_books"
        assert resolve_l2_recording_root(str(external), project) is None


@pytest.mark.unit
def test_resolve_external_path_honoured_with_optin(tmp_path: Path) -> None:
    """With ``allow_external_path=True`` the configured external destination is
    used as-is — config and reality never diverge."""
    from src.data.l2_book_recorder import resolve_l2_recording_root

    project = tmp_path / "project"
    project.mkdir()
    with tempfile.TemporaryDirectory() as external_dir:
        external = Path(external_dir) / "l2_books"
        resolved = resolve_l2_recording_root(
            str(external), project, allow_external_path=True
        )
        assert resolved is not None
        assert resolved == external.resolve()


@pytest.mark.unit
def test_resolve_relative_path_inside_project_still_works(tmp_path: Path) -> None:
    """Repo-contained paths keep working without any opt-in."""
    from src.data.l2_book_recorder import resolve_l2_recording_root

    resolved = resolve_l2_recording_root("data/research/l2_books", tmp_path)
    assert resolved is not None
    assert resolved == (tmp_path / "data" / "research" / "l2_books").resolve()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_path_without_optin_disables_and_writes_nowhere(
    tmp_path: Path,
) -> None:
    """External path + no opt-in: recorder refuses to start, reports DISABLED
    and writes NOTHING under the repo default (no silent fallback)."""
    bus = DataBus()
    with tempfile.TemporaryDirectory() as external_dir:
        cfg = L2BookRecorderConfig(
            enabled=True,
            path=str(Path(external_dir) / "l2_books"),
            flush_interval_sec=0.05,
        )
        rec = L2BookRecorder(bus, ["BTC"], cfg, project_root=tmp_path)
        assert await rec.start() is False
        await rec.stop()
        assert rec.stats["path"] == "DISABLED"
        assert rec.stats["active"] is False
        # No fallback: nothing written under the repo default destination.
        fallback = tmp_path / "data" / "research" / "l2_books"
        assert not fallback.exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_path_with_optin_is_used(tmp_path: Path) -> None:
    """External path + opt-in + writable: the recorder USES the external
    destination — snapshots land there, nothing under the repo root."""
    bus = DataBus()
    with tempfile.TemporaryDirectory() as external_dir:
        external = Path(external_dir) / "l2_books"
        cfg = L2BookRecorderConfig(
            enabled=True,
            interval_sec=0.01,
            depth_levels=5,
            path=str(external),
            flush_interval_sec=0.05,
            allow_external_path=True,
        )
        rec = L2BookRecorder(bus, ["BTC"], cfg, project_root=tmp_path)
        assert await rec.start() is True
        bus.publish(
            "orderbook:BTC",
            _book(
                [(100.0, 1.0), (99.9, 2.0)],
                [(100.1, 1.5), (100.2, 2.0)],
            ),
        )
        for _ in range(40):
            await asyncio.sleep(0.05)
            if rec.stats["written"] >= 1:
                break
        await rec.stop()
        assert rec.stats["written"] >= 1, f"stats={rec.stats}"
        files = list(external.joinpath("BTC").glob("*.jsonl.gz"))
        assert files, f"expected daily gzip under external root; listing={list(external.rglob('*'))}"
        # Nothing was written under the repo project root.
        assert not list(tmp_path.rglob("*.jsonl.gz"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_path_unavailable_disables_with_error(tmp_path: Path) -> None:
    """External path that cannot be created (parent is a regular file):
    recording disables with ERROR and writes nowhere else."""
    bus = DataBus()
    with tempfile.TemporaryDirectory() as external_dir:
        blocker = Path(external_dir) / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        cfg = L2BookRecorderConfig(
            enabled=True,
            path=str(blocker / "l2_books"),
            flush_interval_sec=0.05,
            allow_external_path=True,
        )
        rec = L2BookRecorder(bus, ["BTC"], cfg, project_root=tmp_path)
        assert await rec.start() is False
        await rec.stop()
        assert rec.stats["active"] is False
        fallback = tmp_path / "data" / "research" / "l2_books"
        assert not fallback.exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disk_space_check_disables_recording(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """Free space below the floor disables recording with an ERROR — a full
    volume must not look like a silent feed outage."""
    import src.data.l2_book_recorder as recorder_mod

    bus = DataBus()
    cfg = L2BookRecorderConfig(
        enabled=True,
        path=str(tmp_path / "l2"),
        flush_interval_sec=0.05,
    )
    rec = L2BookRecorder(bus, ["BTC"], cfg, project_root=tmp_path)

    class _Usage:
        total = 1024 * 1024 * 1024
        used = total - 64
        free = 64  # below the 512 MB floor

    monkeypatch.setattr(recorder_mod.shutil, "disk_usage", lambda _p: _Usage())
    with caplog.at_level("ERROR", logger="src.data.l2_book_recorder"):
        assert await rec.start() is False
    await rec.stop()
    assert any("nearly full" in r.message for r in caplog.records)
    assert rec.stats["active"] is False


@pytest.mark.unit
def test_config_from_mapping_carries_allow_external_path() -> None:
    cfg = config_from_mapping(
        {
            "market_data": {
                "l2_recording": {
                    "path": "E:/research/l2_books",
                    "allow_external_path": True,
                }
            }
        },
        Path("."),
    )
    assert cfg.allow_external_path is True
    assert cfg.path == "E:/research/l2_books"
    assert config_from_mapping({}, Path(".")).allow_external_path is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retention_prune_deletes_old_files(tmp_path: Path) -> None:
    bus = DataBus()
    root = tmp_path / "l2"
    btc = root / "BTC"
    btc.mkdir(parents=True)
    old = btc / "2020-01-01.jsonl.gz"
    new = btc / "2099-01-01.jsonl.gz"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    cfg = L2BookRecorderConfig(
        enabled=True,
        path=str(root),
        retention_days=30,
        prune_interval_sec=60,
        flush_interval_sec=0.05,
    )
    rec = L2BookRecorder(bus, ["BTC"], cfg, project_root=tmp_path)
    assert await rec.start() is True
    await rec.stop()
    assert not old.exists()
    assert new.exists()
    assert rec.stats["pruned_files"] >= 1


@pytest.mark.unit
def test_config_from_mapping_defaults() -> None:
    cfg = config_from_mapping({}, Path("."))
    assert cfg.enabled is True
    assert cfg.interval_sec == 2.0
    assert cfg.depth_levels == 10
    assert "l2_books" in cfg.path


@pytest.mark.unit
def test_config_from_nested_market_data() -> None:
    cfg = config_from_mapping(
        {"market_data": {"l2_recording": {"enabled": False, "interval_sec": 5}}},
        Path("."),
    )
    assert cfg.enabled is False
    assert cfg.interval_sec == 5.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recorder_writes_reconstructible_snapshot(tmp_path: Path) -> None:
    bus = DataBus()
    beats: list[int] = []
    cfg = L2BookRecorderConfig(
        enabled=True,
        interval_sec=0.01,
        depth_levels=5,
        path=str(tmp_path / "l2"),
        retention_days=90,
        queue_max=100,
        flush_interval_sec=0.05,
    )
    rec = L2BookRecorder(
        bus,
        ["BTC"],
        cfg,
        project_root=tmp_path,
        on_persist=lambda ts: beats.append(ts),
    )
    await rec.start()
    book = _book(
        [(100.0, 1.0), (99.9, 2.0), (99.8, 3.0), (99.7, 1.0), (99.6, 1.0)],
        [(100.1, 1.5), (100.2, 2.0), (100.3, 1.0), (100.4, 1.0), (100.5, 1.0)],
    )
    bus.publish("orderbook:BTC", book)
    # Allow async DataBus callback + flush
    for _ in range(40):
        await asyncio.sleep(0.05)
        if rec.stats["written"] >= 1:
            break
    await rec.stop()

    assert rec.stats["written"] >= 1, f"stats={rec.stats} root={rec._root}"
    assert beats, "FeedSilence on_persist should fire"
    files = list(Path(rec.stats["path"]).joinpath("BTC").glob("*.jsonl.gz"))
    assert files, f"expected daily gzip under {rec._root}; listing={list(rec._root.rglob('*'))}"
    with gzip.open(files[0], "rt", encoding="utf-8") as fh:
        row = json.loads(fh.readline())
    assert row["symbol"] == "BTC"
    assert row["exchange_ts_ms"] == book.timestamp_ms
    assert "received_ts_ms" in row
    bids = [PriceLevel(p, s) for p, s in row["bids"]]
    asks = [PriceLevel(p, s) for p, s in row["asks"]]
    m = calculate_metrics(bids, asks, "BTC", row["exchange_ts_ms"])
    assert abs(m.spread_pct - row["spread_pct"]) < 1e-12
    assert abs(m.oir_10levels - row["oir_10"]) < 1e-12
    assert abs(m.depth_quality - row["depth_quality"]) < 1e-12


@pytest.mark.unit
def test_reconstruct_metrics_from_row_helper() -> None:
    """Pure reconstruct check without DataBus / filesystem races."""
    bids = [(100.0, 1.0), (99.9, 2.0)]
    asks = [(100.1, 1.5), (100.2, 2.0)]
    pl_b = [PriceLevel(p, s) for p, s in bids]
    pl_a = [PriceLevel(p, s) for p, s in asks]
    m = calculate_metrics(pl_b, pl_a, "BTC", 1)
    row = {
        "spread_pct": m.spread_pct,
        "oir_10": m.oir_10levels,
        "depth_quality": m.depth_quality,
        "mid": m.mid_price,
        "bids": bids,
        "asks": asks,
        "symbol": "BTC",
        "exchange_ts_ms": 1,
    }
    m2 = calculate_metrics(
        [PriceLevel(p, s) for p, s in row["bids"]],
        [PriceLevel(p, s) for p, s in row["asks"]],
        "BTC",
        1,
    )
    assert m2.spread_pct == row["spread_pct"]
    assert m2.oir_10levels == row["oir_10"]
    assert m2.depth_quality == row["depth_quality"]
    assert m2.mid_price == row["mid"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recorder_does_not_raise_on_bad_book(tmp_path: Path) -> None:
    bus = DataBus()
    cfg = L2BookRecorderConfig(
        enabled=True,
        interval_sec=0.01,
        path=str(tmp_path / "l2"),
        flush_interval_sec=0.05,
    )
    rec = L2BookRecorder(bus, ["ETH"], cfg, project_root=tmp_path)
    await rec.start()
    bus.publish("orderbook:ETH", SimpleNamespace(bids=[], asks=[], timestamp_ms=1))
    await asyncio.sleep(0.05)
    await rec.stop()
    assert rec.stats["written"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_size_only_change_waits_for_interval(tmp_path: Path) -> None:
    bus = DataBus()
    cfg = L2BookRecorderConfig(
        enabled=True,
        interval_sec=10.0,  # long — size flicker must not bypass
        depth_levels=2,
        min_mid_change_bps=1.0,
        path=str(tmp_path / "l2"),
        flush_interval_sec=0.05,
    )
    rec = L2BookRecorder(bus, ["SOL"], cfg, project_root=tmp_path)
    await rec.start()
    b1 = _book([(50.0, 1.0), (49.9, 1.0)], [(50.1, 1.0), (50.2, 1.0)])
    b2 = _book([(50.0, 9.0), (49.9, 1.0)], [(50.1, 9.0), (50.2, 1.0)])
    bus.publish("orderbook:SOL", b1)
    await asyncio.sleep(0.1)
    bus.publish("orderbook:SOL", b2)
    await asyncio.sleep(0.15)
    await rec.stop()
    # First snapshot only (size change is not material for bypass)
    assert rec.stats["written"] == 1
