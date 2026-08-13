"""Tests for the pre-push gate script (scripts/run_pre_push_gate.py).

Pins the gate contract documented in the script docstring:

  * the script compiles,
  * ``main()`` drives exactly three stages in order — CI battery, security
    audit, config_hash vs the Fase 10 frozen manifest — returns 0 when all
    pass, short-circuits on failure, and honours the ``--skip-*`` flags,
  * a simulated drift in ``settings.yaml`` (a parameter change that alters
    the effective config hash) makes the gate return **exit 1**, exactly as
    it would block a commit/push.

The drift is simulated without touching the repo's real
``config/settings.yaml``: a temp deployment root receives a copy of the real
settings (+ ``.env`` and the frozen manifest), the copy is drifted, and the
gate's real hash subprocess (``_HASH_CHECK_SNIPPET``) runs against that temp
root. A clean copy is the control — it must pass, proving the harness isn't
a false positive.
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
SCRIPT = ROOT / "scripts" / "run_pre_push_gate.py"
REAL_SETTINGS = ROOT / "config" / "settings.yaml"
REAL_ENV = ROOT / ".env"
REAL_MANIFEST = (
    ROOT / "data" / "research" / "phase10" / "phase10_preregister.json"
)

pytestmark = pytest.mark.unit


def _load_gate():
    """Import scripts/run_pre_push_gate.py by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("run_pre_push_gate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _FakeResult:
    """Minimal subprocess.CompletedProcess stand-in for stage short-circuits."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _stage_of(cmd) -> str:
    """Classify a gate subprocess command by its stage."""
    joined = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    if "run_ci_tests.py" in joined:
        return "ci"
    if "security.audit" in joined:
        return "audit"
    if "-c" in joined and "compute_config_hash" in joined:
        return "hash"
    return "other"


# ---------------------------------------------------------------------------
# 1. The script compiles
# ---------------------------------------------------------------------------

def test_script_compiles() -> None:
    r = subprocess.run(
        [sys.executable, "-m", "py_compile", str(SCRIPT)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# 2. The three stages
# ---------------------------------------------------------------------------

def test_main_drives_three_stages_in_order(monkeypatch, capsys) -> None:
    """No flags => CI, audit, hash run in that order and the gate passes."""
    gate = _load_gate()
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd) if isinstance(cmd, list) else [str(cmd)])
        return _FakeResult(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_pre_push_gate.py"])

    assert gate.main() == 0

    assert [_stage_of(c) for c in calls] == ["ci", "audit", "hash"], calls
    out = capsys.readouterr().out
    assert "[PASS] CI battery (pytest) PASSED" in out
    assert "[PASS] Security audit PASSED" in out
    assert "[PASS] config_hash matches the frozen Fase 10 manifest" in out
    assert "[PASS] PRE-PUSH GATE PASSED" in out


def test_ci_failure_short_circuits_before_audit_and_hash(monkeypatch, capsys) -> None:
    """A failing stage stops the gate early with its exit code (no audit/hash)."""
    gate = _load_gate()
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(_stage_of(cmd))
        rc = 3 if _stage_of(cmd) == "ci" else 0
        return _FakeResult(rc)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_pre_push_gate.py"])

    assert gate.main() == 3
    assert calls == ["ci"], calls
    out = capsys.readouterr().out
    assert "[BLOCKED] GATE BLOCKED: CI failures" in out


def test_skip_flags_remove_stages(monkeypatch) -> None:
    """--skip-audit --skip-hash leaves only the CI stage (default flags path)."""
    gate = _load_gate()
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(_stage_of(cmd))
        return _FakeResult(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys, "argv", ["run_pre_push_gate.py", "--skip-audit", "--skip-hash"],
    )

    assert gate.main() == 0
    assert calls == ["ci"], calls


# ---------------------------------------------------------------------------
# 3. A drifted settings.yaml makes the gate exit 1
# ---------------------------------------------------------------------------

def _build_temp_root(tmp_path: Path, *, drift: bool) -> Path:
    """Temp deployment root with copies of the real settings / .env / manifest.

    ``load_config()`` and ``load_preregister_manifest()`` both resolve
    relative to the subprocess cwd, so a temp root holding the real frozen
    manifest plus a (possibly drifted) copy of settings.yaml reproduces the
    repo's hash check without touching the repo files. When ``drift`` is set,
    ``risk.max_positions`` is flipped 3 -> 5, which alters the effective hash.
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


def _run_gate_with_temp_root(tmp_path: Path, monkeypatch, capsys, gate):
    """Run the real gate, short-circuiting CI/audit and pointing the real
    hash subprocess at *tmp_path* (so settings + manifest resolve there)."""
    real_run = subprocess.run
    calls: list = []

    def fake_run(cmd, **kwargs):
        stage = _stage_of(cmd)
        calls.append(stage)
        if stage in ("ci", "audit"):
            return _FakeResult(0)
        if stage == "hash":
            # The REAL python -c snippet, with cwd redirected to the temp root.
            return real_run(cmd, cwd=str(tmp_path),
                            **{k: v for k, v in kwargs.items() if k != "cwd"})
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_pre_push_gate.py"])
    rc = gate.main()
    return rc, calls, capsys.readouterr().out


def test_drifted_settings_yaml_makes_gate_exit_1(tmp_path, monkeypatch, capsys) -> None:
    """A parameter drift in settings.yaml => the hash stage fails => exit 1."""
    gate = _load_gate()
    root = _build_temp_root(tmp_path, drift=True)

    rc, calls, out = _run_gate_with_temp_root(root, monkeypatch, capsys, gate)

    assert calls == ["ci", "audit", "hash"], calls
    assert rc == 1, out
    assert "[FAIL] config_hash check FAILED" in out
    assert "GATE BLOCKED" in out
    assert "[PASS] PRE-PUSH GATE PASSED" not in out


def test_clean_settings_yaml_passes_hash_stage(tmp_path, monkeypatch, capsys) -> None:
    """Control: the same harness with an un-drifted settings copy => exit 0.

    Proves the drift test above is a real signal (the temp root itself is a
    faithful reproduction of the repo's frozen state), not a harness bug.
    """
    gate = _load_gate()
    root = _build_temp_root(tmp_path, drift=False)

    rc, calls, out = _run_gate_with_temp_root(root, monkeypatch, capsys, gate)

    assert calls == ["ci", "audit", "hash"], calls
    assert rc == 0, out
    assert "[PASS] config_hash matches the frozen Fase 10 manifest" in out
    assert "[PASS] PRE-PUSH GATE PASSED" in out


def test_missing_manifest_exits_two(tmp_path, monkeypatch, capsys) -> None:
    """Contract pin: a missing frozen manifest => exit 2 (unreachable stage)."""
    gate = _load_gate()
    root = _build_temp_root(tmp_path, drift=False)
    # Remove the manifest from the temp root — the hash stage then sees None.
    manifest = root / "data" / "research" / "phase10" / REAL_MANIFEST.name
    manifest.unlink()

    rc, calls, out = _run_gate_with_temp_root(root, monkeypatch, capsys, gate)

    assert calls == ["ci", "audit", "hash"], calls
    assert rc == 2, out
    assert "[FAIL] config_hash check FAILED" in out
