"""JevJudge EXPERIMENT guardrails — audit-fix regression tests (2026-09-17).

Covers the three fail-closed defects found in the EXPERIMENT promotion audit:

1. ``assert_experiment_paper_only`` — an EXPERIMENT-gated execution strategy
   may boot ONLY with ``mode == 'paper'`` AND ``phase08.paper_only`` true.
   ``paper_only`` is an additional requirement, never a bypass, and an
   unreadable config must raise (fail closed), not assume paper.
2. ``JevJudge._mode`` — the strategy is disabled unless the factory injects
   an explicit ``_mode='paper'``; the effective process mode always wins
   over a hand-written ``_mode`` in the YAML section.
3. ``jev_verdicts`` feed contract — the verdict file's freshness is a
   contracted FeedSilenceMonitor feed while JevJudge executes; stale file
   alerts once (fire-once suppression), absent contract stays silent.
"""

from __future__ import annotations

import json
import time

import pytest

from src.data.market_data_health import FeedSilenceMonitor
from src.core.engine import TradingEngine, feed_silence_contracts
from src.research.phase08_preregister import (
    PreregisterManifestError,
    assert_experiment_paper_only,
)
from src.strategies.factory import _instantiate_from_registry
from src.strategies.jev_judge import JevJudge
from src.utils.config import Config


# ---------------------------------------------------------------------------
# 1. assert_experiment_paper_only — full mode x paper_only matrix
# ---------------------------------------------------------------------------

def _experiment_manifest() -> dict:
    return {
        "execution_scope": {"strategies": ["JevJudge"]},
        "baseline_signal_gate": [
            {"strategy": "JevJudge", "verdict": "EXPERIMENT"},
        ],
    }


def _cfg(mode: str, paper_only: bool) -> Config:
    return Config({
        "mode": mode,
        "strategy": {
            "phase08": {
                "paper_only": paper_only,
                "execution_strategies": ["JevJudge"],
            },
        },
    })


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["paper", "testnet", "mainnet"])
@pytest.mark.parametrize("paper_only", [True, False])
def test_experiment_paper_only_matrix(mode: str, paper_only: bool) -> None:
    """Only mode='paper' AND paper_only=True may run an EXPERIMENT strategy."""
    if mode == "paper" and paper_only:
        assert_experiment_paper_only(_experiment_manifest(), _cfg(mode, paper_only))
    else:
        with pytest.raises(PreregisterManifestError):
            assert_experiment_paper_only(
                _experiment_manifest(), _cfg(mode, paper_only)
            )


@pytest.mark.unit
def test_experiment_paper_only_missing_mode_fails_closed() -> None:
    """No mode key -> mode is not 'paper' -> refuse."""
    cfg = Config({
        "strategy": {
            "phase08": {
                "paper_only": True,
                "execution_strategies": ["JevJudge"],
            },
        },
    })
    with pytest.raises(PreregisterManifestError):
        assert_experiment_paper_only(_experiment_manifest(), cfg)


@pytest.mark.unit
def test_experiment_paper_only_unreadable_config_fails_closed() -> None:
    """A config whose .get raises must raise PreregisterManifestError —
    a safety guard that cannot read the mode never assumes paper."""

    class _BrokenConfig:
        def get(self, *args, **kwargs):  # noqa: ANN001
            raise TypeError("unreadable config")

    with pytest.raises(PreregisterManifestError, match="unreadable"):
        assert_experiment_paper_only(_experiment_manifest(), _BrokenConfig())


@pytest.mark.unit
def test_experiment_guard_noop_without_experiment_verdict() -> None:
    """No EXPERIMENT gate entries -> nothing to guard, even on mainnet."""
    manifest = {
        "execution_scope": {"strategies": ["VWAPDeviation"]},
        "baseline_signal_gate": [
            {"strategy": "VWAPDeviation", "verdict": "PASS"},
        ],
    }
    assert_experiment_paper_only(manifest, _cfg("mainnet", False))


# ---------------------------------------------------------------------------
# 2. JevJudge._mode — fail-closed default + factory precedence
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_jev_judge_missing_mode_disabled() -> None:
    """No _mode injected -> disabled (fail closed, never assume paper)."""
    s = JevJudge({})
    assert s._enabled is False


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["mainnet", "testnet", "live", "PAPER2", ""])
def test_jev_judge_non_paper_mode_disabled(mode: str) -> None:
    s = JevJudge({"_mode": mode})
    assert s._enabled is False


@pytest.mark.unit
def test_jev_judge_paper_mode_enabled() -> None:
    s = JevJudge({"_mode": "paper"})
    assert s._enabled is True


