"""Tests for the AUDIT-005 remediation.

Two subprocess call sites were flagged HIGH by the security audit:

  * ``run_manifest.get_git_commit`` — REMEDIATED: now reads ``.git/HEAD``
    (and the ref it points to) directly, no subprocess, same best-effort
    ``"unknown"`` contract.
  * ``crash_recovery.CrashRecovery._run_once`` — ACCEPTED + hardened: the
    subprocess is the module's core function (spawning the bot), but the
    command is allowlist-validated by ``_validate_cmd`` before execution.

These tests pin both: the pure-file git read works in a repo checkout and
never raises, and the command validation refuses everything except the
known interpreter + ``main.py`` + a known ``--mode``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.run_manifest import get_git_commit  # noqa: E402
from src.utils.crash_recovery import CrashRecovery  # noqa: E402

pytestmark = pytest.mark.unit


# -- get_git_commit (remediated: no subprocess) ---------------------------


def test_get_git_commit_reads_head_without_subprocess() -> None:
    # Verify the remediation: no subprocess *call* remains (the word appears
    # only in the docstring explaining the remediation).
    src = Path(__file__).resolve().parents[1] / "src" / "backtest" / "run_manifest.py"
    text = src.read_text(encoding="utf-8")
    assert "import subprocess" not in text
    # Same shape as the auditor's AUDIT-005 regex: a real call site.
    import re

    assert not re.search(
        r"subprocess\.(?:call|run|Popen|check_output|check_call)\s*\(", text
    ), "run_manifest.py must not call subprocess"

    commit = get_git_commit()
    # Running inside a git checkout, HEAD resolves to a 7-char short hash.
    assert commit == "unknown" or len(commit) == 7


def test_get_git_commit_returns_expected_repo_head() -> None:
    commit = get_git_commit()
    if commit == "unknown":
        pytest.skip("not a git checkout")
    # Cross-check against `git rev-parse --short HEAD` if git is available.
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).strip()
    except Exception:
        return
    assert commit == out[:7]


def test_get_git_commit_never_raises_when_no_git() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp)  # no .git here
            assert get_git_commit() == "unknown"
        finally:
            os.chdir(old_cwd)


def test_get_git_commit_handles_worktree_git_file(tmp_path) -> None:
    # A worktree/submodule has `.git` as a *file* pointing at the real dir.
    real_git = tmp_path / "real_git"
    (real_git / "refs" / "heads").mkdir(parents=True)
    (real_git / "refs" / "heads" / "main").write_text("abcdef1234567890\n", encoding="utf-8")
    (real_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    wc = tmp_path / "wc"
    wc.mkdir()
    (wc / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    old_cwd = os.getcwd()
    try:
        os.chdir(wc)
        assert get_git_commit() == "abcdef1"
    finally:
        os.chdir(old_cwd)


# -- crash_recovery._validate_cmd (accepted + hardened) --------------------


def _cr() -> CrashRecovery:
    return CrashRecovery(max_restarts=1, cooldown_seconds=0)


def test_validate_cmd_accepts_standard_bot_command() -> None:
    cmd = (sys.executable, "main.py", "--mode", "paper")
    assert _cr()._validate_cmd(cmd)
    assert _cr()._validate_cmd((sys.executable, "main.py", "--mode=testnet"))


def test_validate_cmd_rejects_foreign_executable() -> None:
    assert not _cr()._validate_cmd(("/bin/sh", "main.py", "--mode", "paper"))
    assert not _cr()._validate_cmd(("not-an-interpreter", "main.py"))


def test_validate_cmd_rejects_non_main_script() -> None:
    assert not _cr()._validate_cmd((sys.executable, "evil.py"))
    assert not _cr()._validate_cmd((sys.executable,))


def test_validate_cmd_rejects_unknown_mode() -> None:
    assert not _cr()._validate_cmd((sys.executable, "main.py", "--mode", "production"))
    assert not _cr()._validate_cmd((sys.executable, "main.py", "--mode=anything"))


def test_validate_cmd_rejects_empty() -> None:
    assert not _cr()._validate_cmd(())
