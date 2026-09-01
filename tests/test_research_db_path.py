"""Regression: research DB path must come from config, never a silent default."""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.research_database import ResearchDatabase, ghost_research_db_path
from src.research.shadow_recorder import ShadowDecision, ShadowRecorder
from src.utils.config import Config

pytestmark = pytest.mark.unit

SRC_ROOT = ROOT / "src"


def test_research_database_requires_explicit_path() -> None:
    with pytest.raises(TypeError, match="explicit db_path"):
        ResearchDatabase(None)  # type: ignore[arg-type]


def test_partial_config_falls_back_to_default_config_path() -> None:
    path = ResearchDatabase.resolve_path(Config({}))
    assert path.name == "hyperliquid.db"
    assert "research" in str(path)


def test_open_uses_configured_path(tmp_path: Path) -> None:
    target = tmp_path / "configured" / "research.db"
    cfg = Config({"research": {"database": {"path": str(target)}}})
    db = ResearchDatabase.open(cfg)
    assert db.db_path.resolve() == target.resolve()
    db.save_feed_silence_alerts([("liquidation_okx", "early", 1, "test")])
    assert target.exists()


def test_shadow_recorder_uses_configured_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "e_drive" / "hyperliquid.db"
    ghost = ROOT / "data" / "research" / "hyperliquid.db"
    ghost.unlink(missing_ok=True)

    cfg = Config({"research": {"database": {"path": str(configured)}}})

    def _fake_load(_p=None):  # noqa: ANN001
        return cfg

    import src.utils.config as config_mod

    monkeypatch.setattr(config_mod, "load_config", _fake_load)

    rec = ShadowRecorder()
    rec.record(
        ShadowDecision(
            symbol="BTC",
            strategy="VWAPDeviation",
            variant="phase08_shadow",
            side="long",
            would_enter=True,
            reason="test",
            timestamp_ms=1_000_000,
        )
    )

    assert configured.exists()
    assert not ghost.exists()


def test_no_bare_research_database_constructor_in_src() -> None:
    pattern = re.compile(r"ResearchDatabase\s*\(\s*\)")
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line) and "Do not pass" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, "Bare ResearchDatabase() found:\n" + "\n".join(offenders)


def test_merge_script_dedupes_natural_keys(tmp_path: Path) -> None:
    from scripts.merge_ghost_research_db import merge_databases

    source = tmp_path / "ghost.db"
    dest = tmp_path / "dest.db"
    src = sqlite3.connect(source)
    src.execute(
        """
        CREATE TABLE shadow_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, strategy TEXT, variant TEXT, side TEXT,
            would_enter INTEGER, reason TEXT, timestamp_ms INTEGER,
            snapshot_json TEXT, ingested_at_ms INTEGER
        )
        """
    )
    src.execute(
        """
        INSERT INTO shadow_decisions
        (symbol, strategy, variant, side, would_enter, reason, timestamp_ms, snapshot_json, ingested_at_ms)
        VALUES ('BTC','VWAPDeviation','phase08_shadow','long',1,'r1',100,NULL,100)
        """
    )
    src.commit()
    src.close()

    dest_conn = sqlite3.connect(dest)
    dest_conn.execute(
        """
        CREATE TABLE shadow_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, strategy TEXT, variant TEXT, side TEXT,
            would_enter INTEGER, reason TEXT, timestamp_ms INTEGER,
            snapshot_json TEXT, ingested_at_ms INTEGER
        )
        """
    )
    dest_conn.execute(
        """
        INSERT INTO shadow_decisions
        (symbol, strategy, variant, side, would_enter, reason, timestamp_ms, snapshot_json, ingested_at_ms)
        VALUES ('BTC','VWAPDeviation','phase08_shadow','long',1,'r1',100,NULL,100)
        """
    )
    dest_conn.commit()
    dest_conn.close()

    stats = merge_databases(source, dest, dry_run=False)
    row = stats["shadow_decisions"]
    assert row["source_rows"] == 1
    assert row["inserted"] == 0
    assert row["dest_after"] == 1


def test_ghost_path_helper_points_at_legacy_relative_default() -> None:
    p = ghost_research_db_path()
    assert p.name == "hyperliquid.db"
    assert "research" in str(p)
