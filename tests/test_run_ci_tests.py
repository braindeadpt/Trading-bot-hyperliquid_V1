"""Tests for the trio CI runner (scripts/run_ci_tests.py).

Pins the contract that one command now runs the full pre-push trio — pytest
battery, security audit, config_hash vs the Fase 10 frozen manifest — in
order, short-circuiting on the first failure, honouring --skip-audit /
--skip-hash, and returning **exit 1** when a drifted settings.yaml makes the
hash stage fail (the same signal the pre-push gate would block on).

The drift is simulated without touching the repo's real config/settings.yaml:
a temp deployment root holds copies of the real settings (+ .env and the
frozen manifest), the copy is drifted, and the runner's real hash subprocess
runs against that temp root. A clean copy is the control (exit 0).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_ci_tests.py"
REAL_SETTINGS = ROOT / "config" / "settings.yaml"
REAL_ENV = ROOT / ".env"
REAL_MANIFEST = (
    ROOT / "data" / "research" / "phase10" / "phase10_preregister.json"
)

pytestmark = pytest.mark.unit


def _load_runner():
    """Import scripts/run_ci_tests.py by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("run_ci_tests", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _FakeResult:
    """Minimal subprocess.CompletedProcess stand-in for stage short-circuits."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _stage_of(cmd) -> str:
    """Classify a runner subprocess command by its stage."""
    joined = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    if "pytest" in joined and "-m" in joined:
        return "pytest"
    if "security.audit" in joined:
        return "audit"
    if "-c" in joined and "compute_config_hash" in joined:
        return "hash"
    return "other"


# ---------------------------------------------------------------------------
# 1. Compiles
# ---------------------------------------------------------------------------

def test_script_compiles() -> None:
    r = subprocess.run(
        [sys.executable, "-m", "py_compile", str(SCRIPT)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# 2. The trio runs in order
# ---------------------------------------------------------------------------

def test_trio_runs_in_order(monkeypatch, capsys) -> None:
    """No flags => pytest, audit, hash run in that order and the trio passes."""
    runner = _load_runner()
    calls: list = []

    def fake_run(cmd, **kwargs):
        joined = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        calls.append((_stage_of(cmd), joined))
        return _FakeResult(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_ci_tests.py"])

    assert runner.main() == 0

    assert [stage for stage, _ in calls] == ["pytest", "audit", "hash"], calls
    # The audit stage always enforces the accepted-HIGH baseline (by rule/file).
    assert "--enforce-baseline" in calls[1][1], calls[1]
    out = capsys.readouterr().out
    assert "All CI tests passed." in out
    assert "[PASS] Security audit PASSED" in out
    assert "[PASS] config_hash matches the frozen Fase 10 manifest" in out
    assert "[PASS] CI + security audit + config_hash - all green." in out


def test_audit_failure_short_circuits_before_hash(monkeypatch, capsys) -> None:
    """A failing audit (e.g. a new HIGH beyond the baseline) stops the runner
    with the audit exit code — the hash stage never runs."""
    runner = _load_runner()
    calls: list = []

    def fake_run(cmd, **kwargs):
        stage = _stage_of(cmd)
        calls.append(stage)
        return _FakeResult(1 if stage == "audit" else 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_ci_tests.py"])

    assert runner.main() == 1
    assert calls == ["pytest", "audit"], calls
    out = capsys.readouterr().out
    assert "[FAIL] Security audit FAILED" in out


def test_pytest_failure_short_circuits(monkeypatch) -> None:
    """A failing pytest battery stops the runner before audit/hash (exit = rc)."""
    runner = _load_runner()
    calls: list = []

    def fake_run(cmd, **kwargs):
        stage = _stage_of(cmd)
        calls.append(stage)
        return _FakeResult(3 if stage == "pytest" else 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_ci_tests.py"])

    assert runner.main() == 3
    assert calls == ["pytest"], calls


def test_skip_flags_remove_stages(monkeypatch) -> None:
    """--skip-audit --skip-hash leaves only the pytest stage (gate uses this)."""
    runner = _load_runner()
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(_stage_of(cmd))
        return _FakeResult(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys, "argv", ["run_ci_tests.py", "--skip-audit", "--skip-hash"],
    )

    assert runner.main() == 0
    assert calls == ["pytest"], calls


# ---------------------------------------------------------------------------
# 3. A drifted settings.yaml makes the runner exit 1
# ---------------------------------------------------------------------------

def _build_temp_root(tmp_path: Path, *, drift: bool) -> Path:
    """Temp deployment root with copies of the real settings / .env / manifest.

    Both load_config() and load_preregister_manifest() resolve relative to
    the subprocess cwd, so a temp root holding the real frozen manifest plus
    a (possibly drifted) copy of settings.yaml reproduces the repo's hash
    check without touching the repo files. Drift flips risk.max_positions
    3 -> 5, which alters the effective config hash.
    """
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "research" / "phase10").mkdir(parents=True, exist_ok=True)

    text = REAL_SETTINGS.read_text(encoding="utf-8")
    if drift:
        text = re.sub(r"max_positions:\s*\d+", "max_positions: 5", text)
    (tmp_path / "config" / "settings.yaml").write_text(text, encoding="utf-8")

    if REAL_ENV.exists():
        shutil.copy2(REAL_ENV, tmp_path / ".env")
    shutil.copy2(
        REAL_MANIFEST, tmp_path / "data" / "research" / "phase10" / REAL_MANIFEST.name,
    )
    return tmp_path


def _run_with_temp_root(tmp_path: Path, monkeypatch, capsys, runner):
    """Run the real runner, short-circuiting pytest/audit and pointing the
    real hash subprocess at *tmp_path* (so settings + manifest resolve there)."""
    real_run = subprocess.run
    calls: list = []

    def fake_run(cmd, **kwargs):
        stage = _stage_of(cmd)
        calls.append(stage)
        if stage in ("pytest", "audit"):
            return _FakeResult(0)
        if stage == "hash":
            # The REAL python -c snippet, with cwd redirected to the temp root.
            return real_run(cmd, cwd=str(tmp_path),
                            **{k: v for k, v in kwargs.items() if k != "cwd"})
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_ci_tests.py"])
    rc = runner.main()
    return rc, calls, capsys.readouterr().out


def test_drifted_settings_yaml_exits_1(tmp_path, monkeypatch, capsys) -> None:
    """A parameter drift in settings.yaml => the hash stage fails => exit 1."""
    runner = _load_runner()
    root = _build_temp_root(tmp_path, drift=True)

    rc, calls, out = _run_with_temp_root(root, monkeypatch, capsys, runner)

    assert calls == ["pytest", "audit", "hash"], calls
    assert rc == 1, out
    assert "[FAIL] config_hash check FAILED" in out


def test_clean_settings_yaml_exits_0(tmp_path, monkeypatch, capsys) -> None:
    """Control: the same harness with an un-drifted settings copy => exit 0.

    Proves the drift test above is a real signal (the temp root faithfully
    reproduces the repo's frozen state), not a harness bug.
    """
    runner = _load_runner()
    root = _build_temp_root(tmp_path, drift=False)

    rc, calls, out = _run_with_temp_root(root, monkeypatch, capsys, runner)

    assert calls == ["pytest", "audit", "hash"], calls
    assert rc == 0, out
    assert "[PASS] config_hash matches the frozen Fase 10 manifest" in out
    assert "[PASS] CI + security audit + config_hash - all green." in out
