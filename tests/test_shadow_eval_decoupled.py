"""Decoupled shadow-outcome evaluation — contract tests.

The heavy 14d evaluator moved out of the bot/dashboard process: a scheduled
``evaluate_shadow_outcomes.py --persist`` run writes ``shadow_outcome_scoreboards``
and the dashboard reads only the newest batch, read-only. These tests pin:

* persisted boards are field-identical to the in-process evaluation output,
* an empty/missing batch surfaces "evaluation pending" (never falls back to
  in-process ``run_evaluation``),
* the newest ``evaluated_at_ms`` batch wins,
* the CLI lock blocks overlapping runs,
* age metadata is exposed for staleness display,
* the read path cannot write,
* variants (``phase08_shadow`` vs ``router_blocked``) keep distinct keys.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.research.shadow_panel as shadow_panel
from src.data.research_database import ResearchDatabase
from src.research.shadow_outcome_evaluator import (
    VARIANT_PHASE08_SHADOW,
    VARIANT_ROUTER_BLOCKED,
    StrategyScoreboard,
    persist_scoreboards,
    run_evaluation,
    scoreboard_key,
)
from src.research.shadow_panel import (
    _load_persisted_boards,
    build_shadow_panel_payload,
)
from src.research.shadow_recorder import (
    ShadowDecision,
    ShadowRecorder,
    build_enriched_market_snapshot,
)
from src.utils.config import Config

pytestmark = [pytest.mark.unit, pytest.mark.integration_offline]

CANDLE_SRC = "hl_ws_1m_tape_agg"


def _cfg(db_path: Path, *, max_hold_s: int = 180) -> Config:
    return Config(
        {
            "research": {"database": {"path": str(db_path)}},
            "strategy": {"checklist_meta": {"max_hold_seconds": max_hold_s}},
        }
    )


def _seed_candles(db: ResearchDatabase, symbol: str, entry_ts: int, n: int = 6) -> None:
    """1m candles after entry_ts; first one touches TP (close > 102)."""
    conn = db._conn()
    cols = (
        "symbol, timestamp_ms, open, high, low, close, volume, "
        "funding_rate, oi_total, oi_delta, buy_volume, sell_volume, "
        "trade_count, source"
    )
    for i in range(n):
        ts = entry_ts + (i + 1) * 60_000
        # TP at 102 hit on the first candle for a 100/1%/2% long
        o, h, l, c = (100.0, 103.0, 99.9, 102.5) if i == 0 else (102.5, 102.8, 102.0, 102.4)
        conn.execute(
            f"INSERT OR REPLACE INTO candles_1m ({cols}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?)",
            (symbol, ts, o, h, l, c, 1.0, CANDLE_SRC),
        )
    conn.commit()


def _decision(
    *,
    strategy: str = "ChecklistMeta",
    variant: str = VARIANT_PHASE08_SHADOW,
    side: str = "long",
    ts: int,
    would_enter: bool = True,
) -> ShadowDecision:
    return ShadowDecision(
        symbol="BTC",
        strategy=strategy,
        variant=variant,
        side=side,
        would_enter=would_enter,
        reason="entry_signal",
        timestamp_ms=ts,
        market_snapshot=build_enriched_market_snapshot(
            price=100.0,
            confidence=0.7,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            size_pct=0.01,
            metadata={"unit": "test"},
        ),
    )


# ── 1. Fixed-window equivalence: in-process eval vs persisted read ──────────

def test_persisted_boards_identical_to_in_process_run(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    cfg = _cfg(db_path)
    db = ResearchDatabase(db_path)
    rec = ShadowRecorder(db)
    entry_ts = int(time.time() * 1000) - 3600_000  # inside any window

    rec.record(_decision(ts=entry_ts))
    rec.record(
        _decision(
            ts=entry_ts + 1,
            variant=VARIANT_ROUTER_BLOCKED,
        )
    )
    rec.record(
        _decision(ts=entry_ts + 2, side="short", would_enter=False)
    )
    _seed_candles(db, "BTC", entry_ts)

    # Path A — direct in-process evaluation (what the old dashboard did).
    direct = run_evaluation(
        research_db_path=db_path,
        live_db_path=None,
        config=cfg,
        persist=False,
    )
    assert direct["n_decisions_loaded"] == 2  # would_enter_only

    # Path B — the new path: same evaluation persisted, then read back.
    persisted = run_evaluation(
        research_db_path=db_path,
        live_db_path=None,
        config=cfg,
        persist=True,
    )
    assert persisted["persisted"] is True

    latest = _load_persisted_boards(cfg)
    assert latest is not None
    boards, evaluated_at_ms = latest
    assert evaluated_at_ms > 0
    # Field-identical per strategy — the read path adds/removes nothing.
    assert boards == direct["strategies"]
    # Both variants present, keyed consistently.
    assert scoreboard_key("ChecklistMeta", VARIANT_PHASE08_SHADOW) in boards
    assert scoreboard_key("ChecklistMeta", VARIANT_ROUTER_BLOCKED) in boards
    shadow = boards[scoreboard_key("ChecklistMeta", VARIANT_PHASE08_SHADOW)]
    assert shadow["n_evaluated"] == 1
    assert shadow["wins"] == 1
    db.close()


# ── 2. Empty / missing batch → explicit pending, never in-process fallback ──

def test_empty_scoreboard_table_returns_pending(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    cfg = _cfg(db_path)
    payload = build_shadow_panel_payload(
        shadow_names=["ChecklistMeta"],
        config=cfg,
        evaluate=True,
    )
    assert payload["evaluation_pending"] is True
    assert payload["evaluated_at_ms"] is None
    assert payload["eval_age_minutes"] is None
    # The heavy evaluator must not even be importable from the panel module.
    assert not hasattr(shadow_panel, "run_evaluation")
    assert not hasattr(shadow_panel, "evaluate_shadow_decisions")


def test_missing_research_db_file_returns_pending(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "does_not_exist" / "research.db")
    assert _load_persisted_boards(cfg) is None


# ── 3. Newest batch wins; batches never mix ──────────────────────────────────

def test_latest_batch_selected_not_mixed(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    cfg = _cfg(db_path)
    db = ResearchDatabase(db_path)
    t_old, t_new = 1_000_000, 2_000_000

    old_board = StrategyScoreboard(
        strategy="OldStrat",
        variant=VARIANT_PHASE08_SHADOW,
        n_evaluated=7,
        candle_source="test",
    )
    new_board = StrategyScoreboard(
        strategy="NewStrat",
        variant=VARIANT_PHASE08_SHADOW,
        n_evaluated=3,
        candle_source="test",
    )
    persist_scoreboards(
        {old_board.key: old_board}, db=db, evaluated_at_ms=t_old
    )
    persist_scoreboards(
        {new_board.key: new_board}, db=db, evaluated_at_ms=t_new
    )

    latest = _load_persisted_boards(cfg)
    assert latest is not None
    boards, ts = latest
    assert ts == t_new
    assert set(boards) == {scoreboard_key("NewStrat", VARIANT_PHASE08_SHADOW)}
    db.close()


# ── 4. CLI lock blocks overlap; release clears it ───────────────────────────

def test_lock_blocks_second_run_and_release_clears(tmp_path: Path, monkeypatch) -> None:
    import scripts.research.evaluate_shadow_outcomes as cli

    lock = tmp_path / ".shadow_eval.lock"
    monkeypatch.setattr(cli, "LOCK_PATH", lock)

    # Foreign live pid holds the lock → acquisition refused.
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    try:
        lock.write_text(str(holder.pid), encoding="utf-8")
        assert cli._lock_acquired() is False
    finally:
        holder.kill()
        holder.wait()

    # After the holder exits cleanly the lock file is gone (release path);
    # our own acquisition then works and release removes the file.
    lock.unlink()
    assert cli._lock_acquired() is True
    assert lock.read_text(encoding="utf-8") == str(os.getpid())
    cli._lock_release()
    assert not lock.exists()


# ── 5. Age metadata surfaces in the payload ──────────────────────────────────

def test_eval_age_metadata_present(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    cfg = _cfg(db_path)
    db = ResearchDatabase(db_path)
    age_ms = 30 * 60_000
    ts = int(time.time() * 1000) - age_ms
    board = StrategyScoreboard(
        strategy="ChecklistMeta",
        variant=VARIANT_PHASE08_SHADOW,
        n_evaluated=5,
        candle_source="test",
    )
    persist_scoreboards({board.key: board}, db=db, evaluated_at_ms=ts)
    db.close()

    payload = build_shadow_panel_payload(
        shadow_names=["ChecklistMeta"],
        config=cfg,
        evaluate=True,
    )
    assert payload["evaluation_pending"] is False
    assert payload["evaluated_at_ms"] == ts
    assert payload["eval_age_minutes"] == pytest.approx(30.0, abs=0.2)
    row = next(r for r in payload["rows"] if r["strategy"] == "ChecklistMeta")
    assert row["hypothetical_trades_closed"] == 5


# ── 6. Read path is genuinely read-only ──────────────────────────────────────

def test_scoreboard_read_cannot_write(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    cfg = _cfg(db_path)
    db = ResearchDatabase(db_path)
    board = StrategyScoreboard(
        strategy="ChecklistMeta",
        variant=VARIANT_PHASE08_SHADOW,
        n_evaluated=1,
        candle_source="test",
    )
    persist_scoreboards({board.key: board}, db=db, evaluated_at_ms=123)
    db.close()

    assert _load_persisted_boards(cfg) is not None
    ro = ResearchDatabase(db_path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        ro._conn().execute("CREATE TABLE write_attempt (x INTEGER)")
    ro.close()


# ── 7. Variant rows never collapse into one key ──────────────────────────────

def test_variants_persisted_as_distinct_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    cfg = _cfg(db_path)
    db = ResearchDatabase(db_path)
    ts = 5_000_000
    b1 = StrategyScoreboard(
        strategy="ChecklistMeta",
        variant=VARIANT_PHASE08_SHADOW,
        n_evaluated=2,
        candle_source="test",
    )
    b2 = StrategyScoreboard(
        strategy="ChecklistMeta",
        variant=VARIANT_ROUTER_BLOCKED,
        n_evaluated=4,
        candle_source="test",
    )
    persist_scoreboards(
        {b1.key: b1, b2.key: b2}, db=db, evaluated_at_ms=ts
    )
    latest = _load_persisted_boards(cfg)
    assert latest is not None
    boards, got_ts = latest
    assert got_ts == ts
    assert boards[b1.key]["n_evaluated"] == 2
    assert boards[b2.key]["n_evaluated"] == 4
    assert boards[b2.key]["variant"] == VARIANT_ROUTER_BLOCKED
    db.close()