@pytest.mark.unit
def test_factory_process_mode_overrides_yaml_mode() -> None:
    """A hand-written `_mode: paper` in strategy.jev_judge YAML must NOT
    survive a process running in mainnet — the effective mode wins."""
    cfg = Config({
        "mode": "mainnet",
        "strategy": {
            "jev_judge": {
                "enabled": True,
                "_mode": "paper",  # attacker/stale YAML value — must lose
            },
        },
    })
    inst = _instantiate_from_registry(cfg, "strategy.jev_judge", JevJudge)
    assert inst is not None
    assert inst._mode == "mainnet"
    assert inst._enabled is False


# ---------------------------------------------------------------------------
# 3. jev_verdicts feed contract (FeedSilenceMonitor)
# ---------------------------------------------------------------------------

def _contract_cfg(with_jev: bool) -> Config:
    return Config({
        "mode": "paper",
        "strategy": {
            "phase08": {
                "execution_strategies": ["JevJudge"] if with_jev else [],
            },
            "jev_judge": {"decision_ttl_ms": 90 * 60_000},
        },
        "market_data": {"feed_silence": {}},
    })


@pytest.mark.unit
def test_jev_verdicts_contracted_only_when_executing() -> None:
    feeds = feed_silence_contracts(_contract_cfg(with_jev=True))
    assert "jev_verdicts" in feeds
    # Threshold tracks the strategy's own decision TTL (90 min default).
    assert feeds["jev_verdicts"] == pytest.approx(5400.0)

    feeds_off = feed_silence_contracts(_contract_cfg(with_jev=False))
    assert "jev_verdicts" not in feeds_off


@pytest.mark.unit
def test_jev_verdicts_fresh_file_no_alert() -> None:
    mon = FeedSilenceMonitor(feeds={"jev_verdicts": 5400.0})
    now = int(time.time() * 1000)
    mon.beat("jev_verdicts", now - 60_000)  # verdict 1 min old
    assert mon.check(now_ms=now) == []


@pytest.mark.unit
def test_jev_verdicts_stale_alerts_once() -> None:
    mon = FeedSilenceMonitor(feeds={"jev_verdicts": 5400.0})
    now = int(time.time() * 1000)
    stale = now - 100 * 60_000  # 100 min old — beyond the 90-min TTL
    mon.beat("jev_verdicts", stale)
    alerts = mon.check(now_ms=now)
    assert len(alerts) == 1
    assert "jev_verdicts" in alerts[0]
    # Fire-once suppression: a second check in the same episode stays quiet.
    assert mon.check(now_ms=now + 60_000) == []


@pytest.mark.unit
def test_jev_verdict_feed_beat_uses_newest_file_ts(tmp_path, monkeypatch) -> None:
    """Engine beat helper: fresh verdict file beats the feed; a stale file
    must NOT beat (its age is the signal)."""
    verdicts = tmp_path / "jev_latest.json"
    now = int(time.time() * 1000)
    verdicts.write_text(json.dumps({
        "BTC": {"ts_ms": now - 5 * 60_000, "action": "long"},
        "ETH": {"ts_ms": now - 10 * 60_000, "action": "short"},
    }))

    mon = FeedSilenceMonitor(feeds={"jev_verdicts": 5400.0})

    class _StubEngine:
        _feed_silence = mon
        _config = Config({
            "database": {"path": str(tmp_path / "bot.db")},
        })

    TradingEngine._beat_jev_verdict_feed(_StubEngine())
    assert mon.check(now_ms=now) == []  # fresh verdicts beat -> no alert

    # Stale file: newest ts beyond TTL -> feed degrades and alerts once.
    verdicts.write_text(json.dumps({
        "BTC": {"ts_ms": now - 200 * 60_000, "action": "long"},
    }))
    mon2 = FeedSilenceMonitor(feeds={"jev_verdicts": 5400.0})
    _StubEngine._feed_silence = mon2
    TradingEngine._beat_jev_verdict_feed(_StubEngine())
    alerts = mon2.check(now_ms=now)
    assert len(alerts) == 1 and "jev_verdicts" in alerts[0]


@pytest.mark.unit
def test_jev_verdict_beat_noop_when_not_contracted(tmp_path) -> None:
    """JevJudge not in execution -> feed not enabled -> helper no-ops."""
    mon = FeedSilenceMonitor(feeds={"jev_verdicts": 5400.0})
    mon.disable_feed("jev_verdicts")  # simulates uncontracted deployment

    verdicts = tmp_path / "jev_latest.json"
    verdicts.write_text(json.dumps({"BTC": {"ts_ms": 1, "action": "long"}}))

    class _StubEngine:
        _feed_silence = mon
        _config = Config({"database": {"path": str(tmp_path / "bot.db")}})

    TradingEngine._beat_jev_verdict_feed(_StubEngine())
    now = int(time.time() * 1000)
    # Feed disabled -> check() never iterates it, no alerts possible.
    assert mon.check(now_ms=now) == []
