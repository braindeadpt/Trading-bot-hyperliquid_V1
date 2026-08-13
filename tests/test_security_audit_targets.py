"""Tests for SecurityAuditor.run(targets=...) — the scoped scan.

The pre-commit fast path (scripts/run_git_hooks.py) audits ONLY the staged
``.py`` files under ``src/`` instead of the whole tree. These tests pin that
scoped behaviour and prove the default (``run()`` with no targets) still
scans the whole tree exactly as before.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from security.audit import SecurityAuditor, Severity  # noqa: E402

pytestmark = pytest.mark.unit


def _make_tree(tmp_path) -> tuple:
    """src/ with a clean file, a subdir with an eval() file, and a junk file."""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    good = src / "good.py"
    good.write_text("x = 1\n", encoding="utf-8")
    evil = src / "sub" / "evil.py"
    evil.write_text("x = eval('1+1')\n", encoding="utf-8")
    nonpy = src / "notes.txt"
    nonpy.write_text("eval is dangerous\n", encoding="utf-8")
    return src, good, evil


def test_targets_absolute_only_scan_that_file(tmp_path) -> None:
    src, good, evil = _make_tree(tmp_path)
    auditor = SecurityAuditor(src_dir=src)
    auditor.run(targets=[good])
    assert auditor.files_scanned == 1
    assert not auditor.findings  # good.py is clean


def test_targets_relative_resolve_against_src_dir(tmp_path) -> None:
    src, good, evil = _make_tree(tmp_path)
    auditor = SecurityAuditor(src_dir=src)
    auditor.run(targets=["sub/evil.py"])
    assert auditor.files_scanned == 1
    assert any(f.severity >= Severity.CRITICAL for f in auditor.findings)
    assert all("evil.py" in str(f.file) for f in auditor.findings)


def test_targets_ignore_non_py_outside_and_missing(tmp_path) -> None:
    src, good, evil = _make_tree(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("x = eval('1+1')\n", encoding="utf-8")
    auditor = SecurityAuditor(src_dir=src)
    auditor.run(targets=[src / "notes.txt", outside, src / "missing.py", "sub/evil.py"])
    # Only the resolvable .py under src/ is scanned — and it is the evil one.
    assert auditor.files_scanned == 1
    assert any(f.severity >= Severity.CRITICAL for f in auditor.findings)


def test_empty_targets_scans_nothing(tmp_path) -> None:
    src, good, evil = _make_tree(tmp_path)
    auditor = SecurityAuditor(src_dir=src)
    auditor.run(targets=[])
    assert auditor.files_scanned == 0
    assert not auditor.findings


def test_default_run_still_scans_whole_tree(tmp_path) -> None:
    """run() without targets keeps the full-tree behaviour (no regression)."""
    src, good, evil = _make_tree(tmp_path)
    auditor = SecurityAuditor(src_dir=src)
    auditor.run()
    assert auditor.files_scanned == 2
    assert any(f.severity >= Severity.CRITICAL for f in auditor.findings)
