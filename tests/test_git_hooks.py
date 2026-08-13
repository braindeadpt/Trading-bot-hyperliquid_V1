"""Tests for the git hooks: scripts/run_git_hooks.py + scripts/install_git_hooks.py.

Pins the contract:

  * pre-commit fast path only touches STAGED files — syntax check, scoped
    security audit of staged src/ files, and the frozen config_hash — and
    stays green on a clean commit while blocking bad syntax, CRITICAL
    findings, a drifted settings.yaml (exit 1) and a missing frozen manifest
    (exit 2);
  * pre-push delegates to the full run_pre_push_gate.py (exit code passes);
  * the installer is idempotent, never clobbers foreign hooks without
    --force (backing up to .bak), and uninstalls only its own hooks.

The git flow is exercised against a real temp repository (git init) with the
real settings / .env / frozen manifest copied in — the same temp-root pattern
used by the pre-push gate tests.
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
RUNNER = ROOT / "scripts" / "run_git_hooks.py"
INSTALLER = ROOT / "scripts" / "install_git_hooks.py"
REAL_SETTINGS = ROOT / "config" / "settings.yaml"
REAL_ENV = ROOT / ".env"
REAL_MANIFEST = (
    ROOT / "data" / "research" / "phase10" / "phase10_preregister.json"
)

pytestmark = pytest.mark.unit


def _load_hook():
    spec = importlib.util.spec_from_file_location("run_git_hooks", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _FakeResult:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


# ---------------------------------------------------------------------------
# Helpers: a temp git repo with the real config / manifest copied in
# ---------------------------------------------------------------------------

def _make_repo(base: Path, *, drift: bool = False, with_manifest: bool = True) -> Path:
    """Create a fresh temp git repo under *base* (callers pass a unique base
    per repo so several repos can share one tmp_path fixture)."""
    repo = base / "repo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    (repo / "config").mkdir(parents=True)
    text = REAL_SETTINGS.read_text(encoding="utf-8")
    if drift:
        text = re.sub(r"max_positions:\s*\d+", "max_positions: 5", text)
    (repo / "config" / "settings.yaml").write_text(text, encoding="utf-8")
    if REAL_ENV.exists():
        shutil.copy2(REAL_ENV, repo / ".env")
    if with_manifest:
        (repo / "data" / "research" / "phase10").mkdir(parents=True)
        shutil.copy2(
            REAL_MANIFEST,
            repo / "data" / "research" / "phase10" / REAL_MANIFEST.name,
        )
    return repo


def _stage(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=str(repo), check=True)


def _run_hook(repo: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        capture_output=True, text=True, cwd=str(repo),
    )


def _run_installer(repo: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(INSTALLER), *args],
        capture_output=True, text=True, cwd=str(repo),
    )


# ---------------------------------------------------------------------------
# Fast-path pieces (in-process)
# ---------------------------------------------------------------------------

def test_syntax_check_flags_bad_and_passes_good(tmp_path) -> None:
    mod = _load_hook()
    repo = _make_repo(tmp_path)
    (repo / "src" / "bad.py").write_text("def f(:\n", encoding="utf-8")
    (repo / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")

    assert mod._syntax_check(repo, ["src/ok.py"]) == 0
    assert mod._syntax_check(repo, ["src/bad.py"]) == 1
    # missing file (rename side) is skipped, not failed
    assert mod._syntax_check(repo, ["src/gone.py"]) == 0


def test_scoped_audit_blocks_critical_in_staged_file(tmp_path) -> None:
    mod = _load_hook()
    repo = _make_repo(tmp_path)
    (repo / "src" / "evil.py").write_text("x = eval('1+1')\n", encoding="utf-8")
    (repo / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")

    assert mod._scoped_audit(repo, ["src/ok.py"], fail_on_high=False) == 0
    assert mod._scoped_audit(repo, ["src/evil.py"], fail_on_high=False) == 1


def test_config_hash_check_clean_drift_missing(tmp_path) -> None:
    mod = _load_hook()

    clean = _make_repo(tmp_path / "clean", drift=False)
    assert mod._config_hash_check(clean) == 0

    drifted = _make_repo(tmp_path / "drift", drift=True)
    assert mod._config_hash_check(drifted) == 1

    no_manifest = _make_repo(tmp_path / "nomanifest", with_manifest=False)
    assert mod._config_hash_check(no_manifest) == 2


def test_pre_push_delegates_to_full_gate(monkeypatch) -> None:
    mod = _load_hook()
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(" ".join(cmd) if isinstance(cmd, list) else str(cmd))
        return _FakeResult(7)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert mod._pre_push(ROOT) == 7
    assert len(calls) == 1
    assert "run_pre_push_gate.py" in calls[0]


# ---------------------------------------------------------------------------
# pre-commit fast path against a real temp git repo
# ---------------------------------------------------------------------------

def test_pre_commit_clean_staged_file_exits_zero(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    _stage(repo, "src/ok.py", "x = 1\n")
    r = _run_hook(repo, "--hook", "pre-commit")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[PASS] pre-commit fast path OK." in r.stdout


def test_pre_commit_no_staged_files_exits_zero(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    r = _run_hook(repo, "--hook", "pre-commit")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "No staged files" in r.stdout


def test_pre_commit_bad_syntax_exits_one(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    _stage(repo, "src/bad.py", "def f(:\n")
    r = _run_hook(repo, "--hook", "pre-commit")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "src/bad.py" in r.stdout
    assert "[BLOCKED] syntax errors" in r.stdout


def test_pre_commit_critical_finding_exits_one(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    _stage(repo, "src/evil.py", "x = eval('1+1')\n")
    r = _run_hook(repo, "--hook", "pre-commit")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "AUDIT-001" in r.stdout


def test_pre_commit_config_drift_exits_one(tmp_path) -> None:
    repo = _make_repo(tmp_path, drift=True)
    _stage(repo, "src/ok.py", "x = 1\n")
    r = _run_hook(repo, "--hook", "pre-commit")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "config_hash drifted" in r.stdout


def test_pre_commit_missing_manifest_exits_two(tmp_path) -> None:
    repo = _make_repo(tmp_path, with_manifest=False)
    _stage(repo, "src/ok.py", "x = 1\n")
    r = _run_hook(repo, "--hook", "pre-commit")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "frozen Fase 10 manifest missing" in r.stdout


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------

def test_install_creates_both_hooks(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    r = _run_installer(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    for name in ("pre-commit", "pre-push"):
        hook = repo / ".git" / "hooks" / name
        assert hook.exists()
        body = hook.read_text(encoding="utf-8")
        assert "Managed by scripts/install_git_hooks.py" in body
        assert "run_git_hooks.py" in body
        assert name in body  # the --hook argument matches the file name


def test_install_is_idempotent(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    assert _run_installer(repo).returncode == 0
    first = (repo / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    r2 = _run_installer(repo)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    second = (repo / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert first == second


def test_install_refuses_foreign_hook_without_force(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    foreign = repo / ".git" / "hooks" / "pre-commit"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("#!/bin/sh\necho my own hook\n", encoding="utf-8")

    r = _run_installer(repo)
    assert r.returncode == 1
    assert "foreign hook" in r.stdout
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\necho my own hook\n"


def test_install_force_backs_up_foreign_hook(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    foreign = repo / ".git" / "hooks" / "pre-commit"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("#!/bin/sh\necho my own hook\n", encoding="utf-8")

    r = _run_installer(repo, "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (repo / ".git" / "hooks" / "pre-commit.bak").exists()
    body = foreign.read_text(encoding="utf-8")
    assert "Managed by scripts/install_git_hooks.py" in body


def test_uninstall_removes_managed_only(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    assert _run_installer(repo).returncode == 0
    foreign = repo / ".git" / "hooks" / "pre-commit"
    foreign.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")

    r = _run_installer(repo, "--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    # pre-push was managed -> removed; pre-commit is now foreign -> left alone
    assert not (repo / ".git" / "hooks" / "pre-push").exists()
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\necho foreign\n"


def test_list_reports_states(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    r = _run_installer(repo, "--list")
    assert "not installed" in r.stdout
    assert _run_installer(repo).returncode == 0
    r2 = _run_installer(repo, "--list")
    assert "managed hook installed" in r2.stdout
