"""Tests for the ``# audit-ok`` suppression mechanism + the closed audit.

Decisions closed on 2026-08-13 (docs/SECURITY.md §2.4):

  * AUDIT-004 (file write, MEDIUM) in ``top_trader_tracker.py`` — REMEDIATED:
    the direct ``path.write_text`` was replaced by the atomic
    ``safe_write_file`` helper (temp file + move, size guard), keeping the
    ``validate_safe_path`` guard. The finding is gone.
  * AUDIT-005 (subprocess, HIGH) in ``crash_recovery.py`` — ACCEPTED +
    HARDENED: the subprocess respawns the bot (cannot be removed) and the
    command is allowlist-validated by ``_validate_cmd``. The call site now
    carries an inline ``# audit-ok: AUDIT-005`` marker, so the audit reports
    it in the ACCEPTED section instead of the blocking counts.

These tests pin both decisions and the suppression contract (marker on the
match line or the line above, only for the rule that actually fired, excluded
from has_critical/has_high_or_above), and pin the closed baseline: the full
tree audit reports 0 HIGH / CRITICAL / MEDIUM findings.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from security.audit import SecurityAuditor, Severity  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _audit_dir(tmp_path: Path, files: dict) -> SecurityAuditor:
    """Write {rel_path: content} under tmp_path/src and scan that tree."""
    src = tmp_path / "src"
    for rel, content in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    auditor = SecurityAuditor(src_dir=src)
    auditor.run()
    return auditor


# ---------------------------------------------------------------------------
# Suppression contract
# ---------------------------------------------------------------------------

def test_marker_on_match_line_suppresses(tmp_path) -> None:
    auditor = _audit_dir(tmp_path, {
        "bad.py": "x = eval('1+1')  # audit-ok: AUDIT-001 — reviewed, safe use\n",
    })
    assert not auditor.findings
    assert len(auditor.suppressed_findings) == 1
    assert auditor.suppressed_findings[0].rule_id == "AUDIT-001"
    assert not auditor.has_critical()


def test_marker_on_line_above_suppresses(tmp_path) -> None:
    auditor = _audit_dir(tmp_path, {
        "bad.py": "# audit-ok: AUDIT-001 — reviewed, safe use\nx = eval('1+1')\n",
    })
    assert not auditor.findings
    assert len(auditor.suppressed_findings) == 1


def test_marker_with_wrong_rule_id_does_not_suppress(tmp_path) -> None:
    auditor = _audit_dir(tmp_path, {
        "bad.py": "x = eval('1+1')  # audit-ok: AUDIT-005 — irrelevant marker\n",
    })
    assert len(auditor.findings) == 1
    assert auditor.findings[0].rule_id == "AUDIT-001"
    assert not auditor.suppressed_findings


def test_stray_marker_without_match_does_nothing(tmp_path) -> None:
    """Self-validating: a marker can only suppress a rule that fired there."""
    auditor = _audit_dir(tmp_path, {
        "clean.py": "x = 1  # audit-ok: AUDIT-001 — nothing to suppress here\n",
    })
    assert not auditor.findings
    assert not auditor.suppressed_findings


def test_suppressed_excluded_from_blocking_counts(tmp_path) -> None:
    auditor = _audit_dir(tmp_path, {
        "ok.py": "x = eval('1+1')  # audit-ok: AUDIT-001\n",
    })
    assert not auditor.has_critical()
    assert not auditor.has_high_or_above()


def test_run_clears_suppressed_between_scans(tmp_path) -> None:
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "bad.py").write_text("x = eval('1+1')  # audit-ok: AUDIT-001\n", encoding="utf-8")
    (src / "other.py").write_text("y = 2\n", encoding="utf-8")
    auditor = SecurityAuditor(src_dir=src)
    auditor.run()
    assert len(auditor.suppressed_findings) == 1
    (src / "bad.py").write_text("x = 1\n", encoding="utf-8")
    auditor.run()
    assert not auditor.suppressed_findings


def test_report_lists_accepted_section(tmp_path) -> None:
    auditor = _audit_dir(tmp_path, {
        "bad.py": "x = eval('1+1')  # audit-ok: AUDIT-001\n",
    })
    report = auditor.generate_report()
    assert "Suppressed: 1" in report
    assert "[ACCEPTED]" in report
    assert "AUDIT-001" in report


# ---------------------------------------------------------------------------
# The closed decisions against the real tree
# ---------------------------------------------------------------------------

def test_top_trader_tracker_has_no_audit004() -> None:
    """AUDIT-004 remediated: no file-write finding in top_trader_tracker."""
    auditor = SecurityAuditor(src_dir=ROOT / "src")
    auditor.run(targets=[ROOT / "src" / "exchanges" / "top_trader_tracker.py"])
    assert not any(f.rule_id == "AUDIT-004" for f in auditor.findings)
    # The write now goes through the atomic helper — assert the refactor stuck.
    text = (ROOT / "src" / "exchanges" / "top_trader_tracker.py").read_text(encoding="utf-8")
    assert ".write_text(" not in text
    assert "safe_write_file" in text


def test_crash_recovery_subprocess_accepted_via_marker() -> None:
    """AUDIT-005 accepted: suppressed, not blocking, still reported."""
    auditor = SecurityAuditor(src_dir=ROOT / "src")
    auditor.run(targets=[ROOT / "src" / "utils" / "crash_recovery.py"])
    assert not any(f.rule_id == "AUDIT-005" for f in auditor.findings)
    assert any(f.rule_id == "AUDIT-005" for f in auditor.suppressed_findings)
    assert not auditor.has_high_or_above()


def test_full_tree_audit_is_closed() -> None:
    """Baseline closed: 0 HIGH / CRITICAL / MEDIUM findings on the real tree.

    This is the regression guard for the closure — a new HIGH/MEDIUM finding
    anywhere in src/ turns CI red and forces an explicit decision.
    """
    auditor = SecurityAuditor(src_dir=ROOT / "src")
    auditor.run()
    blocking = [f for f in auditor.findings if f.severity >= Severity.MEDIUM]
    assert not blocking, (
        "Security audit must stay closed at 0 HIGH + 0 MEDIUM — "
        f"found: {[(f.rule_id, str(f.file), f.line) for f in blocking]}"
    )
    assert any(f.rule_id == "AUDIT-005" for f in auditor.suppressed_findings)
